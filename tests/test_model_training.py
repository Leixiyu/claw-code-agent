from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from inspect import signature
from pathlib import Path
from unittest.mock import patch

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


class _FakeResponse:
    def json(self) -> dict[str, str]:
        return {'status': _FakeClient.backend_status}

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    calls: list[tuple[str, dict[str, object]]] = []
    backend_status = 'running'

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> '_FakeClient':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> _FakeResponse:
        self.calls.append((url, {}))
        return _FakeResponse()


class ModelTrainingTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeClient.calls = []
        _FakeClient.backend_status = 'running'

    def test_defines_model_training_function_signatures(self) -> None:
        expected_parameters = {
            submit_model_training: {'arguments', 'timeout_seconds'},
            get_model_training_status: {
                'arguments',
                'workspace_root',
                'timeout_seconds',
            },
            get_model_training_result: {'arguments', 'timeout_seconds'},
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

    def test_unimplemented_submit_and_result_return_controlled_errors(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_tool_context(
                AgentRuntimeConfig(cwd=Path(tmp_dir)),
                tool_registry=registry,
            )
            for name in {'submit_model_training', 'get_model_training_result'}:
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


if __name__ == '__main__':
    unittest.main()
