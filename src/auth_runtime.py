"""Local prototype authentication; storage must be outside AGENT_WORKSPACE."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from uuid import uuid4

from .user_workspace import initialize_user


class AuthenticationError(ValueError):
    pass


class AuthStore:
    def __init__(self, workspace: Path, directory: Path | None = None):
        self.workspace = workspace.resolve()
        configured = directory or os.environ.get('HARNESS_AUTH_DIR')
        self.directory = Path(configured).expanduser().resolve() if configured else (
            self.workspace.parent / f'.{self.workspace.name}-auth'
        )
        if self.directory == self.workspace or self.directory.is_relative_to(self.workspace):
            raise ValueError('HARNESS_AUTH_DIR must be outside AGENT_WORKSPACE')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'auth.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    expires REAL NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def password_hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()

    def create_user(self, username: str, password: str) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
            raise ValueError('username must contain 1–64 letters, digits, _, . or -')
        if len(password) < 8:
            raise ValueError('password must have at least 8 characters')
        user_id, salt = uuid4().hex, secrets.token_hex(16)
        try:
            with self.connect() as db:
                db.execute('INSERT INTO users VALUES (?, ?, ?, ?)', (
                    user_id, username, salt, self.password_hash(password, salt)))
        except sqlite3.IntegrityError as exc:
            raise ValueError('username already exists') from exc
        initialize_user(self.workspace, user_id)
        return {'user_id': user_id, 'username': username}

    def login(self, username: str, password: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            salt = row['salt'] if row else '00' * 16
            digest = self.password_hash(password, salt)
            if row is None or not hmac.compare_digest(digest, row['password_hash']):
                raise AuthenticationError('invalid username or password')
            token = secrets.token_urlsafe(32)
            expires = time.time() + 12 * 60 * 60
            db.execute('DELETE FROM tokens WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO tokens VALUES (?, ?, ?)', (
                hashlib.sha256(token.encode()).hexdigest(), row['user_id'], expires))
        return {'access_token': token, 'token_type': 'bearer', 'expires_at': expires,
                'user': {'user_id': row['user_id'], 'username': row['username']}}

    def authenticate(self, token: str) -> dict:
        with self.connect() as db:
            row = db.execute('''SELECT u.user_id, u.username FROM tokens t
                JOIN users u ON u.user_id=t.user_id WHERE token_hash=? AND expires>?''',
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if row is None:
            raise AuthenticationError('login required or token expired')
        return dict(row)

    def logout(self, token: str) -> None:
        with self.connect() as db:
            db.execute('DELETE FROM tokens WHERE token_hash=?',
                       (hashlib.sha256(token.encode()).hexdigest(),))

    def find_user(self, username: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT user_id, username FROM users WHERE username=?', (username,)).fetchone()
        if row is None:
            raise ValueError('unknown username')
        return dict(row)
