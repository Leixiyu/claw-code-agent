"""Per-user paths and task ID/status indexes. Never store business results here."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

TASK_INDEX_FILES = {
    'analysis': 'video_analysis_task_id.json',
    'processing': 'video_processing_task_id.json',
    'training': 'model_training_task_id.json',
}
_LOCK = threading.RLock()
TASK_STATUSES = frozenset({'pending', 'running', 'done', 'failed'})


def contained(root: Path, path: Path) -> Path:
    root, path = root.resolve(), path.resolve()
    if not path.is_relative_to(root):
        raise ValueError('path is outside the user workspace')
    return path


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            os.chmod(temporary, 0o600)
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def index_lock(root: Path):
    import fcntl  # Prototype deployment target: Linux/macOS.
    directory = contained(root, root / '.port_sessions')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _LOCK, contained(root, directory / 'task_indexes.lock').open('a+') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_tasks(root: Path, module: str) -> list[dict[str, str | None]]:
    """Read cached states; legacy IDs have unknown (None) status until checked."""
    path = contained(root, root / TASK_INDEX_FILES[module])
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'invalid task index: {path.name}')
    if 'tasks' in payload:
        tasks = payload['tasks']
    else:
        ids = payload.get('task_ids')
        if not isinstance(ids, list):
            raise ValueError(f'invalid task index: {path.name}')
        tasks = [{'task_id': task_id, 'status': None} for task_id in ids]
    if not isinstance(tasks, list):
        raise ValueError(f'invalid task index: {path.name}')
    entries = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError(f'invalid task index: {path.name}')
        task_id, status = task.get('task_id'), task.get('status')
        if (not isinstance(task_id, str) or not task_id
                or 'status' not in task
                or (status is not None and (not isinstance(status, str) or status not in TASK_STATUSES))):
            raise ValueError(f'invalid task index: {path.name}')
        entries[task_id] = {'task_id': task_id, 'status': status}
    return list(entries.values())


def read_task_ids(root: Path, module: str) -> list[str]:
    return [task['task_id'] for task in read_tasks(root, module)]


def add_task_id(root: Path, module: str, task_id: str, *, status: str | None = 'pending') -> None:
    """Index a submission without resetting an existing task's checked status."""
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('task_id must be a non-empty string')
    if status is not None and (not isinstance(status, str) or status not in TASK_STATUSES):
        raise ValueError('invalid task status')
    with index_lock(root):
        tasks = read_tasks(root, module)
        if not any(task['task_id'] == task_id for task in tasks):
            tasks.append({'task_id': task_id, 'status': status})
        atomic_json(contained(root, root / TASK_INDEX_FILES[module]), {'tasks': tasks})


def update_task_status(root: Path, module: str, task_id: str, status: str) -> None:
    """Atomically save a successful Status API query for an already-owned task."""
    if not isinstance(status, str) or status not in TASK_STATUSES:
        raise ValueError('invalid task status')
    with index_lock(root):
        tasks = read_tasks(root, module)
        for task in tasks:
            if task['task_id'] == task_id:
                task['status'] = status
                break
        else:
            raise ValueError('task_id does not belong to the current user/module')
        atomic_json(contained(root, root / TASK_INDEX_FILES[module]), {'tasks': tasks})


def require_task(root: Path, module: str, task_id: str) -> None:
    if task_id not in read_task_ids(root, module):
        raise ValueError('task_id does not belong to the current user/module')


def initialize_user(root: Path, user_id: str) -> Path:
    if not re.fullmatch(r'[a-f0-9]{32}', user_id):
        raise ValueError('invalid user_id')
    workspace = contained(root, root / 'users' / user_id)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in ('uploads', 'sessions', '.port_sessions/scratchpad', 'runtime-home/.claude'):
        contained(workspace, workspace / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    with index_lock(workspace):
        for filename in TASK_INDEX_FILES.values():
            path = contained(workspace, workspace / filename)
            if not path.exists():
                atomic_json(path, {'tasks': []})
    return workspace
