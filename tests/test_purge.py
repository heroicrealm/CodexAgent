"""Administrative purge boundaries, real SQLite/Git, no remote operations."""
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest
import yaml

from dispatcher.config import load_config
from dispatcher.db_guard import connection, check
from dispatcher.models import IssueObservation
from dispatcher.monitoring import snapshot
from dispatcher.projects import mutate
from dispatcher.purge import execute, plan, sql_purge, triggers
from test_monitoring_projects import instance, register, files


class Terminal(StringIO):
    def isatty(self):
        return True


def run(instance, **kwargs):
    _, _, path = instance
    return execute(load_config(path), 'p', path, stdin=Terminal('p\n'), output=lambda s: None, **kwargs)


def seed(instance):
    config, store, path = instance
    register(store)
    store.register_issue(IssueObservation(2, 'owner/empty', 1, 'Other'), 'empty')
    with connection(config.database) as db:
        db.execute("INSERT INTO executions(execution_id,issue_id,status,started_at) VALUES(10,1,'completed','now')")
        db.execute("INSERT INTO pull_requests VALUES(10,1,'owner/repo',1,'work','main','closed',NULL,'now')")
        db.execute("INSERT INTO debug_operations(created_at,level,component,operation,repository,result) VALUES('now','api','test','get','owner/repo','ok')")
        summary = [dict(issue_id=i, reasons=[], target='discovered', repository=r, issue_number=1)
                   for i, r in ((1, 'owner/repo'), (2, 'owner/empty'))]
        db.execute("INSERT INTO recovery_sessions VALUES('s','now','now','completed',2,0,?)", (json.dumps(summary),))
        db.execute("INSERT INTO recovery_operations VALUES('o',1,'retry','completed','now','now',NULL,'{}')")
        db.execute("INSERT INTO recovery_events(issue_id,session_id,operation_id,action,details,created_at) VALUES(1,'s','o','retry','{}','now')")
        db.execute("INSERT INTO recovery_events(operation_id,action,details,created_at) VALUES('o','retry','{}','now')")
        db.execute("INSERT INTO recovery_events(issue_id,session_id,action,details,created_at) VALUES(2,'s','check','{}','now')")
        db.execute("INSERT INTO recovery_execution_archive VALUES('a','o',1,9,'{}','[]','retry','now')")
        db.execute("INSERT INTO recovery_approval_reset VALUES(1,'o','now',NULL,1,'now')")
        db.execute("INSERT INTO error_log(issue_id,execution_id,error_code,error_stage,error_message,created_at) VALUES(1,10,'x','x','private','now')")
    result = config.workspace / 'results'
    result.mkdir()
    (result / '10.txt').write_text('answer')
    (result / '9.txt').write_text('archived answer')
    return load_config(path)


@pytest.mark.parametrize('populated', [False, True])
def test_success_backup_removed_and_readd(instance, monkeypatch, populated):
    config = seed(instance) if populated else load_config(instance[2])
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('external operation'))
    output = []
    execute(config, 'p', instance[2], stdin=Terminal('p\n'), output=output.append)
    assert 'PASS' in output[-1] and 'temporary backup removed' in output[-1]
    assert list((config.root_path / 'backups/purge').iterdir()) == []
    assert [p.id for p in load_config(instance[2]).projects] == ['empty']
    check(config, execution=True)
    with snapshot(config) as (db, _):
        triggers(db)
        assert db.execute("SELECT count(*) FROM issues WHERE repository='owner/repo'").fetchone()[0] == 0
        if populated:
            assert db.execute('SELECT issue_id FROM issues').fetchone()[0] == 2
            assert db.execute('SELECT count(*) FROM recovery_events').fetchone()[0] == 1
            session = db.execute('SELECT * FROM recovery_sessions').fetchone()
            assert session['checked_issues'] == 1
            assert json.loads(session['summary'])[0]['repository'] == 'owner/empty'
    monkeypatch.setattr('dispatcher.projects.validate_remote', lambda *a: None)
    mutate(instance[2], 'add', 'again', repository='owner/repo')
    assert load_config(instance[2]).projects[-1].id == 'again'


@pytest.mark.parametrize('point', ['drop', 'delete', 'create', 'commit'])
def test_transactional_ddl_rollback(instance, point):
    config = seed(instance)
    inventory = plan(config, 'p')
    with connection(config.database) as db:
        db.row_factory = sqlite3.Row
        before = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t}')] for t in inventory['rows']}
        def fail(phase):
            assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            if phase == point:
                raise RuntimeError('injected')
        with pytest.raises(RuntimeError, match='injected'):
            sql_purge(db, config.projects[0], inventory, fail)
        triggers(db)
        assert before == {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t}')] for t in inventory['rows']}
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute('DELETE FROM recovery_events')
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute("UPDATE recovery_events SET action='x'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO recovery_events(issue_id,action,details,created_at) VALUES(999,'x','{}','now')")


@pytest.mark.parametrize('point', ['backup', 'drop', 'delete', 'create', 'commit',
                                  'database-committed', 'result', 'files-removed', 'yaml-intent', 'yaml-removed'])
def test_failures_preserve_verified_backup_and_journal(instance, point):
    config = seed(instance)
    def fail(phase):
        if phase == point:
            raise KeyboardInterrupt('injected')
    with pytest.raises(ValueError, match='backup preserved:') as failure:
        run(instance, checkpoint=fail)
    area = config.root_path / 'backups/purge'
    backup, = area.glob('*.sqlite')
    journal, = area.glob('*.json')
    assert str(backup) in str(failure.value)
    assert backup.stat().st_mode & 0o777 == 0o600
    assert area.stat().st_mode & 0o777 == 0o700
    assert journal.stat().st_mode & 0o777 == 0o600
    check(config, path=backup)
    assert json.loads(journal.read_text())['backup'] == str(backup)
    if point != 'yaml-removed':
        inventory = plan(config, 'p')
        assert not inventory['executable']
        assert str(backup) in inventory['previous_attempts']
        with pytest.raises(ValueError, match='Prior purge'):
            run(instance)
        assert len(list(area.glob('*.sqlite'))) == 1


@pytest.mark.parametrize('terminal', [StringIO('p\n'), Terminal('wrong\n'), Terminal('')])
def test_confirmation_no_mutations(instance, terminal, tmp_path):
    config = load_config(instance[2])
    before = files(tmp_path)
    with pytest.raises(ValueError, match='terminal|confirmation'):
        execute(config, 'p', instance[2], stdin=terminal, output=lambda s: None)
    assert files(tmp_path) == before


def test_dry_run_no_sidecars_or_subprocess(instance, monkeypatch, tmp_path):
    seed(instance)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('external call'))
    before = files(tmp_path)
    paths = set(tmp_path.rglob('*'))
    assert plan(load_config(instance[2]), 'p')['executable']
    assert files(tmp_path) == before
    assert set(tmp_path.rglob('*')) == paths


@pytest.mark.parametrize('kind', ['barrier', 'active', 'quarantine', 'foreign-event', 'summary', 'external', 'symlink', 'orphan'])
def test_blockers(instance, kind, tmp_path):
    config = seed(instance)
    if kind == 'external':
        with open(instance[2]) as stream:
            data = yaml.safe_load(stream)
        data['projects'][0]['checkout'] = str(tmp_path / 'external')
        instance[2].write_text(yaml.safe_dump(data))
    elif kind == 'symlink':
        (config.workspace / 'worktrees').symlink_to(tmp_path)
    elif kind == 'orphan':
        (config.workspace / 'results/999.txt').write_text('unknown')
    else:
        with connection(config.database) as db:
            if kind == 'barrier':
                db.execute('UPDATE dispatcher_metadata SET recovery_required=1')
            elif kind == 'active':
                db.execute("UPDATE executions SET status='executing'")
            elif kind == 'quarantine':
                db.execute("INSERT INTO recovery_quarantine VALUES(1,'local-worktree','{}','now','now')")
            elif kind == 'foreign-event':
                db.execute("INSERT INTO recovery_events(issue_id,operation_id,action,details,created_at) VALUES(2,'o','mixed','{}','now')")
            elif kind == 'summary':
                db.execute("UPDATE recovery_sessions SET summary='[\"owner/repo\"]'")
    config = load_config(instance[2])
    before = files(tmp_path) if kind != 'symlink' else None
    if kind == 'symlink':
        inventory = plan(config, 'p')
        assert not inventory['executable']
        assert 'Symlink' in '; '.join(inventory['blockers'])
    else:
        assert not plan(config, 'p')['executable']
        with pytest.raises(ValueError, match='Purge refused'):
            run(instance)
        assert files(tmp_path) == before


def test_lock_excludes_purge(instance):
    from dispatcher.locking import InstanceLock
    config = load_config(instance[2])
    with InstanceLock(config):
        assert not plan(config, 'p')['executable']


def test_changed_plan_refused(instance):
    config, store, path = instance
    class ChangingTerminal(Terminal):
        def readline(self):
            register(store)
            return 'p\n'
    with pytest.raises(ValueError, match='plan changed'):
        execute(load_config(path), 'p', path, stdin=ChangingTerminal(), output=lambda s: None)
    assert not (config.workspace.parent / 'backups').exists()


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_git(instance):
    config = seed(instance)
    checkout = config.workspace / 'repos/p'
    checkout.mkdir(parents=True)
    git(checkout, 'init', '-b', 'main')
    git(checkout, 'config', 'user.email', 'test@example.com')
    git(checkout, 'config', 'user.name', 'Test')
    git(checkout, 'commit', '--allow-empty', '-m', 'base')
    git(checkout, 'remote', 'add', 'origin', 'git@github.com:owner/repo.git')
    path = config.workspace / 'worktrees/p' / ('e-10-' + 'a' * 32)
    path.parent.mkdir(parents=True)
    git(checkout, 'worktree', 'add', '-b', 'dispatcher-v2/10-' + 'a' * 32, str(path))
    return config, checkout, path


def test_real_git_local_only(instance, monkeypatch, tmp_path):
    config, checkout, tree = make_git(instance)
    original = subprocess.run
    calls = []
    def local(args, **kwargs):
        assert args[0] == 'git'
        assert not set(args) & {'push', 'fetch', 'ls-remote', 'clone'}
        calls.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', local)
    assert plan(config, 'p')['executable']
    run(instance)
    assert not checkout.exists() and not tree.exists()
    assert any('update-ref' in a for a in calls)


@pytest.mark.parametrize('kind', ['dirty', 'foreign-tree', 'foreign-branch', 'origin', 'unregistered'])
def test_git_ownership_refusal(instance, kind, tmp_path):
    config, checkout, tree = make_git(instance)
    if kind == 'dirty':
        (tree / 'user.txt').write_text('preserve')
    elif kind == 'foreign-tree':
        git(checkout, 'worktree', 'add', '-b', 'user', str(tmp_path / 'other-tree'))
    elif kind == 'foreign-branch':
        git(checkout, 'branch', 'user')
    elif kind == 'origin':
        git(checkout, 'remote', 'set-url', 'origin', 'git@github.com:other/repo.git')
    else:
        (tree.parent / 'user').mkdir()
    assert not plan(config, 'p')['executable']
    with pytest.raises(ValueError):
        run(instance)
    assert checkout.exists() and tree.exists()


def test_real_recovery_summary_format_and_session_only_events(instance):
    config = seed(instance)
    with connection(config.database) as db:
        summary = [dict(issue_id=1, reasons=[], target='discovered'), dict(issue_id=2, reasons=[], target='discovered')]
        db.execute('UPDATE recovery_sessions SET summary=?', (json.dumps(summary),))
        db.execute("INSERT INTO recovery_sessions VALUES('only','now','now','completed',1,0,?)", (json.dumps(summary[:1]),))
        db.execute("INSERT INTO recovery_events(session_id,action,details,created_at) VALUES('only','summary','{}','now')")
    assert plan(config, 'p')['executable']
    run(instance)
    with snapshot(config) as (db, _):
        assert db.execute('SELECT count(*) FROM recovery_sessions').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM recovery_events').fetchone()[0] == 1
        assert json.loads(db.execute('SELECT summary FROM recovery_sessions').fetchone()[0]) == summary[1:]


def test_scoped_diagnostic_lines_and_ordinary_project_word(instance):
    config = seed(instance)
    area = config.workspace / 'diagnostics'
    area.mkdir()
    keep = json.dumps(dict(context=dict(repository='owner/empty'), message='keep p as an ordinary word')) + '\n'
    remove = json.dumps(dict(context=dict(repository='owner/repo', project_id='p'), message='remove')) + '\n'
    diagnostic = area / 'poll-errors.jsonl'
    diagnostic.write_text(keep + remove)
    assert len(plan(config, 'p')['diagnostics']) == 1
    run(instance)
    assert diagnostic.read_text() == keep


def test_self_fk_group_deleted_in_one_statement(instance):
    config, store, _ = instance
    register(store)
    store.register_issue(IssueObservation(3, 'owner/repo', 3, 'Child'), 'p')
    with connection(config.database) as db:
        db.execute('UPDATE issues SET execution_group_id=1,correction_of_issue_id=1 WHERE issue_id=3')
        db.execute('INSERT INTO issue_relations VALUES(1,3)')
    run(instance)
    with snapshot(config) as (db, _):
        assert db.execute('SELECT count(*) FROM issues').fetchone()[0] == 0


def test_changed_content_same_ids_refused(instance):
    config = seed(instance)
    expected = plan(config, 'p')
    with connection(config.database) as db:
        db.execute("UPDATE issues SET title='changed' WHERE issue_id=1")
    with connection(config.database) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match='plan changed'):
            sql_purge(db, config.projects[0], expected)
        triggers(db)


def test_trigger_tamper_refused(instance):
    config = seed(instance)
    with connection(config.database) as db:
        db.execute('DROP TRIGGER recovery_events_no_delete')
    with pytest.raises((ValueError, RuntimeError), match='structure|trigger'):
        plan(config, 'p')


def test_admission_changed_inside_sql_boundary(instance):
    config = seed(instance)
    expected = plan(config, 'p')
    with connection(config.database) as db:
        db.execute('UPDATE dispatcher_metadata SET recovery_required=1')
    with connection(config.database) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match='admission changed'):
            sql_purge(db, config.projects[0], expected)
        triggers(db)


@pytest.mark.parametrize('point', ['worktree', 'ref'])
def test_git_boundary_failure_backup_preserved(instance, point):
    config, checkout, tree = make_git(instance)
    def fail(phase):
        if point == phase:
            raise RuntimeError('injected Git boundary')
    with pytest.raises(ValueError, match='backup preserved'):
        run(instance, checkpoint=fail)
    assert checkout.exists()
    assert not tree.exists()
    backup, = (config.root_path / 'backups/purge').glob('*.sqlite')
    check(config, path=backup)
    assert load_config(instance[2]).projects[0].id == 'p'


def test_failed_attempt_restore_and_retry(instance):
    from dispatcher.db_guard import maintain
    config = seed(instance)
    def fail(phase):
        if phase == 'database-committed':
            raise RuntimeError('crash')
    with pytest.raises(ValueError, match='backup preserved'):
        run(instance, checkpoint=fail)
    area = config.root_path / 'backups/purge'
    backup, = area.glob('*.sqlite')
    maintain(config, 'restore', backup)
    # Reconciliation here is modeled locally; no network access in this test.
    with connection(config.database) as db:
        db.execute('UPDATE dispatcher_metadata SET recovery_required=0')
    archive = config.root_path / 'failed-attempt-archive'
    archive.mkdir()
    for source in area.iterdir():
        source.rename(archive / source.name)
    run(instance)
    assert not list(area.iterdir())
    assert (archive / backup.name).exists()  # explicit user archive is never removed


def test_backup_precedes_drop(instance):
    config = seed(instance)
    seen = []
    def verify(phase):
        if phase == 'drop':
            backup, = (config.root_path / 'backups/purge').glob('*.sqlite')
            check(config, path=backup)
            with snapshot(replace(config, database=backup)) as (db, _):
                assert db.execute('SELECT count(*) FROM issues').fetchone()[0] == 2
            seen.append(phase)
    run(instance, checkpoint=verify)
    assert seen == ['drop']


def test_retry_after_yaml_commit_discloses_backup(instance):
    config = seed(instance)
    def fail(phase):
        if phase == 'yaml-removed':
            raise RuntimeError('crash')
    with pytest.raises(ValueError, match='backup preserved'):
        run(instance, checkpoint=fail)
    current = load_config(instance[2])
    inventory = plan(current, 'p')
    assert not inventory['executable']
    assert any('backup verified' in status for status in inventory['previous_attempt_status'])
    with pytest.raises(ValueError, match='backup preserved'):
        execute(current, 'p', instance[2], stdin=Terminal('p\n'), output=lambda s: None)


@pytest.mark.parametrize('stale_socket', [False, True])
def test_dry_run_live_wal_bytes_unchanged(instance, tmp_path, stale_socket):
    import socket
    config = seed(instance)
    if stale_socket:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(config.workspace / '.control.sock'))
    with connection(config.database) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE issues SET title='committed in WAL' WHERE issue_id=1")
        writer.commit()
        before = files(tmp_path)
        inventory = plan(config, 'p')
        assert not inventory['executable']
        assert 'sidecars' in '; '.join(inventory['blockers'])
        assert files(tmp_path) == before


def test_diagnostic_failure_preserves_backup(instance):
    config = seed(instance)
    area = config.workspace / 'diagnostics'
    area.mkdir()
    path = area / 'poll-errors.jsonl'
    path.write_text(json.dumps(dict(context=dict(repository='owner/repo'))) + '\n')
    def fail(phase):
        if phase == 'diagnostic':
            raise RuntimeError('interrupted')
    with pytest.raises(ValueError, match='backup preserved'):
        run(instance, checkpoint=fail)
    assert not path.exists()
    backup, = (config.root_path / 'backups/purge').glob('*.sqlite')
    check(config, path=backup)


def test_repository_prefix_is_not_foreign_reference(instance):
    config = seed(instance)
    with connection(config.database) as db:
        db.execute("UPDATE issues SET repository='owner/repo2' WHERE issue_id=2")
        db.execute("UPDATE recovery_sessions SET summary=?", (json.dumps([
            dict(issue_id=1, reasons=[], target='discovered'),
            dict(issue_id=2, reasons=[], target='discovered')]),))
    assert plan(config, 'p')['executable']
    run(instance)
    with snapshot(config) as (db, _):
        assert db.execute('SELECT repository FROM issues').fetchone()[0] == 'owner/repo2'


def test_git_dry_run_index_and_paths_unchanged(instance, tmp_path):
    config, checkout, tree = make_git(instance)
    before = files(tmp_path)
    stats = {str(p): p.stat().st_mtime_ns for p in tmp_path.rglob('*') if p.is_file()}
    paths = set(tmp_path.rglob('*'))
    assert plan(config, 'p')['executable']
    assert files(tmp_path) == before
    assert set(tmp_path.rglob('*')) == paths
    assert {str(p): p.stat().st_mtime_ns for p in tmp_path.rglob('*') if p.is_file()} == stats


def test_stale_socket_dry_run_and_removal(instance, tmp_path):
    import socket
    from dispatcher.db_guard import quiescent, maintain
    from dispatcher.storage import SchemaError
    config = seed(instance)
    control = config.workspace / '.control.sock'
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(control))
    before, paths = files(tmp_path), set(tmp_path.rglob('*'))
    identity = control.lstat()
    inventory = plan(config, 'p')
    assert inventory['executable'] and inventory['control']['state'] == 'stale'
    assert files(tmp_path) == before and set(tmp_path.rglob('*')) == paths
    # Restore and the global guard remain strict, including with free locks.
    with pytest.raises(SchemaError, match='Control socket'):
        quiescent(config)
    backup = tmp_path / 'restore.sqlite'
    maintain(config, 'backup', backup)
    with pytest.raises(SchemaError, match='Control socket'):
        maintain(config, 'restore', backup)
    run(instance)
    assert control.lstat() == identity  # no unlink TOCTOU, even on success
    check(config, execution=True)
    assert [p.id for p in load_config(instance[2]).projects] == ['empty']


@pytest.mark.parametrize('kind', ['listener', 'locked-listener', 'bound-owner', 'lock-only', 'file', 'symlink', 'pid-reuse', 'live-pid', 'bad-metadata', 'pid-without-socket'])
def test_control_unsafe_no_mutation(instance, tmp_path, kind):
    import socket
    from contextlib import ExitStack
    from dispatcher.locking import InstanceLock
    from dispatcher.monitoring import publish_metadata
    config = load_config(instance[2])
    control = config.workspace / '.control.sock'
    with ExitStack() as stack:
        if kind in ('listener', 'locked-listener', 'bound-owner', 'pid-reuse', 'live-pid', 'bad-metadata'):
            server = stack.enter_context(socket.socket(socket.AF_UNIX))
            server.bind(str(control))
            if kind in ('listener', 'locked-listener'):
                server.listen()
            elif kind != 'bound-owner':
                server.close()
                publish_metadata(config)
                if kind == 'pid-reuse':
                    meta = config.workspace / '.monitor.json'
                    value = json.loads(meta.read_text())
                    value['process_token'] = 'wrong-starttime'
                    meta.write_text(json.dumps(value))
                elif kind == 'bad-metadata':
                    (config.workspace / '.monitor.json').write_text('[]')
        elif kind == 'file':
            control.write_text('preserve')
        elif kind == 'symlink':
            control.symlink_to(tmp_path / 'other')
        if kind == 'pid-without-socket':
            publish_metadata(config)
        if kind in ('lock-only', 'locked-listener'):
            stack.enter_context(InstanceLock(config))
        before, paths = files(tmp_path), set(tmp_path.rglob('*'))
        assert not plan(config, 'p')['executable']
        with pytest.raises(ValueError, match='Purge refused'):
            run(instance)
        assert files(tmp_path) == before and set(tmp_path.rglob('*')) == paths


def test_socket_replaced_after_confirmation_preserved(instance):
    import socket
    config = load_config(instance[2])
    control = config.workspace / '.control.sock'
    with socket.socket(socket.AF_UNIX) as old:
        old.bind(str(control))
    class ReplacingTerminal(Terminal):
        def readline(self):
            control.unlink()
            replacement.bind(str(control))
            replacement.listen()
            return 'p\n'
    with socket.socket(socket.AF_UNIX) as replacement:
        with pytest.raises(ValueError, match='Active control'):
            execute(config, 'p', instance[2], stdin=ReplacingTerminal(), output=lambda s: None)
        assert control.is_socket()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(control))
    assert not (config.root_path / 'backups').exists()


def test_socket_replacement_during_probe_refused(instance, monkeypatch):
    import socket
    from dispatcher.purge import control_admission
    config = load_config(instance[2])
    control = config.workspace / '.control.sock'
    with socket.socket(socket.AF_UNIX) as old:
        old.bind(str(control))
    original = socket.socket.connect
    with socket.socket(socket.AF_UNIX) as replacement:
        def race(probe, path):
            try:
                return original(probe, path)
            finally:
                control.unlink()
                replacement.bind(str(control))
                replacement.listen()
        monkeypatch.setattr(socket.socket, 'connect', race)
        with pytest.raises(ValueError, match='changed during inspection|live kernel owner'):
            control_admission(config)
        assert control.is_socket()


@pytest.mark.parametrize('width', [80, 120])
@pytest.mark.parametrize('color', [False, True])
def test_removal_renderer_compact_literal_and_immutable(instance, monkeypatch, width, color):
    import copy
    from rich.console import Console
    from dispatcher.monitoring_output import render_removal_plan
    config = seed(instance)
    data = plan(config, 'p')
    data['project'] = '[red]проект[/red]' + 'длинный' * 8
    data['blockers'] = ['[red]literal[/red] unsafe socket identity']
    data['executable'] = False
    before = copy.deepcopy(data)
    if color:
        monkeypatch.setenv('NO_COLOR', '1')
    output = StringIO()
    render_removal_plan(data, console=Console(file=output, width=width, force_terminal=color))
    text = output.getvalue()
    for value in ('DRY RUN', 'Ready: NO', 'SQLite records', 'Codex results', '[red]literal[/red]', 'unsafe socket identity'):
        assert value in text
    for value in ('database_fingerprint', 'rowid', 'metadata', '\x1b[31m'):
        assert value not in text
    assert data == before


def test_cli_removal_dry_run_readonly(instance, monkeypatch, capsys, tmp_path):
    from dispatcher.__main__ import main
    config = seed(instance)
    before, paths = files(tmp_path), set(tmp_path.rglob('*'))
    monkeypatch.setattr('sys.argv', ['codex-dispatcher', '--config', str(instance[2]), 'project', 'remove', 'p', '--dry-run'])
    main()
    output = capsys.readouterr().out
    assert 'DRY RUN' in output and 'Ready: YES' in output
    assert 'database_fingerprint' not in output and 'Type full' not in output
    assert files(tmp_path) == before and set(tmp_path.rglob('*')) == paths


def test_stale_socket_dead_metadata_and_inherited_lock(instance):
    import socket
    import sys
    from dispatcher.monitoring import publish_metadata
    from dispatcher.locking import InstanceLock
    config = load_config(instance[2])
    control = config.workspace / '.control.sock'
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(control))
    # A real exited PID gives durable local absence rather than a guessed PID.
    process = subprocess.Popen([sys.executable, '-c', 'pass'])
    process.wait()
    publish_metadata(config)
    meta_path = config.workspace / '.monitor.json'
    meta = json.loads(meta_path.read_text())
    meta.update(pid=process.pid, updated_at='2000-01-01T00:00:00+00:00')
    meta_path.write_text(json.dumps(meta))
    assert plan(config, 'p')['executable']
    with InstanceLock(config) as lock:
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                 pass_fds=lock.fds, stdin=subprocess.PIPE)
    try:
        assert not plan(config, 'p')['executable']
        with pytest.raises(ValueError, match='Purge refused'):
            run(instance)
    finally:
        child.communicate()
    run(instance)
    assert control.is_socket()


def test_uncertain_socket_error_refused(instance, monkeypatch):
    import socket
    config = load_config(instance[2])
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(config.workspace / '.control.sock'))
    def timeout(*args):
        raise TimeoutError('uncertain listener')
    monkeypatch.setattr(socket.socket, 'connect', timeout)
    assert not plan(config, 'p')['executable']
    with pytest.raises(ValueError, match='Purge refused'):
        run(instance)
