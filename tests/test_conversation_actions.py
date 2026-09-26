"""Conversation actions keep history, authentication and the ledger distinct."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from src.agent_types import AssistantTurn, UsageStats
from src.auth_runtime import AuthStore
from src.gui.server import AgentState, create_app, create_user_app

class ConversationActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'workspace'
        self.root.mkdir()
        self.store = AuthStore(self.root)
        self.user = self.store.create_user('alice', 'test-password')
        self.token = self.store.login('alice', 'test-password')['access_token']
        self.state = AgentState(cwd=self.root, model='test', base_url='http://unused.invalid', api_key='fixture',
                                allow_shell=False, allow_write=False, session_directory=self.root/'sessions',
                                stream_model_responses=False)
        self.app = create_app(self.state, auth_store=self.store)
        self.client = TestClient(self.app, headers={'Authorization':'Bearer '+self.token})
        self.addCleanup(self.client.close)
        self.client.get('/api/state')
        self.user_state = self.app.state.user_apps[self.user['user_id']].state.agent_state

    def test_new_clear_slash_clear_and_delete_preserve_lifetime_usage(self):
        turn = AssistantTurn('fixture reply', usage=UsageStats(input_tokens=5, output_tokens=2), usage_reported=True)
        with patch.object(self.user_state.agent.client, 'complete', return_value=turn):
            first = self.client.post('/api/chat', json={'prompt':'first conversation'}).json()
            second = self.client.post('/api/chat', json={'prompt':'new conversation'}).json()
            self.assertNotEqual(first['session_id'], second['session_id'])
            self.assertNotIn('first conversation', str(second['transcript']))
            directory = self.user_state.session_directory
            files = {p.name:p.read_bytes() for p in directory.glob('*.json')}
            self.assertEqual(len(files), 2)
            self.assertEqual(self.client.get('/api/usage').json()['total_tokens'], 14)
            cleared = self.client.post('/api/clear')
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(cleared.json()['action'], 'runtime_state_cleared')
            self.assertIsNone(cleared.json()['active_session_id'])
            self.assertEqual(self.user_state.agent.cumulative_usage.total_tokens, 0)
            self.assertEqual(files, {p.name:p.read_bytes() for p in directory.glob('*.json')})
            slash = self.client.post('/api/chat', json={'prompt':'/clear', 'resume_session_id':first['session_id']}).json()
            self.assertEqual(slash['stop_reason'], 'state_cleared')
            self.assertIsNone(slash['session_id'])
            self.assertEqual(files, {p.name:p.read_bytes() for p in directory.glob('*.json')})
            self.assertEqual(self.client.get('/api/usage').json()['total_tokens'], 14)
            self.assertEqual(self.client.get('/api/auth/me').status_code, 200)
            third = self.client.post('/api/chat', json={'prompt':'after clear'}).json()
            self.assertNotIn(third['session_id'], [first['session_id'], second['session_id']])
            self.assertNotIn('first conversation', str(third['transcript']))
            deleted = self.client.delete('/api/sessions/'+first['session_id']+'?confirm=true')
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(self.client.get('/api/sessions/'+first['session_id']).status_code, 404)
            self.assertEqual(self.client.get('/api/usage').json()['total_tokens'], 21)

    def test_clear_rejects_busy_user_immediately_and_preserves_state(self):
        self.user_state.agent.active_session_id = 'in-flight'
        with self.user_state.lock():
            response = self.client.post('/api/clear')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'conflict')
        self.assertEqual(self.user_state.agent.active_session_id, 'in-flight')
        self.assertEqual(self.client.get('/api/auth/me').status_code, 200)

    def test_provider_profile_activation_does_not_authenticate_or_logout_user(self):
        with TestClient(create_user_app(self.state)) as profiles:
            profiles.post('/api/account/login', json={'target':'local-profile-label'})
            self.assertTrue(profiles.get('/api/account').json()['status']['logged_in'])
            self.assertEqual(self.client.get('/api/auth/me', headers={'Authorization':'Bearer invalid'}).status_code, 401)
            profiles.post('/api/account/logout', json={})
            self.assertFalse(profiles.get('/api/account').json()['status']['logged_in'])
            self.assertEqual(self.client.get('/api/auth/me').status_code, 200)
        self.assertEqual(self.client.get('/api/account').status_code, 403)
