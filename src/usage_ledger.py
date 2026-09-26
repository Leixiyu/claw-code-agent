"""Private, durable per-request token accounting, independent of conversations."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import os
import sqlite3
import time
import threading
import logging
from uuid import uuid4

from .agent_types import UsageStats

TOKEN_FIELDS = tuple(asdict(UsageStats()))
HEARTBEAT_SECONDS = 10
STALE_AFTER_SECONDS = 90
STATE_SQL = """CASE WHEN status != 'pending' THEN status
    WHEN updated_at IS NOT NULL AND updated_at >= ? THEN 'in_progress'
    ELSE 'unresolved' END"""
REASONS = {
    'missing_usage': '模型返回了回答，但未提供完整 Token 用量。',
    'request_failed': '请求异常结束，无法确认完整用量。',
    'stream_interrupted': '输出流异常中断，已收到的用量仍保留。',
    'cancelled': '调用被取消或输出流已关闭，完整用量待核实。',
    'activity_unknown': '请求长时间没有活动记录，可能已中断，完整用量待核实。',
    'legacy_unresolved': '旧版本留下的待核实记录，未保存具体原因。',
    'legacy_pending': '旧版本留下的未完成记录，执行状态和完整用量待核实。',
    'legacy_import': '从旧会话补录的累计值；记录时间是补录时间。',
}



class UsageLedgerError(RuntimeError):
    pass


class UsageLedger:
    def __init__(self, directory: Path):
        self.path = directory / 'usage.sqlite3'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS usage_requests (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
                    created_at REAL NOT NULL, finished_at REAL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'unresolved')),
                    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
                    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_creation_input_tokens >= 0),
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(cache_read_input_tokens >= 0),
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_tokens >= 0)
                );
                CREATE INDEX IF NOT EXISTS usage_user ON usage_requests(user_id);
                CREATE TABLE IF NOT EXISTS usage_session_baselines (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    PRIMARY KEY(user_id, session_id)
                );
            ''')
            # Serialize schema upgrades across CLI/GUI processes. Do not reset data.
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(usage_requests)')}
            for name, definition in (('updated_at', 'REAL'), ('reason', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE usage_requests ADD COLUMN {name} {definition}')
            db.execute('CREATE INDEX IF NOT EXISTS usage_user_time ON usage_requests(user_id, created_at DESC, request_id DESC)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        try:
            with sqlite3.connect(self.path, timeout=30) as db:
                db.row_factory = sqlite3.Row
                # SQLite's default rollback journal + FULL sync is durable and
                # supports the CLI and GUI writing from separate processes.
                db.execute('PRAGMA synchronous=FULL')
                yield db
        except sqlite3.Error as exc:
            raise UsageLedgerError('Token usage ledger unavailable; model request cannot continue.') from exc
        finally:
            if 'db' in locals():
                db.close()

    def ensure_session(self, user_id: str, session_id: str, directory: Path) -> bool:
        """Import a legacy cumulative snapshot exactly once, before new calls.

        The session lock serializes this with turns and deletion. An empty
        baseline is also recorded for new sessions so later scans cannot import
        usage that has already been recorded per request.
        """
        from .session_lifecycle import session_guard, session_path
        from .session_store import read_agent_session, usage_from_payload
        with self.connect() as db:
            if db.execute('SELECT 1 FROM usage_session_baselines WHERE user_id=? AND session_id=?',
                          (user_id, session_id)).fetchone():
                return False
        with session_guard(directory, session_id):
            path = session_path(directory, session_id)
            stored = read_agent_session(session_id, directory=directory) if path.exists() else None
            usage = usage_from_payload(stored.usage) if stored else UsageStats()
            with self.connect() as db:
                inserted = db.execute('INSERT OR IGNORE INTO usage_session_baselines VALUES (?, ?)',
                                      (user_id, session_id)).rowcount
                if inserted and usage.total_tokens:
                    values = self._values(usage)
                    db.execute('''INSERT INTO usage_requests
                        (request_id,user_id,session_id,run_id,model,purpose,created_at,finished_at,status,
                         input_tokens,output_tokens,cache_creation_input_tokens,cache_read_input_tokens,reasoning_tokens)
                        VALUES (?,?,?,?,?,'legacy_import',?,?,'confirmed',?,?,?,?,?)''',
                        (f'legacy:{user_id}:{session_id}', user_id, session_id, 'legacy',
                         str(stored.model_config.get('model', 'unknown')), time.time(), time.time(), *values))
                return bool(inserted)

    def import_sessions(self, user_id: str, directory: Path) -> dict:
        report = {'imported_sessions': 0, 'skipped_sessions': 0}
        for path in directory.glob('*.json'):
            try:
                report['imported_sessions'] += self.ensure_session(user_id, path.stem, directory)
            except (OSError, ValueError, KeyError, TypeError):
                # Unreadable or busy legacy sessions are reported, never counted as zero.
                report['skipped_sessions'] += 1
        return report

    @staticmethod
    def _values(usage: UsageStats):
        values = tuple(getattr(usage, key) for key in TOKEN_FIELDS)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
            raise UsageLedgerError('Invalid token usage received from model backend.')
        return values

    def begin(self, user_id: str, session_id: str, run_id: str, model: str, purpose: str) -> str:
        request_id = uuid4().hex
        with self.connect() as db:
            db.execute('''INSERT INTO usage_requests
                (request_id,user_id,session_id,run_id,model,purpose,created_at,updated_at,status)
                VALUES (?,?,?,?,?,?,?,?, 'pending')''',
                (request_id, user_id, session_id, run_id, model, purpose, time.time(), time.time()))
        return request_id

    def record(self, request_id: str, usage: UsageStats):
        # Usage events from OpenAI-compatible streams are cumulative snapshots.
        # Replace the snapshot; never add repeated provider events together.
        assignments = ','.join(f'{key}=?' for key in TOKEN_FIELDS)
        with self.connect() as db:
            db.execute(f'UPDATE usage_requests SET {assignments}, updated_at=? WHERE request_id=? AND status=\'pending\'',
                       (*self._values(usage), time.time(), request_id))

    def finish(self, request_id: str, *, reported: bool, reason: str | None = None):
        with self.connect() as db:
            db.execute("""UPDATE usage_requests SET status=?, finished_at=?, updated_at=?, reason=?
                          WHERE request_id=? AND status='pending' """,
                       ('confirmed' if reported else 'unresolved', time.time(), time.time(),
                        None if reported else reason or 'missing_usage', request_id))

    @contextmanager
    def tracking(self, request_id: str):
        """Heartbeat blocked network calls too; never mistake a long call for a dead one."""
        stopped = threading.Event()
        def heartbeat():
            while not stopped.wait(HEARTBEAT_SECONDS):
                try:
                    with self.connect() as db:
                        db.execute("UPDATE usage_requests SET updated_at=? WHERE request_id=? AND status='pending'",
                                   (time.time(), request_id))
                except UsageLedgerError:
                    logging.getLogger(__name__).warning('Could not update usage request heartbeat')
        thread = threading.Thread(target=heartbeat, daemon=True, name='usage-heartbeat')
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    def summary(self, user_id: str) -> dict:
        columns = ','.join(f'COALESCE(SUM({key}),0) AS {key}' for key in TOKEN_FIELDS)
        with self.connect() as db:
            row = db.execute(f"""SELECT {columns}, COUNT(*) AS record_count,
                COALESCE(SUM(purpose != 'legacy_import'),0) AS request_count,
                COALESCE(SUM(effective_status = 'in_progress'),0) AS active_requests,
                COALESCE(SUM(effective_status = 'unresolved'),0) AS unresolved_requests,
                COALESCE(SUM(CASE WHEN purpose='legacy_import' THEN
                    input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens ELSE 0 END),0)
                    AS imported_tokens FROM
                (SELECT *, {STATE_SQL} AS effective_status FROM usage_requests WHERE user_id=?)""",
                (time.time() - STALE_AFTER_SECONDS, user_id)).fetchone()
        result = dict(row)
        result['user_id'] = user_id
        result['pending_requests'] = result['active_requests'] + result['unresolved_requests']
        result['total_tokens'] = UsageStats(**{key: result[key] for key in TOKEN_FIELDS}).total_tokens
        return result

    def requests(self, user_id: str, *, limit=20, offset=0, session_id=None, status=None) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError('offset must be non-negative')
        if status not in (None, 'in_progress', 'confirmed', 'unresolved'):
            raise ValueError('invalid usage request status')
        # One clock cutoff and read transaction keep page count/items consistent.
        params = [time.time() - STALE_AFTER_SECONDS, user_id]
        query = f'(SELECT *, {STATE_SQL} AS effective_status FROM usage_requests WHERE user_id=?)'
        filters = []
        if session_id is not None:
            filters.append('session_id=?'); params.append(session_id)
        if status is not None:
            filters.append('effective_status=?'); params.append(status)
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        with self.connect() as db:
            db.execute('BEGIN')
            total = db.execute(f'SELECT COUNT(*) FROM {query}{where}', params).fetchone()[0]
            rows = db.execute(f'SELECT * FROM {query}{where} ORDER BY created_at DESC, request_id DESC LIMIT ? OFFSET ?',
                              (*params, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = {key: row[key] for key in ('request_id', 'session_id', 'run_id', 'model', 'purpose',
                                            'created_at', 'finished_at', 'updated_at', *TOKEN_FIELDS)}
            item['status'] = row['effective_status']
            reason = row['reason']
            if row['status'] == 'unresolved' and reason is None:
                reason = 'legacy_unresolved'
            if row['status'] == 'pending' and item['status'] == 'unresolved':
                reason = 'activity_unknown' if row['updated_at'] is not None else 'legacy_pending'
            if row['purpose'] == 'legacy_import':
                reason = 'legacy_import'
            item['reason'] = reason
            item['reason_message'] = REASONS.get(reason, '')
            item['total_tokens'] = UsageStats(**{key: row[key] for key in TOKEN_FIELDS}).total_tokens
            items.append(item)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
                'has_more': offset + len(items) < total}


class MeteredClient:
    """Record before network I/O; retain pending evidence after crashes/failures."""
    def __init__(self, client, ledger, user_id, session_id, run_id, model, purpose):
        self.client = client
        self.ledger = ledger
        self.identity = (user_id, session_id, run_id, model, purpose)

    def complete(self, *args, **kwargs):
        request_id = self.ledger.begin(*self.identity)
        reported = False
        reason = 'request_failed'
        try:
            with self.ledger.tracking(request_id):
                turn = self.client.complete(*args, **kwargs)
                self.ledger.record(request_id, turn.usage)
                reported = (turn.usage_reported if turn.usage_reported is not None else bool(turn.usage.total_tokens))
                reason = 'missing_usage'
                return turn
        except (KeyboardInterrupt, GeneratorExit):
            reason = 'cancelled'
            raise
        finally:
            self.ledger.finish(request_id, reported=reported, reason=reason)

    def stream(self, *args, **kwargs):
        request_id = self.ledger.begin(*self.identity)
        reported = False
        completed = False
        reason = 'stream_interrupted'
        try:
            with self.ledger.tracking(request_id):
                for event in self.client.stream(*args, **kwargs):
                    if event.type == 'usage':
                        self.ledger.record(request_id, event.usage)
                        reported = event.usage_reported if event.usage_reported is not None else True
                    yield event
                completed = True
                reason = 'missing_usage'
        except (KeyboardInterrupt, GeneratorExit):
            reason = 'cancelled'
            raise
        finally:
            self.ledger.finish(request_id, reported=reported and completed, reason=reason)
