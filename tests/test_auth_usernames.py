import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.auth_runtime import AuthStore
from src.gui.server import AgentState, create_app
from src.main import main


class UsernameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = AuthStore(self.root / 'workspace', self.root / 'auth')

    def test_chinese_and_spaced_names_login_through_gui_api(self):
        state = AgentState(cwd=self.store.workspace, model='test', base_url='http://unused.invalid',
                           api_key='test', allow_shell=False, allow_write=False,
                           session_directory=self.root / 'sessions')
        with TestClient(create_app(state, auth_store=self.store)) as client:
            for name in ('雷晞宇', 'ray chen', 'ray chen lee', 'ray_1.-'):
                with self.subTest(name=name):
                    user = self.store.create_user(name, 'test-password')
                    response = client.post('/api/auth/login', json={'username': name, 'password': 'test-password'})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(client.get('/api/auth/me').json(), user)
                    self.assertEqual(self.store.find_user(name), user)
                    self.assertTrue((self.store.workspace / 'users' / user['user_id']).is_dir())
                    with self.assertRaisesRegex(ValueError, 'already exists'):
                        self.store.create_user(name, 'test-password')
                    client.post('/api/auth/logout')

    def test_unsafe_empty_and_overlong_names_remain_rejected(self):
        for name in ('', ' ', ' ray', 'ray ', 'ray\nchen', 'ray\tchen', '雷/宇', 'ray\\chen',
                     'ray\x00', '<ray>', '雷' * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.create_user(name, 'test-password')

    def test_cli_accepts_a_quoted_multiword_username(self):
        import io
        import os
        from contextlib import redirect_stdout

        with patch.dict(os.environ, {'HARNESS_AUTH_DIR': str(self.store.directory)}), \
                patch('getpass.getpass', return_value='test-password'), redirect_stdout(io.StringIO()):
            result = main(['users-create', 'ray chen', '--workspace-root', str(self.store.workspace)])
        self.assertEqual(result, 0)
        self.assertEqual(self.store.login('ray chen', 'test-password')['user']['username'], 'ray chen')
