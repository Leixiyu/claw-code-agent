"""Lifetime token accounting contracts: no real model/network calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.agent_runtime import LocalCodingAgent
from src.agent_types import AgentRuntimeConfig, AssistantTurn, ModelConfig, StreamEvent, ToolCall, UsageStats
from src.auth_runtime import AuthStore
from src.compact import CompactionResult, _call_compact_model
from src.gui.server import AgentState, create_app
from src.main import main
from src.openai_compat import OpenAICompatClient, _parse_usage, _usage_reported
from src.session_lifecycle import delete_saved_session
from src.session_store import read_agent_session
from src.usage_ledger import MeteredClient, UsageLedger, UsageLedgerError


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ledger = UsageLedger(self.root / 'private')

    def client(self, backend, user='alice'):
        return MeteredClient(backend, self.ledger, user, 'session', 'run', 'model', 'chat')

    def test_idempotent_snapshots_restart_and_concurrency(self):
        def call(index):
            key = self.ledger.begin('alice', 's', str(index), 'm', 'chat')
            usage = UsageStats(input_tokens=10, output_tokens=3, cache_read_input_tokens=2, reasoning_tokens=1)
            self.ledger.record(key, usage)
            self.ledger.record(key, usage)
            self.ledger.finish(key, reported=True)
            self.ledger.record(key, UsageStats(input_tokens=999))
            self.ledger.finish(key, reported=False)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(call, range(20)))
        result = UsageLedger(self.root / 'private').summary('alice')
        self.assertEqual(result['total_tokens'], 300)
        self.assertEqual(result['reasoning_tokens'], 20)
        self.assertEqual(result['request_count'], 20)
        self.assertEqual(result['pending_requests'], 0)
        self.assertEqual(self.ledger.summary('bob')['total_tokens'], 0)

    def test_missing_usage_zero_and_failed_attempt_are_distinct(self):
        backend = Mock()
        backend.complete.side_effect = [
            AssistantTurn('unknown', usage_reported=False),
            AssistantTurn('zero', usage_reported=True),
            RuntimeError('response lost'),
            AssistantTurn('retry', usage=UsageStats(input_tokens=8, output_tokens=2)),
        ]
        client = self.client(backend)
        client.complete([], [])
        client.complete([], [])
        with self.assertRaises(RuntimeError):
            client.complete([], [])
        client.complete([], [])
        result = self.ledger.summary('alice')
        self.assertEqual(result['pending_requests'], 2)
        self.assertEqual(result['total_tokens'], 10)
        self.assertEqual(result['request_count'], 4)

    def test_stream_snapshots_and_interruption_keep_reported_usage(self):
        event = StreamEvent(type='usage', usage=UsageStats(input_tokens=5, output_tokens=3))
        backend = Mock()
        backend.stream.return_value = iter([event, event])
        list(self.client(backend).stream([], []))
        self.assertEqual(self.ledger.summary('alice')['total_tokens'], 8)
        def broken():
            yield event
            raise RuntimeError('stream disconnected')
        backend.stream.return_value = broken()
        with self.assertRaises(RuntimeError):
            list(self.client(backend).stream([], []))
        result = self.ledger.summary('alice')
        self.assertEqual(result['total_tokens'], 16)
        self.assertEqual(result['pending_requests'], 1)
        backend.stream.return_value = iter([event, event])
        stream = self.client(backend).stream([], [])
        next(stream)
        stream.close()
        self.assertEqual(self.ledger.summary('alice')['pending_requests'], 2)

    def test_ledger_failure_prevents_network_request(self):
        backend = Mock()
        with patch.object(self.ledger, 'begin', side_effect=UsageLedgerError('offline')):
            with self.assertRaises(UsageLedgerError):
                self.client(backend).complete([], [])
        backend.complete.assert_not_called()

    def test_cached_and_reasoning_tokens_not_double_counted(self):
        usage = _parse_usage({'prompt_tokens': 100, 'completion_tokens': 20,
                              'prompt_tokens_details': {'cached_tokens': 60},
                              'completion_tokens_details': {'reasoning_tokens': 8}})
        self.assertEqual(usage.input_tokens, 40)
        self.assertEqual(usage.cache_read_input_tokens, 60)
        self.assertEqual(usage.total_tokens, 120)
        anthropic_style = _parse_usage({'input_tokens': 40, 'output_tokens': 20,
                                        'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 10})
        self.assertEqual(anthropic_style.total_tokens, 130)
        zero_events = list(OpenAICompatClient(ModelConfig(model='m'))._parse_stream_payload(
            {'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}))
        self.assertEqual(zero_events[0].type, 'usage')
        self.assertFalse(_usage_reported({'prompt_tokens': None, 'completion_tokens': None}))
        self.assertFalse(_usage_reported({'prompt_tokens': 10}))
        self.assertTrue(_usage_reported({'prompt_tokens': 0, 'completion_tokens': 0}))

    def agent(self, *, ledger=True):
        agent = LocalCodingAgent(model_config=ModelConfig(model='test'),
            runtime_config=AgentRuntimeConfig(cwd=self.root, session_directory=self.root / 'sessions',
                scratchpad_root=self.root / 'scratchpad'),
            authenticated_user_id='alice' if ledger else None,
            usage_ledger=self.ledger if ledger else None, override_system_prompt='Test')
        agent.client = Mock()
        agent.client.complete.return_value = AssistantTurn('done', finish_reason='stop',
            usage=UsageStats(input_tokens=10, output_tokens=2), usage_reported=True)
        return agent

    def test_resume_compact_new_session_delete_and_clear(self):
        agent = self.agent()
        first = agent.run('first')
        stored = read_agent_session(first.session_id, directory=self.root / 'sessions')
        second = agent.resume('second', stored)
        self.assertEqual(second.usage.total_tokens, 24)
        self.assertEqual(second.run_usage.total_tokens, 12)
        self.assertEqual(agent.cumulative_usage.total_tokens, 24)
        def compact(agent, instructions):
            _, usage, _ = _call_compact_model(agent, [])
            return CompactionResult(boundary_message=None, usage=usage)
        with patch('src.compact.compact_conversation', side_effect=compact):
            result = agent.run('/compact')
        self.assertEqual(result.usage.total_tokens, 36)
        self.assertEqual(result.run_usage.total_tokens, 12)
        stored = read_agent_session(first.session_id, directory=self.root / 'sessions')
        self.assertEqual(stored.usage['total_tokens'], 36)
        fresh = self.agent()
        result = fresh.resume('after restart', stored)
        self.assertEqual(fresh.cumulative_usage.total_tokens, 48)
        self.assertEqual(result.run_usage.total_tokens, 12)
        another = fresh.run('new session')
        self.assertEqual(another.usage.total_tokens, 12)
        self.assertEqual(self.ledger.summary('alice')['total_tokens'], 60)
        self.ledger.import_sessions('alice', self.root / 'sessions')
        self.assertEqual(self.ledger.summary('alice')['total_tokens'], 60)
        for session_id in (first.session_id, another.session_id):
            delete_saved_session(self.root / 'sessions', session_id)
        fresh.run('/clear')
        self.assertEqual(fresh.cumulative_usage.total_tokens, 0)
        self.assertEqual(self.ledger.summary('alice')['total_tokens'], 60)

    def test_tool_loop_and_runtime_stream_snapshots(self):
        agent = self.agent()
        usage = UsageStats(input_tokens=10, output_tokens=2)
        agent.client.complete.side_effect = [
            AssistantTurn('', tool_calls=(ToolCall('c1', 'list_dir', {}),), usage=usage),
            AssistantTurn('done', finish_reason='stop', usage=usage),
        ]
        result = agent.run('list files')
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.run_usage.total_tokens, 24)
        self.assertEqual(self.ledger.summary('alice')['request_count'], 2)
        streaming = self.agent()
        streaming.runtime_config = replace(streaming.runtime_config, stream_model_responses=True)
        streaming.client.stream.return_value = iter([
            StreamEvent(type='content_delta', delta='done'),
            StreamEvent(type='usage', usage=usage), StreamEvent(type='usage', usage=usage),
            StreamEvent(type='message_stop', finish_reason='stop'),
        ])
        result = streaming.run('stream')
        self.assertEqual(result.usage.total_tokens, 12)
        self.assertEqual(self.ledger.summary('alice')['total_tokens'], 36)

    def test_auto_compact_failure_preserves_consumed_tokens(self):
        agent = self.agent()
        agent.run('start')
        snapshot = Mock(exceeds_soft_limit=True, exceeds_hard_limit=False,
                        projected_input_tokens=500, soft_input_limit_tokens=400,
                        hard_input_limit_tokens=600, soft_overflow_tokens=100, overflow_tokens=0)
        def compact(agent, custom_instructions=None):
            _, usage, _ = _call_compact_model(agent, [])
            return CompactionResult(boundary_message=None, error='invalid summary', usage=usage)
        with patch('src.agent_runtime.calculate_token_budget', return_value=snapshot), \
             patch.object(agent, '_reduce_context_pressure', return_value=False), \
             patch.object(agent, '_can_auto_compact_with_summary', return_value=True), \
             patch('src.agent_runtime.compact_conversation', side_effect=compact):
            result = agent._preflight_prompt_length(agent.last_session, [], turn_index=1)
        self.assertEqual(result.usage_increment.total_tokens, 12)
        self.assertEqual(self.ledger.summary('alice')['total_tokens'], 24)

    def test_failed_compact_usage_survives_and_legacy_is_imported_once(self):
        agent = self.agent(ledger=False)
        first = agent.run('legacy')
        agent.authenticated_user_id = 'alice'
        agent.usage_ledger = self.ledger
        def failed_compact(agent, instructions):
            _, usage, _ = _call_compact_model(agent, [])
            return CompactionResult(boundary_message=None, error='bad summary', usage=usage)
        with patch('src.compact.compact_conversation', side_effect=failed_compact):
            result = agent.run('/compact')
        self.assertEqual(result.usage.total_tokens, 24)
        self.assertEqual(result.run_usage.total_tokens, 12)
        self.ledger.import_sessions('alice', self.root / 'sessions')
        summary = self.ledger.summary('alice')
        self.assertEqual(summary['total_tokens'], 24)
        self.assertEqual(summary['imported_tokens'], 12)
        self.assertEqual(summary['request_count'], 1)
        delete_saved_session(self.root / 'sessions', first.session_id)
        self.assertEqual(self.ledger.summary('alice')['total_tokens'], 24)


class UsageEntryPointTests(unittest.TestCase):
    def test_cli_api_auth_and_delete_consistency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / 'workspace'
            root.mkdir()
            store = AuthStore(root)
            alice = store.create_user('alice', 'password-alice')
            bob = store.create_user('bob', 'password-bob')
            token = store.login('alice', 'password-alice')['access_token']
            headers = {'Authorization': f'Bearer {token}'}
            state = AgentState(cwd=root, model='test', base_url='http://model.test',
                               api_key='secret', allow_shell=False, allow_write=False,
                               session_directory=root / 'sessions')
            app = create_app(state, auth_store=store)
            response = {'choices': [{'message': {'role': 'assistant', 'content': 'hello'},
                                     'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 9, 'completion_tokens': 3}}
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/usage').status_code, 401)
                with patch('src.openai_compat.OpenAICompatClient._request_json', return_value=response):
                    run = client.post('/api/chat', headers=headers, json={'prompt': 'hello'}).json()
                self.assertEqual(run['usage']['total_tokens'], 12)
                self.assertEqual(run['run_usage']['total_tokens'], 12)
                usage = client.get('/api/usage', headers=headers, params={'user_id': bob['user_id']}).json()
                self.assertEqual(usage['user_id'], alice['user_id'])
                self.assertEqual(usage['total_tokens'], 12)
                self.assertEqual(client.delete('/api/sessions/' + run['session_id'], headers=headers, params={'confirm': 'true'}).status_code, 200)
                self.assertEqual(client.get('/api/usage', headers=headers).json()['total_tokens'], 12)
                bob_token = store.login('bob', 'password-bob')['access_token']
                self.assertEqual(client.get('/api/usage', headers={'Authorization': f'Bearer {bob_token}'}).json()['total_tokens'], 0)
                output = io.StringIO()
                with patch.dict(os.environ, {'HARNESS_AUTH_TOKEN': token}), redirect_stdout(output):
                    self.assertEqual(main(['usage', '--workspace-root', str(root), '--json']), 0)
                self.assertEqual(json.loads(output.getvalue()), usage)
