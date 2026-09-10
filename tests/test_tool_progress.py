from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from src.agent_runtime import LocalCodingAgent
from src.agent_tools import AgentTool, default_tool_registry
from src.agent_types import AgentRuntimeConfig, AssistantTurn, ModelConfig, ToolCall
from src.main import _print_tool_progress
from src.tool_progress import TOOL_PROGRESS_MESSAGES, tool_progress_message


class ToolProgressTests(unittest.TestCase):
    def test_all_builtin_tools_have_labels(self) -> None:
        self.assertEqual(set(default_tool_registry()), set(TOOL_PROGRESS_MESSAGES))
        self.assertTrue(all(TOOL_PROGRESS_MESSAGES.values()))
        self.assertEqual(tool_progress_message('plugin_tool'), '正在调用扩展工具…')

    def test_cli_progress_is_flushed_to_stderr_not_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch('builtins.print', wraps=print) as printer:
            _print_tool_progress({'message': '正在查看目录…'})
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(err.getvalue(), '[进度] 正在查看目录…\n')
        self.assertTrue(printer.call_args.kwargs['flush'])

    def test_progress_precedes_execution_and_does_not_change_result(self) -> None:
        events = []

        def handler(arguments, context):
            self.assertEqual(events[-1]['tool_call_id'], arguments['call_id'])
            self.assertNotIn('arguments', events[-1])
            return 'tool output'

        with tempfile.TemporaryDirectory() as d:
            agent = LocalCodingAgent(
                model_config=ModelConfig(model='test'),
                runtime_config=AgentRuntimeConfig(cwd=Path(d)),
                tool_registry={'list_dir': AgentTool('list_dir', 'List files', {}, handler)},
                on_tool_start=events.append,
            )
            turns = [
                AssistantTurn('', tool_calls=tuple(
                    ToolCall(f'call_{i}', 'list_dir', {'call_id': f'call_{i}'})
                    for i in range(2)
                )),
                AssistantTurn('Final answer', finish_reason='stop'),
            ]
            with patch.object(agent.client, 'complete', side_effect=turns):
                result = agent.run('List files')
            self.assertEqual(result.final_output, 'Final answer')
            self.assertEqual(result.tool_calls, 2)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]['message'], '正在查看目录…')
            self.assertNotIn('正在查看目录', str(result.transcript))

    def test_broken_progress_sink_does_not_prevent_tool_execution(self) -> None:
        handler = Mock(return_value='tool output')
        with tempfile.TemporaryDirectory() as d:
            agent = LocalCodingAgent(
                model_config=ModelConfig(model='test'),
                runtime_config=AgentRuntimeConfig(cwd=Path(d)),
                tool_registry={'list_dir': AgentTool('list_dir', 'List files', {}, handler)},
                on_tool_start=Mock(side_effect=BrokenPipeError),
            )
            with patch.object(agent.client, 'complete', side_effect=[
                AssistantTurn('', tool_calls=(ToolCall('call_1', 'list_dir', {}),)),
                AssistantTurn('Finished', finish_reason='stop'),
            ]), self.assertLogs('src.agent_runtime', level='WARNING'):
                result = agent.run('List files')
            handler.assert_called_once()
            self.assertEqual(result.final_output, 'Finished')
