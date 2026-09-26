"""Request detail pagination, old-schema upgrades and usage activity states."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from src.agent_types import AssistantTurn, UsageStats
from src.auth_runtime import AuthStore
from src.gui.server import AgentState, create_app
from src.main import main
from src.usage_ledger import UsageLedger, MeteredClient, STALE_AFTER_SECONDS


class UsageRequestsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ledger = UsageLedger(self.root / 'auth')

    def test_states_reasons_and_pagination_keep_original_totals(self):
        active = self.ledger.begin('a', 's1', 'r', 'm', 'chat')
        old = self.ledger.begin('a', 's2', 'r', 'm', 'chat')
        unknown = self.ledger.begin('a', 's1', 'r', 'm', 'compact')
        done = self.ledger.begin('a', 's1', 'r', 'm', 'chat')
        self.ledger.record(old, UsageStats(input_tokens=4))
        self.ledger.record(done, UsageStats(input_tokens=10, output_tokens=2))
        self.ledger.finish(done, reported=True)
        self.ledger.finish(unknown, reported=False, reason='missing_usage')
        with self.ledger.connect() as db:
            db.execute('UPDATE usage_requests SET updated_at=? WHERE request_id=?', (time.time()-STALE_AFTER_SECONDS-1, old))
        summary = self.ledger.summary('a')
        self.assertEqual((summary['active_requests'], summary['unresolved_requests'], summary['pending_requests']), (1,2,3))
        self.assertEqual(summary['total_tokens'], 16)
        unresolved = self.ledger.requests('a', status='unresolved')
        self.assertEqual({i['reason'] for i in unresolved['items']}, {'missing_usage', 'activity_unknown'})
        self.assertTrue(all(i['reason_message'] for i in unresolved['items']))
        page1 = self.ledger.requests('a', limit=2)
        page2 = self.ledger.requests('a', limit=2, offset=2)
        self.assertTrue(page1['has_more']); self.assertFalse(page2['has_more'])
        self.assertEqual(len({i['request_id'] for i in page1['items'] + page2['items']}), 4)
        self.assertEqual(self.ledger.requests('b')['total'], 0)
        self.assertEqual(self.ledger.requests('a', session_id='s1')['total'], 3)
        self.ledger.record(old, UsageStats(input_tokens=5))
        self.ledger.finish(old, reported=True)
        self.assertEqual(self.ledger.summary('a')['unresolved_requests'], 1)

    def test_old_schema_upgrade_preserves_data_and_marks_legacy_pending_unknown(self):
        olddir = self.root / 'old'; olddir.mkdir()
        with sqlite3.connect(olddir / 'usage.sqlite3') as db:
            db.execute('''CREATE TABLE usage_requests (request_id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,
                run_id TEXT,model TEXT,purpose TEXT,created_at REAL,finished_at REAL,status TEXT,
                input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,
                cache_creation_input_tokens INTEGER DEFAULT 0,cache_read_input_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0)''')
            db.execute("INSERT INTO usage_requests(request_id,user_id,session_id,run_id,model,purpose,created_at,status,input_tokens) VALUES ('old','a','s','r','m','chat',0,'pending',7)")
        ledger = UsageLedger(olddir)
        UsageLedger(olddir)  # Reopening does not alter or double import records.
        self.assertEqual(ledger.summary('a')['total_tokens'], 7)
        item = ledger.requests('a')['items'][0]
        self.assertEqual(item['reason'], 'legacy_pending')
        self.assertEqual(item['status'], 'unresolved')

    def test_heartbeat_updates_blocked_calls_and_failure_reason_is_private(self):
        request_id = self.ledger.begin('a', 's', 'r', 'm', 'chat')
        with self.ledger.connect() as db:
            db.execute('UPDATE usage_requests SET updated_at=0 WHERE request_id=?', (request_id,))
        with patch('src.usage_ledger.HEARTBEAT_SECONDS', .01), self.ledger.tracking(request_id):
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and self.ledger.summary('a')['active_requests'] == 0:
                time.sleep(.01)
            self.assertEqual(self.ledger.summary('a')['active_requests'], 1)
        client = Mock(); client.complete.side_effect = RuntimeError('secret-provider-response')
        with self.assertRaises(RuntimeError):
            MeteredClient(client, self.ledger, 'a', 's', 'r', 'm', 'chat').complete([], [])
        item = self.ledger.requests('a', status='unresolved')['items'][0]
        self.assertEqual(item['reason'], 'request_failed')
        self.assertNotIn('secret-provider', json.dumps(item))

    def test_api_and_cli_details_are_user_scoped_and_validated(self):
        workspace = self.root / 'workspace'; workspace.mkdir()
        store = AuthStore(workspace, self.root / 'auth')
        user = store.create_user('alice', 'test-password')
        token = store.login('alice', 'test-password')['access_token']
        request_id = self.ledger.begin(user['user_id'], 'deleted-session', 'r', 'm', 'chat')
        self.ledger.finish(request_id, reported=False, reason='missing_usage')
        self.ledger.begin('someone-else', 'private', 'r', 'm', 'chat')
        state = AgentState(cwd=workspace, model='test', base_url='http://unused.invalid', api_key='x',
                           allow_shell=False, allow_write=False, session_directory=workspace/'sessions')
        with TestClient(create_app(state, auth_store=store)) as client:
            headers = {'Authorization': 'Bearer ' + token}
            page = client.get('/api/usage/requests?status=unresolved&user_id=someone-else', headers=headers).json()
            self.assertEqual(page['total'],1)
            self.assertEqual(page['items'][0]['session_id'], 'deleted-session')
            invalid = client.get('/api/usage/requests?limit=101', headers=headers)
            self.assertEqual(invalid.status_code,422)
            self.assertEqual(invalid.json()['code'],'validation_error')
        output=io.StringIO()
        with patch.dict(os.environ, {'HARNESS_AUTH_TOKEN':token, 'HARNESS_AUTH_DIR':str(store.directory)}), redirect_stdout(output):
            code=main(['usage','--workspace-root',str(workspace),'--details','--status','unresolved','--json'])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['requests'],page)
