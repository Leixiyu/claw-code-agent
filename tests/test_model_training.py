from __future__ import annotations

import tempfile
import unittest
from inspect import signature
from pathlib import Path

from src.agent_tools import build_tool_context, default_tool_registry, execute_tool
from src.agent_types import AgentRuntimeConfig
from src.model_training import (
    get_model_training_result,
    get_model_training_status,
    submit_model_training,
)


_MODEL_TRAINING_TOOLS = {
    'submit_model_training',
    'get_model_training_status',
    'get_model_training_result',
}


class ModelTrainingTests(unittest.TestCase):
    def test_defines_model_training_function_signatures(self) -> None:
        functions = (
            submit_model_training,
            get_model_training_status,
            get_model_training_result,
        )

        for function in functions:
            with self.subTest(function=function.__name__):
                self.assertEqual(
                    set(signature(function).parameters),
                    {'arguments', 'timeout_seconds', 'mcp_runtime'},
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

    def test_unimplemented_handlers_return_controlled_errors(self) -> None:
        registry = default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_tool_context(
                AgentRuntimeConfig(cwd=Path(tmp_dir)),
                tool_registry=registry,
            )
            for name in _MODEL_TRAINING_TOOLS:
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
