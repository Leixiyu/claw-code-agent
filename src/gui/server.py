"""FastAPI server backing the local web GUI.

Wraps a single global :class:`LocalCodingAgent`, exposes JSON endpoints for
chat, slash commands, and saved sessions, and serves the static SPA.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


# Sentinel for "field not provided" — lets callers distinguish "leave as-is"
# from "set explicitly to None" (needed for budget knobs where None means
# unlimited, which is itself a valid setting the user may want to apply).
_UNSET: Any = object()

from ..agent_runtime import LocalCodingAgent
from ..agent_slash_commands import get_slash_command_specs
from ..agent_types import (
    AgentPermissions,
    AgentRuntimeConfig,
    BudgetConfig,
    ModelConfig,
    OutputSchemaConfig,
)
from ..bundled_skills import get_bundled_skills
from ..paste_refs import PastedContent, expand_pasted_text_refs
from ..session_catalog import list_saved_sessions
from ..session_lifecycle import (
    SessionBusyError, SessionDeletedError, delete_saved_session, rename_saved_session,
)
from ..session_store import (
    StoredAgentSession,
    read_agent_session,
)
from .account_routes import create_account_router
from .ask_user_routes import create_ask_user_router
from .background_routes import create_background_router
from .diagnostics_routes import create_diagnostics_router
from .mcp_routes import create_mcp_router
from .memory_routes import MemoryPathContext, create_memory_router
from .plans_routes import create_plans_router
from .plugins_routes import create_plugins_router
from .remote_routes import create_remote_router
from .remote_trigger_routes import create_remote_trigger_router
from .search_routes import create_search_router
from .tasks_routes import create_tasks_router
from .team_routes import create_team_router
from .workflow_routes import create_workflow_router
from .worktree_routes import create_worktree_router


STATIC_DIR = Path(__file__).parent / 'static'


# ---------------------------------------------------------------------------
# Agent state holder
# ---------------------------------------------------------------------------

class AgentState:
    """Holds the live agent instance plus a lock for serialized access."""

    def __init__(
        self,
        *,
        cwd: Path,
        model: str,
        base_url: str,
        api_key: str,
        allow_shell: bool,
        allow_write: bool,
        session_directory: Path,
        temperature: float = 0.0,
        timeout_seconds: float = 120.0,
        stream_model_responses: bool = False,
        max_turns: int = 12,
        max_total_tokens: int | None = None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_reasoning_tokens: int | None = None,
        max_total_cost_usd: float | None = None,
        max_tool_calls: int | None = None,
        max_delegated_tasks: int | None = None,
        max_model_calls: int | None = None,
        max_session_turns: int | None = None,
        custom_system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        override_system_prompt: str | None = None,
        response_schema: dict[str, Any] | None = None,
        response_schema_name: str = 'response',
        response_schema_strict: bool = False,
        auto_snip_threshold_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
        compact_preserve_messages: int = 4,
        disable_claude_md_discovery: bool = False,
        additional_working_directories: tuple[Path, ...] = (),
    ) -> None:
        self.cwd = cwd.resolve()
        self.session_directory = session_directory
        self._lock = Lock()
        self._agent: LocalCodingAgent | None = None
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.allow_shell = allow_shell
        self.allow_write = allow_write
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.stream_model_responses = stream_model_responses
        self.max_turns = max_turns
        self.max_total_tokens = max_total_tokens
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_reasoning_tokens = max_reasoning_tokens
        self.max_total_cost_usd = max_total_cost_usd
        self.max_tool_calls = max_tool_calls
        self.max_delegated_tasks = max_delegated_tasks
        self.max_model_calls = max_model_calls
        self.max_session_turns = max_session_turns
        self.custom_system_prompt = custom_system_prompt
        self.append_system_prompt = append_system_prompt
        self.override_system_prompt = override_system_prompt
        self.response_schema = response_schema
        self.response_schema_name = response_schema_name
        self.response_schema_strict = response_schema_strict
        self.auto_snip_threshold_tokens = auto_snip_threshold_tokens
        self.auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self.compact_preserve_messages = compact_preserve_messages
        self.disable_claude_md_discovery = disable_claude_md_discovery
        self.additional_working_directories = tuple(additional_working_directories)
        self._build_agent()

    def _build_agent(self) -> None:
        permissions = AgentPermissions(
            allow_file_write=self.allow_write,
            allow_shell_commands=self.allow_shell,
        )
        budget_config = BudgetConfig(
            max_total_tokens=self.max_total_tokens,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            max_reasoning_tokens=self.max_reasoning_tokens,
            max_total_cost_usd=self.max_total_cost_usd,
            max_tool_calls=self.max_tool_calls,
            max_delegated_tasks=self.max_delegated_tasks,
            max_model_calls=self.max_model_calls,
            max_session_turns=self.max_session_turns,
        )
        output_schema: OutputSchemaConfig | None = None
        if self.response_schema is not None:
            output_schema = OutputSchemaConfig(
                name=self.response_schema_name,
                schema=self.response_schema,
                strict=self.response_schema_strict,
            )
        runtime_config = AgentRuntimeConfig(
            cwd=self.cwd,
            permissions=permissions,
            session_directory=self.session_directory,
            stream_model_responses=self.stream_model_responses,
            max_turns=self.max_turns,
            budget_config=budget_config,
            output_schema=output_schema,
            auto_snip_threshold_tokens=self.auto_snip_threshold_tokens,
            auto_compact_threshold_tokens=self.auto_compact_threshold_tokens,
            compact_preserve_messages=self.compact_preserve_messages,
            disable_claude_md_discovery=self.disable_claude_md_discovery,
            additional_working_directories=self.additional_working_directories,
        )
        model_config = ModelConfig(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout_seconds=self.timeout_seconds,
        )
        self._agent = LocalCodingAgent(
            model_config=model_config,
            runtime_config=runtime_config,
            custom_system_prompt=self.custom_system_prompt,
            append_system_prompt=self.append_system_prompt,
            override_system_prompt=self.override_system_prompt,
        )

    @property
    def agent(self) -> LocalCodingAgent:
        assert self._agent is not None
        return self._agent

    def update(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        cwd: str | None = None,
        allow_shell: bool | None = None,
        allow_write: bool | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        stream_model_responses: bool | None = None,
        max_turns: int | None = None,
        # Budget knobs use the sentinel pattern — `None` means "no limit"
        # (a valid setting), so they need a distinct "not provided" marker.
        max_total_tokens: Any = _UNSET,
        max_input_tokens: Any = _UNSET,
        max_output_tokens: Any = _UNSET,
        max_reasoning_tokens: Any = _UNSET,
        max_total_cost_usd: Any = _UNSET,
        max_tool_calls: Any = _UNSET,
        max_delegated_tasks: Any = _UNSET,
        max_model_calls: Any = _UNSET,
        max_session_turns: Any = _UNSET,
        # System prompt knobs use the sentinel pattern too — None clears,
        # missing leaves untouched.
        custom_system_prompt: Any = _UNSET,
        append_system_prompt: Any = _UNSET,
        override_system_prompt: Any = _UNSET,
        response_schema: Any = _UNSET,
        response_schema_name: str | None = None,
        response_schema_strict: bool | None = None,
        # Context-management knobs.  Threshold values are sentinel-based since
        # `None` is meaningful (= "no automatic snip/compact").
        auto_snip_threshold_tokens: Any = _UNSET,
        auto_compact_threshold_tokens: Any = _UNSET,
        compact_preserve_messages: int | None = None,
        disable_claude_md_discovery: bool | None = None,
        additional_working_directories: list[str] | None = None,
    ) -> None:
        with self._lock:
            if model is not None:
                self.model = model
            if base_url is not None:
                self.base_url = base_url
            if api_key is not None:
                self.api_key = api_key
            if cwd is not None:
                resolved = Path(cwd).expanduser().resolve()
                if not resolved.is_dir():
                    raise ValueError(f'cwd does not exist: {resolved}')
                self.cwd = resolved
            if allow_shell is not None:
                self.allow_shell = allow_shell
            if allow_write is not None:
                self.allow_write = allow_write
            if temperature is not None:
                if temperature < 0:
                    raise ValueError('temperature must be >= 0')
                self.temperature = temperature
            if timeout_seconds is not None:
                if timeout_seconds <= 0:
                    raise ValueError('timeout_seconds must be > 0')
                self.timeout_seconds = timeout_seconds
            if stream_model_responses is not None:
                self.stream_model_responses = stream_model_responses
            if max_turns is not None:
                if max_turns < 1:
                    raise ValueError('max_turns must be >= 1')
                self.max_turns = max_turns

            for name, value, kind in (
                ('max_total_tokens', max_total_tokens, 'int'),
                ('max_input_tokens', max_input_tokens, 'int'),
                ('max_output_tokens', max_output_tokens, 'int'),
                ('max_reasoning_tokens', max_reasoning_tokens, 'int'),
                ('max_total_cost_usd', max_total_cost_usd, 'float'),
                ('max_tool_calls', max_tool_calls, 'int'),
                ('max_delegated_tasks', max_delegated_tasks, 'int'),
                ('max_model_calls', max_model_calls, 'int'),
                ('max_session_turns', max_session_turns, 'int'),
            ):
                if value is _UNSET:
                    continue
                if value is not None:
                    if (kind == 'int' and (not isinstance(value, int) or isinstance(value, bool))) or (
                        kind == 'float' and not isinstance(value, (int, float))
                    ):
                        raise ValueError(f'{name} must be a number or null')
                    if value <= 0:
                        raise ValueError(f'{name} must be > 0 or null')
                setattr(self, name, value)

            for name, value in (
                ('custom_system_prompt', custom_system_prompt),
                ('append_system_prompt', append_system_prompt),
                ('override_system_prompt', override_system_prompt),
            ):
                if value is _UNSET:
                    continue
                if value is not None and not isinstance(value, str):
                    raise ValueError(f'{name} must be a string or null')
                # Treat empty string as a clear, since blank textareas serialize
                # to "" but the runtime expects None for "use default".
                setattr(self, name, value or None)

            if response_schema is not _UNSET:
                if response_schema is not None and not isinstance(response_schema, dict):
                    raise ValueError('response_schema must be a JSON object or null')
                self.response_schema = response_schema
            if response_schema_name is not None:
                if not isinstance(response_schema_name, str) or not response_schema_name.strip():
                    raise ValueError('response_schema_name must be a non-empty string')
                self.response_schema_name = response_schema_name.strip()
            if response_schema_strict is not None:
                self.response_schema_strict = bool(response_schema_strict)

            for name, value in (
                ('auto_snip_threshold_tokens', auto_snip_threshold_tokens),
                ('auto_compact_threshold_tokens', auto_compact_threshold_tokens),
            ):
                if value is _UNSET:
                    continue
                if value is not None:
                    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                        raise ValueError(f'{name} must be a positive integer or null')
                setattr(self, name, value)

            if compact_preserve_messages is not None:
                if compact_preserve_messages < 0:
                    raise ValueError('compact_preserve_messages must be >= 0')
                self.compact_preserve_messages = compact_preserve_messages
            if disable_claude_md_discovery is not None:
                self.disable_claude_md_discovery = bool(disable_claude_md_discovery)
            if additional_working_directories is not None:
                resolved: list[Path] = []
                for raw in additional_working_directories:
                    p = Path(raw).expanduser().resolve()
                    if not p.is_dir():
                        raise ValueError(f'additional working dir does not exist: {p}')
                    resolved.append(p)
                self.additional_working_directories = tuple(resolved)

            self._build_agent()

    def snapshot(self) -> dict[str, Any]:
        return {
            'model': self.model,
            'base_url': self.base_url,
            'cwd': str(self.cwd),
            'session_directory': str(self.session_directory),
            'allow_shell': self.allow_shell,
            'allow_write': self.allow_write,
            'temperature': self.temperature,
            'timeout_seconds': self.timeout_seconds,
            'stream_model_responses': self.stream_model_responses,
            'max_turns': self.max_turns,
            'max_total_tokens': self.max_total_tokens,
            'max_input_tokens': self.max_input_tokens,
            'max_output_tokens': self.max_output_tokens,
            'max_reasoning_tokens': self.max_reasoning_tokens,
            'max_total_cost_usd': self.max_total_cost_usd,
            'max_tool_calls': self.max_tool_calls,
            'max_delegated_tasks': self.max_delegated_tasks,
            'max_model_calls': self.max_model_calls,
            'max_session_turns': self.max_session_turns,
            'custom_system_prompt': self.custom_system_prompt,
            'append_system_prompt': self.append_system_prompt,
            'override_system_prompt': self.override_system_prompt,
            'response_schema': self.response_schema,
            'response_schema_name': self.response_schema_name,
            'response_schema_strict': self.response_schema_strict,
            'auto_snip_threshold_tokens': self.auto_snip_threshold_tokens,
            'auto_compact_threshold_tokens': self.auto_compact_threshold_tokens,
            'compact_preserve_messages': self.compact_preserve_messages,
            'disable_claude_md_discovery': self.disable_claude_md_discovery,
            'additional_working_directories': [
                str(p) for p in self.additional_working_directories
            ],
            'active_session_id': self.agent.active_session_id,
        }

    def lock(self) -> Lock:
        return self._lock


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class PastedContentPayload(BaseModel):
    """JSON shape for an entry in :class:`ChatRequest.pasted_contents`.

    Mirrors :class:`src.paste_refs.PastedContent` minus the id (the dict key
    in the parent payload).  ``type`` is constrained to ``text`` for now —
    image expansion isn't wired into the agent runtime yet.
    """

    type: str = Field(default='text')
    content: str
    media_type: str | None = None
    filename: str | None = None


class SessionRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    resume_session_id: str | None = None
    pasted_contents: dict[int, PastedContentPayload] = Field(default_factory=dict)


class StateUpdate(BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    cwd: str | None = None
    allow_shell: bool | None = None
    allow_write: bool | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None
    stream_model_responses: bool | None = None
    max_turns: int | None = None
    # Budget knobs — null clears the limit, omitted leaves it untouched.
    max_total_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_reasoning_tokens: int | None = None
    max_total_cost_usd: float | None = None
    max_tool_calls: int | None = None
    max_delegated_tasks: int | None = None
    max_model_calls: int | None = None
    max_session_turns: int | None = None
    # System prompt overrides — null clears, omitted leaves untouched.
    custom_system_prompt: str | None = None
    append_system_prompt: str | None = None
    override_system_prompt: str | None = None
    response_schema: dict[str, Any] | None = None
    response_schema_name: str | None = None
    response_schema_strict: bool | None = None
    # Context-management knobs.
    auto_snip_threshold_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    compact_preserve_messages: int | None = None
    disable_claude_md_discovery: bool | None = None
    additional_working_directories: list[str] | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def mount_gui_assets(app: FastAPI) -> None:
    app.mount(
        '/static',
        StaticFiles(directory=str(STATIC_DIR)),
        name='static',
    )

    @app.get('/', include_in_schema=False)
    async def root() -> FileResponse:
        return FileResponse(STATIC_DIR / 'index.html')


def create_app(state: AgentState, *, auth_store=None) -> FastAPI:
    from .auth_app import create_authenticated_app
    return create_authenticated_app(state, auth_store)


def create_user_app(state: AgentState) -> FastAPI:
    app = FastAPI(title='Claw Code GUI', version='1.0')
    from .api_errors import install_error_handlers, exception_payload, error_payload
    install_error_handlers(app)

    app.include_router(create_tasks_router(lambda: state.cwd))
    app.include_router(create_plans_router(lambda: state.cwd))
    app.include_router(
        create_memory_router(
            lambda: MemoryPathContext(
                cwd=state.cwd,
                additional_working_directories=state.additional_working_directories,
            )
        )
    )
    app.include_router(create_background_router(lambda: state.cwd))

    def _apply_cwd(new_cwd: Path) -> None:
        # Worktree enter/exit hands us the new cwd; round-trip it through
        # AgentState.update so the agent gets rebuilt against the new dir.
        state.update(cwd=str(new_cwd))

    app.include_router(create_worktree_router(lambda: state.cwd, _apply_cwd))
    app.include_router(
        create_account_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_remote_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_mcp_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_plugins_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_ask_user_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_workflow_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_search_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_remote_trigger_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(
        create_team_router(
            lambda: state.cwd,
            lambda: state.additional_working_directories,
        )
    )
    app.include_router(create_diagnostics_router())

    mount_gui_assets(app)

    # ------------- info ------------------------------------------------------
    @app.get('/api/state')
    async def get_state() -> dict[str, Any]:
        return state.snapshot()

    @app.post('/api/state')
    async def post_state(payload: StateUpdate) -> dict[str, Any]:
        try:
            # exclude_unset preserves explicit null (e.g. "clear the limit")
            # while omitting fields the client never mentioned.
            state.update(**payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return state.snapshot()

    @app.get('/api/slash-commands')
    async def list_slash_commands() -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        from ..user_access import user_commands
        specs = user_commands() if state.agent.authenticated_user_id else get_slash_command_specs()
        for spec in specs:
            commands.append(
                {
                    'names': list(spec.names),
                    'primary': spec.names[0],
                    'description': spec.description,
                }
            )
        return commands

    @app.get('/api/skills')
    async def list_skills(include_internal: bool = False) -> list[dict[str, Any]]:
        from ..user_access import user_skills
        return [
            {
                'name': skill.name,
                'description': skill.description,
                'when_to_use': skill.when_to_use,
                'aliases': list(skill.aliases),
                'allowed_tools': list(skill.allowed_tools),
                'user_invocable': skill.user_invocable,
            }
            for skill in (user_skills()
                          if state.agent.authenticated_user_id else get_bundled_skills())
            if include_internal or skill.user_invocable
        ]

    # ------------- sessions --------------------------------------------------
    def _forget_deleted_sessions(deleted: list[str]) -> None:
        # Do not wait for a long model call on a different session. Disk-level
        # markers still reject any stale resume/save attempts.
        if state.lock().acquire(blocking=False):
            try:
                if state.agent.active_session_id in deleted:
                    state.agent.clear_runtime_state()
            finally:
                state.lock().release()

    @app.delete('/api/sessions/{session_id}')
    async def delete_session(session_id: str, confirm: bool = False) -> dict:
        if not confirm:
            raise HTTPException(status_code=400, detail='Permanent deletion requires confirmation')
        try:
            report = await asyncio.to_thread(delete_saved_session, state.session_directory, session_id)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _forget_deleted_sessions(report['deleted'])
        return report

    @app.get('/api/sessions')
    async def list_sessions(response: Response, limit: int | None = Query(default=None, ge=1)) -> list[dict[str, Any]]:
        listing = list_saved_sessions(state.session_directory, limit=limit)
        # Keep the existing array response for clients; diagnostics use headers.
        response.headers['X-Session-Total'] = str(listing['total'])
        response.headers['X-Session-Skipped'] = str(listing['skipped'])
        return listing['sessions']

    @app.post('/api/sessions/{session_id}/rename')
    async def rename_session(session_id: str, request: SessionRenameRequest) -> dict:
        try:
            return await asyncio.to_thread(
                rename_saved_session, state.session_directory, session_id, request.name)
        except SessionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get('/api/sessions/{session_id}')
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            stored = read_agent_session(session_id, directory=state.session_directory)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail='Session not found')
        return _serialize_stored_session(stored)

    @app.get('/api/file-history')
    async def list_file_history(limit: int = 200) -> dict[str, Any]:
        """Aggregate file_history entries across every saved session.

        Returns newest first.  ``limit`` caps the response so very long-lived
        workspaces don't ship megabytes of JSON to the browser.
        """
        directory = state.session_directory
        entries: list[dict[str, Any]] = []
        if directory.exists():
            for path in directory.glob('*.json'):
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    continue
                session_id = data.get('session_id', path.stem)
                for raw in data.get('file_history') or []:
                    if not isinstance(raw, dict):
                        continue
                    entry = dict(raw)
                    entry['session_id'] = session_id
                    entries.append(entry)
        entries.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
        return {
            'total': len(entries),
            'returned': min(limit, len(entries)),
            'entries': entries[:limit],
        }

    # ------------- chat ------------------------------------------------------
    def _chat_prompt(request: ChatRequest) -> str:
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail='Prompt is empty')

        if request.pasted_contents:
            store = {
                ref_id: PastedContent(
                    id=ref_id,
                    type=payload.type,
                    content=payload.content,
                    media_type=payload.media_type,
                    filename=payload.filename,
                )
                for ref_id, payload in request.pasted_contents.items()
            }
            prompt = expand_pasted_text_refs(prompt, store)
        return prompt

    def _run_chat(
        request: ChatRequest,
        prompt: str,
        on_tool_start: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        with state.lock():
            agent = state.agent
            previous_callback = agent.on_tool_start
            agent.on_tool_start = on_tool_start
            try:
                if request.resume_session_id is not None:
                    try:
                        stored = read_agent_session(
                            request.resume_session_id,
                            directory=state.session_directory,
                        )
                    except FileNotFoundError:
                        raise HTTPException(
                            status_code=404,
                            detail='Session to resume not found',
                        )
                    result = agent.resume(prompt, stored)
                else:
                    result = agent.run(prompt)
                payload = _serialize_run_result(result)
                failures = {
                    'permission_denied': (403, 'feature_unavailable', '此命令不可用，请使用 /help 查看可用命令。'),
                    'backend_error': (502, 'model_backend_error', '模型服务请求失败，请稍后重试。'),
                    'budget_exceeded': (409, 'budget_exceeded', '本次请求已达到用量或执行次数限制。'),
                    'prompt_too_long': (413, 'context_too_large', '对话内容超过模型容量，请压缩对话或开始新对话。'),
                }
                if result.stop_reason in failures:
                    status, code, message = failures[result.stop_reason]
                    payload.update(error_payload(status, message, code=code, error_type='AgentRunError'))
                    payload['final_output'] = message
                return payload
            except SessionDeletedError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except SessionBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            finally:
                agent.on_tool_start = previous_callback

    @app.post('/api/chat')
    async def chat(request: ChatRequest) -> dict[str, Any]:
        prompt = _chat_prompt(request)
        try:
            payload = await asyncio.to_thread(_run_chat, request, prompt)
        except Exception as exc:
            payload = exception_payload(exc)
        if 'code' in payload:
            return JSONResponse(payload, status_code=payload['status'])
        return payload

    # Keep workers alive on disconnect: losing the display must not cancel or
    # resubmit business operations. The agent lock still serializes requests.
    chat_workers: set[asyncio.Task] = set()

    @app.post('/api/chat/stream')
    async def chat_stream(request: ChatRequest) -> StreamingResponse:
        prompt = _chat_prompt(request)

        async def events():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            closed = Event()

            def emit(event: dict[str, Any]) -> None:
                if not closed.is_set():
                    loop.call_soon_threadsafe(queue.put_nowait, event)

            def run() -> None:
                try:
                    result = _run_chat(request, prompt, emit)
                    emit({'type': 'error', **result} if 'code' in result else {'type': 'result', 'data': result})
                except Exception as exc:
                    emit({'type': 'error', **exception_payload(exc)})

            worker = asyncio.create_task(asyncio.to_thread(run))
            chat_workers.add(worker)
            worker.add_done_callback(chat_workers.discard)
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        event = {'type': 'heartbeat'}
                    yield json.dumps(event, ensure_ascii=False) + '\n'
                    if event['type'] in {'result', 'error'}:
                        break
            finally:
                closed.set()

        return StreamingResponse(
            events(),
            media_type='application/x-ndjson',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    @app.post('/api/clear')
    def clear_state() -> dict[str, Any]:
        # Never queue a reset behind an in-flight reply or block the event loop.
        lock = state.lock()
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '当前用户有回复正在生成，请完成后再清除状态。')
        try:
            state.agent.clear_runtime_state()
            return {**state.snapshot(), 'action': 'runtime_state_cleared'}
        finally:
            lock.release()

    return app


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_run_result(result: Any) -> dict[str, Any]:
    return {
        'final_output': result.final_output,
        'turns': result.turns,
        'tool_calls': result.tool_calls,
        'transcript': [_normalize_transcript_entry(entry) for entry in result.transcript],
        'session_id': result.session_id,
        'usage': result.usage.to_dict(),
        'run_usage': result.run_usage.to_dict(),
        'total_cost_usd': result.total_cost_usd,
        'stop_reason': result.stop_reason,
    }


def _normalize_transcript_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        'role': entry.get('role', ''),
        'content': entry.get('content', ''),
    }
    for key in ('name', 'tool_call_id', 'tool_calls', 'metadata', 'message_id'):
        if key in entry and entry[key] not in (None, '', [], {}):
            out[key] = entry[key]
    return out


def _serialize_stored_session(stored: StoredAgentSession) -> dict[str, Any]:
    return {
        'session_id': stored.session_id,
        'turns': stored.turns,
        'tool_calls': stored.tool_calls,
        'messages': [_normalize_transcript_entry(dict(m)) for m in stored.messages],
        'usage': stored.usage,
        'total_cost_usd': stored.total_cost_usd,
        'model': stored.model_config.get('model'),
        'file_history': [dict(entry) for entry in stored.file_history],
    }
