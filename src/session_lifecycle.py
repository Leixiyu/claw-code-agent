"""Permanent session deletion and cross-process turn/save coordination.

Lock files contain only a deletion marker, never conversation data. Keep them
after deletion so a stale CLI/GUI process cannot resurrect a removed session.
Linux/macOS, like the existing per-user task-index locking implementation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import threading


class SessionBusyError(ValueError):
    """A turn or save is in progress in this or another process."""


class SessionDeletedError(FileNotFoundError):
    """The session was permanently deleted; start a new conversation."""


def session_path(directory: Path, session_id: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise FileNotFoundError('Invalid session ID')
    # Reject redirected storage, including parent-directory symlinks.
    directory = Path(os.path.abspath(directory))
    if directory.resolve() != directory:
        raise ValueError('Session directory must not contain symlinks')
    path = directory / f'{session_id}.json'
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Unsafe session file')
    return path


@dataclass
class _HeldLock:
    fd: int
    active: int = 0


_MUTEX = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_HELD = threading.local()


@contextmanager
def session_guard(directory: Path, session_id: str, *, active: bool = False,
                  allow_deleted: bool = False):
    """Nonblocking, thread-reentrant exclusive lock spanning a complete turn."""
    import fcntl

    path = session_path(directory, session_id)
    lock_dir = path.parent / '.lifecycle'
    if lock_dir.is_symlink():
        raise ValueError('Unsafe session lock directory')
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_dir / f'{session_id}.lock'
    key = str(lock_path)
    with _MUTEX:
        mutex = _THREAD_LOCKS.setdefault(key, threading.RLock())
    if not mutex.acquire(blocking=False):
        raise SessionBusyError(f'Session {session_id} is running; try again after it finishes.')
    held = getattr(_HELD, 'locks', None)
    if held is None:
        held = _HELD.locks = {}
    outer = key not in held
    fd = None
    entered = False
    try:
        if outer:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe session lock file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionBusyError(f'Session {session_id} is running; try again after it finishes.') from exc
            held[key] = _HeldLock(fd)
        lock = held[key]
        if not allow_deleted and os.pread(lock.fd, 1, 0):
            raise SessionDeletedError(f'Session {session_id} was permanently deleted. Start a new chat.')
        if active:
            lock.active += 1
            entered = True
        yield lock
    finally:
        if entered:
            held[key].active -= 1
        if outer:
            held.pop(key, None)
            if fd is not None:
                os.close(fd)  # Closing also releases flock, including on exceptions.
        mutex.release()


def delete_saved_session(directory: Path, session_id: str) -> dict:
    """Delete exactly one session JSON; never its uploads, scratchpad or tasks."""
    path = session_path(directory, session_id)
    if not path.exists():
        raise FileNotFoundError(f'Session {session_id} not found for the current user.')
    with session_guard(directory, session_id, allow_deleted=True) as lock:
        if lock.active:
            raise SessionBusyError(f'Session {session_id} is running; try again after it finishes.')
        # Recheck after acquiring the lock; another process may have deleted it.
        path = session_path(directory, session_id)
        if not path.exists():
            raise FileNotFoundError(f'Session {session_id} not found for the current user.')
        # Mark before unlink. If a process crashes here, later saves still refuse
        # resurrection; a repeated deletion can finish removing the JSON.
        os.pwrite(lock.fd, b'deleted\n', 0)
        os.fsync(lock.fd)
        path.unlink()
    return {'deleted': [session_id]}


def session_deletion_candidates(directory: Path) -> list[str]:
    """Snapshot regular JSON filenames, including unreadable/corrupt sessions."""
    session_path(directory, '_scope_check')
    candidates = []
    for path in directory.glob('*.json'):
        try:
            if session_path(directory, path.stem).is_file():
                candidates.append(path.stem)
        except (ValueError, OSError):
            continue
    return sorted(candidates)


def clear_saved_sessions(directory: Path, session_ids: list[str]) -> dict:
    """Delete a confirmed snapshot only; leave busy/unsafe entries untouched."""
    report: dict[str, list] = {'deleted': [], 'skipped_running': [], 'not_found': [], 'errors': []}
    for session_id in dict.fromkeys(session_ids):
        try:
            delete_saved_session(directory, session_id)
            report['deleted'].append(session_id)
        except SessionBusyError:
            report['skipped_running'].append(session_id)
        except FileNotFoundError:
            report['not_found'].append(session_id)
        except (OSError, ValueError) as exc:
            report['errors'].append({'session_id': session_id, 'error': str(exc)})
    return report
