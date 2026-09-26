"""Public policy, discovery, execution and shared transport error contracts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json

from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from src.auth_runtime import AuthStore
from src.gui.server import AgentState, create_app, create_user_app
from src.agent_slash_commands import get_slash_command_specs, preprocess_slash_command
from src.bundled_skills import get_bundled_skills
from src.agent_tools import default_tool_registry
from src.user_agent import user_tools
from src import user_access as access
from src.usage_ledger import UsageLedgerError


class UserAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'workspace'
        self.root.mkdir()
        self.store = AuthStore(self.root)
        self.user = self.store.create_user('alice', 'test-password')
        self.token = self.store.login('alice', 'test-password')['access_token']
        self.headers = {'Authorization': f'Bearer {self.token}'}
        self.template = AgentState(cwd=self.root, model='test', base_url='http://unused.invalid', api_key='secret',
                                   allow_shell=False, allow_write=False, session_directory=self.root / 'sessions')
        self.app = create_app(self.template, auth_store=self.store)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_every_registered_public_surface_is_classified(self):
        def walk(app):
            for route in app.routes:
                if hasattr(route, 'original_router'):
                    yield from walk(route.original_router)
                elif isinstance(route, APIRoute) and route.include_in_schema:
                    yield route
        keys = {('http', method + ' ' + route.path)
                for app in (self.app, create_user_app(self.template)) for route in walk(app) for method in route.methods}
        keys |= {('command', spec.names[0]) for spec in get_slash_command_specs()}
        keys |= {('skill', skill.name) for skill in get_bundled_skills()}
        keys |= {('tool', name) for name in default_tool_registry()}
        self.assertFalse(access.USER_AVAILABLE & access.USER_UNAVAILABLE)
        self.assertEqual(keys, access.USER_AVAILABLE | access.USER_UNAVAILABLE)
        self.assertFalse(access.user_can('command', 'future-command'))
        self.assertFalse(access.user_can_http('GET', '/api/future'))

    def test_discovery_help_aliases_and_denied_execution_agree(self):
        specs = self.client.get('/api/slash-commands', headers=self.headers).json()
        self.assertEqual({s['primary'] for s in specs}, {'help', 'clear', 'compact'})
        result = self.client.post('/api/chat', headers=self.headers, json={'prompt': '/commands'})
        self.assertEqual(result.status_code, 200)
        self.assertIn('/help', result.json()['final_output'])
        self.assertNotIn('/config', result.json()['final_output'])
        self.assertEqual(self.client.get('/api/skills?include_internal=true', headers=self.headers).json(), [])
        denied = self.client.post('/api/chat', headers=self.headers, json={'prompt': '/config-help'})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()['code'], 'feature_unavailable')
        caps = self.client.get('/api/capabilities', headers=self.headers).json()
        self.assertNotIn('GET /api/account', caps['http'])
        self.assertNotIn('bash', caps['tools'])
        self.assertEqual(set(caps['commands']), {name for s in specs for name in s['names']})

    def test_editing_sets_updates_display_and_execution(self):
        promoted = {('command', 'context'), ('skill', 'debug'), ('http', 'GET /api/file-history')}
        with patch.object(access, 'USER_AVAILABLE', access.USER_AVAILABLE | promoted), \
             patch.object(access, 'USER_UNAVAILABLE', access.USER_UNAVAILABLE - promoted):
            specs = self.client.get('/api/slash-commands', headers=self.headers).json()
            self.assertIn('context', {s['primary'] for s in specs})
            self.assertTrue(access.user_can_command('usage'))
            result = self.client.post('/api/chat', headers=self.headers, json={'prompt': '/usage'})
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('unavailable', result.json()['final_output'])
            skills = self.client.get('/api/skills', headers=self.headers).json()
            self.assertEqual([s['name'] for s in skills], ['debug'])
            agent = self.app.state.user_apps[self.user['user_id']].state.agent_state.agent
            self.assertTrue(preprocess_slash_command(agent, '/debug').should_query)
            self.assertEqual(self.client.get('/api/file-history', headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/api/file-history', headers=self.headers).status_code, 403)
        self.assertFalse(access.user_can_http('POST', '/api/state'))
        self.assertTrue(access.user_can_http('GET', '/api/sessions/example'))
        self.assertFalse(access.user_can_http('GET', '/api/sessions/example/extra'))

    def test_error_contract_matches_http_and_stream(self):
        body = {'prompt': 'test', 'resume_session_id': 'missing'}
        plain = self.client.post('/api/chat', headers=self.headers, json=body)
        stream = self.client.post('/api/chat/stream', headers=self.headers, json=body)
        self.assertEqual(plain.status_code, 404)
        self.assertEqual(stream.status_code, 200)
        event = json.loads(stream.text)
        self.assertEqual(event.pop('type'), 'error')
        self.assertEqual(event, plain.json())
        self.assertEqual(event['code'], 'not_found')
        self.assertEqual(event['message'], event['error'])
        self.assertEqual(event['status'], 404)

    def test_auth_validation_and_unexpected_errors_are_safe_and_uniform(self):
        self.assertEqual(self.client.get('/api/usage').json()['code'], 'authentication_required')
        invalid = self.client.post('/api/chat', headers=self.headers, json={'prompt': '', 'password': 'do-not-echo'})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()['code'], 'validation_error')
        self.assertNotIn('do-not-echo', invalid.text)
        self.client.get('/api/state', headers=self.headers)
        agent = self.app.state.user_apps[self.user['user_id']].state.agent_state.agent
        with patch.object(agent, 'run', side_effect=RuntimeError('secret-provider-key')):
            plain = self.client.post('/api/chat', headers=self.headers, json={'prompt': 'test'})
            stream = self.client.post('/api/chat/stream', headers=self.headers, json={'prompt': 'test'})
        self.assertEqual(plain.json()['code'], 'internal_error')
        self.assertNotIn('secret-provider-key', plain.text + stream.text)
        event = json.loads(stream.text); event.pop('type')
        self.assertEqual(event, plain.json())
        with patch.object(self.store, 'usage_summary', side_effect=UsageLedgerError('DB private path')):
            response = self.client.get('/api/usage', headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'usage_unavailable')
        self.assertNotIn('private path', response.text)
