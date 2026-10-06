"""User-scoped upload metadata; publish records only after successful writes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from .user_workspace import atomic_json, contained, index_lock

CATALOG = 'uploads/videos.json'
UPLOAD_TIMEZONE = timezone(timedelta(hours=8))


def create_upload(workspace: Path, filename: str):
    now = datetime.now(UPLOAD_TIMEZONE)
    original = Path(filename)
    stamp = now.strftime('%Y%m%d_%H%M%S_') + f'{now.microsecond // 1000:03d}'
    basename = f'{original.stem}_{stamp}'
    while True:
        path = contained(workspace, workspace / 'uploads' / f'{basename}{original.suffix}')
        try:
            output = path.open('xb')
            break
        except FileExistsError:
            basename = f'{original.stem}_{stamp}_{uuid4().hex[:12]}'
    record = {'original_filename': filename,
              'uploaded_at': now.isoformat(timespec='milliseconds'),
              'path': path.relative_to(workspace).as_posix()}
    return path, output, record


def read_uploaded_videos(workspace: Path) -> list[dict[str, str]]:
    path = contained(workspace, workspace / CATALOG)
    if not path.exists():
        return []
    records = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records, list):
        raise ValueError('invalid upload catalog: expected an array')
    for record in records:
        if not isinstance(record, dict) or any(
            not isinstance(record.get(key), str) or not record[key]
            for key in ('original_filename', 'uploaded_at', 'path')
        ):
            raise ValueError('invalid upload catalog record')
        relative = Path(record['path'])
        if relative.is_absolute() or relative.parent != Path('uploads'):
            raise ValueError('invalid upload catalog path')
        contained(workspace, workspace / relative)
    return records


def record_upload(workspace: Path, record: dict[str, str]) -> None:
    with index_lock(workspace):
        records = read_uploaded_videos(workspace)
        records.append(record)
        atomic_json(contained(workspace, workspace / CATALOG), records)
