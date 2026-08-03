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


class _FakeStatusResponse(_FakeResponse):
    def json(self) -> dict[str, str]:
        return {'status': _FakeClient.backend_status}


class _FakeResultResponse(_FakeResponse):
    status_code = 200

    def json(self) -> object:
        if _FakeClient.result_json_error:
            raise ValueError('invalid JSON')
        return _FakeClient.result_payload


class _FakeResultHTTPErrorResponse(_FakeResultResponse):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        request = httpx.Request('GET', 'http://video.test/result/task-123')
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            f'HTTP {self.status_code}',
            request=request,
            response=response,
        )


class _FakeClient:
    calls: list[tuple[str, dict[str, object]]] = []
    backend_status = 'running'
    result_payload: object = []
    result_json_error = False
    result_status_code = 200

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> '_FakeClient':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse()

    def get(self, url: str) -> _FakeStatusResponse | _FakeResultResponse:
        self.calls.append((url, {}))
        if '/result/' in url:
            if self.result_status_code != 200:
                return _FakeResultHTTPErrorResponse(self.result_status_code)
            return _FakeResultResponse()
        return _FakeStatusResponse()


class VideoAnalysisToolTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeClient.calls = []
        _FakeClient.backend_status = 'running'
        _FakeClient.result_payload = []
        _FakeClient.result_json_error = False
        _FakeClient.result_status_code = 200

    def _execute(
        self,
        workspace: Path,
        arguments: dict[str, object],
        tool_name: str = 'submit_video_analysis',
    ):
        registry = default_tool_registry()
        context = build_tool_context(
            AgentRuntimeConfig(cwd=workspace),
            tool_registry=registry,
        )
        return execute_tool(registry, tool_name, arguments, context)

    def test_schema_lists_all_video_reference_types(self) -> None:
        tool = default_tool_registry()['submit_video_analysis']
        ref_type = tool.parameters['properties']['video_ref']['properties']['type']

        self.assertEqual(
            ref_type['enum'],
            ['upload_file', 'local_file', 'cos_file'],
        )
        self.assertNotIn('processing_profile', tool.parameters['properties'])

    def test_status_schema_requires_only_task_id(self) -> None:
        tool = default_tool_registry()['get_video_analysis_status']

        self.assertEqual(tool.parameters['required'], ['task_id'])
        self.assertEqual(set(tool.parameters['properties']), {'task_id'})

    def test_result_schema_requires_only_task_id(self) -> None:
        tool = default_tool_registry()['get_video_analysis_result']

        self.assertEqual(tool.parameters['required'], ['task_id'])
        self.assertEqual(set(tool.parameters['properties']), {'task_id'})

    def test_returns_video_analysis_result_as_json_object(self) -> None:
        backend_results = [
            {
                'category_id': 1,
                'category_name': 'fire_extinguisher',
                'start_timestamp_s': 12.5,
                'end_timestamp_s': 18.2,
                'is_detected': True,
                'transcription_text': None,
                'hit_keywords': None,
            },
            {
                'category_id': 2,
                'category_name': 'safety_exit',
                'start_timestamp_s': None,
                'end_timestamp_s': None,
                'is_detected': False,
                'transcription_text': '出口',
                'hit_keywords': ['出口'],
            },
        ]
        _FakeClient.result_payload = backend_results
        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(
                    Path(tmp_dir),
                    {'task_id': 'task-123'},
                    'get_video_analysis_result',
                )

        payload = json.loads(result.content)
        self.assertTrue(result.ok)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload['task_id'], 'task-123')
        self.assertEqual(payload['status'], 'done')
        self.assertEqual(payload['result_count'], 2)
        self.assertEqual(payload['results'], backend_results)
        self.assertEqual(
            _FakeClient.calls,
            [('http://video.test:8000/result/task-123', {})],
        )

    def test_reports_result_http_errors(self) -> None:
        for status_code in (202, 404, 500):
            with self.subTest(status_code=status_code):
                _FakeClient.calls = []
                _FakeClient.result_status_code = status_code
                with tempfile.TemporaryDirectory() as tmp_dir:
                    with (
                        patch.dict(
                            os.environ,
                            {'INFERENCE_API_BASE_URL': 'video.test:8000'},
                        ),
                        patch('src.video_analysis.httpx.Client', _FakeClient),
                    ):
                        result = self._execute(
                            Path(tmp_dir),
                            {'task_id': 'task-123'},
                            'get_video_analysis_result',
                        )

                self.assertFalse(result.ok)
                self.assertIn(f'HTTP {status_code}', result.content)

    def test_rejects_invalid_result_json(self) -> None:
        _FakeClient.result_json_error = True
        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(
                    Path(tmp_dir),
                    {'task_id': 'task-123'},
                    'get_video_analysis_result',
                )

        self.assertFalse(result.ok)
        self.assertIn('returned invalid JSON', result.content)

    def test_rejects_invalid_result_structure(self) -> None:
        invalid_payloads = ({'result': []}, ['not-an-object'])
        for invalid_payload in invalid_payloads:
            with self.subTest(invalid_payload=invalid_payload):
                _FakeClient.result_payload = invalid_payload
                with tempfile.TemporaryDirectory() as tmp_dir:
                    with (
                        patch.dict(
                            os.environ,
                            {'INFERENCE_API_BASE_URL': 'video.test:8000'},
                        ),
                        patch('src.video_analysis.httpx.Client', _FakeClient),
                    ):
                        result = self._execute(
                            Path(tmp_dir),
                            {'task_id': 'task-123'},
                            'get_video_analysis_result',
                        )

                self.assertFalse(result.ok)
                self.assertIn('must be', result.content)

    def test_returns_backend_analysis_statuses_unchanged(self) -> None:
        expected_statuses = {
            'pending': (False, False),
            'running': (False, False),
            'done': (True, True),
            'failed': (True, False),
        }
        for backend_status, expected in expected_statuses.items():
            with self.subTest(backend_status=backend_status):
                _FakeClient.calls = []
                _FakeClient.backend_status = backend_status
                with tempfile.TemporaryDirectory() as tmp_dir:
                    with (
                        patch.dict(
                            os.environ,
                            {'INFERENCE_API_BASE_URL': 'video.test:8000'},
                        ),
                        patch('src.video_analysis.httpx.Client', _FakeClient),
                    ):
                        result = self._execute(
                            Path(tmp_dir),
                            {'task_id': 'task-123'},
                            'get_video_analysis_status',
                        )

                payload = json.loads(result.content)
                is_terminal, result_ready = expected
                self.assertTrue(result.ok)
                self.assertEqual(payload['task_id'], 'task-123')
                self.assertEqual(payload['status'], backend_status)
                self.assertNotIn('backend_status', payload)
                self.assertEqual(payload['is_terminal'], is_terminal)
                self.assertEqual(payload['result_ready'], result_ready)
                self.assertEqual(
                    _FakeClient.calls,
                    [('http://video.test:8000/status/task-123', {})],
                )

    def test_rejects_unknown_backend_analysis_status(self) -> None:
        _FakeClient.backend_status = 'unknown'
        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(
                    Path(tmp_dir),
                    {'task_id': 'task-123'},
                    'get_video_analysis_status',
                )

        self.assertFalse(result.ok)
        self.assertIn("unsupported status 'unknown'", result.content)

    def test_submits_local_file_without_processing_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            arguments = {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'local_file', 'path': '/video/input/test.mp4'},
                'idempotency_key': 'test-fire_inspection-260101-120000',
            }
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(workspace, arguments)

            payload = json.loads(result.content)

        self.assertTrue(result.ok)
        self.assertEqual(payload['task_id'], 'task-123')
        self.assertEqual(payload['status'], 'pending')
        self.assertFalse(payload['idempotency_replayed'])
        self.assertEqual(len(_FakeClient.calls), 1)
        url, kwargs = _FakeClient.calls[0]
        self.assertEqual(url, 'http://video.test:8000/predict')
        self.assertEqual(kwargs, {'params': {'filepath': '/video/input/test.mp4'}})

    def test_uploads_agent_server_file_as_multipart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            upload_path = workspace / 'test.mp4'
            upload_path.write_bytes(b'fake video')
            arguments = {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'upload_file', 'path': 'test.mp4'},
                'idempotency_key': 'test-fire_inspection-260101-120000',
            }
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(workspace, arguments)

            payload = json.loads(result.content)

        self.assertTrue(result.ok)
        self.assertEqual(payload['task_id'], 'task-123')
        self.assertEqual(payload['video_ref'], {'type': 'upload_file', 'path': 'test.mp4'})
        self.assertEqual(len(_FakeClient.calls), 1)
        url, kwargs = _FakeClient.calls[0]
        self.assertEqual(url, 'http://video.test:8000/predict')
        self.assertEqual(set(kwargs), {'files'})
        filename, video_file, content_type = kwargs['files']['file']
        self.assertEqual(filename, 'test.mp4')
        self.assertTrue(video_file.closed)
        self.assertEqual(content_type, 'video/mp4')

    def test_rejects_missing_agent_server_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = self._execute(
                Path(tmp_dir),
                {
                    'scenario': 'fire_inspection',
                    'video_ref': {'type': 'upload_file', 'path': 'missing.mp4'},
                    'idempotency_key': 'missing-fire_inspection-260101-120000',
                },
            )

        self.assertFalse(result.ok)
        self.assertIn('uploaded video file was not found', result.content)

    def test_reuses_task_for_same_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            arguments = {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'local_file', 'path': '/video/input/test.mp4'},
                'idempotency_key': 'test-fire_inspection-260101-120000',
            }
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'http://video.test:8000'}),
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
        self.assertEqual(first_payload['status'], 'pending')
        self.assertEqual(second_payload['status'], 'pending')
        self.assertEqual(second_payload['task_id'], first_payload['task_id'])
        self.assertEqual(len(_FakeClient.calls), 1)

    def test_passes_cos_file_path_without_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            arguments = {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'cos_file', 'path': 'cos://bucket/path/test.mp4'},
                'idempotency_key': 'test-fire_inspection-260101-120000',
            }
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'video.test:8000'}),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(workspace, arguments)

        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.content)['task_id'], 'task-123')
        self.assertEqual(len(_FakeClient.calls), 1)
        url, kwargs = _FakeClient.calls[0]
        self.assertEqual(url, 'http://video.test:8000/predict')
        self.assertEqual(
            kwargs,
            {'params': {'cos_filepath': 'cos://bucket/path/test.mp4'}},
        )

    def test_local_file_does_not_need_to_exist_on_agent_server(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_dir:
            video_system_path = str(Path(workspace_dir) / 'missing-on-agent-server.mp4')
            self.assertFalse(Path(video_system_path).exists())
            with (
                patch.dict(os.environ, {'INFERENCE_API_BASE_URL': 'http://video.test:8000'}),
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
        self.assertIn('INFERENCE_API_BASE_URL is required', result.content)


if __name__ == '__main__':
    unittest.main()
