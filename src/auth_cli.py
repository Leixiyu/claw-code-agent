"""CLI login and administrator-only local account provisioning."""
from __future__ import annotations
import getpass
import json
import os
from pathlib import Path

from .auth_runtime import AuthStore, AuthenticationError
from .user_workspace import atomic_json, initialize_user


def add_auth_commands(subparsers):
    for name in ('users-create', 'login', 'logout', 'whoami', 'migrate-user-data'):
        parser = subparsers.add_parser(name, help=f'Prototype user management: {name}')
        parser.add_argument('--workspace-root', default=os.environ.get('AGENT_WORKSPACE') or '.')
        if name in {'users-create', 'login', 'migrate-user-data'}:
            parser.add_argument('username')
        if name == 'migrate-user-data':
            parser.add_argument('--source', required=True)
            parser.add_argument('--apply', action='store_true', help='Copy into user workspace; originals are never deleted')


def cli_token(store):
    token = os.environ.get('HARNESS_AUTH_TOKEN')
    if not token:
        try:
            token = json.loads((store.directory / 'cli-token.json').read_text())['access_token']
        except (OSError, ValueError, KeyError):
            raise AuthenticationError('Please run claw-code-agent login USERNAME first')
    return token


def handle_auth_command(args):
    store = AuthStore(Path(args.workspace_root))
    if args.command == 'users-create':
        password = getpass.getpass('New password: ')
        if password != getpass.getpass('Confirm password: '):
            raise ValueError('passwords do not match')
        print(json.dumps(store.create_user(args.username, password)))
    elif args.command == 'login':
        payload = store.login(args.username, getpass.getpass('Password: '))
        atomic_json(store.directory / 'cli-token.json', {'access_token': payload['access_token']})
        print(f"Logged in as {payload['user']['username']}")
    elif args.command == 'logout':
        store.logout(cli_token(store))
        (store.directory / 'cli-token.json').unlink(missing_ok=True)
        print('Logged out')
    elif args.command == 'whoami':
        print(json.dumps(store.authenticate(cli_token(store))))
    else:
        from .user_migration import migrate_user_data
        user = store.find_user(args.username)
        print(json.dumps(migrate_user_data(Path(args.source), store.workspace,
                                          user['user_id'], apply=args.apply), indent=2))
    return 0


def prepare_user_args(args):
    root = Path(getattr(args, 'cwd', None) or os.environ.get('AGENT_WORKSPACE') or '.').resolve()
    store = AuthStore(root)
    token = cli_token(store)
    user = store.authenticate(token)
    workspace = initialize_user(root, user['user_id'])
    args._user_id = user['user_id']
    args._workspace_container = root
    args._auth_token = token
    args.cwd = str(workspace)
    args._user_workspace = workspace
    args.background_root = str(workspace / '.port_sessions' / 'background')
