"""Read-only session listing shared by the authenticated CLI and GUI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import unicodedata

from .session_store import read_agent_session, usage_from_payload


def get_session_info(session_id: str, *, directory: Path) -> dict:
    """Read display metadata only; does not restore or run an Agent."""
    session = read_agent_session(session_id, directory=directory)
    usage = usage_from_payload(session.usage)
    return {
        'session_id': session.session_id,
        'message_count': len(session.messages),
        'input_tokens': usage.input_tokens,
        'output_tokens': usage.output_tokens,
    }


def normalize_preview(text: str) -> str:
    # Do not let terminal controls or newlines affect the surrounding listing.
    text = ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in text)
    text = ' '.join(text.split())
    return text if len(text) <= 80 else text[:79] + '…'


def session_preview(payload: dict) -> str:
    """Prefer stable metadata; never mistake a compacted tail for the first query."""
    if isinstance(payload.get('preview'), str):
        return normalize_preview(payload['preview'])
    messages = payload.get('messages', [])
    for message in messages:
        if not isinstance(message, dict):
            continue
        metadata = message.get('metadata') or {}
        if (str(message.get('message_id', '')).startswith('compact')
                or (isinstance(metadata, dict) and (
                    str(metadata.get('kind', '')).startswith('compact')
                    or metadata.get('is_compact_summary')))):
            return ''
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        metadata = message.get('metadata') or {}
        if (message.get('message_id') == 'user_context_0'
                or (isinstance(metadata, dict) and (
                    metadata.get('lineage_id') == 'user_context_0'
                    or metadata.get('kind')))):
            continue
        content = message.get('content')
        if isinstance(content, str) and content.strip() and not content.lstrip().startswith('/'):
            return normalize_preview(content)
    return ''


def list_saved_sessions(directory: Path, *, limit: int | None = 20) -> dict:
    """List valid local JSON sessions, ignoring symlinks and malformed entries."""
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError('limit must be a positive integer')
    sessions = []
    skipped = 0
    for path in directory.glob('*.json'):
        try:
            if path.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_-]+', path.stem):
                raise ValueError('unsafe session file')
            payload = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(payload, dict) or not isinstance(payload.get('messages'), list)
                    or payload.get('session_id', path.stem) != path.stem):
                raise ValueError('invalid session file')
            modified = path.stat().st_mtime
            sessions.append({
                'session_id': path.stem,
                'preview': session_preview(payload),
                'name': normalize_preview(payload['name']) if isinstance(payload.get('name'), str) else '',
                'modified_at': modified,
                'modified_at_display': datetime.fromtimestamp(
                    modified, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'),
                'turns': int(payload.get('turns', 0)),
                'tool_calls': int(payload.get('tool_calls', 0)),
            })
        except (OSError, ValueError, TypeError, OverflowError):
            skipped += 1
    sessions.sort(key=lambda item: (-item['modified_at'], item['session_id']))
    return {'sessions': sessions if limit is None else sessions[:limit],
            'total': len(sessions), 'skipped': skipped}
