from __future__ import annotations
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from src.auth_runtime import AuthStore, AuthenticationError
from src.user_workspace import initialize_user, add_task_id, read_task_ids, read_tasks
from src import business_functions as business
from src.gui.server import AgentState, create_app
from src.agent_types import AgentRunResult, AssistantTurn, ToolCall
from src.main import main
from src.user_migration import migrate_user_data


class UserPrototypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        self.root.mkdir()
        (self.root / 'CLAUDE.md').write_text('Test shared instructions {{AGENT_WORKSPACE_PATH}}')
        self.store = AuthStore(self.root)
        self.a = self.store.create_user('alice', 'test-password-alice')
        self.b = self.store.create_user('bob', 'test-password-bob')
        self.wa = initialize_user(self.root, self.a['user_id'])
        self.wb = initialize_user(self.root, self.b['user_id'])
        self.token = self.store.login('alice', 'test-password-alice')['access_token']
        self.headers = {'Authorization': f'Bearer {self.token}'}

    def app(self):
        state = AgentState(cwd=self.root, model='test', base_url='http://model.test',
                           api_key='secret', allow_shell=False, allow_write=False,
                           session_directory=self.root / 'sessions')
        return create_app(state, auth_store=self.store)

    def test_auth_tokens_and_password_storage(self):
        self.assertEqual(self.store.authenticate(self.token), self.a)
        with self.assertRaises(AuthenticationError):
            self.store.login('alice', 'incorrect-password')
        self.assertNotIn(b'test-password-alice', self.store.path.read_bytes())
        self.assertNotIn(self.token.encode(), self.store.path.read_bytes())
        self.store.logout(self.token)
        with self.assertRaises(AuthenticationError):
            self.store.authenticate(self.token)
        with self.assertRaises(ValueError):
            AuthStore(self.root, self.root / 'auth')

    def test_password_minimum_length_is_eight(self):
        for password in ('', '1234567'):
            with self.subTest(password=password), self.assertRaisesRegex(
                ValueError, '^password must have at least 8 characters$'
            ):
                self.store.create_user('short_password', password)
        for password in ('12345678', '123456789'):
            with self.subTest(length=len(password)):
                username = f'length_{len(password)}'
                user = self.store.create_user(username, password)
                token = self.store.login(username, password)['access_token']
                self.assertEqual(self.store.authenticate(token), user)

    def test_indexes_are_deduplicated_and_user_scoped(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda i: add_task_id(self.wa, 'analysis', f'task-{i % 5}'), range(20)))
        self.assertEqual(len(read_task_ids(self.wa, 'analysis')), 5)
        self.assertEqual(read_task_ids(self.wb, 'analysis'), [])
        payload = json.loads((self.wa / 'video_analysis_task_id.json').read_text())
        self.assertEqual(set(payload), {'tasks'})
        self.assertTrue(all(task['status'] == 'pending' for task in payload['tasks']))

    def test_gui_login_upload_and_logout(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.post('/api/chat', json={'prompt': 'hello'}).status_code, 401)
            self.assertEqual(client.get('/api/sessions').status_code, 401)
            bad = client.post('/api/auth/login', json={'username': 'alice', 'password': 'wrong'})
            self.assertEqual(bad.status_code, 401)
            response = client.post('/api/auth/login', json={'username': 'alice', 'password': 'test-password-alice'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('HttpOnly', response.headers['set-cookie'])
            self.assertEqual(client.get('/api/auth/me').json(), self.a)
            paths = [client.post('/api/uploads', headers={'X-Filename': 'test.mp4'}, content=b'video').json()['video_ref']['path'] for _ in range(2)]
            self.assertNotEqual(*paths)
            self.assertEqual((self.wa / paths[0]).read_bytes(), b'video')
            self.assertEqual(list((self.wb / 'uploads').iterdir()), [])
            self.assertEqual(client.post('/api/uploads', headers={'X-Filename': '../x'}, content=b'x').status_code, 400)
            self.assertEqual(client.post('/api/state', json={'cwd': str(self.root)}).status_code, 403)
            self.assertEqual(client.post('/api/memory', json={}).status_code, 403)
            client.post('/api/auth/logout')
            self.assertEqual(client.get('/api/auth/me').status_code, 401)

    def test_gui_states_stream_and_history_are_isolated(self):
        app = self.app()
        bob_token = self.store.login('bob', 'test-password-bob')['access_token']
        with TestClient(app) as client:
            a = client.get('/api/state', headers=self.headers).json()
            b = client.get('/api/state', headers={'Authorization': f'Bearer {bob_token}'}).json()
            self.assertEqual(a['cwd'], str(self.wa))
            self.assertEqual(a['session_directory'], str(self.wa / 'sessions'))
            self.assertNotEqual(a['cwd'], b['cwd'])
            agent = app.state.user_apps[self.a['user_id']].state.agent_state.agent
            self.assertNotIn('bash', agent.tool_registry)
            self.assertIn('list_model_training_tasks', agent.tool_registry)
            def run(prompt):
                agent.on_tool_start({'type': 'tool_start', 'message': '正在查看目录…'})
                return AgentRunResult(final_output='ok', turns=1, tool_calls=1, transcript=())
            with patch.object(agent, 'run', side_effect=run):
                response = client.post('/api/chat/stream', headers=self.headers, json={'prompt': 'test'})
            self.assertEqual([json.loads(x)['type'] for x in response.text.splitlines()], ['tool_start', 'result'])
            (self.wa / 'sessions' / 'alice-session.json').write_text(json.dumps({'session_id': 'alice-session', 'messages': []}))
            self.assertEqual(len(client.get('/api/sessions', headers=self.headers).json()), 1)
            self.assertEqual(client.get('/api/sessions', headers={'Authorization': f'Bearer {bob_token}'}).json(), [])
            self.assertEqual(client.post('/api/chat', headers={'Authorization': f'Bearer {bob_token}'}, json={'prompt': '/help', 'resume_session_id': 'alice-session'}).status_code, 404)

    def test_cli_requires_login_and_creates_user_scoped_agent(self):
        env = {'AGENT_WORKSPACE': str(self.root), 'HARNESS_AUTH_DIR': str(self.store.directory), 'HARNESS_AUTH_TOKEN': ''}
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['agent', 'hello']), 1)
            with patch('src.auth_cli.getpass.getpass', return_value='test-password-alice'):
                self.assertEqual(main(['login', 'alice']), 0)
            with patch('src.agent_runtime.LocalCodingAgent.run', autospec=True) as run:
                run.return_value = AgentRunResult(final_output='ok', turns=0, tool_calls=0, transcript=())
                self.assertEqual(main(['agent', 'hello']), 0)
                agent = run.call_args.args[0]
                self.assertEqual(agent.runtime_config.cwd, self.wa)
                self.assertEqual(agent.runtime_config.session_directory, self.wa / 'sessions')
            self.assertEqual(main(['logout']), 0)

    def test_business_all_modules_live_results_and_no_result_files(self):
        calls = []
        def backend(request):
            calls.append((request.method, str(request.url)))
            if request.method == 'POST':
                return httpx.Response(200, json={'task_id': {'analysis.test': 'a1', 'processing.test': 'p1', 'training.test': 't1'}[request.url.host]})
            if '/status/' in request.url.path:
                return httpx.Response(200, json={'status': 'done'})
            data = {
                'analysis.test': [{'category_id': 1}],
                'processing.test': {'manifest': {'dataset_id': 'd1', 'scenario': 'fire_inspection', 'status': 'ready'}},
                'training.test': {'metadata': {'model_id': 'm1', 'dataset_id': 'd1', 'status': 'ready'}},
            }
            return httpx.Response(200, json=data[request.url.host])
        client = httpx.Client
        env = {'VIDEO_ANALYSIS_API': 'http://analysis.test', 'VIDEO_PROCESSING_API': 'http://processing.test', 'MODEL_TRAINING_API': 'http://training.test'}
        (self.wa / 'uploads' / 'v.mp4').write_bytes(b'video')
        with patch.dict(os.environ, env), patch.object(business.httpx, 'Client', side_effect=lambda **kw: client(transport=httpx.MockTransport(backend), **kw)):
            kwargs = dict(workspace_root=self.wa, timeout_seconds=2)
            for name, arguments, module, task_id in [
                ('video_analysis', {'scenario': 'fire_inspection', 'video_ref': {'type': 'upload_file', 'path': 'uploads/v.mp4'}}, 'analysis', 'a1'),
                ('video_processing', {'scenario': 'fire_inspection', 'raw_video_refs': ['uploads/v.mp4']}, 'processing', 'p1'),
                ('model_training', {'scenario': 'fire_inspection', 'dataset_ref': 'd1'}, 'training', 't1'),
            ]:
                submit = getattr(business, f'submit_{name}')
                payload = json.loads(submit(arguments, operation_scope='s1', **kwargs)[0])
                self.assertEqual(payload['task_id'], task_id)
                self.assertTrue(json.loads(submit(arguments, operation_scope='s1', **kwargs)[0])['idempotency_replayed'])
                self.assertEqual(read_task_ids(self.wa, module), [task_id])
                self.assertEqual(read_tasks(self.wa, module)[0]['status'], 'pending')
                status = json.loads(getattr(business, f'get_{name}_status')({'task_id': task_id}, **kwargs)[0])
                self.assertTrue(status['result_ready'])
                self.assertEqual(read_tasks(self.wa, module)[0]['status'], 'done')
                submit(arguments, operation_scope='s1', **kwargs)
                self.assertEqual(read_tasks(self.wa, module)[0]['status'], 'done')
                result = json.loads(getattr(business, f'get_{name}_result')({'task_id': task_id}, **kwargs)[0])
                self.assertEqual(result['status'], 'done')
                self.assertNotIn('manifest_path', result)
                self.assertNotIn('metadata_path', result)
                listed = json.loads(getattr(business, f'list_{name}_tasks')({}, **kwargs)[0])
                self.assertEqual(listed['tasks'][0]['result'], result)
                with self.assertRaises(RuntimeError):
                    getattr(business, f'get_{name}_result')({'task_id': task_id}, workspace_root=self.wb, timeout_seconds=2)
            self.assertEqual(len([c for c in calls if c[0] == 'POST']), 3)
            self.assertEqual(len([c for c in calls if '/status/' in c[1]]), 3)
        for root in (self.wa, self.wb):
            for directory in ('tasks', 'datasets', 'models'):
                self.assertFalse((root / directory).exists())

    def test_list_partial_failure_and_empty(self):
        self.assertEqual(json.loads(business.list_video_analysis_tasks({}, workspace_root=self.wa, timeout_seconds=2)[0])['tasks'], [])
        for task, status in (('ok', 'done'), ('running', 'running'), ('bad-result', 'done'), ('failed', 'failed')):
            add_task_id(self.wa, 'analysis', task, status=status)
        def fetch_result(args, **kwargs):
            if args['task_id'] == 'bad-result':
                raise business.VideoAnalysisError('HTTP 404')
            return '{}', {}
        with patch.object(business, 'get_video_analysis_status') as status, patch.object(business, 'get_video_analysis_result', side_effect=fetch_result) as result:
            payload = json.loads(business.list_video_analysis_tasks({}, workspace_root=self.wa, timeout_seconds=2)[0])
        self.assertEqual(payload['task_count'], 4)
        self.assertIn('error', payload['tasks'][2])
        status.assert_not_called()
        self.assertEqual(result.call_count, 2)

    def test_upload_path_escape_and_training_dataset_guard(self):
        other = self.wb / 'uploads' / 'secret.mp4'
        other.write_bytes(b'secret')
        with self.assertRaises(business.VideoAnalysisError):
            business._resolve_uploaded_video(str(other), self.wa)
        with patch.object(business, '_post_model_training') as submit:
            with self.assertRaises(business.ModelTrainingError):
                business.submit_model_training({'scenario': 'fire_inspection', 'dataset_ref': 'missing'}, workspace_root=self.wa, timeout_seconds=2)
            submit.assert_not_called()

    def test_migration_dry_run_and_non_destructive_copy(self):
        legacy = Path(self.temp.name) / 'legacy'
        (legacy / 'uploads').mkdir(parents=True)
        (legacy / 'uploads' / 'v.mp4').write_bytes(b'original')
        (legacy / 'tasks' / 'analysis').mkdir(parents=True)
        (legacy / 'tasks' / 'analysis' / 'a1.json').write_text('{"task_id":"a1","result":{"secret":1}}')
        report = migrate_user_data(legacy, self.root, self.a['user_id'])
        self.assertFalse(report['applied'])
        self.assertEqual(list((self.wa / 'uploads').iterdir()), [])
        migrate_user_data(legacy, self.root, self.a['user_id'], apply=True)
        self.assertEqual((self.wa / 'uploads' / 'v.mp4').read_bytes(), b'original')
        self.assertTrue((legacy / 'tasks' / 'analysis' / 'a1.json').exists())
        self.assertEqual(read_task_ids(self.wa, 'analysis'), ['a1'])
        self.assertFalse((self.wa / 'tasks').exists())

    def test_gui_result_history_can_be_resumed_by_cli(self):
        app = self.app()
        add_task_id(self.wa, 'analysis', 'a1')
        with TestClient(app) as client:
            client.get('/api/state', headers=self.headers)
            agent = app.state.user_apps[self.a['user_id']].state.agent_state.agent
            with patch.dict(os.environ, {'VIDEO_ANALYSIS_API': 'analysis.test'}), patch.object(agent.client, 'complete', side_effect=[
                AssistantTurn('', tool_calls=(ToolCall('call1', 'get_video_analysis_result', {'task_id': 'a1'}),)),
                AssistantTurn('Video checked', finish_reason='stop'),
            ]), patch.object(business, '_get_backend_result', return_value=[{'category_id': 99}]):
                response = client.post('/api/chat', headers=self.headers, json={'prompt': 'Get analysis result'})
            self.assertEqual(response.status_code, 200, response.text)
            sid = response.json()['session_id']
            path = self.wa / 'sessions' / f'{sid}.json'
            saved = json.loads(path.read_text())
            self.assertIn('category_id', str(saved['messages']))
            self.assertFalse((self.wa / 'tasks').exists())
            saved['messages'][0]['content'] = 'LEGACY_SINGLE_USER_POLICY'
            path.write_text(json.dumps(saved))
        out = io.StringIO()
        with patch.dict(os.environ, {'AGENT_WORKSPACE': str(self.root), 'HARNESS_AUTH_DIR': str(self.store.directory), 'HARNESS_AUTH_TOKEN': self.token}), patch('src.openai_compat.OpenAICompatClient.complete', return_value=AssistantTurn('Answer from context', finish_reason='stop')) as complete, redirect_stdout(out):
            self.assertEqual(main(['agent-resume', sid, 'What did you find?']), 0)
        self.assertIn('Answer from context', out.getvalue())
        self.assertIn('category_id', str(complete.call_args.args[0]))
        self.assertNotIn('LEGACY_SINGLE_USER_POLICY', str(complete.call_args.args[0]))
        self.assertEqual(json.loads(path.read_text())['runtime_config']['cwd'], str(self.wa))

    def test_gui_compact_then_cli_list_status_result_without_submit(self):
        app = self.app()
        add_task_id(self.wa, 'analysis', 'recover-a1')
        with TestClient(app) as client:
            client.get('/api/state', headers=self.headers)
            agent = app.state.user_apps[self.a['user_id']].state.agent_state.agent
            with patch.object(agent.client, 'complete', return_value=AssistantTurn('Understood', finish_reason='stop')):
                response = client.post('/api/chat', headers=self.headers,
                                       json={'prompt': 'Only analyze. Do not train or submit another task.'})
                sid = response.json()['session_id']
                for prompt in ('Please remember my restrictions.', 'Find my earlier analysis task when needed.'):
                    response = client.post('/api/chat', headers=self.headers,
                                           json={'prompt': prompt, 'resume_session_id': sid})
                    self.assertEqual(response.status_code, 200)
            with patch.object(agent.client, 'complete', return_value=AssistantTurn(
                '<summary>Only analyze. Do not train or submit another task. '
                'Task ID missing: use analysis List, then Status and Result.</summary>', finish_reason='stop')):
                response = client.post('/api/chat', headers=self.headers,
                                       json={'prompt': '/compact', 'resume_session_id': sid})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('Conversation compacted', response.json()['final_output'])
        saved_path = self.wa / 'sessions' / f'{sid}.json'
        saved = json.loads(saved_path.read_text())
        self.assertTrue(any(message.get('metadata', {}).get('kind') == 'compact_summary' for message in saved['messages']))
        turns = [
            AssistantTurn('', tool_calls=(ToolCall('recover-list', 'list_video_analysis_tasks', {}),)),
            AssistantTurn('', tool_calls=(ToolCall('recover-status', 'get_video_analysis_status', {'task_id': 'recover-a1'}),)),
            AssistantTurn('', tool_calls=(ToolCall('recover-result', 'get_video_analysis_result', {'task_id': 'recover-a1'}),)),
            AssistantTurn('Recovered analysis result; no new task submitted.', finish_reason='stop'),
        ]
        with patch.dict(os.environ, {'AGENT_WORKSPACE': str(self.root), 'HARNESS_AUTH_DIR': str(self.store.directory),
                                    'HARNESS_AUTH_TOKEN': self.token, 'VIDEO_ANALYSIS_API': 'analysis.test'}), patch(
            'src.openai_compat.OpenAICompatClient.complete', side_effect=turns
        ) as model, patch.object(business, '_get_backend_status', return_value='done') as status, patch.object(
            business, '_get_backend_result', return_value=[{'category_id': 3}]
        ) as result, patch.object(business, '_post_local_video') as submit, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['agent-resume', sid, 'Recover my earlier analysis result']), 0)
        submit.assert_not_called()
        status.assert_called_once()
        result.assert_called_once()
        self.assertIn('Do not train or submit another task', str(model.call_args_list[0]))
        self.assertEqual(read_tasks(self.wa, 'analysis'), [{'task_id': 'recover-a1', 'status': 'done'}])
        self.assertIn('category_id', saved_path.read_text())
        self.assertFalse((self.wa / 'tasks').exists())

    def test_expired_token_and_cross_origin_are_rejected(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.post('/api/auth/login', headers={'Origin': 'https://untrusted.test'}, json={'username': 'alice', 'password': 'test-password-alice'}).status_code, 403)
            with self.store.connect() as db:
                db.execute('UPDATE tokens SET expires=0')
            self.assertEqual(client.get('/api/auth/me', headers=self.headers).status_code, 401)

    def test_failed_upload_removes_partial_file(self):
        with TestClient(self.app()) as client, patch.dict(os.environ, {'HARNESS_MAX_UPLOAD_BYTES': '3'}):
            response = client.post('/api/uploads', headers={**self.headers, 'X-Filename': 'large.mp4'}, content=b'1234')
            self.assertEqual(response.status_code, 413)
            self.assertEqual(list((self.wa / 'uploads').iterdir()), [])

    def test_migration_rebinds_session_and_preserves_source(self):
        legacy = Path(self.temp.name) / 'legacy'
        (legacy / 'sessions').mkdir(parents=True)
        scratch = legacy / '.port_sessions' / 'scratchpad' / 's1'
        scratch.mkdir(parents=True)
        (scratch / 'note.txt').write_text('scratch')
        payload = {'session_id': 's1', 'runtime_config': {'cwd': str(legacy)},
                   'scratchpad_directory': str(scratch), 'messages': [{'role': 'tool', 'content': 'historical result'}]}
        source = legacy / 'sessions' / 's1.json'
        source.write_text(json.dumps(payload))
        migrate_user_data(legacy, self.root, self.a['user_id'], apply=True)
        migrated = json.loads((self.wa / 'sessions' / 's1.json').read_text())
        self.assertEqual(migrated['runtime_config']['cwd'], str(self.wa))
        self.assertEqual(migrated['messages'], payload['messages'])
        self.assertEqual((Path(migrated['scratchpad_directory']) / 'note.txt').read_text(), 'scratch')
        self.assertEqual(json.loads(source.read_text()), payload)
        migrate_user_data(legacy, self.root, self.a['user_id'], apply=True)
        (self.wa / 'sessions' / 's1.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            migrate_user_data(legacy, self.root, self.a['user_id'], apply=True)

class PublicAssetsTests(unittest.TestCase):
    def test_static_requests_do_not_construct_user_api_apps(self):
        from src.gui.server import create_user_app
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / 'workspace'
            root.mkdir()
            store = AuthStore(root)
            store.create_user('alice', 'test-password')
            token = store.login('alice', 'test-password')['access_token']
            state = AgentState(cwd=root, model='test', base_url='http://unused.invalid', api_key='fixture',
                               allow_shell=False, allow_write=False, session_directory=root/'sessions')
            with patch('src.gui.server.create_user_app', wraps=create_user_app) as build_user_app:
                app = create_app(state, auth_store=store)
                with TestClient(app) as client:
                    self.assertEqual(build_user_app.call_count, 0)
                    self.assertEqual(client.get('/').status_code, 200)
                    self.assertEqual(client.get('/static/app.js').status_code, 200)
                    self.assertEqual(client.get('/unknown').json()['code'], 'not_found')
                    self.assertEqual(client.get('/api/state').status_code, 401)
                    self.assertEqual(build_user_app.call_count, 0)
                    headers = {'Authorization':'Bearer '+token}
                    self.assertEqual(client.get('/api/state', headers=headers).status_code, 200)
                    self.assertEqual(build_user_app.call_count, 1)
                    self.assertEqual(client.get('/static/app.css').status_code, 200)
                    self.assertEqual(client.get('/api/state', headers=headers).status_code, 200)
                    self.assertEqual(build_user_app.call_count, 1)
