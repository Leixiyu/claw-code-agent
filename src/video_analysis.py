from __future__ import annotations

import json
import mimetypes
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import httpx


INFERENCE_API_BASE_URL_ENV = 'INFERENCE_API_BASE_URL'
SUPPORTED_SCENARIOS = frozenset({'fire_inspection'})
VIDEO_REF_TYPES = frozenset({'upload_file', 'local_file', 'cos_file'})

_IDEMPOTENCY_DIRECTORY = Path('.port_sessions') / 'business_functions'
_IDEMPOTENCY_FILE = 'video_analysis_idempotency.json'
_IDEMPOTENCY_LOCK_FILE = 'video_analysis_idempotency.lock'
_PROCESS_LOCK = threading.Lock()


class VideoAnalysisError(RuntimeError):
    """Raised when a video-analysis Function cannot validate or submit a request."""


def _require_string(container: dict[str, Any], key: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise VideoAnalysisError(f'{key} must be a non-empty string')
    return value


def _validate_idempotency_key(key: str, video_name: str, scenario: str) -> None:
    prefix = f'{video_name}-{scenario}-'
    if not key.startswith(prefix):
        raise VideoAnalysisError(
            'idempotency_key must use the format '
            f'{video_name}-{scenario}-YYMMDD-HHMMSS'
        )
    timestamp = key[len(prefix) :]
    try:
        parsed = datetime.strptime(timestamp, '%y%m%d-%H%M%S')
    except ValueError as exc:
        raise VideoAnalysisError(
            'idempotency_key timestamp contains an invalid calendar date or 24-hour time'
        ) from exc
    if parsed.strftime('%y%m%d-%H%M%S') != timestamp:
        raise VideoAnalysisError(
            'idempotency_key timestamp must use the exact zero-padded YYMMDD-HHMMSS format'
        )


def _predict_endpoint(base_url: str) -> str:
    normalized = base_url.strip().rstrip('/')
    if not normalized:
        raise VideoAnalysisError(
            f'{INFERENCE_API_BASE_URL_ENV} is required to submit video analysis'
        )
    if '://' not in normalized:
        normalized = f'http://{normalized}'
    return f'{normalized}/predict'


def _resolve_uploaded_video(raw_path: str, workspace_root: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root.resolve() / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VideoAnalysisError(f'uploaded video file was not found: {raw_path!r}') from exc
    if not resolved.is_file():
        raise VideoAnalysisError(f'uploaded video path is not a regular file: {raw_path!r}')
    return resolved


def _task_id_from_response(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError as exc:
        raise VideoAnalysisError('video analysis API returned invalid JSON') from exc
    if not isinstance(payload, dict):
        raise VideoAnalysisError('video analysis API response must be a JSON object')
    task_id = payload.get('task_id')
    if not isinstance(task_id, str) or not task_id:
        raise VideoAnalysisError('video analysis API response is missing task_id')
    return task_id


def _post_local_video(endpoint: str, local_path: str, timeout_seconds: float) -> str:
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(endpoint, params={'filepath': local_path})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise VideoAnalysisError(f'video analysis API returned HTTP {status_code}') from exc
    except httpx.HTTPError as exc:
        raise VideoAnalysisError(f'failed to call video analysis API: {exc}') from exc

    return _task_id_from_response(response)


def _post_uploaded_video(endpoint: str, upload_path: Path, timeout_seconds: float) -> str:
    content_type = mimetypes.guess_type(upload_path.name)[0] or 'application/octet-stream'
    try:
        with upload_path.open('rb') as video_file:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    endpoint,
                    files={'file': (upload_path.name, video_file, content_type)},
                )
                response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise VideoAnalysisError(f'video analysis API returned HTTP {status_code}') from exc
    except httpx.HTTPError as exc:
        raise VideoAnalysisError(f'failed to call video analysis API: {exc}') from exc
    except OSError as exc:
        raise VideoAnalysisError(f'failed to read uploaded video: {exc}') from exc

    return _task_id_from_response(response)


def _post_cos_video(endpoint: str, cos_path: str, timeout_seconds: float) -> str:
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(endpoint, params={'cos_filepath': cos_path})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise VideoAnalysisError(f'video analysis API returned HTTP {status_code}') from exc
    except httpx.HTTPError as exc:
        raise VideoAnalysisError(f'failed to call video analysis API: {exc}') from exc

    return _task_id_from_response(response)


def _read_registry(workspace_root: Path) -> dict[str, Any]:
    """Read the persisted idempotency registry, or return an empty registry."""
    registry_path = workspace_root.resolve() / _IDEMPOTENCY_DIRECTORY / _IDEMPOTENCY_FILE
    if not registry_path.exists():
        return {'version': 1, 'entries': {}}
    try:
        payload = json.loads(registry_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoAnalysisError(f'failed to read video-analysis idempotency state: {exc}') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('entries'), dict):
        raise VideoAnalysisError('video-analysis idempotency state has an invalid format')
    return payload


def _write_registry(workspace_root: Path, registry: dict[str, Any]) -> None:
    """Persist the idempotency registry using an atomic file replacement."""
    state_dir = workspace_root.resolve() / _IDEMPOTENCY_DIRECTORY
    registry_path = state_dir / _IDEMPOTENCY_FILE
    temporary_path = state_dir / f'.{_IDEMPOTENCY_FILE}.{os.getpid()}.tmp'
    try:
        temporary_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(registry_path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise VideoAnalysisError(f'failed to persist video-analysis idempotency state: {exc}') from exc


def _render_result(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2),
        {
            'action': 'submit_video_analysis',
            'task_id': payload['task_id'],
            'status': payload['status'],
            'scenario': payload['scenario'],
            'idempotency_key': payload['idempotency_key'],
            'idempotency_replayed': payload['idempotency_replayed'],
        },
    )


@contextmanager
def _locked_registry(workspace_root: Path) -> Iterator[dict[str, Any]]:
    """Lock the idempotency registry while it is being read or updated."""
    state_dir = workspace_root.resolve() / _IDEMPOTENCY_DIRECTORY
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / _IDEMPOTENCY_LOCK_FILE
    with _PROCESS_LOCK:
        with lock_path.open('a+', encoding='utf-8') as lock_file:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except ImportError:
                fcntl = None  # type: ignore[assignment]
            try:
                yield _read_registry(workspace_root)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def submit_video_analysis(
    arguments: dict[str, Any],
    *,
    workspace_root: Path,
    timeout_seconds: float,
    base_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Submit a video reference to the asynchronous analysis pipeline.

    ``local_file`` is a path visible to the video-analysis server and is sent
    through the ``filepath`` query parameter. ``upload_file`` is a path visible
    to the Agent Harness server and is sent as multipart file content.
    ``cos_file`` is passed through the ``cos_filepath`` query parameter without
    path validation. The downstream ``/predict`` endpoint's default processing
    parameters remain authoritative.
    """

    scenario = _require_string(arguments, 'scenario')
    if scenario not in SUPPORTED_SCENARIOS:
        raise VideoAnalysisError(
            f'unsupported scenario {scenario!r}; supported scenarios: '
            f'{", ".join(sorted(SUPPORTED_SCENARIOS))}'
        )

    video_ref = arguments.get('video_ref')
    if not isinstance(video_ref, dict):
        raise VideoAnalysisError('video_ref must be an object')
    ref_type = _require_string(video_ref, 'type')
    if ref_type not in VIDEO_REF_TYPES:
        raise VideoAnalysisError(
            f'unsupported video_ref.type {ref_type!r}; expected one of: '
            f'{", ".join(sorted(VIDEO_REF_TYPES))}'
        )
    raw_path = _require_string(video_ref, 'path')
    if ref_type == 'upload_file':
        upload_path = _resolve_uploaded_video(raw_path, workspace_root)
        video_name = upload_path.stem
        stat = upload_path.stat()
        request_video_ref = {
            'type': ref_type,
            'path': str(upload_path),
            'size_bytes': stat.st_size,
            'modified_time_ns': stat.st_mtime_ns,
        }
    else:
        upload_path = None
        video_name = Path(raw_path).stem
        request_video_ref = {
            'type': ref_type,
            'path': raw_path,
        }

    idempotency_key = _require_string(arguments, 'idempotency_key')
    _validate_idempotency_key(idempotency_key, video_name, scenario)

    endpoint = _predict_endpoint(base_url or os.environ.get(INFERENCE_API_BASE_URL_ENV, ''))
    request_fingerprint = {
        'scenario': scenario,
        'video_ref': request_video_ref,
    }

    with _locked_registry(workspace_root) as registry:
        entries = registry.setdefault('entries', {})
        existing = entries.get(idempotency_key)
        if existing is not None:
            if existing.get('request') != request_fingerprint:
                raise VideoAnalysisError(
                    'idempotency_key has already been used for a different video-analysis request'
                )
            response_payload = dict(existing['response'])
            response_payload['idempotency_replayed'] = True
            return _render_result(response_payload)

        if upload_path is None:
            if ref_type == 'cos_file':
                task_id = _post_cos_video(endpoint, raw_path, timeout_seconds)
            else:
                task_id = _post_local_video(endpoint, raw_path, timeout_seconds)
        else:
            task_id = _post_uploaded_video(endpoint, upload_path, timeout_seconds)
        response_payload = {
            'task_id': task_id,
            'status': 'queued',
            'scenario': scenario,
            'video_ref': {
                'type': ref_type,
                'path': raw_path,
            },
            'idempotency_key': idempotency_key,
            'idempotency_replayed': False,
        }
        entries[idempotency_key] = {
            'request': request_fingerprint,
            'response': response_payload,
        }
        _write_registry(workspace_root, registry)

    return _render_result(response_payload)
