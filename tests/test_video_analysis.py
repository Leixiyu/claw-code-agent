from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime
from pathlib import Path

import httpx

from src.agent_tools import build_tool_context, default_tool_registry, execute_tool
from src.agent_types import AgentRuntimeConfig
from src.env_loader import load_project_env


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SUPPORTED_VIDEO_TYPES = frozenset({'upload_file', 'local_file', 'cos_file'})
_TERMINAL_STATUSES = frozenset({'done', 'failed'})


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _positive_float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f'{name} must be a number') from exc
    if value <= 0:
        raise RuntimeError(f'{name} must be greater than zero')
    return value


class VideoAnalysisIntegrationTests(unittest.TestCase):
    """Exercise the registered video-analysis Tools against the real HTTP API."""

    @classmethod
    def setUpClass(cls) -> None:
        load_project_env(_PROJECT_ROOT / '.env')
        if not _env_truthy('RUN_VIDEO_ANALYSIS_INTEGRATION'):
            raise unittest.SkipTest(
                'set RUN_VIDEO_ANALYSIS_INTEGRATION=1 to call the real video API'
            )

        cls.base_url = os.environ.get('VIDEO_ANALYSIS_API', '').strip().rstrip('/')
        if not cls.base_url:
            raise unittest.SkipTest('VIDEO_ANALYSIS_API is missing from .env')
        if '://' not in cls.base_url:
            cls.base_url = f'http://{cls.base_url}'

        cls.video_type = os.environ.get(
            'VIDEO_ANALYSIS_TEST_VIDEO_TYPE',
            'upload_file',
        ).strip()
        if cls.video_type not in _SUPPORTED_VIDEO_TYPES:
            raise RuntimeError(
                'VIDEO_ANALYSIS_TEST_VIDEO_TYPE must be upload_file, local_file, or cos_file'
            )

        cls.video_path = os.environ.get('VIDEO_ANALYSIS_TEST_VIDEO_PATH', '').strip()
        if not cls.video_path:
            raise unittest.SkipTest(
                'VIDEO_ANALYSIS_TEST_VIDEO_PATH is missing from .env; '
                'set it to a real test video before running the integration test'
            )
        if cls.video_type == 'upload_file':
            upload_path = Path(cls.video_path).expanduser()
            if not upload_path.is_absolute():
                upload_path = _PROJECT_ROOT / upload_path
            try:
                upload_path = upload_path.resolve(strict=True)
            except OSError as exc:
                raise unittest.SkipTest(
                    f'upload test video was not found: {cls.video_path!r}'
                ) from exc
            if not upload_path.is_file():
                raise unittest.SkipTest(
                    f'upload test video is not a regular file: {cls.video_path!r}'
                )
            cls.video_path = str(upload_path)

        cls.scenario = os.environ.get(
            'VIDEO_ANALYSIS_TEST_SCENARIO',
            'fire_inspection',
        ).strip()
        cls.request_timeout_seconds = _positive_float_env(
            'VIDEO_ANALYSIS_TEST_REQUEST_TIMEOUT_SECONDS',
            300.0,
        )
        cls.poll_interval_seconds = _positive_float_env(
            'VIDEO_ANALYSIS_TEST_POLL_INTERVAL_SECONDS',
            5.0,
        )
        cls.poll_timeout_seconds = _positive_float_env(
            'VIDEO_ANALYSIS_TEST_POLL_TIMEOUT_SECONDS',
            1800.0,
        )

        registry = default_tool_registry()
        context = build_tool_context(
            AgentRuntimeConfig(
                cwd=_PROJECT_ROOT,
                command_timeout_seconds=cls.request_timeout_seconds,
            ),
            tool_registry=registry,
        )
        cls.registry = registry
        cls.context = context

    def _execute(self, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = execute_tool(
            self.registry,
            tool_name,
            arguments,
            self.context,
        )
        self.assertTrue(result.ok, f'{tool_name} failed: {result.content}')
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError as exc:
            self.fail(f'{tool_name} returned invalid JSON: {exc}')
        self.assertIsInstance(payload, dict)
        return payload

    def test_real_video_analysis_workflow(self) -> None:
        health_response = httpx.get(
            f'{self.base_url}/health',
            timeout=self.request_timeout_seconds,
        )
        health_response.raise_for_status()
        health_payload = health_response.json()
        self.assertIsInstance(health_payload, dict)
        self.assertEqual(health_payload.get('status'), 'healthy')

        video_name = Path(self.video_path).stem
        idempotency_key = (
            f'{video_name}-{self.scenario}-'
            f'{datetime.now().strftime("%y%m%d-%H%M%S")}'
        )
        submitted = self._execute(
            'submit_video_analysis',
            {
                'scenario': self.scenario,
                'video_ref': {
                    'type': self.video_type,
                    'path': self.video_path,
                },
                'idempotency_key': idempotency_key,
            },
        )
        self.assertEqual(submitted.get('status'), 'pending')
        task_id = submitted.get('task_id')
        self.assertIsInstance(task_id, str)
        self.assertTrue(task_id)
        print(f'\nsubmitted video-analysis task: {task_id}', flush=True)

        deadline = time.monotonic() + self.poll_timeout_seconds
        status_payload: dict[str, object] | None = None
        last_status: object = None
        while time.monotonic() < deadline:
            status_payload = self._execute(
                'get_video_analysis_status',
                {'task_id': task_id},
            )
            status = status_payload.get('status')
            self.assertIn(status, {'pending', 'running', 'done', 'failed'})
            if status != last_status:
                print(f'video-analysis status: {status}', flush=True)
                last_status = status
            if status in _TERMINAL_STATUSES:
                break
            time.sleep(self.poll_interval_seconds)
        else:
            self.fail(
                f'video-analysis task {task_id} did not finish within '
                f'{self.poll_timeout_seconds:.0f} seconds'
            )

        self.assertIsNotNone(status_payload)
        self.assertEqual(
            status_payload.get('status'),
            'done',
            f'video-analysis task {task_id} did not succeed: {status_payload}',
        )

        result_payload = self._execute(
            'get_video_analysis_result',
            {'task_id': task_id},
        )
        self.assertEqual(result_payload.get('task_id'), task_id)
        self.assertEqual(result_payload.get('status'), 'done')
        self.assertIsInstance(result_payload.get('results'), list)
        self.assertEqual(
            result_payload.get('result_count'),
            len(result_payload['results']),
        )
        print(
            f'video-analysis result count: {result_payload["result_count"]}',
            flush=True,
        )


if __name__ == '__main__':
    unittest.main()
