"""Recovery integration uses a fake HTTP transport and real, local Git only."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import pytest

from dispatcher.config import Config, Project
from dispatcher.db_guard import maintain, check, connection, metadata_ddl, bind, inspection, validate
from dispatcher.git import Git, GitError
from dispatcher.github import GitHubAdapter, GitHubError
from dispatcher.models import IssueObservation, State
from dispatcher.recovery import Recovery, RecoveryStore
from dispatcher.services import GroupService, StateService, Scheduler, PollService
from dispatcher.storage import Storage, SchemaError, _ddl
from dispatcher.execution import Publisher, Executor
from dispatcher.cleanup import Cleanup
from dispatcher.input import snapshot_hash
from dispatcher.locking import InstanceLock, AlreadyRunning


class Logger:
    def record(self, *args, **kwargs):
        pass


class FakeHTTP:
    def __init__(self):
        self.issues = {1: self.issue(1)}
        self.prs, self.comments, self.calls = [], [], []
        self.fail_get = False
        self.uncertain_post = False
        self.keep_approve = False

    @staticmethod
    def issue(number):
        return {'id': number, 'number': number, 'title': f'Issue {number}', 'body': '',
                'state': 'open', 'labels': [{'name': 'dispatcher:codex-ready'}]}

    def __call__(self, method, endpoint, payload):
        self.calls.append((method, endpoint))
        if method == 'GET' and self.fail_get:
            raise GitHubError(503)
        path = endpoint.split('?')[0]
        if method == 'GET':
            if path.endswith('/pulls'):
                return self.prs
            if path.endswith('/issues'):
                return [i for i in self.issues.values() if i['state'] == 'open']
            if path.endswith('/sub_issues'):
                return []
            if path.endswith('/parent'):
                raise GitHubError(404)
            if path.endswith('/comments'):
                return self.comments
            if '/issues/' in path:
                return self.issues[int(path.rsplit('/', 1)[1])]
        if method == 'DELETE' and '/labels/' in path:
            if not self.keep_approve:
                number = int(path.split('/issues/')[1].split('/')[0])
                self.issues[number]['labels'] = [l for l in self.issues[number]['labels']
                                                 if l['name'] != 'dispatcher:approve']
            return None
        if method == 'POST' and path.endswith('/comments'):
            value = {'id': len(self.comments) + 1, 'body': payload['body']}
            self.comments.append(value)
            if self.uncertain_post:
                raise GitHubError()
            return value
        pytest.fail(f'Unexpected external mutation: {method} {endpoint}')


class LocalGit(Git):
    """Origin identity is injected; all ref/worktree operations use a local bare repo."""
    def run(self, directory, *args, **kwargs):
        if args == ('remote', 'get-url', 'origin'):
            return 'git@github.com:owner/repo.git'
        return super().run(directory, *args, **kwargs)


def git(*args):
    return subprocess.run(['git', *map(str, args)], check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def env(tmp_path):
    remote, checkout = tmp_path / 'remote.git', tmp_path / 'checkout'
    git('init', '--bare', remote)
    git('init', '-b', 'main', checkout)
    git('-C', checkout, 'config', 'user.email', 'test@example.invalid')
    git('-C', checkout, 'config', 'user.name', 'Synthetic test')
    (checkout / 'seed').write_text('seed')
    git('-C', checkout, 'add', 'seed')
    git('-C', checkout, 'commit', '-m', 'seed')
    git('-C', checkout, 'remote', 'add', 'origin', remote)
    git('-C', checkout, 'push', 'origin', 'main')
    config = Config(tmp_path / 'workspace', tmp_path / 'db',
                    (Project('p', 'owner/repo', checkout=checkout),))
    maintain(config, 'bootstrap')
    store = Storage(config.database)
    store.register_issue(IssueObservation(1, 'owner/repo', 1, 'Issue 1'), 'p')
    http = FakeHTTP()
    gh, localgit = GitHubAdapter(config, Logger(), transport=http, retries=0), LocalGit(config)
    return config, store, http, gh, localgit, Recovery(config, gh, localgit)


def barrier(config):
    with connection(config.database) as db:
        db.execute('UPDATE dispatcher_metadata SET recovery_required=1')


def quarantine(config, reason='execution-uncertain', issue_id=1):
    with RecoveryStore(config).transaction() as db:
        RecoveryStore.quarantine(db, issue_id, reason, 'synthetic')


def rows(config, query):
    with inspection(config.database) as db:
        return db.execute(query).fetchall()


def active(env, state='running', worktree=True):
    config, store, http, gh, localgit, service = env
    with store.transaction() as db:
        db.execute("UPDATE issues SET state=?,approved_at='before',approved_hash=?,"
                   "approved_title=title,approved_body='',base_branch='main',work_branch='codex/task-0001' WHERE issue_id=1",
                   (state, snapshot_hash('Issue 1', '', None)))
        db.execute("INSERT INTO executions(issue_id,status,started_at,codex_started_at) VALUES(1,'executing','before','before')")
    if worktree:
        path, sha = localgit.prepare(config.projects[0], 1, 'main', 'codex/task-0001')
        (path / 'unfinished').write_text('local work to discard')
        store.update_execution(1, worktree_path=str(path), initial_commit=sha)
    store.record_error(1, 'synthetic', 'codex', 'private diagnostic retained', 1)
    quarantine(config)
    return store.execution(1)


def test_current_schema_constraints(env):
    config, store, *_ = env
    assert check(config)[1] == 2
    quarantine(config)
    with store.transaction() as db:
        db.execute("INSERT INTO recovery_operations VALUES('a',1,'retry','intent','t','t',NULL,'{}')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO recovery_operations VALUES('b',1,'retry','intent','t','t',NULL,'{}')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO recovery_quarantine VALUES(999,'execution-uncertain','x','t','t')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO recovery_quarantine VALUES(1,'unknown','x','t','t')")


def legacy(config):
    config.database.unlink()
    StoragePath = config.database
    with connection(StoragePath) as db:
        db.executescript(_ddl(1))
        db.execute("INSERT INTO schema_migrations VALUES(1,'before')")
        db.execute(metadata_ddl(1))
        db.execute('INSERT INTO dispatcher_metadata VALUES(1,1,?,1)', (check_identity(config),))
    StoragePath.chmod(0o600)


def check_identity(config):
    return (config.workspace / '.dispatcher-instance').read_text().strip()


def test_explicit_upgrade_restore_historical_and_rollback(env, tmp_path, monkeypatch):
    config, store, *_ = env
    identity = check(config)[2]
    legacy(config)
    old = tmp_path / 'old-backup'
    maintain(config, 'backup', old)
    assert check(config)[1:] == (1, identity, 1)
    with pytest.raises(SchemaError, match='upgrade'):
        Storage(config.database)
    import dispatcher.db_guard as guard
    migrate = guard.migrate

    def fail(db):
        migrate(db)
        raise RuntimeError('fault after DDL')

    monkeypatch.setattr(guard, 'migrate', fail)
    with pytest.raises(RuntimeError):
        maintain(config, 'upgrade')
    assert check(config)[1:] == (1, identity, 1)
    assert list(config.database.parent.glob('db.before-upgrade-*'))
    monkeypatch.setattr(guard, 'migrate', migrate)
    maintain(config, 'upgrade')
    assert check(config)[1:] == (2, identity, 1)
    maintain(config, 'upgrade')
    maintain(config, 'restore', old)
    assert check(config)[1:] == (2, identity, 1)
    with inspection(old) as db:
        assert validate(db)[1] == 1


def test_recover_dry_run_readonly_and_atomic_release(env):
    config, store, http, gh, localgit, service = env
    barrier(config)
    before = config.database.read_bytes()
    planned = service.recover(dry_run=True)
    assert planned == [{'issue_id': 1, 'reasons': [], 'target': 'discovered',
                        'repository': 'owner/repo', 'issue_number': 1}]
    assert config.database.read_bytes() == before
    assert check(config)[3] == 1
    assert all(m == 'GET' for m, _ in http.calls)
    assert service.recover() == planned
    assert check(config)[3] == 0
    assert rows(config, 'SELECT count(*) FROM recovery_sessions') == [(1,)]


@pytest.mark.parametrize('fault', ['github', 'identity', 'origin', 'unknown-tree', 'wal', 'socket', 'lock'])
def test_global_fail_closed(env, fault, monkeypatch):
    config, store, http, gh, localgit, service = env
    barrier(config)
    if fault == 'github':
        http.fail_get = True
    elif fault == 'identity':
        http.issues[1]['id'] = 999
    elif fault == 'origin':
        service.git = Git(config)  # actual origin is local, not owner/repo
    elif fault == 'unknown-tree':
        localgit.prepare(config.projects[0], 99, 'main', 'other')
    elif fault == 'wal':
        wal = Path(str(config.database) + '-wal')
        wal.write_bytes(b'unknown')
    elif fault == 'socket':
        (config.workspace / '.control.sock').touch()
    if fault == 'lock':
        with InstanceLock(config), pytest.raises(AlreadyRunning):
            service.recover()
    else:
        with pytest.raises((SchemaError, GitHubError, sqlite3.DatabaseError)):
            service.recover()
    if fault == 'wal':
        wal.unlink(missing_ok=True)
    assert check(config)[3] == 1
    assert rows(config, 'SELECT count(*) FROM recovery_sessions') == [(0,)]


def test_no_partial_admission_when_facts_change(env, monkeypatch):
    config, store, http, gh, localgit, service = env
    barrier(config)
    collect = service.collect
    count = 0

    def changing():
        nonlocal count
        count += 1
        if count == 2:
            http.issues[1]['title'] = 'changed'
        return collect()

    monkeypatch.setattr(service, 'collect', changing)
    with pytest.raises(SchemaError, match='changed'):
        service.recover()
    assert check(config)[3] == 1
    assert not rows(config, 'SELECT * FROM recovery_sessions')


def test_independent_issue_and_existing_quarantine_survive(env):
    config, store, http, gh, localgit, service = env
    active(env)
    store.register_issue(IssueObservation(2, 'owner/repo', 2, 'Issue 2'), 'p')
    http.issues[2] = http.issue(2)
    with store.transaction() as db:
        db.execute("UPDATE issues SET state='pending',approved_at='t',approved_hash=? WHERE issue_id=2",
                   (snapshot_hash('Issue 2', '', None),))
    barrier(config)
    service.recover()
    assert store.blocked(1)
    assert not store.blocked(2)
    groups = GroupService(store)
    state = StateService(store, groups, config)
    assert Scheduler(store, groups, state).reserve(1) is None
    assert Scheduler(store, groups, state).reserve(2).issue_id == 2
    service.recover()
    assert store.blocked(1)


def test_disabled_isolation_persists_after_enable(env):
    config, store, http, gh, localgit, service = env
    disabled = replace(config, projects=(replace(config.projects[0], enabled=False),))
    barrier(config)
    Recovery(disabled, object(), object()).recover()
    assert store.blocked(1)
    service.recover()
    assert store.blocked(1)


def test_new_after_backup_is_not_approved(env):
    config, store, http, gh, localgit, service = env
    http.issues[2] = http.issue(2)
    http.issues[2]['labels'].append({'name': 'dispatcher:approve'})
    barrier(config)
    service.recover()
    assert store.issue(2)['state'] == 'discovered'
    assert store.blocked(2)


@pytest.mark.parametrize('phase', ['intent', 'archived', 'approval-reset', 'cleaned', 'reset',
                                   'post-comment', 'commented'])
def test_retry_resumes_with_archive_fk_comment_dedup_and_fresh_approval(env, phase):
    config, store, http, gh, localgit, service = env
    execution = active(env)
    http.issues[1]['labels'].append({'name': 'dispatcher:approve'})

    def fail(value):
        if value == phase:
            raise RuntimeError('fault-' + value)

    service.fault = fail
    with pytest.raises(RuntimeError, match='fault'):
        service.command('retry', 'p', 1)
    assert store.blocked(1)
    if phase != 'intent':
        assert rows(config, 'SELECT count(*) FROM recovery_execution_archive') == [(1,)]
    service.fault = lambda phase: None
    op = service.command('retry', 'p', 1)
    assert not store.blocked(1)
    assert store.issue(1)['state'] == 'discovered'
    assert store.issue(1)['approved_at'] is None
    assert not store.rows('SELECT * FROM executions')
    assert rows(config, 'SELECT execution_id,error_message FROM error_log') == [(None, 'private diagnostic retained')]
    assert rows(config, 'SELECT count(*) FROM recovery_execution_archive') == [(1,)]
    assert rows(config, 'SELECT approval_absent_observed FROM recovery_approval_reset') == [(1,)]
    assert rows(config, 'SELECT count(*) FROM state_history WHERE new_state="running"') == [(0,)]
    assert not Path(execution['worktree_path']).exists()
    assert len(http.comments) == 1
    assert op in http.comments[0]['body']
    assert not any(c['name'] == 'dispatcher:approve' for c in http.issues[1]['labels'])
    groups = GroupService(store)
    state = StateService(store, groups, config)
    assert Scheduler(store, groups, state).reserve(1) is None
    http.issues[1]['labels'].append({'name': 'dispatcher:approve'})
    assert state.approve(config.projects[0], gh.issue(config.projects[0], 1), None)
    assert Scheduler(store, groups, state).reserve(1) is not None


def test_uncertain_post_is_found_on_resume(env):
    config, store, http, gh, localgit, service = env
    active(env)
    http.uncertain_post = True
    with pytest.raises(GitHubError):
        service.command('retry', 'p', 1)
    assert store.blocked(1)
    service.command('retry', 'p', 1)
    assert len(http.comments) == 1
    assert not store.blocked(1)


def test_comment_intent_before_post_resumes_after_complete_read(env):
    config, store, http, gh, localgit, service = env
    active(env)
    service.fault = lambda phase: (_ for _ in ()).throw(RuntimeError('crash')) if phase == 'comment-intent' else None
    with pytest.raises(RuntimeError):
        service.command('retry', 'p', 1)
    service.fault = lambda phase: None
    service.command('retry', 'p', 1)
    assert not store.blocked(1)
    assert len(http.comments) == 1


def test_retry_archive_precedes_any_cleanup_and_stale_approve_blocks(env, monkeypatch):
    config, store, http, gh, localgit, service = env
    execution = active(env)
    remove = localgit.recovery_remove

    def checked_remove(project, plan):
        assert rows(config, 'SELECT count(*) FROM recovery_execution_archive') == [(1,)]
        assert rows(config, 'SELECT approval_absent_observed FROM recovery_approval_reset') == [(1,)]
        remove(project, plan)

    monkeypatch.setattr(localgit, 'recovery_remove', checked_remove)
    http.keep_approve = True
    http.issues[1]['labels'].append({'name': 'dispatcher:approve'})
    with pytest.raises(SchemaError, match='absence'):
        service.command('retry', 'p', 1)
    assert Path(execution['worktree_path']).exists()
    assert store.blocked(1)
    http.keep_approve = False
    service.command('retry', 'p', 1)


def test_retry_crash_inside_git_cleanup_resumes(env, monkeypatch):
    config, store, http, gh, localgit, service = env
    active(env)
    run = localgit.run
    crashed = False

    def interrupt(directory, *args, **kwargs):
        nonlocal crashed
        result = run(directory, *args, **kwargs)
        if args[:2] == ('worktree', 'remove') and not crashed:
            crashed = True
            raise RuntimeError('after removal')
        return result

    monkeypatch.setattr(localgit, 'run', interrupt)
    with pytest.raises(RuntimeError):
        service.command('retry', 'p', 1)
    service.command('retry', 'p', 1)
    assert not store.blocked(1)


@pytest.mark.parametrize('conflict', ['remote', 'pr', 'foreign-path', 'local-branch', 'published', 'group'])
def test_retry_refuses_conflicts_preserving_work(env, conflict):
    config, store, http, gh, localgit, service = env
    execution = active(env)
    checkout = config.projects[0].checkout
    if conflict == 'remote':
        git('-C', checkout, 'push', 'origin', 'HEAD:refs/heads/codex/task-0001')
    elif conflict == 'pr':
        http.prs.append(pr_value())
    elif conflict == 'foreign-path':
        with store.transaction() as db:
            db.execute('UPDATE executions SET worktree_path=? WHERE execution_id=1', (str(checkout),))
    elif conflict == 'local-branch':
        git('-C', checkout, 'branch', 'codex/task-0001')
    elif conflict == 'published':
        store.update_execution(1, push_completed=1)
    elif conflict == 'group':
        store.register_issue(IssueObservation(2, 'owner/repo', 2, 'Issue 2'), 'p')
        http.issues[2] = http.issue(2)
        with store.transaction() as db:
            db.execute('INSERT INTO issue_relations VALUES(1,2)')
    with pytest.raises((SchemaError, GitError)):
        service.command('retry', 'p', 1)
    assert Path(execution['worktree_path']).exists()
    assert store.blocked(1)
    assert not rows(config, 'SELECT * FROM recovery_execution_archive')


def pr_value(state='open', merged=False, sha='a' * 40):
    return {'id': 100, 'number': 10, 'state': state, 'merged_at': 'now' if merged else None,
            'body': 'Dispatcher result for #1', 'head': {'ref': 'codex/task-0001', 'sha': sha,
            'repo': {'full_name': 'owner/repo'}}, 'base': {'ref': 'main', 'repo': {'full_name': 'owner/repo'}}}


@pytest.mark.parametrize('state,pr_state,merged,target', [
    ('running', 'open', False, 'review'), ('review', 'open', False, 'review'),
    ('review', 'closed', True, 'done'), ('review', 'closed', False, 'rejected'),
    ('manual-running', 'open', False, 'review'),
])
def test_accept_proven_pr_without_fictional_execution(env, state, pr_state, merged, target):
    config, store, http, gh, localgit, service = env
    active(env, state, worktree=False)
    store.update_execution(1, result_commit='a' * 40)
    http.prs = [pr_value(pr_state, merged)]
    service.command('accept', 'p', 1)
    assert store.issue(1)['state'] == target
    assert len(store.rows('SELECT * FROM executions')) == 1
    assert not store.blocked(1)
    assert all(m == 'GET' for m, _ in http.calls)


def test_accept_branch_name_alone_is_not_proof(env):
    config, store, http, gh, localgit, service = env
    active(env, worktree=False)
    http.prs = [pr_value()]
    with pytest.raises(SchemaError, match='ownership'):
        service.command('accept', 'p', 1)
    assert store.blocked(1)


def test_cancel_safe_issue_preserves_local_artifacts(env):
    config, store, http, gh, localgit, service = env
    quarantine(config, 'approval-uncertain')
    service.command('cancel', 'p', 1)
    assert store.issue(1)['state'] == 'cancelled'
    assert not store.blocked(1)
    assert rows(config, "SELECT action FROM recovery_events") == [('cancel',)]


def test_quarantined_child_blocks_nested_container_without_parent_quarantine(env):
    config, store, http, gh, localgit, service = env
    for n in (2, 3, 4):
        store.register_issue(IssueObservation(n, 'owner/repo', n, f'Issue {n}'), 'p')
    with store.transaction() as db:
        db.execute("UPDATE issues SET state='pending',approved_at='t' WHERE issue_id IN (1,2,4)")
        db.execute('INSERT INTO issue_relations VALUES(1,2)')
        db.execute('INSERT INTO issue_relations VALUES(2,3)')
    quarantine(config, issue_id=3)
    assert store.blocked(1) and store.blocked(2) and not store.blocked(4)
    assert rows(config, 'SELECT issue_id FROM recovery_quarantine') == [(3,)]
    assert not GroupService(store).evaluate(1).eligible


def test_all_direct_side_effect_paths_refuse_quarantine(env):
    config, store, http, gh, localgit, service = env
    active(env)
    groups = GroupService(store)
    state = StateService(store, groups, config)
    publisher = Publisher(config, store, gh, localgit, state)
    executor = Executor(config, store, localgit, state, publisher, object())
    before = len(http.calls)
    for action in (lambda: publisher.publish(1), lambda: publisher.phase_output(1),
                   lambda: publisher.output(1, 'blocked'), lambda: publisher.fail(1),
                   lambda: publisher.send_final_error_report(1), lambda: executor.prepare(1),
                   lambda: executor.run_once(1)):
        with pytest.raises(SchemaError, match='quarantine'):
            action()
    executor.recover()
    Cleanup(config, store, localgit).known()
    assert len(http.calls) == before
    assert not state.transition(1, State.RUNNING, State.REVIEW, 'blocked')
    assert store.issue(1)['state'] == 'running'


def test_retry_completed_repeated_command_has_no_second_post(env):
    config, store, http, gh, localgit, service = env
    active(env)
    op = service.command('retry', 'p', 1)
    assert service.command('retry', 'p', 1) == op
    assert len(http.comments) == 1


def test_recovery_audit_is_append_only(env):
    config, store, http, gh, localgit, service = env
    service.recover()
    with store.transaction() as db:
        for query in ('DELETE FROM recovery_events', "UPDATE recovery_events SET action='tamper'"):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                db.execute(query)


def test_accept_known_pr_without_execution(env):
    config, store, http, gh, localgit, service = env
    from dispatcher.models import PRObservation, PRStatus
    with store.transaction() as db:
        db.execute("UPDATE issues SET state='review',base_branch='main',work_branch='codex/task-0001' WHERE issue_id=1")
    store.record_pr(PRObservation(100, 1, 'owner/repo', 10, 'codex/task-0001', 'main', PRStatus.OPEN))
    quarantine(config, 'pr-conflict')
    http.prs = [pr_value('closed', True)]
    service.command('accept', 'p', 1)
    assert store.issue(1)['state'] == 'done'
    assert not store.rows('SELECT * FROM executions')


@pytest.mark.parametrize('state', ['discovered', 'pending', 'awaiting-children', 'preparing', 'running',
                                   'manual-running', 'review', 'rejected', 'need-fix', 'error', 'done', 'cancelled'])
def test_recover_state_classification(env, state):
    config, store, http, gh, localgit, service = env
    with store.transaction() as db:
        db.execute("UPDATE issues SET state=?,approved_at=?,approved_hash=? WHERE issue_id=1",
                   (state, 'before' if state in ('pending', 'awaiting-children') else None,
                    snapshot_hash('Issue 1', '', None)))
    barrier(config)
    plan = service.recover()
    assert check(config)[3] == 0
    if state in ('preparing', 'running'):
        assert 'execution-uncertain' in plan[0]['reasons']
        assert store.blocked(1)
    elif state == 'review':
        assert 'pr-conflict' in plan[0]['reasons']
    else:
        assert not plan[0]['reasons']


@pytest.mark.parametrize('merged,closed,target', [(False, False, 'review'), (True, True, 'done'), (False, True, 'rejected')])
def test_recover_review_pr_and_retargeted_merge(env, merged, closed, target):
    config, store, http, gh, localgit, service = env
    active(env, 'review', False)
    with RecoveryStore(config).transaction() as db:
        db.execute('DELETE FROM recovery_quarantine')
    store.update_execution(1, result_commit='a' * 40)
    http.prs = [pr_value('closed' if closed else 'open', merged)]
    http.prs[0]['base']['ref'] = 'other-base'
    barrier(config)
    service.recover()
    assert store.issue(1)['state'] == target


def test_recover_known_closed_issue_is_fetched(env):
    config, store, http, gh, localgit, service = env
    with store.transaction() as db:
        db.execute("UPDATE issues SET state='done' WHERE issue_id=1")
    http.issues[1]['state'] = 'closed'
    barrier(config)
    assert service.recover()[0]['reasons'] == []
    assert any('/issues/1' in endpoint for method, endpoint in http.calls)


def test_poll_cannot_remove_quarantine_or_stale_approve(env):
    config, store, http, gh, localgit, service = env
    active(env)
    http.issues[1]['labels'].append({'name': 'dispatcher:approve'})
    groups = GroupService(store)
    state = StateService(store, groups, config)
    publisher = Publisher(config, store, gh, localgit, state)
    PollService(config, store, gh, state, publisher, Logger()).poll_project(config.projects[0])
    assert store.blocked(1)
    assert store.issue(1)['state'] == 'running'
    assert all(method == 'GET' for method, endpoint in http.calls)


def test_readonly_metrics_and_status_with_barrier(env):
    config, store, http, gh, localgit, _ = env
    verbose = replace(config, debug_mode='api')
    service = Recovery(verbose, gh, localgit)
    barrier(config)
    before = config.database.read_bytes()
    service.status()
    service.recover(dry_run=True)
    assert config.database.read_bytes() == before
    service.recover()
    assert rows(config, "SELECT count(*) FROM debug_operations WHERE level='api'")[0][0] > 0


def test_retry_leaves_unresolved_reason(env):
    config, store, http, gh, localgit, service = env
    active(env)
    quarantine(config, 'pr-conflict')
    service.command('retry', 'p', 1)
    assert store.blocked(1)
    assert store.issue(1)['state'] == 'discovered'
    assert rows(config, 'SELECT reason_code FROM recovery_quarantine') == [('pr-conflict',)]


def test_retry_late_remote_result_retains_quarantine(env):
    config, store, http, gh, localgit, service = env
    active(env)

    def pause(phase):
        if phase == 'reset':
            raise RuntimeError('stop')

    service.fault = pause
    with pytest.raises(RuntimeError):
        service.command('retry', 'p', 1)
    git('-C', config.projects[0].checkout, 'push', 'origin', 'HEAD:refs/heads/codex/task-0001')
    service.fault = lambda phase: None
    with pytest.raises(SchemaError, match='Remote'):
        service.command('retry', 'p', 1)
    assert store.blocked(1)


def test_restore_v2_preserves_quarantine_events_and_operation(env, tmp_path):
    config, store, http, gh, localgit, service = env
    quarantine(config)
    with RecoveryStore(config).transaction() as db:
        db.execute("INSERT INTO recovery_operations VALUES('op',1,'retry','intent','t','t',NULL,'{}')")
        RecoveryStore.event(db, 1, 'intent', operation='op')
    destination = tmp_path / 'snapshot-v2'
    maintain(config, 'backup', destination)
    with connection(config.database) as db:
        assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    maintain(config, 'restore', destination)
    assert check(config)[3] == 1
    assert rows(config, 'SELECT reason_code FROM recovery_quarantine') == [('execution-uncertain',)]
    assert rows(config, 'SELECT operation_id,phase FROM recovery_operations') == [('op', 'intent')]
    assert rows(config, 'SELECT operation_id,action FROM recovery_events') == [('op', 'intent')]


def test_recovery_sql_failure_rolls_back_admission_and_quarantine(env, monkeypatch):
    config, store, http, gh, localgit, service = env
    barrier(config)

    def fail(*args, **kwargs):
        raise RuntimeError('fault inside admission transaction')

    monkeypatch.setattr(service, 'apply_result', fail)
    with pytest.raises(RuntimeError):
        service.recover()
    assert check(config)[3] == 1
    assert not rows(config, 'SELECT * FROM recovery_sessions')
    assert not rows(config, 'SELECT * FROM recovery_events')


def test_strict_recovery_schema_json_and_identity(env):
    config, store, http, gh, localgit, service = env
    quarantine(config)
    with store.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO recovery_operations VALUES('op',1,'retry','intent','t','t',NULL,'invalid json')")
        db.execute('DROP INDEX idx_recovery_active')
    with pytest.raises(SchemaError):
        check(config)


def test_status_v1_readonly_and_upgrade_required(env):
    config, store, http, gh, localgit, service = env
    legacy(config)
    before = config.database.read_bytes()
    assert service.status()['upgrade_required']
    assert config.database.read_bytes() == before
    with pytest.raises(SchemaError, match='upgrade'):
        service.command('retry', 'p', 1)


def test_missing_execution_error_message_is_retained_privately(env):
    config, store, http, gh, localgit, service = env
    active(env)
    store.update_execution(1, error_message='private execution-only diagnostic')
    service.command('retry', 'p', 1)
    assert rows(config, "SELECT error_message FROM error_log WHERE error_stage='recovery_archive'") == [
        ('private execution-only diagnostic',)]
    archive = rows(config, 'SELECT execution_snapshot,related_diagnostics FROM recovery_execution_archive')[0]
    assert 'private execution-only diagnostic' not in ''.join(archive)


def test_recover_cli_dry_run_and_status_do_not_construct_runtime(env, monkeypatch, capsys):
    config, store, http, gh, localgit, service = env
    config_file = config.database.parent / 'dispatcher.yaml'
    config_file.write_text('dispatcher:\n  workspace: workspace\n  database: db\nprojects: []\n')
    monkeypatch.setattr('dispatcher.__main__.Runtime', lambda *a: pytest.fail('runtime created'))
    monkeypatch.setattr('sys.argv', ['codex-dispatcher', '--config', str(config_file),
                                   'db', 'recovery', 'status', '--json'])
    from dispatcher.__main__ import main
    main()
    assert json.loads(capsys.readouterr().out)['format_version'] == 2


def test_completed_retry_requires_observed_approve_even_in_direct_state_service(env):
    config, store, http, gh, localgit, service = env
    active(env)
    service.command('retry', 'p', 1)
    groups = GroupService(store)
    state = StateService(store, groups, config)
    assert not state.approve(config.projects[0], gh.issue(config.projects[0], 1), None)
    assert store.issue(1)['state'] == 'discovered'


def test_accept_terminal_parent_remains_blocked_by_quarantined_child(env):
    config, store, http, gh, localgit, service = env
    active(env, 'review', False)
    store.update_execution(1, result_commit='a' * 40)
    http.prs = [pr_value('closed', True)]
    store.register_issue(IssueObservation(2, 'owner/repo', 2, 'Issue 2'), 'p')
    http.issues[2] = http.issue(2)
    with store.transaction() as db:
        db.execute("UPDATE issues SET state='done' WHERE issue_id=2")
        db.execute('INSERT INTO issue_relations VALUES(1,2)')
    quarantine(config, issue_id=2)
    with pytest.raises(SchemaError, match='child'):
        service.command('accept', 'p', 1)
    assert store.issue(1)['state'] == 'review'


def test_recovery_api_collection_is_bounded_per_repository(env):
    config, store, http, gh, localgit, service = env
    service.recover()
    assert len(http.calls) <= 14  # two complete read passes; no per-PR followups when batch contains it
    assert sum('/pulls?' in endpoint for method, endpoint in http.calls) == 2


def test_accept_clean_confirmed_worktree_resolves_local_quarantine(env):
    config, store, http, gh, localgit, service = env
    execution = active(env)
    path = Path(execution['worktree_path'])
    sha = localgit.commit_push(config.projects[0], path, 'codex/task-0001')
    store.update_execution(1, result_commit=sha, push_completed=1)
    http.prs = [pr_value(sha=sha)]
    quarantine(config, 'local-worktree')
    quarantine(config, 'branch-conflict')
    quarantine(config, 'approval-uncertain')
    service.command('accept', 'p', 1)
    assert store.issue(1)['state'] == 'review'
    assert not store.blocked(1)
    assert path.exists()  # accept observes the result without deleting local files
    assert store.execution(1)['status'] == 'completed'


def test_archived_error_post_intent_does_not_suppress_new_attempt_report(env):
    config, store, http, gh, localgit, service = env
    active(env)
    store.record_error(1, 'error_report_attempt', 'error_report', 'old uncertain post', 1)
    service.command('retry', 'p', 1)
    http.issues[1]['labels'].append({'name': 'dispatcher:approve'})
    groups = GroupService(store)
    state = StateService(store, groups, config)
    assert state.approve(config.projects[0], gh.issue(config.projects[0], 1), None)
    new = Scheduler(store, groups, state).reserve(1)
    store.record_error(1, 'codex_start_failed', 'codex', 'new failure', new.execution_id)
    publisher = Publisher(config, store, gh, localgit, state)
    assert publisher.send_final_error_report(1)
    assert len(http.comments) == 2
    assert 'codex_start_failed' in http.comments[-1]['body']


def test_crash_after_completed_commit_is_safe_and_idempotent(env):
    config, store, http, gh, localgit, service = env
    active(env)
    service.fault = lambda phase: (_ for _ in ()).throw(RuntimeError('lost reply')) if phase == 'completed' else None
    with pytest.raises(RuntimeError):
        service.command('retry', 'p', 1)
    assert not store.blocked(1)
    assert store.issue(1)['state'] == 'discovered'
    service.fault = lambda phase: None
    op = service.command('retry', 'p', 1)
    assert op in http.comments[0]['body']
    assert len(http.comments) == 1


def test_incomplete_comment_pagination_keeps_global_barrier(env):
    config, store, http, gh, localgit, service = env
    transport = gh.transport

    def incomplete(method, endpoint, payload):
        if method == 'GET' and '/comments?' in endpoint:
            if 'page=2' in endpoint:
                raise GitHubError(503)
            return [{'id': n, 'body': 'unrelated'} for n in range(100)]
        return transport(method, endpoint, payload)

    gh.transport = incomplete
    barrier(config)
    with pytest.raises(GitHubError):
        service.recover()
    assert check(config)[3] == 1
    assert not rows(config, 'SELECT * FROM recovery_sessions')
    assert not rows(config, 'SELECT * FROM recovery_events')


def test_display_identity_uses_sqlite_and_new_github_observation(env):
    config, store, http, gh, localgit, service = env
    with store.transaction() as db:
        db.execute('UPDATE issues SET issue_number=41 WHERE issue_id=1')
    http.issues = {41: dict(http.issue(41), id=1), 42: dict(http.issue(42), id=987654321)}
    planned = service.recover(dry_run=True)
    assert [(p['issue_id'], p['repository'], p['issue_number']) for p in planned] == [
        (1, 'owner/repo', 41), (987654321, 'owner/repo', 42)]
    assert store.issue(987654321) is None
    service.recover()
    assert store.issue(987654321)['issue_number'] == 42


def test_status_join_keeps_equal_numbers_in_distinct_repositories(env):
    config, store, http, gh, localgit, service = env
    store.register_issue(IssueObservation(987654321, 'Other/Repo', 1, 'Other issue'), 'other')
    quarantine(config, issue_id=1)
    quarantine(config, 'local-worktree', issue_id=1)
    quarantine(config, issue_id=987654321)
    with RecoveryStore(config).transaction() as db:
        for op, issue_id, phase in [('first', 1, 'completed'), ('second', 987654321, 'archived')]:
            db.execute('INSERT INTO recovery_operations VALUES (?,?,?,?,?,?,?,?)',
                       (op, issue_id, 'retry', phase, 'before', 'after', None, '{}'))
    before = config.database.read_bytes()
    result = service.status()
    assert not http.calls
    assert config.database.read_bytes() == before
    assert {(q['issue_id'], q['repository'], q['issue_number']) for q in result['quarantine']} == {
        (1, 'owner/repo', 1), (987654321, 'other/repo', 1)}
    assert [(o['issue_id'], o['repository'], o['issue_number'], o['phase']) for o in result['operations']] == [
        (1, 'owner/repo', 1, 'completed'), (987654321, 'other/repo', 1, 'archived')]
    assert all('details' not in o for o in result['operations'])


@pytest.mark.parametrize('count', [2, 3])
def test_worktree_enumeration_raw_stability(env, count):
    config, store, http, gh, adapter, service = env
    project = config.projects[0]
    paths = []
    for number in range(1, count):
        path, _ = adapter.prepare(project, number, 'main', 'unused')
        (path / 'local-result').write_text('unpublished')
        paths.append(str(path))
    checkout = project.checkout
    before = git('-C', checkout, 'for-each-ref')
    registrations = checkout / '.git' / 'worktrees'
    registered_before = {str(p): p.read_bytes() for p in registrations.rglob('*') if p.is_file()}
    previous = None
    for _ in range(12):
        args = ['git', '-C', str(checkout), 'worktree', 'list', '--porcelain', '-z']
        raw = subprocess.run(args, capture_output=True, check=True)
        assert raw.stderr == b''
        shell = subprocess.run(['bash', '--noprofile', '--norc', '-c', 'exec "$@"',
                                'bash', *args], capture_output=True, check=True)
        cwd = subprocess.run(['git', *args[3:]], cwd=checkout, capture_output=True, check=True)
        assert shell.stdout == cwd.stdout == raw.stdout
        assert shell.stderr == cwd.stderr == b''
        assert raw.stdout.count(b'worktree ') == count
        assert previous is None or previous == raw.stdout
        previous = raw.stdout
        command = adapter.command(args, 'worktree')
        assert command.returncode == 0 and command.stderr == ''
        assert command.stdout.encode() == raw.stdout
        assert adapter.run(checkout, 'worktree', 'list', '--porcelain', '-z').encode() == raw.stdout
        plain = subprocess.run(args[:-1], capture_output=True, check=True)
        assert plain.stdout.count(b'worktree ') == count
        listed = adapter.worktrees(project)
        assert len(listed) == count
        assert {t['worktree'] for t in listed[1:]} == set(paths)
    assert git('-C', checkout, 'for-each-ref') == before
    assert {str(p): p.read_bytes() for p in registrations.rglob('*') if p.is_file()} == registered_before
    for path in paths:
        assert (Path(path) / 'local-result').read_text() == 'unpublished'


@pytest.mark.parametrize('damage', ['truncated', 'duplicate-field', 'duplicate-path', 'failure'])
def test_worktree_ambiguous_enumeration_refused(env, monkeypatch, damage):
    config, _, _, _, adapter, _ = env
    path = str(config.projects[0].checkout)
    record = f'worktree {path}\0HEAD {"a" * 40}\0branch refs/heads/main\0\0'
    raw = {'truncated': record[:-1], 'duplicate-field': record.replace('HEAD ', 'HEAD x\0HEAD '),
           'duplicate-path': record * 2, 'failure': record}[damage]
    original = adapter.run
    def run(directory, *args, **kwargs):
        if args[:2] == ('worktree', 'list'):
            return subprocess.CompletedProcess([], int(damage == 'failure'), raw, '')
        return original(directory, *args, **kwargs)
    monkeypatch.setattr(adapter, 'run', run)
    with pytest.raises(GitError):
        adapter.worktrees(config.projects[0])


def test_restore_older_snapshot_unknown_worktree_keeps_barrier(env, tmp_path):
    config, store, http, gh, adapter, service = env
    snapshot = tmp_path / 'old-snapshot'
    maintain(config, 'backup', snapshot)
    path, sha = adapter.prepare(config.projects[0], 1, 'main', 'unused')
    (path / 'unpublished').write_text('retain')
    refs = git('-C', config.projects[0].checkout, 'for-each-ref')
    with connection(config.database) as db:
        assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    maintain(config, 'restore', snapshot)
    calls = list(http.calls)
    for _ in range(2):
        with pytest.raises(SchemaError, match='Unknown worktree'):
            service.recover()
        assert check(config)[3] == 1
        assert (path / 'unpublished').read_text() == 'retain'
        assert git('-C', config.projects[0].checkout, 'for-each-ref') == refs
        assert http.calls == calls


@pytest.mark.parametrize('conflict', ['branch', 'locked', 'prunable', 'foreign', 'symlink', 'changed-head'])
def test_cleanup_registration_conflicts_preserve_artifacts(env, conflict):
    config, store, _, _, adapter, service = env
    execution = dict(active(env))
    project = config.projects[0]
    path = Path(execution['worktree_path'])
    local = service.local(project)
    if conflict == 'changed-head':
        plan = adapter.recovery_cleanup_plan(project, execution, local)
        git('-C', path, 'add', '.')
        git('-C', path, 'commit', '-m', 'changed after planning')
        with pytest.raises(GitError, match='identity changed'):
            adapter.recovery_remove(project, plan)
    else:
        if conflict == 'branch':
            git('-C', path, 'switch', '-c', 'foreign')
        elif conflict == 'locked':
            git('-C', project.checkout, 'worktree', 'lock', path)
        elif conflict == 'prunable':
            moved = path.with_name('retained-directory')
            path.rename(moved)
        elif conflict == 'foreign':
            execution['worktree_path'] = str(project.checkout)
        elif conflict == 'symlink':
            moved = path.with_name('retained-directory')
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=True)
        with pytest.raises((GitError, SchemaError)):
            local = service.local(project)
            adapter.recovery_cleanup_plan(project, execution, local)
    retained = moved if conflict in ('prunable', 'symlink') else path
    assert (retained / 'unfinished').read_text() == 'local work to discard'


def test_cleanup_positive_unique_owned_dirty_worktree(env):
    config, store, _, _, adapter, service = env
    execution = active(env)
    project = config.projects[0]
    extra, _ = adapter.prepare(project, 2, 'main', 'unused')
    (extra / 'retained').write_text('other work')
    plan = adapter.recovery_cleanup_plan(project, execution, service.local(project))
    adapter.recovery_remove(project, plan)
    assert not Path(execution['worktree_path']).exists()
    assert (extra / 'retained').read_text() == 'other work'
