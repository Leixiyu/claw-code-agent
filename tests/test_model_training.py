from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import httpx

from src.agent_tools import build_tool_context, default_tool_registry, execute_tool
from src.agent_types import AgentRuntimeConfig
from src.business_functions import (
    get_model_training_result,
    get_model_training_status,
    submit_model_training,
)


_MODEL_TRAINING_TOOLS = {
    'submit_model_training',
    'get_model_training_status',
    'get_model_training_result',
}


def _write_dataset_manifest(
    workspace: Path,
    *,
    dataset_id: str = 'fire-inspect-01',
    scenario: str = 'fire_inspection',
    status: str = 'ready',
) -> Path:
    dataset_directory = workspace / 'datasets'
    dataset_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = dataset_directory / f'{dataset_id}.json'
    manifest_path.write_text(
        json.dumps(
            {
                'dataset_id': dataset_id,
                'scenario': scenario,
                'version': 1,
                'status': status,
                'video_count': 1,
            }
        ),
        encoding='utf-8',
    )
    return manifest_path


class _FakeResponse:
    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        url: str = 'http://training.test',
        method: str = 'GET',
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.request = httpx.Request(method, url)

    def json(self) -> object:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(
                self.status_code,
                request=self.request,
            )
            raise httpx.HTTPStatusError(
                'fake HTTP error',
                request=self.request,
                response=response,
            )


class _FakeClient:
    calls: list[tuple[str, dict[str, object]]] = []
    backend_status = 'running'
    submit_payload: object = {'task_id': 'training-task-123'}
    submit_status_code = 200
    result_payload: object = {}
    result_status_code = 200

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> '_FakeClient':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> _FakeResponse:
        self.calls.append((url, {}))
        if '/status/' in url:
            return _FakeResponse({'status': self.backend_status}, url=url)
        return _FakeResponse(
            self.result_payload,
            self.result_status_code,
            url,
        )

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse(
            self.submit_payload,
            self.submit_status_code,
            url,
            'POST',
        )


class ModelTrainingTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeClient.calls = []
        _FakeClient.backend_status = 'running'
        _FakeClient.submit_payload = {'task_id': 'training-task-123'}
        _FakeClient.submit_status_code = 200
        _FakeClient.result_payload = {}
        _FakeClient.result_status_code = 200

    def test_defines_model_training_function_signatures(self) -> None:
        expected_parameters = {
            submit_model_training: {
                'arguments',
                'workspace_root',
                'timeout_seconds',
            },
            get_model_training_status: {
                'arguments',
                'workspace_root',
                'timeout_seconds',
            },
            get_model_training_result: {
                'arguments',
                'workspace_root',
                'timeout_seconds',
            },
        }

        for function, expected in expected_parameters.items():
            with self.subTest(function=function.__name__):
                self.assertEqual(
                    set(signature(function).parameters),
                    expected,
                )

    def test_registers_all_model_training_tools(self) -> None:
        registry = default_tool_registry()

        self.assertTrue(_MODEL_TRAINING_TOOLS.issubset(registry))

    def test_submit_schema_uses_dataset_reference(self) -> None:
        parameters = default_tool_registry()['submit_model_training'].parameters

        self.assertEqual(
            parameters['required'],
            ['scenario', 'dataset_ref', 'idempotency_key'],
        )
        self.assertNotIn('path', parameters['properties'])

    def test_status_and_result_schemas_require_only_task_id(self) -> None:
        registry = default_tool_registry()

        for name in {'get_model_training_status', 'get_model_training_result'}:
            with self.subTest(tool=name):
                parameters = registry[name].parameters
                self.assertEqual(parameters['required'], ['task_id'])
                self.assertEqual(set(parameters['properties']), {'task_id'})

    def test_submit_sends_only_logical_json_and_persists_training_task(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            _write_dataset_manifest(workspace)
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            arguments = {
                'scenario': 'fire_inspection',
                'dataset_ref': 'fire-inspect-01',
                'idempotency_key': 'training-fire-inspect-01-001',
            }
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'submit_model_training',
                    arguments,
                    context,
                )

            task_record = json.loads(
                (
                    workspace
                    / 'tasks'
                    / 'training'
                    / 'training-task-123.json'
                ).read_text(encoding='utf-8')
            )

        self.assertTrue(result.ok)
        self.assertEqual(
            json.loads(result.content),
            {
                'task_id': 'training-task-123',
                'status': 'pending',
                'scenario': 'fire_inspection',
                'dataset_ref': 'fire-inspect-01',
                'idempotency_key': 'training-fire-inspect-01-001',
                'idempotency_replayed': False,
            },
        )
        self.assertEqual(
            _FakeClient.calls,
            [
                (
                    'http://training.test:8000/train',
                    {
                        'json': {
                            'scenario': 'fire_inspection',
                            'dataset_ref': 'fire-inspect-01',
                            'idempotency_key': 'training-fire-inspect-01-001',
                        }
                    },
                )
            ],
        )
        self.assertEqual(task_record['module'], 'training')
        self.assertEqual(task_record['status'], 'pending')
        self.assertEqual(task_record['scenario'], 'fire_inspection')
        self.assertEqual(task_record['request'], {'dataset_ref': 'fire-inspect-01'})
        self.assertEqual(
            task_record['idempotency_key'],
            'training-fire-inspect-01-001',
        )

    def test_submit_replays_same_idempotency_key_without_second_request(self) -> None:
        registry = default_tool_registry()
        arguments = {
            'scenario': 'fire_inspection',
            'dataset_ref': 'fire-inspect-01',
            'idempotency_key': 'training-fire-inspect-01-001',
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            _write_dataset_manifest(workspace)
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                first = execute_tool(
                    registry,
                    'submit_model_training',
                    arguments,
                    context,
                )
                second = execute_tool(
                    registry,
                    'submit_model_training',
                    arguments,
                    context,
                )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertFalse(json.loads(first.content)['idempotency_replayed'])
        self.assertTrue(json.loads(second.content)['idempotency_replayed'])
        self.assertEqual(len(_FakeClient.calls), 1)

    def test_submit_rejects_reused_key_for_different_dataset(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            _write_dataset_manifest(workspace)
            _write_dataset_manifest(workspace, dataset_id='fire-inspect-02')
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                first = execute_tool(
                    registry,
                    'submit_model_training',
                    {
                        'scenario': 'fire_inspection',
                        'dataset_ref': 'fire-inspect-01',
                        'idempotency_key': 'training-request-001',
                    },
                    context,
                )
                second = execute_tool(
                    registry,
                    'submit_model_training',
                    {
                        'scenario': 'fire_inspection',
                        'dataset_ref': 'fire-inspect-02',
                        'idempotency_key': 'training-request-001',
                    },
                    context,
                )

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertIn('different model-training request', second.content)
        self.assertEqual(len(_FakeClient.calls), 1)

    def test_submit_validates_arguments_and_api_configuration(self) -> None:
        registry = default_tool_registry()
        invalid_arguments = (
            {},
            {
                'scenario': 'unknown',
                'dataset_ref': 'fire-inspect-01',
                'idempotency_key': 'training-request-001',
            },
            {
                'scenario': 'fire_inspection',
                'dataset_ref': '',
                'idempotency_key': 'training-request-001',
            },
            {
                'scenario': 'fire_inspection',
                'dataset_ref': 'fire-inspect-01',
                'idempotency_key': '',
            },
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    context = build_tool_context(
                        AgentRuntimeConfig(cwd=Path(tmp_dir)),
                        tool_registry=registry,
                    )
                    result = execute_tool(
                        registry,
                        'submit_model_training',
                        arguments,
                        context,
                    )
                self.assertFalse(result.ok)

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            _write_dataset_manifest(workspace)
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with patch.dict(os.environ, {}, clear=True):
                result = execute_tool(
                    registry,
                    'submit_model_training',
                    {
                        'scenario': 'fire_inspection',
                        'dataset_ref': 'fire-inspect-01',
                        'idempotency_key': 'training-request-001',
                    },
                    context,
                )

        self.assertFalse(result.ok)
        self.assertIn('MODEL_TRAINING_API is required', result.content)

    def test_submit_reports_backend_http_errors(self) -> None:
        _FakeClient.submit_status_code = 500
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            _write_dataset_manifest(workspace)
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'submit_model_training',
                    {
                        'scenario': 'fire_inspection',
                        'dataset_ref': 'fire-inspect-01',
                        'idempotency_key': 'training-request-001',
                    },
                    context,
                )

        self.assertFalse(result.ok)
        self.assertIn('model training API returned HTTP 500', result.content)

    def test_submit_rejects_missing_dataset_manifest_without_http_request(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'submit_model_training',
                    {
                        'scenario': 'fire_inspection',
                        'dataset_ref': 'missing-dataset',
                        'idempotency_key': 'training-request-001',
                    },
                    context,
                )

            training_tasks_created = (workspace / 'tasks' / 'training').exists()

        self.assertFalse(result.ok)
        self.assertIn('dataset manifest was not found', result.content)
        self.assertEqual(_FakeClient.calls, [])
        self.assertFalse(training_tasks_created)

    def test_submit_rejects_dataset_manifest_that_is_not_ready(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            _write_dataset_manifest(workspace, status='processing')
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'submit_model_training',
                    {
                        'scenario': 'fire_inspection',
                        'dataset_ref': 'fire-inspect-01',
                        'idempotency_key': 'training-request-001',
                    },
                    context,
                )

        self.assertFalse(result.ok)
        self.assertIn("is not ready (manifest status: 'processing')", result.content)
        self.assertEqual(_FakeClient.calls, [])

    def test_submit_rejects_manifest_identity_or_scenario_mismatch(self) -> None:
        cases = (
            (
                {
                    'dataset_id': 'another-dataset',
                    'scenario': 'fire_inspection',
                    'status': 'ready',
                },
                'dataset_id does not match',
            ),
            (
                {
                    'dataset_id': 'fire-inspect-01',
                    'scenario': 'another_scenario',
                    'status': 'ready',
                },
                'scenario does not match',
            ),
        )
        registry = default_tool_registry()
        for manifest, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                _FakeClient.calls = []
                with tempfile.TemporaryDirectory() as tmp_dir:
                    workspace = Path(tmp_dir)
                    manifest_path = _write_dataset_manifest(workspace)
                    manifest_path.write_text(
                        json.dumps(manifest),
                        encoding='utf-8',
                    )
                    context = build_tool_context(
                        AgentRuntimeConfig(cwd=workspace),
                        tool_registry=registry,
                    )
                    with (
                        patch.dict(
                            os.environ,
                            {'MODEL_TRAINING_API': 'training.test:8000'},
                        ),
                        patch('src.business_functions.httpx.Client', _FakeClient),
                    ):
                        result = execute_tool(
                            registry,
                            'submit_model_training',
                            {
                                'scenario': 'fire_inspection',
                                'dataset_ref': 'fire-inspect-01',
                                'idempotency_key': 'training-request-001',
                            },
                            context,
                        )

                self.assertFalse(result.ok)
                self.assertIn(expected_error, result.content)
                self.assertEqual(_FakeClient.calls, [])

    def test_status_returns_backend_states_and_persists_training_task(self) -> None:
        expected_statuses = {
            'pending': (False, False),
            'running': (False, False),
            'done': (True, True),
            'failed': (True, False),
        }
        registry = default_tool_registry()
        for backend_status, expected in expected_statuses.items():
            with self.subTest(backend_status=backend_status):
                _FakeClient.calls = []
                _FakeClient.backend_status = backend_status
                with tempfile.TemporaryDirectory() as tmp_dir:
                    workspace = Path(tmp_dir)
                    context = build_tool_context(
                        AgentRuntimeConfig(cwd=workspace),
                        tool_registry=registry,
                    )
                    with (
                        patch.dict(
                            os.environ,
                            {'MODEL_TRAINING_API': 'training.test:8000'},
                        ),
                        patch('src.business_functions.httpx.Client', _FakeClient),
                    ):
                        result = execute_tool(
                            registry,
                            'get_model_training_status',
                            {'task_id': 'training-task-123'},
                            context,
                        )

                    task_record = json.loads(
                        (
                            workspace
                            / 'tasks'
                            / 'training'
                            / 'training-task-123.json'
                        ).read_text(encoding='utf-8')
                    )

                is_terminal, result_ready = expected
                payload = json.loads(result.content)
                self.assertTrue(result.ok)
                self.assertEqual(
                    payload,
                    {
                        'task_id': 'training-task-123',
                        'status': backend_status,
                        'is_terminal': is_terminal,
                        'result_ready': result_ready,
                    },
                )
                self.assertEqual(
                    result.metadata['action'],
                    'get_model_training_status',
                )
                self.assertEqual(task_record['module'], 'training')
                self.assertEqual(task_record['status'], backend_status)
                self.assertEqual(task_record['is_terminal'], is_terminal)
                self.assertEqual(task_record['result_ready'], result_ready)
                self.assertIsNone(
                    datetime.fromisoformat(task_record['created_at']).tzinfo
                )
                self.assertIsNone(
                    datetime.fromisoformat(task_record['updated_at']).tzinfo
                )
                self.assertEqual(
                    _FakeClient.calls,
                    [
                        (
                            'http://training.test:8000/status/training-task-123',
                            {},
                        )
                    ],
                )

    def test_status_rejects_unknown_backend_state(self) -> None:
        _FakeClient.backend_status = 'unknown'
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_tool_context(
                AgentRuntimeConfig(cwd=Path(tmp_dir)),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'get_model_training_status',
                    {'task_id': 'training-task-123'},
                    context,
                )

        self.assertFalse(result.ok)
        self.assertIn("unsupported status 'unknown'", result.content)

    def test_status_requires_model_training_api(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_tool_context(
                AgentRuntimeConfig(cwd=Path(tmp_dir)),
                tool_registry=registry,
            )
            with patch.dict(os.environ, {}, clear=True):
                result = execute_tool(
                    registry,
                    'get_model_training_status',
                    {'task_id': 'training-task-123'},
                    context,
                )

        self.assertFalse(result.ok)
        self.assertIn('MODEL_TRAINING_API is required', result.content)

    def test_result_persists_model_metadata_and_training_task(self) -> None:
        model_metadata = {
            'model_id': 'fire-inspect-model-v1',
            'scenario': 'fire_inspection',
            'dataset_id': 'fire-inspect-01',
            'version': 1,
            'status': 'ready',
            'created_at': '2026-08-10T12:00:00Z',
        }
        _FakeClient.result_payload = {
            'task_id': 'training-task-123',
            'status': 'done',
            'model_id': 'fire-inspect-model-v1',
            'metadata_path': 'models/fire-inspect-model-v1.json',
            'metadata': model_metadata,
        }
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'get_model_training_result',
                    {'task_id': 'training-task-123'},
                    context,
                )

            metadata_path = workspace / 'models' / 'fire-inspect-model-v1.json'
            persisted_metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            task_record = json.loads(
                (
                    workspace
                    / 'tasks'
                    / 'training'
                    / 'training-task-123.json'
                ).read_text(encoding='utf-8')
            )

        self.assertTrue(result.ok)
        self.assertEqual(
            json.loads(result.content),
            {
                'task_id': 'training-task-123',
                'status': 'done',
                'model_id': 'fire-inspect-model-v1',
                'metadata_path': 'models/fire-inspect-model-v1.json',
            },
        )
        self.assertEqual(persisted_metadata, model_metadata)
        self.assertEqual(task_record['module'], 'training')
        self.assertEqual(task_record['status'], 'done')
        self.assertTrue(task_record['is_terminal'])
        self.assertTrue(task_record['result_ready'])
        self.assertEqual(
            task_record['result'],
            {
                'model_id': 'fire-inspect-model-v1',
                'metadata_path': 'models/fire-inspect-model-v1.json',
            },
        )
        self.assertEqual(
            result.metadata,
            {
                'action': 'get_model_training_result',
                'task_id': 'training-task-123',
                'status': 'done',
                'model_id': 'fire-inspect-model-v1',
                'metadata_path': 'models/fire-inspect-model-v1.json',
            },
        )
        self.assertEqual(
            _FakeClient.calls,
            [('http://training.test:8000/result/training-task-123', {})],
        )

    def test_result_rejects_invalid_backend_metadata(self) -> None:
        invalid_payloads = (
            [],
            {},
            {'metadata': []},
            {'metadata': {}},
            {'metadata': {'model_id': ''}},
        )
        registry = default_tool_registry()
        for backend_payload in invalid_payloads:
            with self.subTest(backend_payload=backend_payload):
                _FakeClient.result_payload = backend_payload
                with tempfile.TemporaryDirectory() as tmp_dir:
                    context = build_tool_context(
                        AgentRuntimeConfig(cwd=Path(tmp_dir)),
                        tool_registry=registry,
                    )
                    with (
                        patch.dict(
                            os.environ,
                            {'MODEL_TRAINING_API': 'training.test:8000'},
                        ),
                        patch('src.business_functions.httpx.Client', _FakeClient),
                    ):
                        result = execute_tool(
                            registry,
                            'get_model_training_result',
                            {'task_id': 'training-task-123'},
                            context,
                        )

                self.assertFalse(result.ok)

    def test_result_rejects_unsafe_model_id(self) -> None:
        _FakeClient.result_payload = {
            'metadata': {'model_id': '../outside'},
        }
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            context = build_tool_context(
                AgentRuntimeConfig(cwd=workspace),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'get_model_training_result',
                    {'task_id': 'training-task-123'},
                    context,
                )

            self.assertFalse((workspace.parent / 'outside.json').exists())

        self.assertFalse(result.ok)
        self.assertIn('unsafe for metadata storage', result.content)

    def test_result_reports_not_ready(self) -> None:
        _FakeClient.result_status_code = 202
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_tool_context(
                AgentRuntimeConfig(cwd=Path(tmp_dir)),
                tool_registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {'MODEL_TRAINING_API': 'training.test:8000'},
                ),
                patch('src.business_functions.httpx.Client', _FakeClient),
            ):
                result = execute_tool(
                    registry,
                    'get_model_training_result',
                    {'task_id': 'training-task-123'},
                    context,
                )

        self.assertFalse(result.ok)
        self.assertIn('result is not ready (HTTP 202)', result.content)

    def test_result_reports_backend_http_errors(self) -> None:
        registry = default_tool_registry()
        for status_code in (404, 500):
            with self.subTest(status_code=status_code):
                _FakeClient.result_status_code = status_code
                with tempfile.TemporaryDirectory() as tmp_dir:
                    context = build_tool_context(
                        AgentRuntimeConfig(cwd=Path(tmp_dir)),
                        tool_registry=registry,
                    )
                    with (
                        patch.dict(
                            os.environ,
                            {'MODEL_TRAINING_API': 'training.test:8000'},
                        ),
                        patch('src.business_functions.httpx.Client', _FakeClient),
                    ):
                        result = execute_tool(
                            registry,
                            'get_model_training_result',
                            {'task_id': 'training-task-123'},
                            context,
                        )

                self.assertFalse(result.ok)
                self.assertIn(f'returned HTTP {status_code}', result.content)

    def test_result_requires_model_training_api(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_tool_context(
                AgentRuntimeConfig(cwd=Path(tmp_dir)),
                tool_registry=registry,
            )
            with patch.dict(os.environ, {}, clear=True):
                result = execute_tool(
                    registry,
                    'get_model_training_result',
                    {'task_id': 'training-task-123'},
                    context,
                )

        self.assertFalse(result.ok)
        self.assertIn('MODEL_TRAINING_API is required', result.content)


if __name__ == '__main__':
    unittest.main()
