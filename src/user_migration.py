"""Explicit, non-destructive legacy workspace migration. Dry-run by default."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil

from .user_workspace import add_task_id, atomic_json, contained, initialize_user


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.digest()


def migrate_user_data(source: Path, root: Path, user_id: str, *, apply=False) -> dict:
    source = source.resolve(strict=True)
    target = initialize_user(root, user_id) if apply else contained(root, root / 'users' / user_id)
    if source == target or source.is_relative_to(root / 'users'):
        raise ValueError('source must be a legacy workspace, not a user workspace')
    copies = []
    sessions = []
    task_ids = {'analysis': set(), 'processing': set(), 'training': set()}
    for module in task_ids:
        for path in (source / 'tasks' / module).glob('*.json'):
            payload = json.loads(contained(source, path).read_text())
            if isinstance(payload.get('task_id'), str):
                task_ids[module].add(payload['task_id'])
        filename = {'analysis': 'video_analysis', 'processing': 'video_processing', 'training': 'model_training'}[module]
        registry = source / '.port_sessions' / 'business_functions' / f'{filename}_idempotency.json'
        if registry.exists():
            for entry in json.loads(contained(source, registry).read_text()).get('entries', {}).values():
                task_id = entry.get('task_id') or entry.get('response', {}).get('task_id')
                if task_id:
                    task_ids[module].add(task_id)
    for directory in (source / 'sessions', source / '.port_sessions' / 'agent'):
        for path in directory.glob('*.json'):
            payload = json.loads(contained(source, path).read_text())
            sid = payload.get('session_id', path.stem)
            if not sid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in sid):
                raise ValueError('invalid legacy session ID')
            config = payload.setdefault('runtime_config', {})
            config.update(cwd=str(target), session_directory=str(target / 'sessions'),
                          scratchpad_root=str(target / '.port_sessions' / 'scratchpad'),
                          additional_working_directories=[])
            old_scratch = payload.get('scratchpad_directory')
            new_scratch = target / '.port_sessions' / 'scratchpad' / sid
            if old_scratch:
                old_scratch = contained(source, Path(old_scratch))
                if old_scratch.exists():
                    for item in old_scratch.rglob('*'):
                        if item.is_file():
                            copies.append((contained(source, item), new_scratch / item.relative_to(old_scratch)))
            payload['scratchpad_directory'] = str(new_scratch)
            sessions.append((target / 'sessions' / f'{sid}.json', payload))
    for path in (source / 'uploads').rglob('*'):
        if path.is_file():
            copies.append((contained(source, path), target / 'uploads' / path.relative_to(source / 'uploads')))
    # Validate conflicts before copying; never overwrite different user data.
    for src, dst in copies:
        contained(target, dst)
        if dst.exists() and _digest(src) != _digest(dst):
            raise ValueError(f'migration conflict: {dst}')
    unique_sessions = {}
    for dst, payload in sessions:
        if dst in unique_sessions and unique_sessions[dst] != payload:
            raise ValueError(f'conflicting legacy sessions: {dst.name}')
        unique_sessions[dst] = payload
        contained(target, dst)
        if dst.exists() and json.loads(dst.read_text()) != payload:
            raise ValueError(f'migration conflict: {dst}')
    report = {'applied': apply, 'source': str(source), 'target': str(target),
              'copy_count': len(copies), 'session_count': len(unique_sessions),
              'task_ids': {key: sorted(value) for key, value in task_ids.items()},
              'originals_preserved': True,
              'note': 'No result/manifest/metadata files are copied. Historical chat content is preserved; current queries use APIs.'}
    if apply:
        for src, dst in copies:
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(src, dst)
        for dst, payload in unique_sessions.items():
            atomic_json(dst, payload)
        for module, ids in task_ids.items():
            for task_id in ids:
                add_task_id(target, module, task_id, status=None)
        atomic_json(target / '.port_sessions' / 'migration_report.json', report)
    return report
