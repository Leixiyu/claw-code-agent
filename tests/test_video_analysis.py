from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.agent_tools import build_tool_context, default_tool_registry, execute_tool
from src.agent_types import AgentRuntimeConfig


class _FakeResponse:
    status_code = 200
    text = '{"task_id":"task-123"}'

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {'task_id': 'task-123'}


class _FakeClient:
    calls: list[tuple[str, dict[str, object]]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> '_FakeClient':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, *, params: dict[str, object]) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse()


class VideoAnalysisToolTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeClient.calls = []

    def _execute(self, workspace: Path, arguments: dict[str, object]):
        registry = default_tool_registry()
        context = build_tool_context(
            AgentRuntimeConfig(cwd=workspace),
            tool_registry=registry,
        )
        return execute_tool(registry, 'submit_video_analysis', arguments, context)

    def test_schema_reserves_all_video_reference_types(self) -> None:
        tool = default_tool_registry()['submit_video_analysis']
        ref_type = tool.parameters['properties']['video_ref']['properties']['type']

        self.assertEqual(
            ref_type['enum'],
            ['upload_file', 'local_file', 'cos_file'],
        )
        self.assertNotIn('processing_profile', tool.parameters['properties'])

    def test_submits_local_file_without_processing_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            arguments = {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'local_file', 'path': '/video/input/test.mp4'},
                'idempotency_key': 'test-fire_inspection-260101-120000',
            }
            with (
                patch.dict(os.environ, {'BUSINESS_API_BASE_URL': 'video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(workspace, arguments)

            payload = json.loads(result.content)

        self.assertTrue(result.ok)
        self.assertEqual(payload['task_id'], 'task-123')
        self.assertEqual(payload['status'], 'queued')
        self.assertFalse(payload['idempotency_replayed'])
        self.assertEqual(len(_FakeClient.calls), 1)
        url, params = _FakeClient.calls[0]
        self.assertEqual(url, 'http://video.test:8000/predict')
        self.assertEqual(params, {'filepath': '/video/input/test.mp4'})

    def test_reuses_task_for_same_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            arguments = {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'local_file', 'path': '/video/input/test.mp4'},
                'idempotency_key': 'test-fire_inspection-260101-120000',
            }
            with (
                patch.dict(os.environ, {'BUSINESS_API_BASE_URL': 'http://video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                first = self._execute(workspace, arguments)
                second = self._execute(workspace, arguments)

            first_payload = json.loads(first.content)
            second_payload = json.loads(second.content)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(first_payload['idempotency_replayed'])
        self.assertTrue(second_payload['idempotency_replayed'])
        self.assertEqual(second_payload['task_id'], first_payload['task_id'])
        self.assertEqual(len(_FakeClient.calls), 1)

    def test_rejects_reserved_reference_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = self._execute(
                Path(tmp_dir),
                {
                    'scenario': 'fire_inspection',
                    'video_ref': {'type': 'cos_file', 'path': 'cos://bucket/test.mp4'},
                    'idempotency_key': 'test-fire_inspection-260101-120000',
                },
            )

        self.assertFalse(result.ok)
        self.assertIn('reserved by the contract but is not implemented', result.content)

    def test_local_file_does_not_need_to_exist_on_agent_server(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_dir:
            video_system_path = str(Path(workspace_dir) / 'missing-on-agent-server.mp4')
            self.assertFalse(Path(video_system_path).exists())
            with (
                patch.dict(os.environ, {'BUSINESS_API_BASE_URL': 'http://video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(
                    Path(workspace_dir),
                    {
                        'scenario': 'fire_inspection',
                        'video_ref': {'type': 'local_file', 'path': video_system_path},
                        'idempotency_key': (
                            'missing-on-agent-server-fire_inspection-260101-120000'
                        ),
                    },
                )

        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.content)['task_id'], 'task-123')
        self.assertEqual(len(_FakeClient.calls), 1)

    def test_requires_key_to_match_video_and_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            result = self._execute(
                workspace,
                {
                    'scenario': 'fire_inspection',
                    'video_ref': {'type': 'local_file', 'path': 'test.mp4'},
                    'idempotency_key': 'other-fire_inspection-260101-120000',
                },
            )

        self.assertFalse(result.ok)
        self.assertIn('test-fire_inspection-YYMMDD-HHMMSS', result.content)

    def test_rejects_invalid_calendar_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            result = self._execute(
                workspace,
                {
                    'scenario': 'fire_inspection',
                    'video_ref': {'type': 'local_file', 'path': 'test.mp4'},
                    'idempotency_key': 'test-fire_inspection-261332-120000',
                },
            )

        self.assertFalse(result.ok)
        self.assertIn('invalid calendar date or 24-hour time', result.content)

    def test_rejects_noncanonical_timestamp_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            result = self._execute(
                workspace,
                {
                    'scenario': 'fire_inspection',
                    'video_ref': {'type': 'local_file', 'path': 'test.mp4'},
                    'idempotency_key': 'test-fire_inspection-26011-120000',
                },
            )

        self.assertFalse(result.ok)
        self.assertIn('exact zero-padded YYMMDD-HHMMSS format', result.content)

    def test_reports_missing_api_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            with patch.dict(os.environ, {}, clear=True):
                result = self._execute(
                    workspace,
                    {
                        'scenario': 'fire_inspection',
                        'video_ref': {'type': 'local_file', 'path': 'test.mp4'},
                        'idempotency_key': 'test-fire_inspection-260101-120000',
                    },
                )

        self.assertFalse(result.ok)
        self.assertIn('BUSINESS_API_BASE_URL is required', result.content)


if __name__ == '__main__':
    unittest.main()
