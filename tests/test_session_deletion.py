"""Permanent deletion on temporary fixtures only; no model/business network calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.agent_runtime import LocalCodingAgent
from src.agent_types import AgentRuntimeConfig, AssistantTurn, ModelConfig
from src.auth_runtime import AuthStore
from src.gui.server import AgentState, create_app
from src.main import _run_agent_chat_loop, main
from src.session_catalog import list_saved_sessions
from src.session_lifecycle import (
    SessionBusyError, SessionDeletedError, clear_saved_sessions,
    delete_saved_session, session_deletion_candidates, session_guard,
)
from src.session_store import StoredAgentSession, read_agent_session, save_agent_session


class SessionDeletionFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / 'sessions'
        self.stored = StoredAgentSession(
            session_id='one', model_config={}, runtime_config={}, system_prompt_parts=(),
            user_context={}, system_context={}, messages=({'role': 'user', 'content': 'private query'},),
            turns=1, tool_calls=0, usage={}, total_cost_usd=0,
            file_history=(), budget_state={}, plugin_state={}, preview='private query',
        )

    def save(self, sid='one', directory=None):
        return save_agent_session(replace(self.stored, session_id=sid), directory or self.directory)


class SessionDeletionTests(SessionDeletionFixture):
    def test_permanent_delete_only_session_and_reject_stale_save(self):
        path = self.save()
        saved = read_agent_session('one', self.directory)
        unrelated = [self.root / 'uploads' / 'video.mp4', self.root / 'video_analysis_task_id.json',
                     self.root / '.port_sessions' / 'scratchpad' / 'one' / 'note.txt',
                     self.root / '.port_sessions' / 'background' / 'bg_one.json']
        for item in unrelated:
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b'preserved')
        self.assertEqual(delete_saved_session(self.directory, 'one'), {'deleted': ['one']})
        self.assertFalse(path.exists())
        self.assertEqual(list_saved_sessions(self.directory)['total'], 0)
        for item in unrelated:
            self.assertEqual(item.read_bytes(), b'preserved')
        for item in self.directory.rglob('*'):
            if item.is_file():
                self.assertNotIn(b'private query', item.read_bytes())
        with self.assertRaises(SessionDeletedError):
            save_agent_session(saved, self.directory)
        with self.assertRaises(FileNotFoundError):
            read_agent_session('one', self.directory)
        with self.assertRaises(FileNotFoundError):
            delete_saved_session(self.directory, 'one')
        self.save('new')

    def test_clear_snapshot_busy_corrupt_symlink_and_unrelated_user(self):
        self.save()
        self.save('busy')
        (self.directory / 'corrupt.json').write_text('not JSON')
        other = self.save('other', self.root / 'other-user' / 'sessions')
        (self.directory / 'link.json').symlink_to(other)
        snapshot = session_deletion_candidates(self.directory)
        self.assertEqual(snapshot, ['busy', 'corrupt', 'one'])
        self.save('after-confirmation')
        with session_guard(self.directory, 'busy', active=True):
            report = clear_saved_sessions(self.directory, snapshot + ['missing', '../other'])
        self.assertEqual(report['deleted'], ['corrupt', 'one'])
        self.assertEqual(report['skipped_running'], ['busy'])
        self.assertEqual(report['not_found'], ['missing', '../other'])
        self.assertTrue(other.exists())
        self.assertTrue((self.directory / 'busy.json').exists())
        self.assertTrue((self.directory / 'after-confirmation.json').exists())
        with self.assertRaises(ValueError):
            delete_saved_session(self.directory, 'link')
        for sid in ('', '../one', 'a/b', '.', 'a' * 201):
            with self.assertRaises(FileNotFoundError):
                delete_saved_session(self.directory, sid)

    def test_lock_links_and_redirected_directory_rejected(self):
        self.save()
        marker = self.directory / '.lifecycle' / 'one.lock'
        marker.unlink()
        secret = self.root / 'secret'
        secret.write_text('secret')
        marker.symlink_to(secret)
        with self.assertRaises(OSError):
            delete_saved_session(self.directory, 'one')
        marker.unlink()
        os.link(secret, marker)
        with self.assertRaises(ValueError):
            delete_saved_session(self.directory, 'one')
        self.assertEqual(secret.read_text(), 'secret')
        alias = self.root / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            delete_saved_session(alias, 'one')

    def test_cross_process_busy_and_durable_marker(self):
        self.save()
        code = (
            'from pathlib import Path\nimport sys\n'
            'from src.session_lifecycle import session_guard\n'
            'with session_guard(Path(sys.argv[1]), "one", active=True):\n'
            ' print("locked", flush=True)\n input()\n'
        )
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.directory)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            with self.assertRaises(SessionBusyError):
                delete_saved_session(self.directory, 'one')
            self.assertEqual(clear_saved_sessions(self.directory, ['one'])['skipped_running'], ['one'])
            process.communicate('\n', timeout=10)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        delete_saved_session(self.directory, 'one')
        check = subprocess.run([sys.executable, '-B', '-c',
            'from src.session_lifecycle import session_guard, SessionDeletedError\n'
            'from pathlib import Path\nimport sys\n'
            'try:\n with session_guard(Path(sys.argv[1]), "one"): pass\n'
            'except SessionDeletedError:\n print("deleted")\n', str(self.directory)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(check.stdout.strip(), 'deleted')

    def test_failed_unlink_is_reported_and_can_be_retried_without_resurrection(self):
        path = self.save()
        with patch.object(Path, 'unlink', side_effect=PermissionError('test denied')):
            report = clear_saved_sessions(self.directory, ['one'])
        self.assertEqual(report['deleted'], [])
        self.assertEqual(report['errors'][0]['session_id'], 'one')
        self.assertTrue(path.exists())
        with self.assertRaises(SessionDeletedError):
            self.save()
        self.assertEqual(delete_saved_session(self.directory, 'one'), {'deleted': ['one']})

    def agent(self):
        agent = LocalCodingAgent(model_config=ModelConfig(model='test'),
            runtime_config=AgentRuntimeConfig(cwd=self.root, session_directory=self.directory,
                                             scratchpad_root=self.root / 'scratchpad'),
            override_system_prompt='Test system prompt.')
        agent.client = MagicMock()
        agent.client.complete.return_value = AssistantTurn('OK', finish_reason='stop')
        return agent

    def test_live_turn_busy_stale_resume_compact_and_clear(self):
        agent = self.agent()
        result = agent.run('hello')
        stored = read_agent_session(result.session_id, self.directory)
        entered, release = threading.Event(), threading.Event()

        def respond(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test release timed out')
            return AssistantTurn('finished', finish_reason='stop')

        agent.client.complete.side_effect = respond
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(agent.resume, 'continue', stored)
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(SessionBusyError):
                    delete_saved_session(self.directory, result.session_id)
            finally:
                release.set()
            future.result(timeout=10)
        delete_saved_session(self.directory, result.session_id)
        calls = agent.client.complete.call_count
        with self.assertRaises(SessionDeletedError):
            agent.resume('stale', stored)
        with self.assertRaises(SessionDeletedError):
            agent.run('/compact')
        self.assertEqual(agent.client.complete.call_count, calls)
        agent.run('/clear')
        self.assertIsNone(agent.active_session_id)
        self.assertFalse((self.directory / f'{result.session_id}.json').exists())

    def test_cli_chat_stale_session_not_resubmitted(self):
        agent = self.agent()
        result = agent.run('hello')
        delete_saved_session(self.directory, result.session_id)
        output = []
        with patch.object(agent, 'run') as run:
            _run_agent_chat_loop(agent, initial_prompt='must not be resubmitted',
                resume_session_id=result.session_id, show_transcript=False,
                input_func=lambda _: '/quit', output_func=output.append)
            run.assert_not_called()
        self.assertTrue(any('已删除' in line for line in output))


class AuthenticatedDeletionTests(SessionDeletionFixture):
    # Shared temporary fixture; keep authentication/API tests independent of a real .env.
    def setUp(self):
        super().setUp()
        self.workspace = self.root / 'workspace'
        self.store = AuthStore(self.workspace, self.root / 'auth')
        alice = self.store.create_user('alice', 'password-alice')
        bob = self.store.create_user('bob', 'password-bob')
        self.directory = self.workspace / 'users' / alice['user_id'] / 'sessions'
        self.other_directory = self.workspace / 'users' / bob['user_id'] / 'sessions'
        self.token = self.store.login('alice', 'password-alice')['access_token']
        self.headers = {'Authorization': f'Bearer {self.token}'}
        self.env = {'AGENT_WORKSPACE': str(self.workspace), 'HARNESS_AUTH_DIR': str(self.store.directory),
                    'HARNESS_AUTH_TOKEN': self.token}

    def test_user_directory_symlink_cannot_redirect_deletion(self):
        other = self.save('bob-only', self.other_directory)
        self.directory.rmdir()  # Empty fixture directory created by account setup.
        self.directory.symlink_to(self.other_directory, target_is_directory=True)
        with patch.dict(os.environ, self.env), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['session-delete', 'bob-only', '--yes']), 1)
            self.assertEqual(main(['sessions-clear', '--yes']), 1)
        state = AgentState(cwd=self.workspace, model='test', base_url='http://model.invalid',
                           api_key='fake', allow_shell=False, allow_write=False,
                           session_directory=self.workspace / 'sessions')
        with TestClient(create_app(state, auth_store=self.store)) as client:
            self.assertEqual(client.delete('/api/sessions/bob-only?confirm=true', headers=self.headers).status_code, 403)
            self.assertEqual(client.post('/api/sessions/clear-preview', headers=self.headers).status_code, 403)
        self.assertTrue(other.exists())

    def test_cli_confirmation_yes_cancel_empty_expiry_and_user_scope(self):
        own = self.save()
        other = self.save('bob-only', self.other_directory)
        with patch.dict(os.environ, self.env):
            with patch('builtins.input', return_value='n'), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['session-delete', 'one']), 0)
            self.assertTrue(own.exists())
            with patch('builtins.input', side_effect=EOFError), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['sessions-clear']), 0)
            self.assertTrue(own.exists())
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(['session-delete', 'bob-only', '--yes']), 1)
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['session-delete', 'one', '--yes']), 0)
            self.assertIn('"deleted"', out.getvalue())
            self.assertFalse(own.exists())
            self.save('two')
            self.save('busy')
            with session_guard(self.directory, 'busy', active=True), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(['sessions-clear', '--yes']), 0)
            self.assertIn('skipped_running', out.getvalue())
            with patch('builtins.input', return_value='yes'), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['sessions-clear']), 0)
            with patch('builtins.input') as prompt, redirect_stdout(io.StringIO()):
                self.assertEqual(main(['sessions-clear']), 0)
                prompt.assert_not_called()
            self.save('expired')
            with self.store.connect() as db:
                db.execute('UPDATE tokens SET expires = 0')
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(['sessions-clear', '--yes']), 1)
            self.assertTrue((self.directory / 'expired.json').exists())
        self.assertTrue(other.exists())

    def test_gui_delete_clear_authorization_confirmation_and_stale_chat(self):
        self.save()
        self.save('busy')
        other = self.save('bob-only', self.other_directory)
        state = AgentState(cwd=self.workspace, model='test', base_url='http://model.invalid',
                           api_key='fake', allow_shell=False, allow_write=False,
                           session_directory=self.workspace / 'sessions')
        app = create_app(state, auth_store=self.store)
        with TestClient(app) as client:
            self.assertEqual(client.delete('/api/sessions/one?confirm=true').status_code, 401)
            self.assertEqual(client.request('DELETE', '/api/sessions', json={'session_ids': ['one'], 'confirm': True}).status_code, 401)
            self.assertEqual(client.delete('/api/sessions/one', headers=self.headers).status_code, 400)
            self.assertEqual(client.delete('/api/sessions/bob-only?confirm=true', headers=self.headers).status_code, 404)
            self.assertEqual(client.delete('/api/sessions/one?confirm=true',
                headers={**self.headers, 'Origin': 'http://evil.invalid'}).status_code, 403)
            inner = next(iter(app.state.user_apps.values()))
            agent = inner.state.agent_state.agent
            agent.active_session_id = 'one'
            with session_guard(self.directory, 'busy', active=True):
                self.assertEqual(client.delete('/api/sessions/busy?confirm=true', headers=self.headers).status_code, 409)
                response = client.delete('/api/sessions/one?confirm=true', headers=self.headers)
                self.assertEqual(response.json(), {'deleted': ['one']})
                self.assertIsNone(agent.active_session_id)
                self.assertEqual(client.get('/api/sessions/one', headers=self.headers).status_code, 404)
                with patch.object(agent, 'run') as run:
                    response = client.post('/api/chat', headers=self.headers,
                        json={'prompt': 'continue', 'resume_session_id': 'one'})
                    self.assertEqual(response.status_code, 404)
                    run.assert_not_called()
                self.save('two')
                (self.directory / 'corrupt.json').write_text('bad JSON')
                preview = client.post('/api/sessions/clear-preview', headers=self.headers).json()
                self.save('new-after-preview')
                self.assertEqual(client.request('DELETE', '/api/sessions', headers=self.headers,
                    json=preview).status_code, 400)
                response = client.request('DELETE', '/api/sessions', headers=self.headers,
                    json={**preview, 'confirm': True})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['deleted'], ['corrupt', 'two'])
                self.assertEqual(response.json()['skipped_running'], ['busy'])
            self.assertTrue((self.directory / 'new-after-preview.json').exists())
            self.assertTrue(other.exists())
            self.store.logout(self.token)
            self.assertEqual(client.delete('/api/sessions/busy?confirm=true', headers=self.headers).status_code, 401)


if __name__ == '__main__':
    unittest.main()
