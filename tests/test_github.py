import json
import subprocess
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from dispatcher.config import Config, Project, load
from dispatcher.github import Adapter, GitHubError, valid_branch
from dispatcher.github.sync import SyncService
from dispatcher.storage import Store


def raw(number=1, **changes):
    value = dict(id=1000+number, number=number, url=f'https://api.github.com/repos/owner/repo/issues/{number}',
                 html_url=f'https://github.com/owner/repo/issues/{number}', title='Task', body='$(touch /tmp/never)',
                 user={'login': 'Trusted'}, labels=[{'name': 'READY'}], state='open', updated_at='2026-10-24T00:00:00Z')
    value.update(changes)
    return value


def response(data, status=200, headers=''):
    return subprocess.CompletedProcess([], int(status != 200),
        stdout=f'HTTP/2.0 {status} OK\n{headers}\n'+json.dumps(data), stderr='SECRET')


@pytest.fixture
def project():
    return Project('first', True, 'owner/repo', 'main', 'ready', ('trusted',))


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'tasks.sqlite3')
    yield value
    value.close()


@pytest.fixture(autouse=True)
def stage2_label_boundary(monkeypatch):
    monkeypatch.setattr(Adapter, 'reconcile', lambda *args, **kwargs: None)
    monkeypatch.setattr(Adapter, 'repository_identity', lambda *args: 100)


def config(project):
    from dispatcher.initialization import fingerprint
    project = replace(project, init_status='success', init_fingerprint=fingerprint(project))
    return Config(Path('/tmp/config'), Path('/tmp/work'), Path('/tmp/logs'), 30, 1, 'codex', (project,))


def poll(store, project, items):
    adapter = Adapter(run=Mock(return_value=response(items)))
    return SyncService(config(project), store, adapter).poll()


def test_versions_approval_and_metadata(store, project):
    assert poll(store, project, [raw()])['discovered'] == 1
    initial = store.get('owner/repo', 1)
    assert initial['state'] == 'discovered' and initial['available'] == 1
    assert initial['approved_version'] is None
    assert poll(store, project, [raw()])['unchanged'] == 1
    assert poll(store, project, [raw(body='second')])['updated'] == 1
    assert store.get('owner/repo', 1)['version'] == 2
    assert store.get('owner/repo', 1)['state'] == 'discovered'
    assert store.confirm('owner/repo', 1, 2)
    poll(store, project, [raw(body='second', updated_at='later', labels=[{'name':'ready'}, {'name':'extra'}])])
    assert store.get('owner/repo', 1)['approved_version'] == 2
    assert store.get('owner/repo', 1)['state'] == 'pending'
    poll(store, project, [raw(body='third')])
    row = store.get('owner/repo', 1)
    assert row['state'] == 'discovered' and row['approved_version'] is None and row['version'] == 3
    assert not store.reset_confirmation('owner/repo', 1, 2)
    assert store.reset_confirmation('owner/repo', 1, 3)
    assert store.db.execute('SELECT count(*) FROM history').fetchone()[0] == 4


@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('replacement', [[], [raw(labels=[])], [raw(state='closed')], [raw(user={'login':'stranger'})]])
def test_revocation_restore(store, project, replacement, confirmed):
    poll(store, project, [raw()])
    if confirmed:
        assert store.confirm('owner/repo', 1, 1)
    assert poll(store, project, replacement)['revoked'] == 1
    assert store.get('owner/repo', 1)['approved_version'] is None
    assert not store.get('owner/repo', 1)['available']
    assert store.get('owner/repo', 1)['state'] == 'revoked'
    poll(store, project, [raw()])
    assert store.get('owner/repo', 1)['state'] == 'discovered'
    assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('state', ['running','review','done','failed','cancelled','interrupted'])
def test_frozen_attempts(store, project, state):
    poll(store, project, [raw()])
    with store.db:
        store.db.execute('UPDATE tasks SET state=?,approved_version=version', (state,))
    original_row = dict(store.get('owner/repo',1))
    original = original_row['snapshot']
    poll(store, project, [raw(body='changed')])
    row = store.get('owner/repo',1)
    assert row['snapshot'] == original and 'changed' in row['observed']
    poll(store, project, [])
    poll(store, project, [raw()])
    final_row = store.get('owner/repo', 1)
    for key in ('state', 'version', 'content_hash', 'snapshot', 'approved_version'):
        assert final_row[key] == original_row[key]
    assert not store.reset_confirmation('owner/repo',1,1)


def test_filters_and_fail_closed(store, project):
    result = poll(store, project, [raw(1), raw(2, pull_request={}), raw(3, labels=[]), raw(4,user={'login':'bad'})])
    assert result['discovered'] == 1 and result['skipped_untrusted'] == 1
    assert poll(store, replace(project, allowed_authors=()), [raw()])['revoked'] == 1


def test_pages_restart_and_partial_failure(tmp_path, project):
    path = tmp_path / 'state.db'
    store = Store(path)
    run = Mock(side_effect=[response([raw(i) for i in range(1,101)]), response([raw(101)])])
    assert SyncService(config(project),store,Adapter(run=run)).poll()['discovered'] == 101
    assert 'page=2' in run.call_args.args[0][-1]
    store.close()
    store = Store(path)
    try:
        run.side_effect = [response([raw(i) for i in range(1,101)]), subprocess.TimeoutExpired('gh',30),
                           subprocess.TimeoutExpired('gh',30), subprocess.TimeoutExpired('gh',30)]
        result = SyncService(config(project),store,Adapter(run=run,sleep=lambda _:None)).poll()
        assert result['errors'] and result['revoked'] == 0
        assert store.get('owner/repo',101)['available']
        run.side_effect = [response([raw(i) for i in range(1,101)]), response([raw(101)])]
        assert SyncService(config(project),store,Adapter(run=run)).poll()['unchanged'] == 101
    finally:
        store.close()


def test_projects_isolated(store, project):
    second = replace(project,id='second',repository='other/repo',task_label='go',allowed_authors=('another',))
    disabled = replace(project,id='disabled',enabled=False)
    adapter = Mock()
    adapter.fetch.side_effect = [GitHubError('SECRET'), Adapter(run=Mock(return_value=response([
        raw(url='https://api.github.com/repos/other/repo/issues/1', user={'login':'another'},labels=[{'name':'go'}])]))).fetch(second)]
    result = SyncService(replace(config(project),projects=(config(project).projects[0],config(second).projects[0],disabled)),store,adapter).poll()
    assert result['discovered'] == 1 and len(result['errors']) == 1
    assert adapter.fetch.call_count == 2 and 'SECRET' not in str(result)


@pytest.mark.parametrize('status', [401,403,429,500])
def test_api_errors(status, project):
    run, sleep = Mock(return_value=response({},status,'Retry-After: 2\n')), Mock()
    with pytest.raises(GitHubError):
        Adapter(run=run,sleep=sleep).fetch(project)
    assert run.call_count == (1 if status == 401 else 3)
    assert sleep.call_count <= 2


def test_malformed_and_repository(project):
    for reply in [subprocess.CompletedProcess([],0,stdout='HTTP/2.0 200 OK\n\nSECRET'), response({}),
                  response([raw(url='https://api.github.com/repos/attacker/repo/issues/1')])]:
        with pytest.raises(GitHubError) as error:
            Adapter(run=Mock(return_value=reply)).fetch(project)
        assert 'SECRET' not in str(error.value)


def test_safe_arguments_and_branch(project):
    run = Mock(return_value=response([raw(title='`SECRET`', body='Target branch: feature/task\n$(touch /tmp/never)')]))
    item = Adapter(executable='/custom/gh',run=run).fetch(project)[0]
    assert item['target_branch'] == 'feature/task'
    assert run.call_args.kwargs['shell'] is False
    assert 'SECRET' not in str(run.call_args)
    assert run.call_args.args[0][0] == '/custom/gh'
    for branch in ['-bad','a..b','a b','a.lock','a/.b','a@{b','a\\b']:
        assert not valid_branch(branch)


def test_config_and_stopped_loop(tmp_path, project, store):
    path = tmp_path / 'config.yaml'
    path.write_text('''executables:
  github: /custom/gh
projects:
  - id: example
    enabled: true
    repository: owner/repo
    default_branch: develop
    labels:
      ready: go
    security:
      allowed_authors: [trusted]
''')
    loaded = load(path)
    assert loaded.github_executable == '/custom/gh'
    assert loaded.projects[0].base_branch == 'develop'
    stop = Event(); stop.set()
    adapter = Mock()
    SyncService(config(project),store,adapter).run(stop)
    adapter.fetch.assert_not_called()


def test_transport_retries_and_sanitized_result(store, project):
    for outcome in [subprocess.TimeoutExpired('SECRET', 30), OSError('SECRET'),
                    subprocess.CompletedProcess([], 1, stdout='', stderr='SECRET')]:
        run = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
        result = SyncService(config(project), store, Adapter(run=run,sleep=lambda _:None)).poll()
        assert run.call_count == 3
        assert result['errors'] and 'SECRET' not in str(result)


def test_atomic_rollback(store, project):
    adapter = Adapter(run=Mock(return_value=response([raw(),raw(2)])))
    items = adapter.fetch(project)
    # Fail the second insert: the first insert and history must roll back too.
    store.db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON tasks WHEN NEW.number=2 BEGIN SELECT RAISE(ABORT,'test'); END")
    service_adapter = Mock(); service_adapter.fetch.return_value = items
    result = SyncService(config(project),store,service_adapter).poll()
    assert result['errors']
    assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM history').fetchone()[0] == 0


def test_watch_stops_on_signal(tmp_path):
    import os
    import signal
    import sys
    import time
    path = tmp_path / 'config.yaml'
    path.write_text(f'''paths:
  workspace: {tmp_path}/work
  logs: {tmp_path}/logs
projects:
  - id: disabled
    enabled: false
    repository: owner/repo
''')
    child = subprocess.Popen([sys.executable,'-m','dispatcher','--config',str(path),'poll','--watch'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([child.stdout], [], [], 5)[0]
        assert json.loads(child.stdout.readline())['discovered'] == 0
        started = time.monotonic()
        os.kill(child.pid, signal.SIGTERM)
        child.communicate(timeout=3)
        assert child.returncode == 0 and time.monotonic()-started < 3
    finally:
        if child.poll() is None:
            child.kill(); child.wait()


def test_confirmation_contract(store, project):
    poll(store, project, [raw()])
    assert not store.confirm('owner/repo', 1, 0)
    assert not store.confirm('owner/repo', 2, 1)
    assert store.confirm('OWNER/REPO', 1, 1)
    assert not store.confirm('owner/repo', 1, 1)
    row = store.get('owner/repo', 1)
    assert row['state'] == 'pending' and row['approved_version'] == row['version']
    assert poll(store, project, [raw()])['unchanged'] == 1
    assert store.get('owner/repo', 1)['state'] == 'pending'
    assert not store.reset_confirmation('owner/repo', 1, 0)
    assert store.get('owner/repo', 1)['state'] == 'pending'
    assert store.reset_confirmation('owner/repo', 1, 1)
    row = store.get('owner/repo', 1)
    assert row['state'] == 'discovered' and row['approved_version'] is None
    poll(store, project, [])
    assert not store.confirm('owner/repo', 1, 1)
    assert not store.reset_confirmation('owner/repo', 1, 1)


@pytest.mark.parametrize('state', ['running', 'review', 'done', 'rejected', 'failed', 'cancelled', 'interrupted'])
def test_confirmation_never_changes_frozen_states(store, project, state):
    poll(store, project, [raw()])
    with store.db:
        store.db.execute('UPDATE tasks SET state=?,approved_version=version', (state,))
    before = dict(store.get('owner/repo', 1))
    assert not store.confirm('owner/repo', 1, 1)
    assert not store.reset_confirmation('owner/repo', 1, 1)
    assert dict(store.get('owner/repo', 1)) == before


@pytest.mark.parametrize('changed', [False, True])
def test_restore_always_requires_confirmation(store, project, changed):
    poll(store, project, [raw()])
    assert store.confirm('owner/repo', 1, 1)
    poll(store, project, [])
    poll(store, project, [raw(body='edited') if changed else raw()])
    row = store.get('owner/repo', 1)
    assert row['state'] == 'discovered' and row['approved_version'] is None
    assert row['version'] == 1 + int(changed)
    assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.fixture
def legacy_database(tmp_path):
    """Create the shipped v1 schema directly, without running current Store code."""
    import sqlite3
    path = tmp_path / 'legacy.sqlite3'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
        INSERT INTO schema_migrations VALUES(1);
        CREATE TABLE tasks(
            repository TEXT NOT NULL, number INTEGER NOT NULL, issue_id INTEGER NOT NULL,
            project_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            available INTEGER NOT NULL CHECK(available IN (0,1)),
            version INTEGER NOT NULL, content_hash TEXT NOT NULL, snapshot TEXT NOT NULL,
            observed TEXT NOT NULL, approved_version INTEGER,
            created_at TEXT NOT NULL, synced_at TEXT NOT NULL,
            PRIMARY KEY(repository, number));
        CREATE TABLE history(
            id INTEGER PRIMARY KEY, repository TEXT NOT NULL, number INTEGER NOT NULL,
            version INTEGER NOT NULL, event TEXT NOT NULL, snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(repository,number) REFERENCES tasks(repository,number));
    ''')
    cases = [
        ('approved', 3, 1, 'pending', 3),
        ('approved', None, 1, 'discovered', None),
        ('approved', 2, 1, 'discovered', None),
        ('approved', 3, 0, 'revoked', None),
        ('pending', None, 1, 'discovered', None),
        ('pending', 3, 1, 'pending', 3),
        ('pending', 2, 1, 'discovered', None),
        ('pending', 3, 0, 'revoked', None),
        ('revoked', 3, 0, 'revoked', None),
        ('discovered', 3, 1, 'discovered', None),
    ] + [(state, 2, 1, state, 2) for state in
         ('running', 'review', 'done', 'rejected', 'failed', 'cancelled', 'interrupted')]
    with db:
        for number, (state, approved, available, _, _) in enumerate(cases, 1):
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('owner/repo', number, 1000+number, 'first', state, available, 3,
                        'original hash', 'original snapshot', 'original observation', approved, 'created', 'synced'))
            db.execute('INSERT INTO history VALUES(?,?,?,?,?,?,?)',
                       (number, 'owner/repo', number, 3, 'legacy event', 'legacy snapshot', 'legacy time'))
    db.row_factory = sqlite3.Row
    before = [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY number')]
    history = [tuple(row) for row in db.execute('SELECT * FROM history ORDER BY id')]
    db.close()
    return path, cases, before, history


def test_legacy_migration_backup_reset_and_idempotency(legacy_database):
    import sqlite3
    path, _, before, history = legacy_database
    store = Store(path)
    assert not store.queue()
    store.close()
    backups = list(path.parent.glob('*.bak'))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    backup.row_factory = sqlite3.Row
    assert [dict(r) for r in backup.execute('SELECT * FROM tasks ORDER BY number')] == before
    assert [tuple(r) for r in backup.execute('SELECT * FROM history ORDER BY id')] == history
    backup.close()
    store = Store(path)
    assert not store.queue()
    assert len(list(path.parent.glob('*.bak'))) == 1
    store.close()


def test_legacy_migration_rolls_back_on_error(legacy_database, monkeypatch):
    import sqlite3
    from dispatcher.storage import migrations
    path, _, before, history = legacy_database
    def fail(db):
        db.execute('CREATE TABLE partial(id INTEGER)')
        raise sqlite3.IntegrityError('test')
    monkeypatch.setattr(migrations, 'initialize', fail)
    with pytest.raises(RuntimeError, match='original data retained'):
        Store(path)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    assert [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY number')] == before
    assert [tuple(r) for r in db.execute('SELECT * FROM history ORDER BY id')] == history
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='partial'").fetchone()
    assert [r[0] for r in db.execute('SELECT version FROM schema_migrations')] == [1]
    db.close()
    assert list(path.parent.glob('*.bak'))



@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('edit', [{'title': 'Revised task'}, {'body': 'Target branch: feature/revised'}])
def test_significant_fields_invalidate_only_current_confirmation(store, project, confirmed, edit):
    poll(store, project, [raw()])
    if confirmed:
        assert store.confirm('owner/repo', 1, 1)
    poll(store, project, [raw(**edit)])
    row = store.get('owner/repo', 1)
    assert row['state'] == 'discovered' and row['approved_version'] is None
    assert row['version'] == 2
    assert poll(store, project, [raw(**edit)])['unchanged'] == 1
    assert store.get('owner/repo', 1)['version'] == 2
    assert not store.confirm('owner/repo', 1, 1)
    assert store.confirm('owner/repo', 1, 2)


def test_current_confirmation_survives_reopen(tmp_path, project):
    path = tmp_path / 'current.sqlite3'
    store = Store(path)
    try:
        poll(store, project, [raw()])
        assert store.confirm('owner/repo', 1, 1)
        before = dict(store.get('owner/repo', 1))
    finally:
        store.close()
    store = Store(path)
    try:
        assert dict(store.get('owner/repo', 1)) == before
        assert poll(store, project, [raw()])['unchanged'] == 1
        row = store.get('owner/repo', 1)
        assert row['state'] == 'pending' and row['approved_version'] == 1
    finally:
        store.close()
