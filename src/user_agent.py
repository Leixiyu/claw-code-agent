"""Shared CLI/GUI prototype policy. No process-global HOME/cwd switching."""
from dataclasses import replace
from pathlib import Path

from .agent_tools import default_tool_registry
from .agent_types import AgentPermissions
from .user_workspace import contained


def user_runtime_config(config, workspace: Path):
    return replace(config, cwd=workspace, session_directory=workspace / 'sessions',
                   scratchpad_root=workspace / '.port_sessions' / 'scratchpad',
                   disable_claude_md_discovery=True, additional_working_directories=(),
                   permissions=AgentPermissions())


def user_tools():
    names = {'list_dir', 'read_file', 'glob_search', 'grep_search', 'sleep'}
    for module in ('video_analysis', 'video_processing', 'model_training'):
        names.update({f'submit_{module}', f'get_{module}_status', f'get_{module}_result', f'list_{module}_tasks'})
    return {name: tool for name, tool in default_tool_registry().items() if name in names}


def user_prompt(root: Path, workspace: Path) -> str:
    path = contained(root, root / 'CLAUDE.md')
    common = path.read_text(encoding='utf-8') if path.exists() else 'You are a video business assistant.'
    common = common.replace('{{AGENT_WORKSPACE_PATH}}', str(workspace))
    return common + f'''\n\nCurrent authenticated user workspace: {workspace}
Only access this workspace. The parent workspace and other users are not accessible.
Task indexes contain task_id and last-checked status, not business results.
List tools use cached status and fetch live results only for tasks marked done.
Call Status tools to refresh pending/running/unknown (null) statuses when current progress is needed.
Results/manifest/metadata come from the Business APIs.
Use existing conversation context when sufficient; query APIs for current status.
When status is done, immediately call the corresponding result function.
Do not create separate result, dataset manifest or model metadata files.
User-level HOME configuration, shell, configuration editing and delegation are disabled in this prototype.
'''


def validate_user_session(stored, workspace: Path):
    if Path(stored.runtime_config['cwd']).resolve() != workspace.resolve():
        raise ValueError('session workspace does not belong to the current user; migrate it first')
    if stored.scratchpad_directory:
        contained(workspace, Path(stored.scratchpad_directory))
    return stored
