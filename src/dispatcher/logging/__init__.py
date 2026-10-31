"""Allowlisted structured lifecycle events, without CLI output or configuration."""
import json
import os
from datetime import datetime, timezone


def event(path, name, *, level='INFO', project=None, task=None, reason=None, action=None, cleanup_id=None, attempt=None, result=None):
    record = dict(timestamp=datetime.now(timezone.utc).isoformat(), level=level, event=name)
    if project is not None:
        record['project'] = project
    if task is not None:
        record['task'] = task
    if reason is not None:
        record['reason'] = reason
    if action is not None:
        record['action'] = action
    if cleanup_id is not None and task is not None:
        record['issue'] = task
    for key, value in (('cleanup_id', cleanup_id), ('attempt', attempt), ('result', result)):
        if value is not None:
            record[key] = value
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(record) + '\n')
