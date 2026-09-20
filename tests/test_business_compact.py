"""Business compaction contracts; fake models/backends only."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.agent_runtime import LocalCodingAgent
from src.agent_session import AgentMessage, AgentSessionState
from src.agent_types import AgentRuntimeConfig, AssistantTurn, ModelConfig
from src.business_context import (
    build_reference_checkpoint, split_compaction_messages,
)
from src.compact import compact_conversation
from src.session_store import read_agent_session


def tool_message(payload, name='get_video_processing_result', call_id='call1'):
    return AgentMessage(role='tool', name=name, tool_call_id=call_id,
                        content=json.dumps(payload, ensure_ascii=False))


class BusinessCompactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.agent = LocalCodingAgent(
            model_config=ModelConfig(model='test-model'),
            runtime_config=AgentRuntimeConfig(cwd=self.root,
                session_directory=self.root / 'sessions', scratchpad_root=self.root / 'scratchpad'),
            authenticated_user_id='alice',
            override_system_prompt='User Alice only. Workspace: ' + str(self.root),
        )
        self.agent.client = MagicMock()
        self.agent.client.complete.return_value = AssistantTurn(
            '<summary>Goal: analyze videos. User selected d-selected, forbids training. '
            'Training has NOT been approved. Next: query task-running; do not resubmit.</summary>',
            finish_reason='stop')
        self.instructions = AgentMessage('system', 'SYSTEM: workspace=/alice; no cross-user access')
        self.constraint = 'Use dataset d-selected, not the other dataset. Do not train. ' + 'user details ' * 90
        self.large = tool_message({
            'manifest': {'files': ['LARGE_BODY' * 2000], 'dataset_id': 'd-selected', 'status': 'ready'},
            'task_id': 'task-done', 'status': 'done', 'dataset_id': 'd-selected',
        })
        self.messages = [
            self.instructions, AgentMessage('user', self.constraint),
            AgentMessage('assistant', 'Selected d-selected. No training approval. ' * 30),
            AgentMessage('assistant', '', tool_calls=({'id': 'call1', 'type': 'function',
                'function': {'name': 'get_video_processing_result', 'arguments': '{"task_id":"task-done"}'}},)),
            self.large,
            tool_message({'task_id': 'task-running', 'status': 'running'}, 'get_model_training_status', 'call2'),
            AgentMessage('user', 'Continue only the requested analysis.'),
            AgentMessage('assistant', 'I will check progress, not submit again.'),
            AgentMessage('user', 'Keep the earlier restrictions.'),
            AgentMessage('assistant', 'Understood.'),
        ]
        self.agent.last_session = AgentSessionState(system_prompt_parts=(self.instructions.content,),
                                                    messages=list(self.messages))

    def test_rule_snip_keeps_decisions_and_structured_references(self):
        session = self.agent.last_session
        self.assertTrue(self.agent._snip_session_pass(session, [], turn_index=1,
                         target_tokens=0, current_total=50000, reactive=False))
        self.assertEqual(session.messages[0], self.instructions)
        self.assertEqual(session.messages[1].content, self.constraint)
        self.assertEqual(session.messages[2], self.messages[2])
        shortened = session.messages[4]
        self.assertNotIn('LARGE_BODY', shortened.content)
        self.assertIn('d-selected', shortened.content)
        self.assertIn('task-done', shortened.content)
        self.assertTrue(shortened.metadata['business_references'])
        self.assertFalse(self.agent._compact_session_pass(session, [], turn_index=1,
                         usage_total=10000, reactive=True))

    def test_manual_compact_preserves_system_and_recovery_without_big_bodies(self):
        with patch('src.session_memory_compact.try_session_memory_compaction') as shared:
            result = compact_conversation(self.agent)
        self.assertIsNone(result.error)
        shared.assert_not_called()
        session = self.agent.last_session
        self.assertEqual(session.messages[0], self.instructions)
        text = '\n'.join(message.content for message in session.messages)
        for value in ('d-selected', 'task-running', 'task-done', 'NOT been approved', 'last-known'):
            self.assertIn(value, text)
        self.assertNotIn('LARGE_BODY', text)
        sent = self.agent.client.complete.call_args.args[0]
        self.assertIn(self.constraint, str(sent))
        self.assertNotIn('LARGE_BODY', str(sent))
        self.assertIn('authorization', sent[-1]['content'])
        self.assertIn('Never Submit', sent[-1]['content'])
        self.assertEqual(self.agent.client.complete.call_args.kwargs['tools'], [])

    def test_recent_large_results_are_condensed_without_orphan_tool_calls(self):
        self.agent.runtime_config = replace(self.agent.runtime_config, compact_preserve_messages=1)
        self.agent.last_session.messages = self.messages[:5]
        result = compact_conversation(self.agent)
        self.assertIsNone(result.error)
        messages = self.agent.last_session.messages
        self.assertEqual(messages[-2].tool_calls[0]['id'], 'call1')
        self.assertEqual(messages[-1].tool_call_id, 'call1')
        self.assertNotIn('LARGE_BODY', messages[-1].content)

    def test_business_ptl_and_incomplete_summary_do_not_destroy_history(self):
        for mode in ('too_long', 'incomplete'):
            with self.subTest(mode=mode):
                before = list(self.agent.last_session.messages)
                if mode == 'too_long':
                    self.agent.client.complete.side_effect = RuntimeError('prompt is too long')
                else:
                    self.agent.client.complete.side_effect = None
                    self.agent.client.complete.return_value = AssistantTurn('partial', finish_reason='length')
                result = compact_conversation(self.agent)
                self.assertIsNotNone(result.error)
                self.assertEqual(self.agent.last_session.messages, before)

    def test_explicit_threshold_uses_semantic_business_summary(self):
        self.agent.runtime_config = replace(self.agent.runtime_config, auto_compact_threshold_tokens=500)
        events = []
        result = self.agent._preflight_prompt_length(self.agent.last_session, events, turn_index=1)
        self.assertEqual(result.model_calls_increment, 1)
        self.assertTrue(any(event['type'] == 'auto_compact_summary' for event in events))
        self.assertFalse(any(event['type'] == 'compact_boundary' for event in events))
        self.assertEqual(self.agent.last_session.messages[0], self.instructions)

    def test_reference_selection_and_repeated_compaction(self):
        messages = [tool_message({'task_id': 'pending', 'status': 'running'})]
        messages += [tool_message({'task_id': f'task-{i}', 'status': 'done', 'dataset_id': f'ds-{i}'})
                     for i in range(30)]
        messages.append(AgentMessage('user', 'Use ds-0, not another dataset.'))
        checkpoint = build_reference_checkpoint(messages)[0]
        records = checkpoint.metadata['business_references']
        self.assertEqual(len(records), 10)  # unfinished + explicitly selected + recent eight
        self.assertIn('pending', checkpoint.content)
        self.assertIn('"ds-0"', checkpoint.content)
        self.assertNotIn('"ds-1"', checkpoint.content)
        next_checkpoint = build_reference_checkpoint([checkpoint,
            tool_message({'task_id': 'pending', 'status': 'done'})])[0]
        pending = [record for record in next_checkpoint.metadata['business_references'] if record['task_id'] == 'pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'done')

    def test_all_original_system_messages_survive_generic_and_business_compact(self):
        extra = AgentMessage('system', 'Do not access /bob.')
        for authenticated in (None, 'alice'):
            with self.subTest(authenticated=authenticated):
                self.agent.authenticated_user_id = authenticated
                # No business tools, so None exercises the generic path.
                self.agent.last_session.messages = [self.instructions, extra] + [
                    AgentMessage('user' if i % 2 else 'assistant', f'conversation {i}') for i in range(10)]
                result = compact_conversation(self.agent, 'Keep the goal')
                self.assertIsNone(result.error)
                self.assertEqual(self.agent.last_session.messages[:2], [self.instructions, extra])

    def test_shared_memory_branch_preserves_original_instructions(self):
        from src.compact import CompactionResult
        self.agent.authenticated_user_id = None
        self.agent.last_session.messages = [self.instructions] + [AgentMessage('user', str(i)) for i in range(6)]
        memory = CompactionResult(boundary_message=AgentMessage('user', 'memory boundary'),
            summary_messages=[AgentMessage('user', 'memory summary')],
            messages_to_keep=[self.instructions, AgentMessage('user', 'tail')])
        with patch('src.session_memory_compact.try_session_memory_compaction', return_value=memory):
            result = compact_conversation(self.agent)
        self.assertIsNone(result.error)
        self.assertEqual(self.agent.last_session.messages.count(self.instructions), 1)
        self.assertEqual(self.agent.last_session.messages[0], self.instructions)

    def test_manual_compact_persists_and_resumes_without_submit(self):
        # First save a real user-scoped session, then resume /compact as the CLI does.
        self.agent.client.complete.return_value = AssistantTurn('Ready', finish_reason='stop')
        seeded = self.agent.run('Do not train. Only explain analysis.')
        stored = read_agent_session(seeded.session_id, self.root / 'sessions')
        stored = replace(stored, messages=tuple(message.to_transcript_entry() for message in self.messages))
        self.agent.client.complete.return_value = AssistantTurn('<summary>Do not train. Next: Status.</summary>', finish_reason='stop')
        result = self.agent.resume('/compact', stored)
        self.assertIn('Conversation compacted', result.final_output)
        saved = read_agent_session(seeded.session_id, self.root / 'sessions')
        self.assertTrue(any(message.get('metadata', {}).get('kind') == 'compact_summary' for message in saved.messages))
        self.assertIn('task-running', str(saved.messages))
        self.assertNotIn('LARGE_BODY', str(saved.messages))
        self.agent.client.complete.return_value = AssistantTurn('Need Status, not a new submission.', finish_reason='stop')
        with patch('src.agent_runtime.execute_tool_streaming') as execute:
            self.agent.resume('Explain the next step', saved)
        execute.assert_not_called()
        sent = self.agent.client.complete.call_args.args[0]
        self.assertIn('Do not train', str(sent))
        self.assertIn('task-running', str(sent))
        self.assertTrue(any(message['role'] == 'system' for message in sent))


if __name__ == '__main__':
    unittest.main()
