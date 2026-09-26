"""CLI login and administrator-only local account provisioning."""
from __future__ import annotations
import getpass
import argparse
import json
import os
from pathlib import Path

from .auth_runtime import AuthStore, AuthenticationError
from .user_workspace import atomic_json, initialize_user


AUTH_COMMANDS = (
    'users-create', 'login', 'logout', 'whoami', 'migrate-user-data',
    'sessions', 'session-info', 'session-delete', 'sessions-clear', 'usage',
)


def _positive_limit(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('limit must be a positive integer')
    return number


def add_auth_commands(subparsers):
    for name in AUTH_COMMANDS:
        help_text = ('Show a saved session summary for the logged-in user (does not resume chat)'
                     if name == 'session-info' else f'Prototype user management: {name}')
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument('--workspace-root', default=os.environ.get('AGENT_WORKSPACE') or '.')
        if name in {'session-info', 'session-delete'}:
            parser.add_argument('session_id')
        if name in {'session-delete', 'sessions-clear'}:
            parser.add_argument('--yes', action='store_true', help='Confirm permanent deletion without prompting')
        if name == 'usage':
            parser.add_argument('--json', action='store_true', help='Print token usage as JSON')
            parser.add_argument('--details', action='store_true', help='Show paginated model request usage')
            parser.add_argument('--limit', type=_positive_limit, default=20)
            parser.add_argument('--offset', type=int, default=0)
            parser.add_argument('--session-id')
            parser.add_argument('--status', choices=('in_progress', 'confirmed', 'unresolved'))
        if name == 'sessions':
            parser.add_argument('--limit', type=_positive_limit, default=20)
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
    elif args.command == 'usage':
        user = store.authenticate(cli_token(store))
        summary = store.usage_summary(user['user_id'])
        if not args.details and (args.session_id or args.status or args.offset or args.limit != 20):
            raise ValueError('Use --details with request filters or pagination.')
        requests = None
        if args.details:
            requests = store.usage_ledger().requests(user['user_id'], limit=args.limit, offset=args.offset,
                                                     session_id=args.session_id, status=args.status)
        if args.json:
            print(json.dumps({**summary, **({'requests': requests} if requests is not None else {})}, ensure_ascii=False, indent=2))
        else:
            print(f"当前用户：{user['username']}")
            print(f"历史总 Token 用量（total_tokens）：{summary['total_tokens']:,}")
            for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                        'cache_creation_input_tokens', 'reasoning_tokens'):
                print(f"{key}={summary[key]}")
            print(f"历史补录 Token：{summary['imported_tokens']:,}")
            print(f"进行中：{summary['active_requests']}；待核实：{summary['unresolved_requests']}；未补录会话：{summary['history_import_skipped']}")
            print('包括已删除对话；reasoning 已包含在 output 中，不重复计入总量。')
            if requests is not None:
                from datetime import datetime, timezone
                labels = {'in_progress': '进行中', 'confirmed': '用量已确认', 'unresolved': '待核实'}
                print(f"请求明细：共 {requests['total']} 条，从第 {requests['offset'] + 1} 条起")
                for item in requests['items']:
                    timestamp = datetime.fromtimestamp(item['created_at'], timezone.utc).isoformat()
                    print(f"{timestamp} {item['request_id']} session={item['session_id']} model={item['model']} tokens={item['total_tokens']} {labels[item['status']]}")
                    if item['reason_message']:
                        print('  ' + item['reason_message'])
    elif args.command in {'session-delete', 'sessions-clear'}:
        from .session_lifecycle import (clear_saved_sessions, delete_saved_session,
                                        session_deletion_candidates, session_path)
        user = store.authenticate(cli_token(store))
        store.usage_summary(user['user_id'])  # Preserve legacy totals before deletion.
        # Preserve the lexical path so lifecycle checks can detect a symlink
        # redirect to another user's directory, even inside the same root.
        directory = store.workspace / 'users' / user['user_id'] / 'sessions'
        session_path(directory, '_scope_check')
        if args.command == 'session-delete':
            if not session_path(directory, args.session_id).is_file():
                raise ValueError('Session not found for the current user.')
            candidates = [args.session_id]
        else:
            candidates = session_deletion_candidates(directory)
        if not candidates:
            print('当前用户没有可删除的会话。')
            return 0
        print(f"用户 {user['username']}：将永久删除 {len(candidates)} 个会话，无法恢复。")
        if args.command == 'session-delete':
            print(f'Session ID: {args.session_id}')
        print('只删除会话 JSON；保留 uploads、业务任务索引和 scratchpad，运行中的会话不删除。')
        if not args.yes:
            try:
                confirmed = input('确认永久删除？[y/N] ').strip().lower() in {'y', 'yes'}
            except EOFError:
                confirmed = False
            if not confirmed:
                print('已取消，未删除任何会话。')
                return 0
        # The login can expire while the user is reading the confirmation.
        store.authenticate(cli_token(store))
        report = (delete_saved_session(directory, candidates[0]) if args.command == 'session-delete'
                  else clear_saved_sessions(directory, candidates))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get('errors') else 0
    elif args.command == 'session-info':
        from .session_catalog import get_session_info
        from .user_workspace import contained
        user = store.authenticate(cli_token(store))
        directory = contained(store.workspace, store.workspace / 'users' / user['user_id'] / 'sessions')
        try:
            info = get_session_info(args.session_id, directory=directory)
        except FileNotFoundError as exc:
            raise ValueError('Session not found for the current user, or invalid session ID.') from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Cannot read session: invalid or unsupported session data.') from exc
        print(f"{info['session_id']}\n{info['message_count']} messages\nin={info['input_tokens']} out={info['output_tokens']}")
    elif args.command == 'sessions':
        from .session_catalog import list_saved_sessions
        from .user_workspace import contained
        user = store.authenticate(cli_token(store))
        directory = contained(store.workspace, store.workspace / 'users' / user['user_id'] / 'sessions')
        listing = list_saved_sessions(directory, limit=args.limit)
        print(f"当前用户：{user['username']}")
        print(f"共 {listing['total']} 个会话，显示 {len(listing['sessions'])} 个（北京时间）")
        if not listing['sessions']:
            print('当前用户暂无可读取的已保存会话。')
        else:
            print('更新时间             Session ID                        会话预览')
            for item in listing['sessions']:
                preview = item['preview'] or '无原始会话预览'
                print(f"{item['modified_at_display']}  {item['session_id']}  {preview}")
            print('\n继续聊天：claw-code-agent agent-chat --resume-session-id <session_id>')
        if listing['skipped']:
            print(f"已跳过 {listing['skipped']} 个损坏或不安全的会话文件。")
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
    args._usage_ledger = store.usage_ledger()
    args._usage_ledger.import_sessions(user['user_id'], workspace / 'sessions')
    args._user_id = user['user_id']
    args._workspace_container = root
    args._auth_token = token
    args.cwd = str(workspace)
    args._user_workspace = workspace
    args.background_root = str(workspace / '.port_sessions' / 'background')
