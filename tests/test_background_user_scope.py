"""User-scoped background commands and GUI boundary regression tests."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.auth_runtime import AuthStore
from src.background_runtime import BackgroundSessionRecord, BackgroundSessionRuntime, _process_identity, _is_process_running
from src.gui.server import AgentState, create_app, create_user_app
from src.main import main


class BackgroundUserScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.store = AuthStore(self.workspace, self.root / 'auth')
        self.alice = self.store.create_user('alice', 'password-alice')
        self.bob = self.store.create_user('bob', 'password-bob')
        self.wa = self.workspace / 'users' / self.alice['user_id']
        self.wb = self.workspace / 'users' / self.bob['user_id']
        self.a = BackgroundSessionRuntime.for_workspace(self.wa)
        self.b = BackgroundSessionRuntime.for_workspace(self.wb)
        self.token = self.store.login('alice', 'password-alice')['access_token']
        self.env = {'AGENT_WORKSPACE': str(self.workspace), 'HARNESS_AUTH_DIR': str(self.store.directory),
                    'HARNESS_AUTH_TOKEN': self.token}

    def record(self, runtime, sid='bg_demo', **changes):
        record = BackgroundSessionRecord(sid, 0, 'test', str(runtime.workspace), 'test', 'agent',
            'completed', str(runtime.log_path(sid)), str(runtime.record_path(sid)), '2026-09-20', ())
        record = replace(record, **changes)
        runtime.save_record(record)
        runtime.log_path(sid).write_text(f'{sid} private log')
        return record

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_cli_aliases_share_user_directory_and_clean_errors(self):
        self.record(self.a, 'bg_alice')
        self.record(self.b, 'bg_bob')
        for prefix in (['agent-ps'], ['daemon', 'ps']):
            code, output, _ = self.run_cli(prefix)
            self.assertEqual(code, 0)
            self.assertIn('bg_alice', output)
            self.assertNotIn('bg_bob', output)
        for operation in ('logs', 'attach', 'kill'):
            for prefix in ([f'agent-{operation}'], ['daemon', operation]):
                with self.subTest(prefix=prefix):
                    self.assertEqual(self.run_cli([*prefix, 'bg_alice'])[0], 0)
                    for sid in ('bg_bob', '../bg_alice', str(self.b.record_path('bg_bob'))):
                        code, _, error = self.run_cli([*prefix, sid])
                        self.assertEqual(code, 1)
                        self.assertNotIn('Traceback', error)
        # Direct worker calls must not overwrite a record or invoke a model.
        original = self.a.record_path('bg_alice').read_bytes()
        with patch('src.main._build_agent') as build:
            for prefix in (['agent-bg-worker'], ['daemon', 'worker']):
                code, _, _ = self.run_cli([*prefix, 'bg_alice', '/help', '--background-root', str(self.b.root)])
                self.assertEqual(code, 1)
            build.assert_not_called()
        self.assertEqual(original, self.a.record_path('bg_alice').read_bytes())

    def test_records_logs_and_session_paths_cannot_cross_users(self):
        record = self.record(self.a)
        other = self.record(self.b, 'bg_bob')
        attacks = [
            {'background_id': 'bg_bob'}, {'workspace_cwd': str(self.wb)},
            {'record_path': other.record_path}, {'log_path': other.log_path},
            {'session_id': 'other', 'session_path': str(self.wb / 'sessions' / 'other.json')},
            {'pid': -1, 'status': 'running'},
        ]
        for changes in attacks:
            with self.subTest(changes=changes), patch('src.background_runtime.os.killpg') as kill:
                path = self.a.record_path(record.background_id)
                path.write_text(json.dumps(asdict(replace(record, **changes))))
                for action in (self.a.load_record, self.a.read_logs, self.a.kill):
                    with self.assertRaises(ValueError):
                        action(record.background_id)
                self.assertEqual(self.a.list_records(), ())
                kill.assert_not_called()
        self.a.record_path(record.background_id).write_text(json.dumps(asdict(record)))
        self.assertEqual(self.a.read_logs(record.background_id), 'bg_demo private log')

    def test_symlinks_hardlinks_and_malformed_records_are_rejected(self):
        other = self.record(self.b, 'bg_bob')
        (self.a.root / 'bg_link.json').symlink_to(other.record_path)
        os.link(other.record_path, self.a.root / 'bg_hard.json')
        for sid in ('bg_link', 'bg_hard'):
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                self.a.load_record(sid)
        (self.a.root / 'bg_bad.json').write_text('invalid')
        (self.a.root / 'bg_array.json').write_text('[]')
        self.assertEqual(self.a.list_records(), ())
        self.record(self.a, 'bg_log')
        self.a.log_path('bg_log').unlink()
        (self.a.root / 'bg_log.log').symlink_to(other.log_path)
        with self.assertRaises(ValueError):
            self.a.read_logs('bg_log')
        outside = self.root / 'other-workspace'
        (outside / '.port_sessions').mkdir(parents=True)
        (outside / '.port_sessions' / 'background').symlink_to(self.b.root)
        with self.assertRaises(ValueError):
            BackgroundSessionRuntime.for_workspace(outside)

    def test_kill_refuses_unverified_or_reused_process_ids(self):
        record = self.record(self.a, pid=22222, status='running', process_identity='original')
        with patch('src.background_runtime._is_process_running', return_value=True), patch(
                'src.background_runtime._process_identity', return_value='different'), patch(
                'src.background_runtime.os.killpg') as signal_process:
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.a.kill(record.background_id)
            signal_process.assert_not_called()
        self.a.save_record(replace(record, process_identity=None))
        with patch('src.background_runtime._is_process_running', return_value=True), patch(
                'src.background_runtime.os.killpg') as signal_process:
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.a.kill(record.background_id)
            signal_process.assert_not_called()

    def test_daemon_and_agent_kill_stop_only_test_processes(self):
        # These are dedicated disposable child processes, never production PIDs.
        for prefix in (['daemon', 'kill'], ['agent-kill']):
            record = self.a.launch([sys.executable, '-c', 'import time; time.sleep(30)'],
                prompt='test sleeper', workspace_cwd=self.wa, model='test')
            try:
                # Separate CLI process: it cannot waitpid/reap the test child.
                project = Path(__file__).resolve().parents[1]
                result = subprocess.run([sys.executable, '-B', '-m', 'src.main', *prefix, record.background_id],
                    cwd=project, env={**os.environ, **self.env}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('status=killed', result.stdout)
            finally:
                from src.background_runtime import _DETACHED_PROCESSES
                process = _DETACHED_PROCESSES.pop(record.pid, None)
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_daemon_start_worker_finishes_and_records_survive_concurrent_reads(self):
        code, output, error = self.run_cli(['daemon', 'start', '/help'])
        self.assertEqual(code, 0, error)
        sid = next(line.split('=', 1)[1] for line in output.splitlines() if line.startswith('background_id='))
        for _ in range(100):
            record = self.a.load_record(sid)
            if record.status != 'running':
                break
            time.sleep(.05)
        self.assertEqual(record.status, 'completed', self.a.read_logs(sid))
        self.assertIn('# Slash Commands', self.a.read_logs(sid))
        self.assertEqual(self.a.record_path(sid).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.a.log_path(sid).stat().st_mode & 0o777, 0o600)

    def test_gui_public_boundary_and_internal_router_errors(self):
        def state(cwd):
            return AgentState(cwd=cwd, model='test', base_url='http://127.0.0.1:1', api_key='test',
                              allow_shell=False, allow_write=False, session_directory=cwd / 'sessions')
        self.record(self.a, 'bg_alice')
        self.record(self.b, 'bg_bob')
        with TestClient(create_app(state(self.workspace), auth_store=self.store)) as client:
            self.assertEqual(client.get('/api/background').status_code, 401)
            login = client.post('/api/auth/login', json={'username': 'alice', 'password': 'password-alice'})
            self.assertEqual(login.status_code, 200)
            for method, url in (('get', '/api/background'), ('get', '/api/background/bg_bob/logs'),
                                ('post', '/api/background/bg_bob/kill')):
                self.assertEqual(getattr(client, method)(url).status_code, 403)
            self.assertEqual(client.get('/api/state').status_code, 200)
            self.assertEqual(client.get('/api/sessions').status_code, 200)
            self.assertEqual(client.post('/api/chat', json={'prompt': '/help'}).status_code, 200)
        with TestClient(create_user_app(state(self.wa))) as client:
            listing = client.get('/api/background').json()['sessions']
            self.assertEqual([item['background_id'] for item in listing], ['bg_alice'])
            self.assertEqual(client.get('/api/background/bg_bob/logs').status_code, 404)
            self.assertEqual(client.get('/api/background/invalid').status_code, 400)
            path = self.a.record_path('bg_alice')
            data = json.loads(path.read_text())
            data['log_path'] = str(self.b.log_path('bg_bob'))
            path.write_text(json.dumps(data))
            self.assertEqual(client.get('/api/background/bg_alice/logs').status_code, 400)
            self.assertEqual(client.post('/api/background/bg_alice/kill').status_code, 400)

    def test_all_background_commands_require_login(self):
        self.env['HARNESS_AUTH_TOKEN'] = ''
        commands = [['agent-bg', '/help'], ['agent-bg-worker', 'bg_x', '/help', '--background-root', str(self.a.root)],
                    ['agent-ps'], ['agent-logs', 'bg_x'], ['agent-attach', 'bg_x'], ['agent-kill', 'bg_x'],
                    ['daemon', 'start', '/help'], ['daemon', 'worker', 'bg_x', '/help', '--background-root', str(self.a.root)],
                    ['daemon', 'ps'], ['daemon', 'logs', 'bg_x'], ['daemon', 'attach', 'bg_x'], ['daemon', 'kill', 'bg_x']]
        with patch('src.background_runtime.subprocess.Popen') as launch:
            for args in commands:
                with self.subTest(args=args):
                    self.assertEqual(self.run_cli(args)[0], 1)
            launch.assert_not_called()

    def test_linux_process_identity_and_zombie_detection(self):
        fields = ['S', '1', '42'] + ['0'] * 17
        fields[19] = '9876'
        data = '42 (worker (name)) ' + ' '.join(fields)
        with patch('src.background_runtime.sys.platform', 'linux'), patch.object(
                Path, 'read_text', side_effect=[data, 'boot-id\n']), patch.object(
                Path, 'stat', return_value=SimpleNamespace(st_uid=1000)):
            self.assertEqual(_process_identity(42), 'linux:boot-id:1000:9876:42')
        with patch('src.background_runtime.sys.platform', 'linux'), patch.object(
                Path, 'read_text', return_value='42 (worker) Z 0'), patch(
                'src.background_runtime.os.kill') as send_signal:
            self.assertFalse(_is_process_running(42))
            send_signal.assert_not_called()

    def test_publish_failure_reaps_its_own_child(self):
        from src.background_runtime import _DETACHED_PROCESSES
        original = set(_DETACHED_PROCESSES)
        with patch('src.background_runtime.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.a.launch([sys.executable, '-c', 'import time; time.sleep(30)'],
                              prompt='test', workspace_cwd=self.wa, model='test')
        self.assertEqual(set(_DETACHED_PROCESSES), original)
