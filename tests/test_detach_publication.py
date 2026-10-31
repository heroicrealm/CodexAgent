"""D2/D3: publication precedes deletion and ordinary diagnostics reconcile."""
import json
from pathlib import Path
from unittest.mock import Mock

from test_stage04 import env, issue, adapter, register, finish, git
from test_detachment import wire, request_stop
from dispatcher.cleanup import CleanupService
from dispatcher.detachment import StopController
from dispatcher.github import GitHubError
from dispatcher.runtime import Lifecycle
from dispatcher.scheduler import Dispatcher
from dispatcher.workspace import TaskError
import pytest


def test_idle_dirty_detach_push_retry_restart(env, monkeypatch):
    config, project, _, _, remote = env
    items = [issue(project)]
    api = adapter(items)
    lifecycle = Lifecycle()
    d = Dispatcher(config, lifecycle, api)
    try:
        register(d, project, items)
        d.admit()
        attempt = next(iter(d.active))
        finish(d)
        saved = d.execution.get(attempt)
        worktree = Path(saved['worktree'])
        (worktree / 'salvage').write_text('keep me')
        request_stop(d.store)
        cleanup = CleanupService(config, d.store)
        monkeypatch.setattr(cleanup.manager, 'push', lambda *a, **k: (_ for _ in ()).throw(TaskError('git_operation_failed')))
        cleanup.tick()
        assert worktree.exists()
        assert (worktree / 'salvage').exists()
        assert d.store.db.execute('SELECT state FROM cleanup_jobs').fetchone()[0] == 'pending'
        d.close()
        d = Dispatcher(config, lifecycle, api)
        with d.store.db:
            d.store.db.execute('UPDATE cleanup_jobs SET next_retry_at=NULL')
        CleanupService(config, d.store).tick()
        assert not worktree.exists()
        saved = d.execution.get(attempt)
        assert saved['pushed'] and saved['remote_sha'] == saved['result_sha']
        assert git(remote, 'rev-parse', saved['branch']) == saved['result_sha']
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert d.store.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 1
        assert not any(c.args[1] == 'PATCH' and c.args[0].endswith('/pulls/2') for c in api.write.call_args_list)
    finally:
        lifecycle.cancel()
        d.close()


def test_ordinary_comment_pending_actor_and_lost_response(env):
    config, project, *_ = env
    api = adapter([issue(project)])
    lifecycle = Lifecycle()
    d = Dispatcher(config, lifecycle, api)
    try:
        d.store.apply(project, [issue(project)])
        api.control_issue.return_value = None
        api.control_issue.side_effect = None
        api.command_events.return_value = ([dict(id=78,event='unlabeled',label={'name':project.task_label},
            actor={'login':'alice','type':'User'},created_at='2026-10-24T00:09:00Z')],
            dict(databaseId=1,labels=dict(nodes=[],pageInfo={'hasNextPage':False})))
        d.stops.observe(project,d.store.queue()[0],raw=dict(id=1,number=1,labels=[]))
        comments = []
        api.pages.side_effect = lambda endpoint: comments
        def lost(endpoint, method, payload):
            comments.append(dict(id=99,body=payload['body']))
            raise GitHubError(kind='unavailable')
        api.write.side_effect = lost
        d.stops.diagnostics()
        api.write.side_effect = None
        d.stops.diagnostics()
        d.stops.diagnostics()
        assert len(comments) == 1
        body = comments[0]['body']
        assert body.startswith('Tracking stopped')
        assert 'alice' in body and '2026-' in body
        assert 'Emergency stop' not in body
        assert d.store.db.execute('SELECT delivered FROM stop_outbox').fetchone()[0] == 1
    finally:
        lifecycle.cancel()
        d.close()


@pytest.mark.parametrize('failure', ['commit', 'verify', 'lost_push'])
def test_publication_failures_preserve_and_retry_without_codex(env, monkeypatch, failure):
    config, project, control, _, remote = env
    control.write_text(json.dumps({'exec':'dirty'}))
    items = [issue(project)]
    d = Dispatcher(config, Lifecycle(), adapter(items))
    try:
        register(d, project, items)
        d.admit()
        attempt = next(iter(d.active))
        finish(d)
        worktree = Path(d.execution.get(attempt)['worktree'])
        (worktree / 'result.txt').write_text('additional unpublished result')
        request_stop(d.store)
        cleanup = CleanupService(config, d.store)
        original_git = cleanup.manager.git
        original_push = cleanup.manager.push
        def broken(cwd, *args, **kwargs):
            if failure == 'commit' and args[0] == 'commit' or failure == 'verify' and args[0] == 'ls-remote':
                raise TaskError('git_operation_failed', 'GIT')
            return original_git(cwd, *args, **kwargs)
        monkeypatch.setattr(cleanup.manager, 'git', broken)
        if failure == 'lost_push':
            def lost(*args, **kwargs):
                original_push(*args, **kwargs)
                raise TaskError('git_operation_failed', 'GIT')
            monkeypatch.setattr(cleanup.manager, 'push', lost)
        cleanup.tick()
        assert worktree.exists() and (worktree / 'result.txt').exists()
        journal = d.store.db.execute('SELECT * FROM detach_publications').fetchone()
        assert journal['state'] == 'pending' and journal['error']
        monkeypatch.setattr(cleanup.manager, 'git', original_git)
        monkeypatch.setattr(cleanup.manager, 'push', original_push)
        with d.store.db:
            d.store.db.execute('UPDATE cleanup_jobs SET next_retry_at=NULL')
        cleanup.tick()
        assert not worktree.exists()
        journal = d.store.db.execute('SELECT * FROM detach_publications').fetchone()
        assert journal['state'] == 'confirmed' and journal['no_diff'] == 0
        saved = d.execution.get(attempt)
        assert git(remote, 'rev-parse', saved['branch']) == saved['result_sha']
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert d.store.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 1
    finally:
        d.lifecycle.cancel()
        d.close()


@pytest.mark.parametrize('timeline', ['missing', 'untrusted_body', 'wrong_issue', 'future', 'duplicate'])
def test_actor_fallback_and_duplicate_poll(env, timeline):
    config, project, *_ = env
    items = [issue(project, body='removed by malicious-actor')]
    api = adapter(items)
    d = Dispatcher(config, Lifecycle(), api)
    try:
        d.store.apply(project, items)
        events = [dict(id=78,event='unlabeled',label={'name':project.task_label},
            actor={'login':'alice','type':'User'},created_at='2026-10-24T00:09:00Z')]
        result = dict(databaseId=1,labels=dict(nodes=[],pageInfo={'hasNextPage':False}))
        if timeline in ('missing','untrusted_body'):
            events = []
        elif timeline == 'wrong_issue':
            result['databaseId'] = 99
        elif timeline == 'future':
            events[0]['created_at'] = '2026-10-24T00:11:00Z'
        else:
            events *= 2
        api.command_events.return_value = events, result
        for _ in range(2):
            d.stops.observe(project,d.store.queue()[0],raw=dict(id=1,number=1,labels=[]))
            d.stops.diagnostics()
        body = d.store.db.execute('SELECT body FROM stop_outbox').fetchone()[0]
        data = json.loads(body.split('```json\n')[1].split('\n```')[0])
        assert data['actor'] == ('alice' if timeline == 'duplicate' else 'Не удалось определить')
        assert data['detach_publication']['state'] == 'pending'
        assert api.write.call_count == 1
        assert d.store.db.execute('SELECT count(*) FROM stop_requests').fetchone()[0] == 1
    finally:
        d.lifecycle.cancel()
        d.close()


def test_clean_detach_confirms_sha_and_updates_same_comment(env):
    config, project, *_ = env
    items = [issue(project)]
    api = adapter(items)
    d = Dispatcher(config, Lifecycle(), api)
    comments = []
    api.pages.side_effect = lambda endpoint: comments
    def deliver(endpoint, method, payload):
        if '/comments' in endpoint:
            if method == 'POST':
                comments.append(dict(id=99,body=payload['body']))
            else:
                comments[0]['body'] = payload['body']
            return comments[0]
        return {'id':123}
    try:
        register(d,project,items)
        d.admit()
        attempt = next(iter(d.active))
        finish(d)
        sha = d.execution.get(attempt)['result_sha']
        request_stop(d.store)
        api.write.side_effect = deliver
        d.stops.diagnostics()
        assert len(comments) == 1 and 'pending' in comments[0]['body']
        CleanupService(config,d.store).tick()
        d.stops.diagnostics()
        d.stops.diagnostics()
        assert len(comments) == 1
        data = json.loads(comments[0]['body'].split('```json\n')[1].split('\n```')[0])
        assert data['detach_publication']['state'] == 'confirmed'
        assert data['detach_publication']['no_diff'] == 1
        assert data['detach_publication']['remote_sha'] == sha
        assert d.execution.get(attempt)['result_sha'] == sha
        assert api.write.call_args.args[1] == 'PATCH'
    finally:
        d.lifecycle.cancel()
        d.close()


def test_process_disappearing_during_detach_scan_keeps_other_live_members(monkeypatch):
    from dispatcher.runtime import group_members
    vanished, live = Mock(), Mock()
    vanished.read_text.side_effect = ProcessLookupError(3, 'No such process')
    live.read_text.return_value = '123 (worker) S 1 123'
    live.parent.name = '123'
    monkeypatch.setattr(Path, 'glob', lambda *args: iter((vanished, live)))
    assert group_members(123) == [123]
    lifecycle = Lifecycle()
    child = Mock(pid=123, returncode=None)
    lifecycle.children = [child]
    monkeypatch.setattr('os.waitid', lambda *args: None)
    assert lifecycle._active() == [child]
    child.wait.assert_not_called()
