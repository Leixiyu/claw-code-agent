"""FastAPI router for the local background-session runtime.

Wraps :class:`BackgroundSessionRuntime` so the GUI can list, inspect, and
terminate detached agent runs.  The runtime roots itself at
``<cwd>/.port_sessions/background`` to match the CLI layout.

Launching a new background session from the GUI isn't wired up here yet —
that needs careful thought about which CLI flags to forward, and belongs in
a follow-up slice.  Read + logs + kill is enough to make existing background
runs observable and recoverable.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from ..background_runtime import BackgroundSessionRuntime


@contextmanager
def _background_errors():
    try:
        yield
    except FileNotFoundError as exc:
        raise HTTPException(404, 'background session not found') from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, 'background task operation could not be completed') from exc


def create_background_router(get_cwd: Callable[[], Path]) -> APIRouter:
    router = APIRouter(prefix='/api/background', tags=['background'])

    def _runtime() -> BackgroundSessionRuntime:
        return BackgroundSessionRuntime.for_workspace(get_cwd())

    @router.get('')
    def list_background() -> dict[str, Any]:
        with _background_errors():
            runtime = _runtime()
            records = runtime.list_records()
        return {
            'root': str(runtime.root),
            'sessions': [asdict(record) for record in records],
            'counts': {
                'running': sum(1 for r in records if r.status == 'running'),
                'completed': sum(1 for r in records if r.status == 'completed'),
                'failed': sum(1 for r in records if r.status == 'failed'),
                'exited': sum(1 for r in records if r.status == 'exited'),
            },
        }

    @router.get('/{background_id}')
    def get_background(background_id: str) -> dict[str, Any]:
        with _background_errors():
            record = _runtime().load_record(background_id)
        return asdict(record)

    @router.get('/{background_id}/logs')
    def get_logs(background_id: str, tail: int | None = None) -> dict[str, Any]:
        with _background_errors():
            content = _runtime().read_logs(background_id, tail=tail)
        return {
            'background_id': background_id,
            'tail': tail,
            'content': content,
        }

    @router.post('/{background_id}/kill')
    def kill_background(background_id: str) -> dict[str, Any]:
        with _background_errors():
            record = _runtime().kill(background_id)
        return asdict(record)

    return router
