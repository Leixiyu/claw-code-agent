"""Public documentation must match routing, permissions and transport contracts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import APIRouter
from fastapi.testclient import TestClient

from src import user_access as access
from src.auth_runtime import AuthStore
from src.gui.server import AgentState, create_app


class PublicOpenAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'workspace'
        self.root.mkdir()
        self.store = AuthStore(self.root)
        self.state = AgentState(cwd=self.root, model='private-test-model',
            base_url='http://private-provider.invalid', api_key='private-api-key',
            allow_shell=False, allow_write=False, session_directory=self.root / 'sessions')
        self.app = create_app(self.state, auth_store=self.store)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def schema(self):
        response = self.client.get('/openapi.json')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_docs_cover_exact_public_surface_without_exposing_runtime_data(self):
        with patch('src.gui.server.AgentState', side_effect=AssertionError('must not create a user agent')), \
             patch.object(self.state, 'snapshot', side_effect=AssertionError('must not read runtime data')):
            schema = self.schema()
            self.assertEqual(self.client.get('/docs').status_code, 200)
            self.assertEqual(self.schema(), schema)
        self.assertFalse(self.app.state.user_apps)
        documented = {('http', method.upper() + ' ' + path)
                      for path, operations in schema['paths'].items() for method in operations}
        self.assertEqual(documented, {item for item in access.USER_AVAILABLE if item[0] == 'http'})
        self.assertEqual(len(schema['tags']), 7)
        self.assertNotIn('delete', schema['paths']['/api/sessions'])
        self.assertNotIn('post', schema['paths']['/api/state'])
        self.assertNotIn('StateUpdate', schema['components']['schemas'])
        encoded = json.dumps(schema)
        for value in ('private-api-key', 'private-test-model', 'private-provider.invalid', str(self.root)):
            self.assertNotIn(value, encoded)
        self.assertIs(self.app.openapi(), self.app.openapi_schema)

    def test_policy_changes_add_and_remove_docs_without_another_allowlist(self):
        # A nested router also guards against dropping FastAPI route wrappers/prefixes.
        router = APIRouter(prefix='/api/future')

        @router.get('/info')
        def info():
            return {'ok': True}

        self.app.include_router(router)
        promoted = {('http', 'GET /api/future/info'), ('http', 'GET /api/file-history')}
        denied = {('http', 'GET /api/usage')}
        with patch.object(access, 'USER_AVAILABLE', access.USER_AVAILABLE | promoted), \
             patch.object(access, 'USER_UNAVAILABLE', (access.USER_UNAVAILABLE - promoted) | denied):
            schema = self.schema()
        self.assertIn('/api/file-history', schema['paths'])
        self.assertIn('/api/future/info', schema['paths'])
        self.assertEqual(schema['paths']['/api/future/info']['get']['tags'], ['其他接口'])
        self.assertNotIn('/api/usage', schema['paths'])
        self.assertNotIn('HarnessUsage', schema['components']['schemas'])

    def test_auth_schemes_and_cookie_bearer_execution(self):
        schema = self.schema()
        schemes = schema['components']['securitySchemes']
        self.assertEqual(schemes['BearerAuth']['scheme'], 'bearer')
        self.assertNotIn('bearerFormat', schemes['BearerAuth'])
        self.assertEqual(schemes['SessionCookie']['name'], 'harness_session')
        for path, methods in schema['paths'].items():
            for operation in methods.values():
                expected = [] if path in ('/health', '/api/auth/login') else [{'BearerAuth': []}, {'SessionCookie': []}]
                self.assertEqual(operation['security'], expected)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)
        user = self.store.create_user('测试 用户', 'test-password')
        login = self.client.post('/api/auth/login', json={'username': '测试 用户', 'password': 'test-password'})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me').json(), user)
        self.client.cookies.clear()
        headers = {'Authorization': 'Bearer ' + login.json()['access_token']}
        self.assertEqual(self.client.get('/api/auth/me', headers=headers).json(), user)
        self.assertEqual(self.client.post('/api/auth/logout', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me', headers=headers).status_code, 401)

    def test_upload_stream_validation_and_health_match_actual_contracts(self):
        schema = self.schema()
        paths = schema['paths']
        upload = paths['/api/uploads']['post']
        self.assertEqual(upload['parameters'][0]['name'], 'X-Filename')
        self.assertTrue(upload['parameters'][0]['required'])
        self.assertEqual(set(upload['requestBody']['content']), {'application/octet-stream'})
        stream = paths['/api/chat/stream']['post']
        self.assertEqual(set(stream['responses']['200']['content']), {'application/x-ndjson'})
        self.assertNotIn('409', stream['responses'])  # Failures inside the worker are events.
        self.assertEqual(paths['/health']['get']['responses']['503']['content']['application/json']['schema'],
                         {'$ref': '#/components/schemas/HarnessHealth'})
        self.assertEqual(paths['/api/chat']['post']['responses']['422']['content']['application/json']['schema'],
                         {'$ref': '#/components/schemas/HarnessError'})
        self.store.create_user('tester', 'test-password')
        self.client.post('/api/auth/login', json={'username': 'tester', 'password': 'test-password'})
        response = self.client.post('/api/uploads', content=b'fixture-video',
            headers={'X-Filename': 'test.mp4', 'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'video_ref': {'type': 'upload_file', 'path': 'uploads/test.mp4'}, 'size_bytes': 13})
        stream_response = self.client.post('/api/chat/stream', json={'prompt': 'test', 'resume_session_id': 'missing'})
        self.assertEqual(stream_response.status_code, 200)
        self.assertEqual(json.loads(stream_response.text)['type'], 'error')
        invalid = self.client.post('/api/chat', json={'prompt': ''})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()['code'], 'validation_error')
        self.assertEqual(self.client.get('/api/usage').json()['total_tokens'], 0)

    def test_all_schema_references_resolve_and_operation_ids_are_unique(self):
        schema = self.schema()

        def walk(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    target = schema
                    for part in value['$ref'].removeprefix('#/').split('/'):
                        target = target[part.replace('~1', '/').replace('~0', '~')]
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(schema)
        identifiers = [operation['operationId'] for methods in schema['paths'].values() for operation in methods.values()]
        self.assertEqual(len(identifiers), len(set(identifiers)))


if __name__ == '__main__':
    unittest.main()
