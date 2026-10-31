"""Correctable preparation errors revoke approval; they are never terminal."""
import fcntl
import hashlib
import json
import re
from pathlib import Path

import yaml
from dispatcher.github import GitHubError, valid_branch
from dispatcher.storage import now
from dispatcher.workspace import TaskError


CODES = frozenset(('invalid_branch', 'invalid_codex_config', 'base_branch_missing', 'branch_ownership_conflict', 'default_branch_protected'))


def parameters(body):
    class UniqueLoader(yaml.SafeLoader):
        pass
    def mapping(loader, node):
        result = {}
        for key, value in node.value:
            name = loader.construct_object(key)
            if not isinstance(name, str) or name in result:
                raise ValueError('duplicate or non-string key')
            result[name] = loader.construct_object(value)
        return result
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    blocks = re.findall(r'^```codex-config[^\S\n]*\n(.*?)^```[^\S\n]*$', body or '', re.M | re.S)
    if not blocks:
        if re.search(r'^```codex-config\b', body or '', re.M):
            raise TaskError('invalid_codex_config', 'GIT')
        return {}
    try:
        if len(blocks) != 1:
            raise ValueError
        value = yaml.load(blocks[0], Loader=UniqueLoader)
        if not isinstance(value, dict) or value.keys() - {'base_branch', 'branch_name'}:
            raise ValueError
        if any(not isinstance(v, str) or not valid_branch(v) for v in value.values()):
            raise ValueError
        return value
    except (ValueError, TypeError, yaml.YAMLError):
        raise TaskError('invalid_codex_config', 'GIT') from None


def blocked(store, row):
    return row is not None and store.db.execute(
        'SELECT 1 FROM configuration_errors WHERE issue_id=? AND content_hash=?',
        (row['issue_id'], row['content_hash'])).fetchone() is not None


def record(store, row, code):
    with store.db as db:
        db.execute('BEGIN IMMEDIATE')
        current = store.get(row['repository'], row['number'])
        if (current['version'] != row['version'] or current['current_iteration_id'] != row['current_iteration_id']
                or current['state'] not in ('pending', 'running')):
            return
        if db.execute('SELECT 1 FROM attempts WHERE repository=? AND number=? AND codex_started=1 AND iteration_id=?',
                      (row['repository'], row['number'], row['current_iteration_id'])).fetchone():
            return
        key = hashlib.sha256(f'{row["issue_id"]}:{row["content_hash"]}'.encode()).hexdigest()
        body = (f'Configuration error: {code}.\nCorrect the branch parameters / codex-config in the Issue '
                'and approve the new version. Codex was not started.\n'
                f'<!-- codex-config-error:{key} -->')
        db.execute('INSERT OR IGNORE INTO configuration_errors VALUES(?,?,?,?,?,?,NULL)',
                   (row['issue_id'], row['content_hash'], code, now(), body, 0))
        db.execute("UPDATE attempts SET state='failed',error_code=?,category='GIT',finished_at=? WHERE repository=? AND number=? AND iteration_id=? AND codex_started=0 AND state='running'",
                   (code, now(), row['repository'], row['number'], row['current_iteration_id']))
        store._set_state(current, 'discovered', None)
        if not db.execute('SELECT 1 FROM attempts WHERE repository=? AND number=? AND initial_sha IS NOT NULL',
                          (row['repository'], row['number'])).fetchone():
            db.execute('UPDATE tasks SET base_branch=NULL,work_branch=NULL,branch_source=NULL,branch_status=NULL WHERE issue_id=?', (row['issue_id'],))
        store._record_transition(current, 'discovered', 'configuration:' + key, 'scheduler', dict(configuration_error=code))
        store._history(row['repository'], row['number'], row['version'], 'configuration_error', json.dumps(dict(code=code)), now())


def flush(store, adapter, projects):
    database = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    with (database.parent / 'configuration-outbox.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        for row in store.db.execute('''SELECT e.*,t.repository,t.number FROM configuration_errors e
                JOIN tasks t USING(issue_id) WHERE delivered=0''').fetchall():
            if not any(p.repository.casefold() == row['repository'] for p in projects):
                continue
            if store.stop_requested(store.get(row['repository'], row['number'])):
                continue
            try:
                endpoint = f'repos/{row["repository"]}/issues/{row["number"]}/comments'
                marker = row['body'].splitlines()[-1]
                comments = adapter.pages(endpoint)
                if not isinstance(comments, list) or any(not isinstance(c, dict) or not isinstance(c.get('body', ''), str) for c in comments):
                    raise GitHubError(kind='invalid_response')
                found = next((c for c in comments if marker in c.get('body', '')), None)
                if found is None:
                    if store.stop_requested(store.get(row['repository'], row['number'])):
                        continue
                    found = adapter.write(endpoint, 'POST', dict(body=row['body']))
                if not isinstance(found, dict) or type(found.get('id')) is not int:
                    raise GitHubError(kind='invalid_response')
                with store.db as db:
                    db.execute('UPDATE configuration_errors SET delivered=1,comment_id=? WHERE issue_id=? AND content_hash=?',
                               (str(found['id']), row['issue_id'], row['content_hash']))
            except GitHubError:
                continue
