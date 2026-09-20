"""Session previews and authenticated CLI/GUI listings; no network/LLM calls."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.auth_runtime import AuthStore
from src.agent_runtime import LocalCodingAgent
from src.agent_types import AgentRuntimeConfig, ModelConfig, AssistantTurn
from src.gui.server import AgentState, create_app
from src.main import main, build_parser
from src.session_catalog import get_session_info, list_saved_sessions, normalize_preview, session_preview
from src.session_store import StoredAgentSession, read_agent_session, save_agent_session


class SessionCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'sessions'
        self.directory.mkdir()

    def write(self, sid, messages=None, **fields):
        path = self.directory / f'{sid}.json'
        path.write_text(json.dumps({'session_id': sid, 'messages': messages or [], **fields}))
        return path

    def test_preview_excludes_system_commands_and_compacted_tail(self):
        messages = [
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': 'injected', 'message_id': 'user_context_0'},
            {'role': 'user', 'content': '/help'},
            {'role': 'user', 'content': '  标注\n 两个视频\t谢谢  '},
        ]
        self.assertEqual(session_preview({'messages': messages}), '标注 两个视频 谢谢')
        messages.insert(0, {'role': 'user', 'content': 'summary', 'metadata': {'kind': 'compact_summary'}})
        self.assertEqual(session_preview({'messages': messages}), '')
        self.assertEqual(session_preview({'preview': '原始 query', 'messages': messages}), '原始 query')
        self.assertEqual(session_preview({'preview': '', 'messages': messages}), '')
        self.assertEqual(len(normalize_preview('中' * 100)), 80)
        self.assertTrue(normalize_preview('中' * 100).endswith('…'))
        self.assertNotIn('\x1b', normalize_preview('a\x1bb'))

    def test_listing_sort_limits_corruption_and_no_mutation(self):
        self.assertEqual(list_saved_sessions(self.directory)['total'], 0)
        paths = [self.write(f's{i}', [{'role': 'user', 'content': f'query {i}'}]) for i in range(23)]
        for i, path in enumerate(paths):
            os.utime(path, (1000 + i, 1000 + i))
        (self.directory / 'bad.json').write_text('invalid JSON')
        (self.directory / 'wrong.json').write_text('[]')
        (self.directory / 'mismatch.json').write_text('{"session_id":"other", "messages":[]}')
        secret = self.root / 'secret.json'
        secret.write_text('{"messages": [], "preview":"other user"}')
        (self.directory / 'link.json').symlink_to(secret)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        result = list_saved_sessions(self.directory)
        self.assertEqual((result['total'], result['skipped'], len(result['sessions'])), (23, 4, 20))
        self.assertEqual(result['sessions'][0]['session_id'], 's22')
        self.assertEqual(result['sessions'][0]['modified_at_display'], '1970-01-01 08:17:02')
        self.assertEqual(len(list_saved_sessions(self.directory, limit=50)['sessions']), 23)
        self.assertEqual(len(list_saved_sessions(self.directory, limit=1)['sessions']), 1)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()})
        with self.assertRaises(ValueError):
            list_saved_sessions(self.directory, limit=0)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(['sessions', '--limit', '-1'])

    def test_preview_persists_through_resume_compact_and_new_session(self):
        agent = LocalCodingAgent(model_config=ModelConfig(model='test'),
            runtime_config=AgentRuntimeConfig(cwd=self.root, session_directory=self.directory,
                                             scratchpad_root=self.root / 'scratchpad'),
            override_system_prompt='Test system prompt.')
        agent.client = MagicMock()
        agent.client.complete.return_value = AssistantTurn('OK', finish_reason='stop')
        result = agent.run('  帮我标注\n两个视频 ')
        saved = read_agent_session(result.session_id, self.directory)
        self.assertEqual(saved.preview, '帮我标注 两个视频')
        agent.resume('第二个问题', saved)
        agent.resume('第三个问题', read_agent_session(result.session_id, self.directory))
        agent.client.complete.return_value = AssistantTurn('<summary>Summary.</summary>', finish_reason='stop')
        compacted = agent.resume('/compact', read_agent_session(result.session_id, self.directory))
        self.assertIn('Conversation compacted', compacted.final_output)
        saved = read_agent_session(result.session_id, self.directory)
        self.assertEqual(saved.preview, '帮我标注 两个视频')
        self.assertEqual(list_saved_sessions(self.directory)['sessions'][0]['preview'], saved.preview)
        agent.client.complete.return_value = AssistantTurn('OK', finish_reason='stop')
        agent.resume('继续', saved)
        self.assertEqual(read_agent_session(result.session_id, self.directory).preview, saved.preview)
        newer = agent.run('独立新会话')
        self.assertEqual(read_agent_session(newer.session_id, self.directory).preview, '独立新会话')
        # Legacy compacted records must not acquire a misleading new first query.
        path = self.directory / f'{result.session_id}.json'
        data = json.loads(path.read_text())
        data.pop('preview')
        path.write_text(json.dumps(data))
        agent.resume('不是原始问题', read_agent_session(result.session_id, self.directory))
        self.assertEqual(read_agent_session(result.session_id, self.directory).preview, '')

    def test_authenticated_cli_gui_consistency_and_expired_login(self):
        workspace = self.root / 'workspace'
        store = AuthStore(workspace, self.root / 'auth')
        alice = store.create_user('alice', 'password-alice')
        bob = store.create_user('bob', 'password-bob')
        token = store.login('alice', 'password-alice')['access_token']
        self.directory = workspace / 'users' / alice['user_id'] / 'sessions'
        self.write('alice-session', [{'role': 'user', 'content': '我的视频标注'}])
        (self.directory / 'bad.json').write_text('broken')
        (workspace / 'users' / bob['user_id'] / 'sessions' / 'bob-session.json').write_text(
            json.dumps({'session_id': 'bob-session', 'messages': [], 'preview': 'BOB PRIVATE'}))
        environment = {'AGENT_WORKSPACE': str(workspace), 'HARNESS_AUTH_DIR': str(store.directory),
                       'HARNESS_AUTH_TOKEN': token}
        output = io.StringIO()
        with patch.dict(os.environ, environment), redirect_stdout(output), patch(
                'src.agent_runtime.LocalCodingAgent.run') as run:
            self.assertEqual(main(['sessions', '--limit', '50']), 0)
            run.assert_not_called()
        self.assertIn('alice-session', output.getvalue())
        self.assertIn('我的视频标注', output.getvalue())
        self.assertIn('已跳过 1', output.getvalue())
        self.assertNotIn('BOB PRIVATE', output.getvalue())
        state = AgentState(cwd=workspace, model='test', base_url='http://test', api_key='test',
                           allow_shell=False, allow_write=False, session_directory=workspace / 'sessions')
        with TestClient(create_app(state, auth_store=store)) as client:
            self.assertEqual(client.get('/api/sessions').status_code, 401)
            headers = {'Authorization': f'Bearer {token}'}
            response = client.get('/api/sessions?limit=50', headers=headers)
            self.assertEqual(response.json(), list_saved_sessions(self.directory, limit=50)['sessions'])
            self.assertEqual(response.headers['X-Session-Skipped'], '1')
            self.assertEqual(response.headers['X-Session-Total'], '1')
            self.assertEqual(client.get('/api/sessions?limit=0', headers=headers).status_code, 422)
        with store.connect() as db:
            db.execute('UPDATE tokens SET expires = 0')
        with patch.dict(os.environ, environment), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['sessions']), 1)
        environment['HARNESS_AUTH_TOKEN'] = ''
        with patch.dict(os.environ, environment), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['sessions']), 1)

    def test_session_info_is_authenticated_user_scoped_and_read_only(self):
        workspace = self.root / 'workspace'
        store = AuthStore(workspace, self.root / 'auth')
        alice = store.create_user('alice', 'password-alice')
        bob = store.create_user('bob', 'password-bob')
        token = store.login('alice', 'password-alice')['access_token']
        directory = workspace / 'users' / alice['user_id'] / 'sessions'
        stored = StoredAgentSession(
            session_id='same-id', model_config={}, runtime_config={}, system_prompt_parts=(),
            user_context={}, system_context={}, messages=(
                {'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'hi'}),
            turns=1, tool_calls=0, usage={'input_tokens': 1200, 'output_tokens': 350},
            total_cost_usd=0, file_history=(), budget_state={}, plugin_state={})
        path = save_agent_session(stored, directory)
        self.assertEqual(get_session_info('same-id', directory=directory), {
            'session_id': 'same-id', 'message_count': 2, 'input_tokens': 1200, 'output_tokens': 350,
        })
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(['load-session', 'same-id'])
        other_directory = workspace / 'users' / bob['user_id'] / 'sessions'
        from dataclasses import replace
        save_agent_session(replace(stored, usage={'input_tokens': 9999}), other_directory)
        other_path = save_agent_session(replace(stored, session_id='bob-only'), other_directory)
        (directory / 'symlink.json').symlink_to(other_path)
        (directory / 'broken.json').write_text('{}')
        (directory / 'invalid-json.json').write_text('broken')
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        env = {'AGENT_WORKSPACE': str(workspace), 'HARNESS_AUTH_DIR': str(store.directory),
               'HARNESS_AUTH_TOKEN': token}
        with patch.dict(os.environ, env), patch('src.agent_runtime.LocalCodingAgent.run') as run:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['session-info', 'same-id']), 0)
            self.assertEqual(output.getvalue(), 'same-id\n2 messages\nin=1200 out=350\n')
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
            for sid in ('missing', 'bob-only', '../same-id', 'symlink', 'broken', 'invalid-json'):
                with self.subTest(sid=sid), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(main(['session-info', sid]), 1)
                    self.assertNotIn('Traceback', errors.getvalue())
            # Same root selection option as the sessions command.
            with patch.dict(os.environ, {'AGENT_WORKSPACE': str(self.root / 'wrong')}), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['session-info', 'same-id', '--workspace-root', str(workspace)]), 0)
            run.assert_not_called()
            with store.connect() as db:
                db.execute('UPDATE tokens SET expires = 0')
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(['session-info', 'same-id']), 1)
        env['HARNESS_AUTH_TOKEN'] = ''
        with patch.dict(os.environ, env), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['session-info', 'same-id']), 1)
