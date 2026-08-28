from __future__ import annotations

import json
import os
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import httpx

from src.agent_tools import build_tool_context, default_tool_registry, execute_tool
from src.agent_types import AgentRuntimeConfig
from src.video_analysis import (
    get_video_processing_result,
    get_video_processing_status,
    submit_video_processing,
)


_VIDEO_PROCESSING_TOOLS = {
    'submit_video_processing',
    'get_video_processing_status',
    'get_video_processing_result',
}


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request('POST', 'http://processing.test/process')
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f'HTTP {self.status_code}',
                request=request,
                response=response,
            )

    def json(self) -> object:
        if _FakeClient.json_error:
            raise ValueError('invalid JSON')
        return _FakeClient.response_payload


class _FakeClient:
    calls: list[tuple[str, dict[str, object]]] = []
    response_payload: object = {'task_id': 'processing-task-123'}
    response_status_code = 200
    json_error = False

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> '_FakeClient':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, kwargs))
        response = _FakeResponse()
        response.status_code = self.response_status_code
        return response


class VideoProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeClient.calls = []
        _FakeClient.response_payload = {'task_id': 'processing-task-123'}
        _FakeClient.response_status_code = 200
        _FakeClient.json_error = False

    def _execute(self, workspace: Path, arguments: dict[str, object]):
        registry = default_tool_registry()
        context = build_tool_context(
            AgentRuntimeConfig(cwd=workspace),
            tool_registry=registry,
        )
        return execute_tool(
            registry,
            'submit_video_processing',
            arguments,
            context,
        )

    def test_defines_video_processing_function_signatures(self) -> None:
        functions = {
            submit_video_processing: {
                'arguments',
                'workspace_root',
                'timeout_seconds',
                'mcp_runtime',
            },
            get_video_processing_status: {
                'arguments',
                'timeout_seconds',
                'mcp_runtime',
            },
            get_video_processing_result: {
                'arguments',
                'timeout_seconds',
                'mcp_runtime',
            },
        }

        for function, expected_parameters in functions.items():
            with self.subTest(function=function.__name__):
                self.assertEqual(set(signature(function).parameters), expected_parameters)

    def test_registers_all_video_processing_tools(self) -> None:
        registry = default_tool_registry()

        self.assertTrue(_VIDEO_PROCESSING_TOOLS.issubset(registry))

    def test_submit_schema_uses_raw_video_references(self) -> None:
        parameters = default_tool_registry()['submit_video_processing'].parameters

        self.assertEqual(
            parameters['required'],
            ['scenario', 'raw_video_refs', 'idempotency_key'],
        )
        raw_video_refs = parameters['properties']['raw_video_refs']
        self.assertEqual(raw_video_refs['type'], 'array')
        self.assertEqual(raw_video_refs['items'], {'type': 'string'})
        self.assertEqual(raw_video_refs['minItems'], 1)

    def test_status_and_result_schemas_require_only_task_id(self) -> None:
        registry = default_tool_registry()

        for name in {'get_video_processing_status', 'get_video_processing_result'}:
            with self.subTest(tool=name):
                parameters = registry[name].parameters
                self.assertEqual(parameters['required'], ['task_id'])
                self.assertEqual(set(parameters['properties']), {'task_id'})

    def test_uploads_harness_videos_as_repeated_multipart_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            first_video = workspace / 'first.mp4'
            second_video = workspace / 'second.mov'
            first_video.write_bytes(b'first video')
            second_video.write_bytes(b'second video')
            arguments = {
                'scenario': 'fire_inspection',
                'raw_video_refs': ['first.mp4', str(second_video)],
                'idempotency_key': 'processing-batch-001',
            }
            with (
                patch.dict(
                    os.environ,
                    {'VIDEO_PROCESSING_API': 'processing.test:8000'},
                ),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                result = self._execute(workspace, arguments)

            payload = json.loads(result.content)

        self.assertTrue(result.ok)
        self.assertEqual(payload['task_id'], 'processing-task-123')
        self.assertEqual(payload['status'], 'pending')
        self.assertEqual(payload['scenario'], 'fire_inspection')
        self.assertEqual(payload['raw_video_refs'], arguments['raw_video_refs'])
        self.assertEqual(payload['idempotency_key'], 'processing-batch-001')
        self.assertFalse(payload['idempotency_replayed'])
        self.assertEqual(len(_FakeClient.calls), 1)
        url, kwargs = _FakeClient.calls[0]
        self.assertEqual(url, 'http://processing.test:8000/process')
        self.assertEqual(set(kwargs), {'files'})
        multipart_files = kwargs['files']
        self.assertEqual([field for field, _ in multipart_files], ['file', 'file'])
        first_part = multipart_files[0][1]
        second_part = multipart_files[1][1]
        self.assertEqual(first_part[0], 'first.mp4')
        self.assertEqual(first_part[2], 'video/mp4')
        self.assertTrue(first_part[1].closed)
        self.assertEqual(second_part[0], 'second.mov')
        self.assertEqual(second_part[2], 'video/quicktime')
        self.assertTrue(second_part[1].closed)

    def test_replays_same_idempotent_submission_without_second_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / 'test.mp4').write_bytes(b'fake video')
            arguments = {
                'scenario': 'fire_inspection',
                'raw_video_refs': ['test.mp4'],
                'idempotency_key': 'processing-batch-001',
            }
            with (
                patch.dict(
                    os.environ,
                    {'VIDEO_PROCESSING_API': 'http://processing.test:8000'},
                ),
                patch('src.video_analysis.httpx.Client', _FakeClient),
            ):
                first = self._execute(workspace, arguments)
                second = self._execute(workspace, arguments)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(json.loads(first.content)['idempotency_replayed'])
        self.assertTrue(json.loads(second.content)['idempotency_replayed'])
        self.assertEqual(len(_FakeClient.calls), 1)

    def test_rejects_missing_or_invalid_raw_video_references(self) -> None:
        invalid_values = (None, [], [''], ['missing.mp4'])
        for raw_video_refs in invalid_values:
            with self.subTest(raw_video_refs=raw_video_refs):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    result = self._execute(
                        Path(tmp_dir),
                        {
                            'scenario': 'fire_inspection',
                            'raw_video_refs': raw_video_refs,
                            'idempotency_key': 'processing-batch-001',
                        },
                    )

                self.assertFalse(result.ok)

    def test_reports_missing_processing_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / 'test.mp4').write_bytes(b'fake video')
            with patch.dict(os.environ, {}, clear=True):
                result = self._execute(
                    workspace,
                    {
                        'scenario': 'fire_inspection',
                        'raw_video_refs': ['test.mp4'],
                        'idempotency_key': 'processing-batch-001',
                    },
                )

        self.assertFalse(result.ok)
        self.assertIn('VIDEO_PROCESSING_API is required', result.content)

    def test_rejects_invalid_submit_response(self) -> None:
        invalid_payloads = ([], {}, {'task_id': ''})
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                _FakeClient.calls = []
                _FakeClient.response_payload = payload
                with tempfile.TemporaryDirectory() as tmp_dir:
                    workspace = Path(tmp_dir)
                    (workspace / 'test.mp4').write_bytes(b'fake video')
                    with (
                        patch.dict(
                            os.environ,
                            {'VIDEO_PROCESSING_API': 'processing.test:8000'},
                        ),
                        patch('src.video_analysis.httpx.Client', _FakeClient),
                    ):
                        result = self._execute(
                            workspace,
                            {
                                'scenario': 'fire_inspection',
                                'raw_video_refs': ['test.mp4'],
                                'idempotency_key': 'processing-batch-001',
                            },
                        )

                self.assertFalse(result.ok)

    def test_unimplemented_status_and_result_handlers_return_controlled_errors(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_tool_context(
                AgentRuntimeConfig(cwd=Path(tmp_dir)),
                tool_registry=registry,
            )
            for name in {
                'get_video_processing_status',
                'get_video_processing_result',
            }:
                with self.subTest(tool=name):
                    result = execute_tool(registry, name, {}, context)
                    self.assertFalse(result.ok)
                    self.assertEqual(
                        result.content,
                        f'{name} is registered but not implemented',
                    )
                    self.assertEqual(
                        result.metadata,
                        {'error_kind': 'tool_execution_error'},
                    )


if __name__ == '__main__':
    unittest.main()
