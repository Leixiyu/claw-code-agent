from __future__ import annotations

import os
import re
from pathlib import Path


_VARIABLE_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
_KEY_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def load_project_env(path: Path | None = None) -> Path | None:
    """Load a local .env without overriding variables from the process."""
    env_path = (path or (Path.cwd() / '.env')).resolve()
    if not env_path.is_file():
        return None

    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition('=')
        key = key.strip()
        if not separator or not _KEY_PATTERN.fullmatch(key):
            continue

        value, expand = _parse_value(raw_value)
        if expand:
            value = _VARIABLE_PATTERN.sub(
                lambda match: os.environ.get(
                    match.group(1),
                    loaded.get(match.group(1), ''),
                ),
                value,
            )
        loaded[key] = value
        os.environ.setdefault(key, value)

    if not os.environ.get('OPENAI_API_KEY') and os.environ.get('DASHSCOPE_API_KEY'):
        os.environ['OPENAI_API_KEY'] = os.environ['DASHSCOPE_API_KEY']
    return env_path


def _parse_value(raw_value: str) -> tuple[str, bool]:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1], False
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return (
            value[1:-1]
            .replace(r'\"', '"')
            .replace(r'\n', '\n')
            .replace(r'\\', '\\'),
            True,
        )
    value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
    return value, True
