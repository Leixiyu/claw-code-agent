"""Authenticated public GUI, routing every request to a user-owned AgentState."""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request as ScopeRequest

from ..auth_runtime import AuthenticationError, AuthStore
from ..user_workspace import contained, initialize_user
from ..user_agent import user_prompt, user_tools, user_runtime_config


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


def create_authenticated_app(template, auth_store=None):
    from .server import AgentState, create_user_app
    store = auth_store or AuthStore(template.cwd)
    root = store.workspace
    app = FastAPI(title='Claw Code authenticated prototype')
    user_apps = {}
    app.state.user_apps = user_apps
    app.state.auth_store = store
    public_files = create_user_app(template)

    def user_app(user):
        user_id = user['user_id']
        if user_id not in user_apps:
            workspace = initialize_user(root, user_id)
            settings = {key: getattr(template, key) for key in inspect.signature(AgentState).parameters}
            settings.update(cwd=workspace, session_directory=workspace / 'sessions',
                            allow_shell=False, allow_write=False, disable_claude_md_discovery=True,
                            additional_working_directories=(), custom_system_prompt=None,
                            append_system_prompt=None, override_system_prompt=user_prompt(root, workspace))
            state = AgentState(**settings)
            state.agent.runtime_config = user_runtime_config(state.agent.runtime_config, workspace)
            state.agent.tool_registry = user_tools()
            from ..agent_tools import build_tool_context
            state.agent.tool_context = build_tool_context(state.agent.runtime_config, tool_registry=state.agent.tool_registry)
            state.agent.authenticated_user_id = user_id
            inner = create_user_app(state)
            inner.state.agent_state = state
            user_apps[user_id] = inner
        return user_apps[user_id]

    @app.post('/api/auth/login')
    async def login(body: LoginRequest, request: Request):
        try:
            payload = await asyncio.to_thread(store.login, body.username, body.password)
        except AuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        response = JSONResponse(payload)
        response.set_cookie('harness_session', payload['access_token'], httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', max_age=43200)
        return response

    @app.get('/api/auth/me')
    async def me(request: Request):
        return request.scope['auth_user']

    @app.post('/api/auth/logout')
    async def logout(request: Request):
        store.logout(request.scope['auth_token'])
        response = JSONResponse({'logged_out': True})
        response.delete_cookie('harness_session')
        return response

    @app.post('/api/uploads')
    async def upload(request: Request):
        user = request.scope['auth_user']
        workspace = initialize_user(root, user['user_id'])
        filename = unquote(request.headers.get('x-filename', ''))
        if not filename or filename in {'.', '..'} or any(c in filename for c in ('/', '\\', '\x00')):
            raise HTTPException(400, 'X-Filename must be a filename without directories')
        if len(filename.encode()) > 180:
            raise HTTPException(400, 'filename is too long')
        directory = contained(workspace, workspace / 'uploads')
        path = contained(workspace, directory / filename)
        try:
            output = path.open('xb')
        except FileExistsError:
            path = contained(workspace, directory / f'{uuid4().hex[:12]}-{filename}')
            output = path.open('xb')
        complete = False
        size = 0
        try:
            with output:
                os.chmod(path, 0o600)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > int(os.environ.get('HARNESS_MAX_UPLOAD_BYTES', 2 * 1024**3)):
                        raise HTTPException(413, 'upload exceeds HARNESS_MAX_UPLOAD_BYTES')
                    output.write(chunk)
            if size == 0:
                raise HTTPException(400, 'empty upload')
            complete = True
        finally:
            if not complete:
                path.unlink(missing_ok=True)
        return {'video_ref': {'type': 'upload_file', 'path': path.relative_to(workspace).as_posix()}, 'size_bytes': size}

    class Dispatch:
        async def __call__(self, scope, receive, send):
            path = scope.get('path', '')
            if path.startswith('/api/'):
                # Administrative features of the original single-user GUI are
                # intentionally unavailable to prototype end users.
                allowed = {'/api/chat', '/api/chat/stream', '/api/clear', '/api/state',
                           '/api/sessions', '/api/slash-commands', '/api/skills'}
                if path not in allowed and not path.startswith('/api/sessions/'):
                    return await JSONResponse({'detail': 'unavailable in prototype'}, 403)(scope, receive, send)
                if path == '/api/state' and scope['method'] != 'GET':
                    return await JSONResponse({'detail': 'settings are administrator-managed'}, 403)(scope, receive, send)
                if scope['method'] == 'DELETE' or path == '/api/sessions/clear-preview':
                    from ..session_lifecycle import session_path
                    try:
                        session_path(root / 'users' / scope['auth_user']['user_id'] / 'sessions', '_scope_check')
                    except (OSError, ValueError):
                        return await JSONResponse({'detail': 'unsafe user session directory'}, 403)(scope, receive, send)
                target = user_app(scope['auth_user'])
            else:
                target = public_files
            await target(scope, receive, send)

    app.mount('/', Dispatch())

    class AuthenticationMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                return await self.app(scope, receive, send)
            request = ScopeRequest(scope)
            path = scope['path']
            if path.startswith('/api/'):
                origin = request.headers.get('origin')
                if origin and urlsplit(origin).netloc != request.url.netloc:
                    return await JSONResponse({'detail': 'cross-origin request rejected'}, 403)(scope, receive, send)
                if path != '/api/auth/login':
                    authorization = request.headers.get('authorization', '')
                    token = authorization[7:] if authorization.startswith('Bearer ') else request.cookies.get('harness_session', '')
                    try:
                        scope['auth_user'] = store.authenticate(token)
                        scope['auth_token'] = token
                    except AuthenticationError as exc:
                        return await JSONResponse({'detail': str(exc)}, 401)(scope, receive, send)
            await self.app(scope, receive, send)

    app.add_middleware(AuthenticationMiddleware)
    return app
