from __future__ import annotations

import json
import mimetypes
import os
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import httpx

if TYPE_CHECKING:
    from .mcp_runtime import MCPRuntime


VIDEO_ANALYSIS_API_ENV = 'VIDEO_ANALYSIS_API'
VIDEO_PROCESSING_API_ENV = 'VIDEO_PROCESSING_API'
SUPPORTED_SCENARIOS = frozenset({'fire_inspection'})
VIDEO_REF_TYPES = frozenset({'upload_file', 'local_file', 'cos_file'})

_VIDEO_ANALYSIS_STATUSES = frozenset({'pending', 'running', 'done', 'failed'})

_BUSINESS_FUNCTIONS_DIRECTORY = Path('.port_sessions') / 'business_functions'
_ANALYSIS_IDEMPOTENCY_FILE = 'video_analysis_idempotency.json'
_ANALYSIS_IDEMPOTENCY_LOCK_FILE = 'video_analysis_idempotency.lock'
_ANALYSIS_PROCESS_LOCK = threading.Lock()
_PROCESSING_IDEMPOTENCY_FILE = 'video_processing_idempotency.json'
_PROCESSING_IDEMPOTENCY_LOCK_FILE = 'video_processing_idempotency.lock'
_PROCESSING_PROCESS_LOCK = threading.Lock()


class VideoAnalysisError(RuntimeError):
    """Raised when a video-analysis Function cannot complete a request."""


class VideoProcessingError(RuntimeError):
    """Raised when a video-processing operation cannot be completed."""


def _require_string(
    container: dict[str, Any],
    key: str,
    *,
    error_type: type[RuntimeError] = VideoAnalysisError,
) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise error_type(f'{key} must be a non-empty string')
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
            f'{VIDEO_ANALYSIS_API_ENV} is required to submit video analysis'
        )
    if '://' not in normalized:
        normalized = f'http://{normalized}'
    return f'{normalized}/predict'


def _processing_endpoint(base_url: str) -> str:
    normalized = base_url.strip().rstrip('/')
    if not normalized:
        raise VideoProcessingError(
            f'{VIDEO_PROCESSING_API_ENV} is required to submit video processing'
        )
    if '://' not in normalized:
        normalized = f'http://{normalized}'
    return f'{normalized}/process'


def _status_endpoint(base_url: str, task_id: str) -> str:
    normalized = base_url.strip().rstrip('/')
    if not normalized:
        raise VideoAnalysisError(
            f'{VIDEO_ANALYSIS_API_ENV} is required to query video analysis status'
        )
    if '://' not in normalized:
        normalized = f'http://{normalized}'
    return f'{normalized}/status/{task_id}'


def _result_endpoint(base_url: str, task_id: str) -> str:
    normalized = base_url.strip().rstrip('/')
    if not normalized:
        raise VideoAnalysisError(
            f'{VIDEO_ANALYSIS_API_ENV} is required to query video analysis result'
        )
    if '://' not in normalized:
        normalized = f'http://{normalized}'
    return f'{normalized}/result/{task_id}'


def _resolve_uploaded_video(
    raw_path: str,
    workspace_root: Path,
    *,
    error_type: type[RuntimeError] = VideoAnalysisError,
) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root.resolve() / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise error_type(f'uploaded video file was not found: {raw_path!r}') from exc
    if not resolved.is_file():
        raise error_type(f'uploaded video path is not a regular file: {raw_path!r}')
    return resolved


def _task_id_from_response(
    response: httpx.Response,
    *,
    operation: str = 'video analysis',
    error_type: type[RuntimeError] = VideoAnalysisError,
) -> str:
    try:
        payload = response.json()
    except ValueError as exc:
        raise error_type(f'{operation} API returned invalid JSON') from exc
    if not isinstance(payload, dict):
        raise error_type(f'{operation} API response must be a JSON object')
    task_id = payload.get('task_id')
    if not isinstance(task_id, str) or not task_id:
        raise error_type(f'{operation} API response is missing task_id')
    return task_id


def _status_from_response(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError as exc:
        raise VideoAnalysisError('video analysis status API returned invalid JSON') from exc
    if not isinstance(payload, dict):
        raise VideoAnalysisError('video analysis status API response must be a JSON object')
    status = payload.get('status')
    if not isinstance(status, str) or not status:
        raise VideoAnalysisError('video analysis status API response is missing status')
    return status


def _results_from_response(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise VideoAnalysisError('video analysis result API returned invalid JSON') from exc
    if not isinstance(payload, list):
        raise VideoAnalysisError('video analysis result API response must be a JSON array')
    if any(not isinstance(item, dict) for item in payload):
        raise VideoAnalysisError(
            'video analysis result API response items must be JSON objects'
        )
    return payload


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


def _post_uploaded_videos(
    endpoint: str,
    upload_paths: list[Path],
    timeout_seconds: float,
) -> str:
    try:
        with ExitStack() as stack:
            files = []
            for upload_path in upload_paths:
                video_file = stack.enter_context(upload_path.open('rb'))
                content_type = (
                    mimetypes.guess_type(upload_path.name)[0]
                    or 'application/octet-stream'
                )
                files.append(('file', (upload_path.name, video_file, content_type)))
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(endpoint, files=files)
                response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise VideoProcessingError(
            f'video processing API returned HTTP {status_code}'
        ) from exc
    except httpx.HTTPError as exc:
        raise VideoProcessingError(
            f'failed to call video processing API: {exc}'
        ) from exc
    except OSError as exc:
        raise VideoProcessingError(f'failed to read uploaded video: {exc}') from exc

    return _task_id_from_response(
        response,
        operation='video processing',
        error_type=VideoProcessingError,
    )


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


def _get_backend_status(endpoint: str, timeout_seconds: float) -> str:
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(endpoint)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise VideoAnalysisError(f'video analysis status API returned HTTP {status_code}') from exc
    except httpx.HTTPError as exc:
        raise VideoAnalysisError(f'failed to call video analysis status API: {exc}') from exc

    return _status_from_response(response)


def _get_backend_result(
    endpoint: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(endpoint)
            if response.status_code == 202:
                raise VideoAnalysisError('video analysis result is not ready (HTTP 202)')
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        raise VideoAnalysisError(f'video analysis result API returned HTTP {status_code}') from exc
    except httpx.HTTPError as exc:
        raise VideoAnalysisError(f'failed to call video analysis result API: {exc}') from exc

    return _results_from_response(response)


def _read_registry(
    workspace_root: Path,
    *,
    registry_file: str = _ANALYSIS_IDEMPOTENCY_FILE,
    operation: str = 'video-analysis',
    error_type: type[RuntimeError] = VideoAnalysisError,
) -> dict[str, Any]:
    """Read the persisted idempotency registry, or return an empty registry."""
    registry_path = (
        workspace_root.resolve()
        / _BUSINESS_FUNCTIONS_DIRECTORY
        / registry_file
    )
    if not registry_path.exists():
        return {'version': 1, 'entries': {}}
    try:
        payload = json.loads(registry_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise error_type(f'failed to read {operation} idempotency state: {exc}') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('entries'), dict):
        raise error_type(f'{operation} idempotency state has an invalid format')
    return payload


def _write_registry(
    workspace_root: Path,
    registry: dict[str, Any],
    *,
    registry_file: str = _ANALYSIS_IDEMPOTENCY_FILE,
    operation: str = 'video-analysis',
    error_type: type[RuntimeError] = VideoAnalysisError,
) -> None:
    """Persist the idempotency registry using an atomic file replacement."""
    state_dir = workspace_root.resolve() / _BUSINESS_FUNCTIONS_DIRECTORY
    registry_path = state_dir / registry_file
    temporary_path = state_dir / f'.{registry_file}.{os.getpid()}.tmp'
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
        raise error_type(f'failed to persist {operation} idempotency state: {exc}') from exc


def _render_analysis_submit_result(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
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


def _render_processing_submit_result(
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2),
        {
            'action': 'submit_video_processing',
            'task_id': payload['task_id'],
            'status': payload['status'],
            'scenario': payload['scenario'],
            'idempotency_key': payload['idempotency_key'],
            'idempotency_replayed': payload['idempotency_replayed'],
        },
    )


def _render_status_result(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2),
        {
            'action': 'get_video_analysis_status',
            'task_id': payload['task_id'],
            'status': payload['status'],
            'is_terminal': payload['is_terminal'],
            'result_ready': payload['result_ready'],
        },
    )


def _render_analysis_result(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2),
        {
            'action': 'get_video_analysis_result',
            'task_id': payload['task_id'],
            'status': payload['status'],
            'result_count': payload['result_count'],
        },
    )


@contextmanager
def _locked_registry(
    workspace_root: Path,
    *,
    registry_file: str = _ANALYSIS_IDEMPOTENCY_FILE,
    lock_filename: str = _ANALYSIS_IDEMPOTENCY_LOCK_FILE,
    process_lock: threading.Lock = _ANALYSIS_PROCESS_LOCK,
    operation: str = 'video-analysis',
    error_type: type[RuntimeError] = VideoAnalysisError,
) -> Iterator[dict[str, Any]]:
    """Lock the idempotency registry while it is being read or updated."""
    state_dir = workspace_root.resolve() / _BUSINESS_FUNCTIONS_DIRECTORY
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / lock_filename
    with process_lock:
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
                yield _read_registry(
                    workspace_root,
                    registry_file=registry_file,
                    operation=operation,
                    error_type=error_type,
                )
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

    endpoint = _predict_endpoint(base_url or os.environ.get(VIDEO_ANALYSIS_API_ENV, ''))
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
            response_payload['status'] = 'pending'
            response_payload['idempotency_replayed'] = True
            return _render_analysis_submit_result(response_payload)

        if upload_path is None:
            if ref_type == 'cos_file':
                task_id = _post_cos_video(endpoint, raw_path, timeout_seconds)
            else:
                task_id = _post_local_video(endpoint, raw_path, timeout_seconds)
        else:
            task_id = _post_uploaded_video(endpoint, upload_path, timeout_seconds)
        response_payload = {
            'task_id': task_id,
            'status': 'pending',
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

    return _render_analysis_submit_result(response_payload)


def submit_video_processing(
    arguments: dict[str, Any],
    *,
    workspace_root: Path,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Upload raw Harness-hosted videos for labeling and dataset preparation."""
    del mcp_runtime

    scenario = _require_string(
        arguments,
        'scenario',
        error_type=VideoProcessingError,
    )
    if scenario not in SUPPORTED_SCENARIOS:
        raise VideoProcessingError(
            f'unsupported scenario {scenario!r}; supported scenarios: '
            f'{", ".join(sorted(SUPPORTED_SCENARIOS))}'
        )

    raw_video_refs = arguments.get('raw_video_refs')
    if not isinstance(raw_video_refs, list) or not raw_video_refs:
        raise VideoProcessingError('raw_video_refs must be a non-empty array')
    if any(not isinstance(raw_path, str) or not raw_path for raw_path in raw_video_refs):
        raise VideoProcessingError('raw_video_refs items must be non-empty strings')

    upload_paths = [
        _resolve_uploaded_video(
            raw_path,
            workspace_root,
            error_type=VideoProcessingError,
        )
        for raw_path in raw_video_refs
    ]
    request_video_refs = []
    for upload_path in upload_paths:
        stat = upload_path.stat()
        request_video_refs.append(
            {
                'path': str(upload_path),
                'size_bytes': stat.st_size,
                'modified_time_ns': stat.st_mtime_ns,
            }
        )

    idempotency_key = _require_string(
        arguments,
        'idempotency_key',
        error_type=VideoProcessingError,
    )
    endpoint = _processing_endpoint(
        os.environ.get(VIDEO_PROCESSING_API_ENV, '')
    )
    request_fingerprint = {
        'scenario': scenario,
        'raw_video_refs': request_video_refs,
    }

    with _locked_registry(
        workspace_root,
        registry_file=_PROCESSING_IDEMPOTENCY_FILE,
        lock_filename=_PROCESSING_IDEMPOTENCY_LOCK_FILE,
        process_lock=_PROCESSING_PROCESS_LOCK,
        operation='video-processing',
        error_type=VideoProcessingError,
    ) as registry:
        entries = registry.setdefault('entries', {})
        existing = entries.get(idempotency_key)
        if existing is not None:
            if existing.get('request') != request_fingerprint:
                raise VideoProcessingError(
                    'idempotency_key has already been used for a different '
                    'video-processing request'
                )
            response_payload = dict(existing['response'])
            response_payload['status'] = 'pending'
            response_payload['idempotency_replayed'] = True
            return _render_processing_submit_result(response_payload)

        task_id = _post_uploaded_videos(endpoint, upload_paths, timeout_seconds)
        response_payload = {
            'task_id': task_id,
            'status': 'pending',
            'scenario': scenario,
            'raw_video_refs': list(raw_video_refs),
            'idempotency_key': idempotency_key,
            'idempotency_replayed': False,
        }
        entries[idempotency_key] = {
            'request': request_fingerprint,
            'response': response_payload,
        }
        _write_registry(
            workspace_root,
            registry,
            registry_file=_PROCESSING_IDEMPOTENCY_FILE,
            operation='video-processing',
            error_type=VideoProcessingError,
        )

    return _render_processing_submit_result(response_payload)


def get_video_analysis_status(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    base_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Query and normalize the current state of a video-analysis task."""
    task_id = _require_string(arguments, 'task_id')
    endpoint = _status_endpoint(
        base_url or os.environ.get(VIDEO_ANALYSIS_API_ENV, ''),
        task_id,
    )
    status = _get_backend_status(endpoint, timeout_seconds)
    if status not in _VIDEO_ANALYSIS_STATUSES:
        raise VideoAnalysisError(
            f'video analysis status API returned unsupported status {status!r}'
        )

    payload = {
        'task_id': task_id,
        'status': status,
        'is_terminal': status in {'done', 'failed'},
        'result_ready': status == 'done',
    }
    return _render_status_result(payload)


def get_video_processing_status(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the authoritative status of a video-processing task."""
    raise VideoProcessingError('get_video_processing_status is registered but not implemented')


def get_video_analysis_result(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    base_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the completed video-analysis result as a JSON object."""
    task_id = _require_string(arguments, 'task_id')
    endpoint = _result_endpoint(
        base_url or os.environ.get(VIDEO_ANALYSIS_API_ENV, ''),
        task_id,
    )
    results = _get_backend_result(endpoint, timeout_seconds)
    payload = {
        'task_id': task_id,
        'status': 'done',
        'result_count': len(results),
        'results': results,
    }
    return _render_analysis_result(payload)


def get_video_processing_result(
    arguments: dict[str, Any],
    *,
    timeout_seconds: float,
    mcp_runtime: MCPRuntime | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the public dataset manifest for a completed processing task."""
    raise VideoProcessingError('get_video_processing_result is registered but not implemented')
