from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .user_workspace import atomic_json, contained


DEFAULT_BACKGROUND_DIR = Path('.port_sessions') / 'background'
_DETACHED_PROCESSES: dict[int, subprocess.Popen[Any]] = {}


@dataclass(frozen=True)
class BackgroundSessionRecord:
    background_id: str
    pid: int
    prompt: str
    workspace_cwd: str
    model: str
    mode: str
    status: str
    log_path: str
    record_path: str
    started_at: str
    command: tuple[str, ...]
    finished_at: str | None = None
    exit_code: int | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    session_path: str | None = None
    process_identity: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> 'BackgroundSessionRecord':
        return cls(
            background_id=str(payload.get('background_id') or ''),
            pid=int(payload.get('pid') or 0),
            prompt=str(payload.get('prompt') or ''),
            workspace_cwd=str(payload.get('workspace_cwd') or ''),
            model=str(payload.get('model') or ''),
            mode=str(payload.get('mode') or 'agent'),
            status=str(payload.get('status') or 'unknown'),
            log_path=str(payload.get('log_path') or ''),
            record_path=str(payload.get('record_path') or ''),
            started_at=str(payload.get('started_at') or ''),
            command=tuple(
                str(item)
                for item in payload.get('command', [])
                if isinstance(item, (str, int, float))
            ),
            finished_at=(
                str(payload.get('finished_at'))
                if isinstance(payload.get('finished_at'), str) and payload.get('finished_at')
                else None
            ),
            exit_code=(
                int(payload.get('exit_code'))
                if isinstance(payload.get('exit_code'), int)
                else None
            ),
            stop_reason=(
                str(payload.get('stop_reason'))
                if isinstance(payload.get('stop_reason'), str) and payload.get('stop_reason')
                else None
            ),
            session_id=(
                str(payload.get('session_id'))
                if isinstance(payload.get('session_id'), str) and payload.get('session_id')
                else None
            ),
            session_path=(
                str(payload.get('session_path'))
                if isinstance(payload.get('session_path'), str) and payload.get('session_path')
                else None
            ),
            process_identity=payload.get('process_identity') if isinstance(payload.get('process_identity'), str) else None,
        )


class BackgroundSessionRuntime:
    def __init__(self, root: Path | None = None, *, workspace: Path | None = None) -> None:
        self.workspace = workspace.resolve() if workspace is not None else None
        requested = root or DEFAULT_BACKGROUND_DIR
        if requested.is_symlink():
            raise ValueError('background directory must not be a symlink')
        self.root = contained(self.workspace, requested) if self.workspace else requested.resolve()
        if self.workspace and self.root != self.workspace / DEFAULT_BACKGROUND_DIR:
            raise ValueError('background directory does not belong to the current user')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @classmethod
    def for_workspace(cls, workspace: Path) -> 'BackgroundSessionRuntime':
        return cls(workspace.resolve() / DEFAULT_BACKGROUND_DIR, workspace=workspace)

    def _path(self, background_id: str, suffix: str) -> Path:
        if not isinstance(background_id, str) or not re.fullmatch(r'bg_[A-Za-z0-9_-]{1,128}', background_id):
            raise ValueError('invalid background ID')
        path = self.root / f'{background_id}{suffix}'
        self._check_path(path)
        return path

    def _check_path(self, path: Path) -> None:
        if self.workspace:
            contained(self.workspace, self.root)
        contained(self.root, path)
        if path.is_symlink():
            raise ValueError('background files must not be symlinks')
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('background files must be regular, unlinked files')

    @contextmanager
    def _locked(self):
        # Parent publishes the launch record before the worker can read it.
        import fcntl
        path = self.root / '.records.lock'
        self._check_path(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _validate_record(self, record: BackgroundSessionRecord, background_id: str) -> None:
        if record.background_id != background_id:
            raise ValueError('background record ID does not match its filename')
        if (Path(record.record_path) != self.record_path(background_id)
                or Path(record.log_path) != self.log_path(background_id)):
            raise ValueError('background record or log path is outside its assigned location')
        if record.pid < 0 or (record.status == 'running' and record.pid <= 1):
            raise ValueError('invalid background process ID')
        if self.workspace:
            if Path(record.workspace_cwd).resolve() != self.workspace:
                raise ValueError('background task does not belong to the current user')
            if record.session_path:
                if not record.session_id or not re.fullmatch(r'[A-Za-z0-9_-]+', record.session_id):
                    raise ValueError('invalid background session reference')
                expected = contained(self.workspace, self.workspace / 'sessions' / f'{record.session_id}.json')
                if Path(record.session_path).resolve() != expected:
                    raise ValueError('background session reference is outside the user sessions directory')

    def require_worker(self, background_id: str) -> BackgroundSessionRecord:
        record = self.load_record(background_id)
        if (record.status != 'running' or record.pid != os.getpid()
                or not record.process_identity
                or record.process_identity != _process_identity(os.getpid())):
            raise ValueError('worker must be the process launched for this background task')
        return record

    def create_id(self) -> str:
        return f'bg_{uuid4().hex[:12]}'

    def record_path(self, background_id: str) -> Path:
        return self._path(background_id, '.json')

    def log_path(self, background_id: str) -> Path:
        return self._path(background_id, '.log')

    def launch(
        self,
        command: list[str],
        *,
        prompt: str,
        workspace_cwd: Path,
        model: str,
        mode: str = 'agent',
        background_id: str | None = None,
        process_cwd: Path | None = None,
        process_env: dict[str, str] | None = None,
    ) -> BackgroundSessionRecord:
        background_id = background_id or self.create_id()
        if self.workspace and workspace_cwd.resolve() != self.workspace:
            raise ValueError('background launch workspace does not belong to the current user')
        log_path = self.log_path(background_id)
        record_path = self.record_path(background_id)
        with self._locked():
            if record_path.exists():
                raise ValueError('background ID already exists')
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                process = subprocess.Popen(
                    command, stdout=handle, stderr=subprocess.STDOUT,
                    cwd=str(process_cwd or Path.cwd()), env=process_env, start_new_session=True,
                )
            _DETACHED_PROCESSES[process.pid] = process
            try:
                record = BackgroundSessionRecord(
                    background_id=background_id, pid=process.pid, prompt=prompt,
                    workspace_cwd=str(workspace_cwd.resolve()), model=model, mode=mode,
                    status='running', log_path=str(log_path), record_path=str(record_path),
                    started_at=_utc_now(), command=tuple(command),
                    process_identity=_process_identity(process.pid),
                )
                self._validate_record(record, background_id)
                atomic_json(record_path, asdict(record))
            except Exception:
                # Do not leave an unregistered child when publishing fails.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                _DETACHED_PROCESSES.pop(process.pid, None)
                raise
        return record

    def save_record(self, record: BackgroundSessionRecord) -> Path:
        self._validate_record(record, record.background_id)
        path = self.record_path(record.background_id)
        with self._locked():
            self._validate_record(record, record.background_id)
            atomic_json(path, asdict(record))
        return path

    def load_record(self, background_id: str) -> BackgroundSessionRecord:
        with self._locked():
            path = self.record_path(background_id)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, encoding='utf-8') as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError('invalid background record')
            try:
                record = BackgroundSessionRecord.from_dict(data)
            except (TypeError, ValueError) as exc:
                raise ValueError('invalid background record') from exc
            self._validate_record(record, background_id)
        return self.refresh_record(record)

    def list_records(self) -> tuple[BackgroundSessionRecord, ...]:
        records: list[BackgroundSessionRecord] = []
        for path in sorted(self.root.glob('bg_*.json')):
            try:
                records.append(self.load_record(path.stem))
            except (OSError, ValueError):
                continue
        return tuple(sorted(records, key=lambda item: item.started_at, reverse=True))

    def refresh_record(self, record: BackgroundSessionRecord) -> BackgroundSessionRecord:
        self._validate_record(record, record.background_id)
        if record.status != 'running':
            return record
        if _is_process_running(record.pid):
            return record
        updated = BackgroundSessionRecord(
            background_id=record.background_id,
            pid=record.pid,
            prompt=record.prompt,
            workspace_cwd=record.workspace_cwd,
            model=record.model,
            mode=record.mode,
            status='exited',
            log_path=record.log_path,
            record_path=record.record_path,
            started_at=record.started_at,
            command=record.command,
            finished_at=record.finished_at or _utc_now(),
            exit_code=record.exit_code,
            stop_reason=record.stop_reason,
            session_id=record.session_id,
            session_path=record.session_path,
            process_identity=record.process_identity,
        )
        self.save_record(updated)
        process = _DETACHED_PROCESSES.pop(record.pid, None)
        if process is not None and process.returncode is None:
            process.returncode = updated.exit_code
        return updated

    def mark_finished(
        self,
        background_id: str,
        *,
        exit_code: int,
        stop_reason: str | None = None,
        session_id: str | None = None,
        session_path: str | None = None,
        status: str | None = None,
    ) -> BackgroundSessionRecord:
        record = self.load_record(background_id)
        final_status = status or ('completed' if exit_code == 0 else 'failed')
        updated = BackgroundSessionRecord(
            background_id=record.background_id,
            pid=record.pid,
            prompt=record.prompt,
            workspace_cwd=record.workspace_cwd,
            model=record.model,
            mode=record.mode,
            status=final_status,
            log_path=record.log_path,
            record_path=record.record_path,
            started_at=record.started_at,
            command=record.command,
            finished_at=_utc_now(),
            exit_code=exit_code,
            stop_reason=stop_reason,
            session_id=session_id,
            session_path=session_path,
            process_identity=record.process_identity,
        )
        self.save_record(updated)
        process = _DETACHED_PROCESSES.pop(record.pid, None)
        if process is not None and process.returncode is None:
            process.returncode = updated.exit_code
        return updated

    def kill(self, background_id: str) -> BackgroundSessionRecord:
        record = self.load_record(background_id)
        if record.status != 'running':
            return record
        # Refuse old/unverifiable records rather than signal an unrelated/reused PID.
        if not record.process_identity or record.process_identity != _process_identity(record.pid):
            raise ValueError('cannot verify background process identity; refusing to stop it')
        if os.getpgid(record.pid) != record.pid:
            raise ValueError('background process is not its own process group leader')
        try:
            os.killpg(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            return self.refresh_record(record)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                waited_pid, _ = os.waitpid(record.pid, os.WNOHANG)
                if waited_pid == record.pid:
                    break
            except ChildProcessError:
                pass  # CLI/GUI usually is not the worker's parent; poll below.
            except OSError:
                pass
            if not _is_process_running(record.pid):
                break
            time.sleep(0.05)
        if _is_process_running(record.pid):
            raise ValueError('background process did not stop; it has not been marked killed')
        updated = BackgroundSessionRecord(
            background_id=record.background_id,
            pid=record.pid,
            prompt=record.prompt,
            workspace_cwd=record.workspace_cwd,
            model=record.model,
            mode=record.mode,
            status='killed',
            log_path=record.log_path,
            record_path=record.record_path,
            started_at=record.started_at,
            command=record.command,
            finished_at=_utc_now(),
            exit_code=-signal.SIGTERM,
            stop_reason='killed',
            session_id=record.session_id,
            session_path=record.session_path,
            process_identity=record.process_identity,
        )
        self.save_record(updated)
        process = _DETACHED_PROCESSES.pop(record.pid, None)
        if process is not None and process.returncode is None:
            process.returncode = updated.exit_code
        return updated

    def read_logs(self, background_id: str, *, tail: int | None = None) -> str:
        record = self.load_record(background_id)
        path = self.log_path(record.background_id)
        if not path.exists():
            return ''
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding='utf-8', errors='replace') as handle:
            text = handle.read()
        if tail is None or tail <= 0:
            return text
        lines = text.splitlines()
        return '\n'.join(lines[-tail:])

    def render_ps(self) -> str:
        records = self.list_records()
        lines = ['# Background Sessions', '']
        if not records:
            lines.append('No local background sessions are currently recorded.')
            return '\n'.join(lines)
        for record in records:
            parts = [
                record.background_id,
                f'status={record.status}',
                f'pid={record.pid}',
                f'model={record.model}',
                f'cwd={record.workspace_cwd}',
            ]
            if record.exit_code is not None:
                parts.append(f'exit_code={record.exit_code}')
            lines.append('- ' + '; '.join(parts))
            lines.append(f'  prompt: {_snapshot_text(record.prompt)}')
        return '\n'.join(lines)

    def render_logs(self, background_id: str, *, tail: int | None = None) -> str:
        record = self.load_record(background_id)
        log_text = self.read_logs(background_id, tail=tail)
        lines = [
            '# Background Logs',
            '',
            f'- Background session: {record.background_id}',
            f'- Status: {record.status}',
            f'- PID: {record.pid}',
            f'- Log path: {record.log_path}',
            '',
            log_text.rstrip() or '(empty log)',
        ]
        return '\n'.join(lines)

    def render_attach(self, background_id: str, *, tail: int | None = None) -> str:
        record = self.load_record(background_id)
        lines = [
            '# Background Attach',
            '',
            f'- Background session: {record.background_id}',
            f'- Status: {record.status}',
            f'- Workspace cwd: {record.workspace_cwd}',
        ]
        if record.session_id:
            lines.append(f'- Agent session id: {record.session_id}')
        if record.session_path:
            lines.append(f'- Agent session path: {record.session_path}')
        lines.extend(['', self.read_logs(background_id, tail=tail).rstrip() or '(empty log)'])
        return '\n'.join(lines)


def build_background_worker_command(
    *,
    background_id: str,
    prompt: str,
    forwarded_args: list[str],
) -> list[str]:
    return [
        sys.executable,
        '-m',
        'src.main',
        'agent-bg-worker',
        background_id,
        prompt,
        *forwarded_args,
    ]


def _process_identity(pid: int) -> str | None:
    """Stable process birth information; never treat a bare PID as ownership."""
    if pid <= 1:
        return None
    try:
        if sys.platform.startswith('linux'):
            directory = Path('/proc') / str(pid)
            data = (directory / 'stat').read_text()
            fields = data[data.rfind(')') + 2:].split()
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return f'linux:{boot}:{directory.stat().st_uid}:{fields[19]}:{fields[2]}'
        result = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'uid=', '-o', 'pgid='],
            capture_output=True, text=True, timeout=2, check=False,
        )
        value = ' '.join(result.stdout.split())
        return f'ps:{value}' if result.returncode == 0 and value else None
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return None


def _is_process_running(pid: int) -> bool:
    if pid <= 1:
        return False
    process = _DETACHED_PROCESSES.get(pid)
    if process is not None and process.poll() is not None:
        return False
    try:
        if sys.platform.startswith('linux'):
            data = (Path('/proc') / str(pid) / 'stat').read_text()
            if data[data.rfind(')') + 2:].split()[0] == 'Z':
                return False
        elif sys.platform == 'darwin':
            result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                    capture_output=True, text=True, timeout=2, check=False)
            if result.returncode != 0 or result.stdout.strip().startswith('Z'):
                return False
        os.kill(pid, 0)
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        # Uncertain liveness must not be treated as a successful termination.
        return True
    return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_text(text: str, limit: int = 140) -> str:
    normalized = ' '.join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + '...'
