"""6.4.1.7 natural-drain, fencing, ownership and restart acceptance."""
import json
from dataclasses import replace
from pathlib import Path
import signal
import sys
import time
from unittest.mock import Mock

import pytest

from test_stage04 import env, issue, adapter, register, finish, git
from test_stage602 import store, PROJECT, item, running
from test_stage603 import publication
from dispatcher.cleanup import CleanupService
from dispatcher.config import Label, load
from dispatcher.detachment import StopController, fence, request_in_transaction
from dispatcher.execution_store import ExecutionStore
from dispatcher.github import Adapter, GitHubError
from dispatcher.runtime import Lifecycle, identity, group_members
from dispatcher.scheduler import Dispatcher
from dispatcher.storage import Store
from dispatcher.workspace import TaskError


def request_stop(store, reason='ready_removed'):
    row = store.queue()[0]
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        return request_in_transaction(store, row, reason, {'verified':True, 'observed_at':'2026-10-24T00:10:00Z'})


def wire(api, project, items, actor='alice'):
    api.control_issue.side_effect = lambda p, n: dict(id=next(i['id'] for i in items if i['number']==n),
        number=n, labels=[dict(name=v) for v in next(i['labels'] for i in items if i['number']==n)])
    def events(p, n):
        current = next(i for i in items if i['number']==n)
        return [dict(id=77, event='labeled', label=dict(name=p.label('emergency_stop').name),
                     actor=dict(type='User', login=actor), created_at='2026-10-24T00:01:00Z')], dict(
            databaseId=current['id'], labels=dict(nodes=[dict(name=v) for v in current['labels']], pageInfo=dict(hasNextPage=False)))
    api.command_events.side_effect = events


@pytest.mark.parametrize('state', ['discovered','pending','revoked','review','rejected','failed','interrupted'])
def test_idle_detach_and_terminal_history(store, state):
    store.apply(PROJECT, [item()])
    with store.db:
        store.db.execute('UPDATE tasks SET state=?', (state,))
    store.apply(PROJECT, [item(eligible=False, labels=[])])
    assert store.queue()[0]['state'] == 'detached'
    stop = store.db.execute('SELECT * FROM stop_requests').fetchone()
    assert stop['reason'] == 'ready_removed' and stop['finalized_at']
    assert not store.confirm(PROJECT.repository, 1, 1)
    store.apply(PROJECT, [item(eligible=False, labels=[])])
    assert store.db.execute('SELECT count(*) FROM stop_requests').fetchone()[0] == 1


@pytest.mark.parametrize('state', ['done','cancelled'])
def test_completed_results_survive_stop(store, state):
    store.apply(PROJECT, [item()])
    with store.db:
        store.db.execute('UPDATE tasks SET state=?', (state,))
    store.apply(PROJECT, [item(eligible=False, labels=[])])
    assert store.queue()[0]['state'] == state
    assert not store.stop_requested(store.queue()[0])
    request_stop(store, 'emergency_stop')
    assert store.queue()[0]['state'] == state
    assert store.stop_requested(store.queue()[0])


def test_running_detach_natural_exit_no_publication_dirty_cleanup(env):
    config, project, control, *_ = env
    stub = config.executable
    Path(stub).write_text(Path(stub).read_text().replace("if mode == 'sleep': time.sleep(60)", "if mode == 'sleep': time.sleep(.3)").replace("if mode != 'dirty':", "if mode not in ('dirty','sleep'):"))
    control.write_text(json.dumps({'exec':'sleep'}))
    items = [issue(project)]
    api = adapter(items)
    wire(api, project, items)
    lifecycle = Lifecycle()
    d = Dispatcher(config, lifecycle, api)
    try:
        register(d, project, items)
        d.admit()
        attempt = next(iter(d.active))
        task = d.active[attempt]
        task['deadline'] = 0  # Detach drains naturally, including past old timeout.
        items[0]['labels'] = []
        d.stops.observe(project, d.store.queue()[0])
        assert d.execution.get(attempt)['state'] == 'running'
        assert group_members(task['child'].pid)
        finish(d)
        saved = d.execution.get(attempt)
        assert saved['exit_code'] == 0 and saved['state'] == 'completed'
        assert d.store.queue()[0]['state'] == 'detached'
        assert not saved['pushed']
        assert not d.execution.db.execute('SELECT * FROM result_outbox').fetchall()
        assert not d.execution.db.execute('SELECT * FROM task_pull_requests').fetchall()
        CleanupService(config, d.store).tick()
        assert not task['worktree'].exists()
        assert d.store.db.execute('SELECT state FROM cleanup_jobs').fetchone()[0] == 'completed'
        # New cycle retains history, and reserves a fresh approved iteration.
        old_iteration = saved['iteration_id']
        d.store.apply(project, [issue(project, server_observed_at='2026-10-24T00:11:00Z')])
        assert d.store.queue()[0]['state'] == 'discovered'
        assert d.store.confirm(project.repository, 1, 1)
        assert d.store.queue()[0]['current_iteration_id'] != old_iteration
        assert d.execution.get(attempt)['exit_code'] == 0
    finally:
        lifecycle.cancel()
        d.close()


@pytest.mark.parametrize('with_ready', [True,False])
def test_emergency_only_target_group_and_labels(env, with_ready):
    config, project, control, *_ = env
    config = replace(config, max_parallel_tasks=2)
    Path(config.executable).write_text(Path(config.executable).read_text().replace('time.sleep(60)', 'time.sleep(.7)'))
    control.write_text(json.dumps({'exec':'sleep'}))
    items = [issue(project), issue(project, id=2, number=2)]
    api = adapter(items)
    wire(api, project, items)
    lifecycle = Lifecycle(max_parallel_tasks=2, force_grace_timeout_seconds=1)
    d = Dispatcher(config, lifecycle, api)
    try:
        register(d, project, items)
        d.admit(); d.admit()
        first, second = list(d.active)
        other = d.active[second]['child']
        items[0]['labels'] = [project.label('emergency_stop').name, 'bug', 'dispatcher:custom'] + ([project.task_label] if with_ready else [])
        d.stops.observe(project, d.store.get(project.repository, 1))
        d.collect()
        assert group_members(d.active[first]['child'].pid)
        deadline = time.monotonic() + 5
        while first in d.active and time.monotonic() < deadline:
            d.collect()
            time.sleep(.01)
        assert d.store.get(project.repository, 1)['state'] == 'detached'
        assert other.returncode is None or other.returncode == 0
        assert not lifecycle.stopping
        # Reconcile exact configured names, preserving the emergency and user labels.
        labels = items[0]['labels'] + [project.label('running').name, project.label('approve').name]
        api.label_page.return_value = [dict(name=v) for v in labels]
        writes = []
        api.write.side_effect = lambda endpoint, method, payload: writes.append((endpoint,method,payload)) or {'id':8}
        Adapter.reconcile(api, project, d.store.get(project.repository, 1), store=d.store)
        from urllib.parse import unquote
        removed = [unquote(e.rsplit('/',1)[1]) for e,m,_ in writes if m=='DELETE']
        assert project.label('running').name in removed
        assert project.label('approve').name in removed
        assert not set(removed) & {'bug','dispatcher:custom',project.label('emergency_stop').name}
        items[0]['labels'].append(project.task_label)
        d.store.apply(project, items)
        assert d.store.get(project.repository, 1)['state'] == 'detached'
    finally:
        lifecycle.cancel()
        d.collect()
        d.close()


def test_untrusted_emergency_never_authorizes_signal_or_launch(store):
    store.apply(PROJECT, [item()])
    api = Mock(spec=Adapter)
    items = [dict(item(), labels=[PROJECT.task_label, PROJECT.label('emergency_stop').name])]
    wire(api, PROJECT, items, actor='intruder')
    api.response_date = '2026-10-24T00:10:00Z'
    with pytest.raises(GitHubError):
        StopController(None,store,api).observe(PROJECT,store.queue()[0])
    assert not store.stop_requested(store.queue()[0])
    store.apply(PROJECT, items)
    assert not store.confirm(PROJECT.repository,1,1)


@pytest.mark.parametrize('step', [1,2,3,4])
def test_publication_fence_before_each_step(publication, step):
    publisher, execution, attempt, api, git_manager, _ = publication
    reads = 0
    def read(project, number):
        nonlocal reads
        reads += 1
        return dict(id=101, number=1, labels=[] if reads>=step else [dict(name=PROJECT.task_label)])
    api.control_issue.side_effect = read
    publisher.publish(PROJECT, execution.get(attempt))
    assert execution.store.stop_requested(execution.store.queue()[0])
    if step<=2:
        git_manager.publication_commit.assert_not_called()
    if step<=3:
        git_manager.push.assert_not_called()
    api.write.assert_not_called()
    assert execution.db.execute('SELECT count(*) FROM task_pull_requests').fetchone()[0] == 0


def test_outbox_lost_response_retry_and_progress_update(store):
    store.apply(PROJECT, [item()])
    request_stop(store, 'emergency_stop')
    api = Mock(spec=Adapter)
    comments = []
    api.pages.side_effect = lambda endpoint: list(comments)
    def lost(endpoint, method, payload):
        comments.append(dict(id=17, body=payload['body']))
        raise GitHubError()
    api.write.side_effect = lost
    controller = StopController(None, store, api)
    controller.diagnostics()
    assert store.stop_requested(store.queue()[0])
    assert store.db.execute('SELECT delivered FROM stop_outbox').fetchone()[0] == 0
    api.write.side_effect = None
    controller.diagnostics(); controller.diagnostics()
    assert len(comments) == 1
    assert store.db.execute('SELECT delivered FROM stop_outbox').fetchone()[0] == 1


def test_group_leader_exit_surviving_descendant_requires_sigkill(tmp_path):
    lifecycle = Lifecycle(force_grace_timeout_seconds=1)
    marker = tmp_path/'child'
    code = ('import os,signal,pathlib,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            f'pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
    leader = lifecycle.submit([sys.executable,'-c', f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{code!r}])'])
    start = identity(leader.pid)
    try:
        deadline = time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.01)
        method, confirmed = lifecycle.stop_attempt(leader.pid,start)
        assert method == 'SIGKILL' and confirmed
        assert not group_members(leader.pid)
        assert not lifecycle._active()
    finally:
        lifecycle.cancel()


def test_restart_drain_preserves_group_and_never_restarts_codex(env):
    config, project, control, *_ = env
    Path(config.executable).write_text(Path(config.executable).read_text().replace('time.sleep(60)', 'time.sleep(.5)'))
    control.write_text(json.dumps({'exec':'sleep'}))
    registry = config.workspace/'.runtime'/'children.json'
    lifecycle = Lifecycle(registry=registry)
    d = Dispatcher(config,lifecycle,adapter([issue(project)]))
    try:
        register(d,project,[issue(project)])
        d.admit()
        attempt = next(iter(d.active))
        child = d.active[attempt]['child']
        request_stop(d.store)
        # A replacement Lifecycle leaves ordinary-detached groups untouched.
        replacement = Lifecycle(registry=registry)
        replacement.recover(lambda pid: True)
        assert group_members(child.pid)
        d.close()
        d = Dispatcher(config,replacement,adapter([issue(project)]))
        assert d.execution.get(attempt)['state'] == 'running'
        deadline = time.monotonic()+5
        while group_members(child.pid) and time.monotonic()<deadline:
            time.sleep(.02)
        d.recover_stops()
        assert d.store.queue()[0]['state'] == 'running'
        assert d.store.db.execute('SELECT error FROM stop_requests').fetchone()[0] == 'process_completion_unconfirmed'
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    finally:
        lifecycle.cancel()
        d.close()


def test_migration_idempotent_and_rollback(store):
    from dispatcher.storage.migrations import upgrade609
    store.apply(PROJECT,[item()])
    request_stop(store)
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        upgrade609(store.db); upgrade609(store.db)
    assert store.db.execute('SELECT count(*) FROM stop_requests').fetchone()[0] == 1
    assert store.db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_independent_watcher_with_real_gh_and_codex_stubs(env):
    from dispatcher.detachment import StopWatcher
    config, project, control, *_ = env
    control.write_text(json.dumps({'exec':'sleep'}))
    remote = config.source.parent/'remote-control.json'
    remote.write_text(json.dumps([project.task_label]))
    gh = config.source.parent/'gh-control-stub'
    gh.write_text(f'#!{sys.executable}\n' + '''
import json,pathlib,sys
root=pathlib.Path(__file__).parent
if sys.argv[1:3]==['repo','view']:
    print(json.dumps({'defaultBranchRef':{'name':'main'}})); sys.exit(0)
args=sys.argv[1:]
endpoint=next((a for a in args if a.startswith('repos/') or a=='graphql'),'')
names=json.loads((root/'remote-control.json').read_text())
labels=[{'name':n} for n in names]
if endpoint=='graphql':
    value={'data':{'repository':{'issue':{'databaseId':1,'title':'Task','body':'Requirements',
      'state':'OPEN','author':{'login':'alice'},'lastEditedAt':None,
      'labels':{'nodes':labels,'pageInfo':{'hasNextPage':False}}}}}}
elif '/events' in endpoint:
    value=[{'id':77,'event':'labeled','created_at':'2026-10-24T00:01:00Z',
      'actor':{'login':'alice','type':'User'},'label':{'name':'dispatcher:emergency-stop'}}]
elif '/comments' in endpoint:
    saved=root/'comments.json'
    comments=json.loads(saved.read_text()) if saved.exists() else []
    if '--method' in args and args[args.index('--method')+1] in ('POST','PATCH'):
        payload=json.load(sys.stdin)
        value={'id':19,**payload}
        saved.write_text(json.dumps([value]))
    else: value=comments
else: value={'id':1,'number':1,'labels':labels,'url':'https://api.github.com/repos/owner/repo/issues/1'}
print('HTTP/2.0 200 OK\\nDate: Sat, 24 Oct 2026 00:10:00 GMT\\n\\n'+json.dumps(value))
''')
    gh.chmod(0o700)
    config = replace(config, github_executable=str(gh), emergency_poll_interval_seconds=1)
    lifecycle = Lifecycle(force_grace_timeout_seconds=1)
    d = Dispatcher(config,lifecycle,adapter([issue(project)]))
    watcher = StopWatcher(config,lifecycle)
    try:
        register(d,project,[issue(project)])
        d.admit()
        attempt=next(iter(d.active))
        child=d.active[attempt]['child']
        remote.write_text(json.dumps([project.task_label,project.label('emergency_stop').name]))
        watcher.start()
        # No scheduler polling or collect while the independent watcher works.
        deadline=time.monotonic()+6
        while not d.store.stop_requested(d.store.queue()[0]) and time.monotonic()<deadline:
            time.sleep(.02)
        assert d.store.stop_requested(d.store.queue()[0])
        assert group_members(child.pid)
        d.collect()
        assert d.store.queue()[0]['state']=='running'
        assert not lifecycle.stopping
    finally:
        watcher.close()
        lifecycle.cancel()
        d.close()


def test_stale_snapshot_cannot_reattach_and_dirty_unknown_artifact_preserved(env):
    config,project,*_=env
    d=Dispatcher(config,Lifecycle(),adapter([issue(project)]))
    try:
        register(d,project,[issue(project)])
        d.admit(); finish(d)
        task=d.store.queue()[0]
        worktree=d.git.paths(project,1)[2]
        (worktree/'dirty').write_text('owned content')
        unknown=worktree.parent/'foreign-file'
        unknown.write_text('preserve')
        request_stop(d.store)
        cleanup=CleanupService(config,d.store)
        cleanup.tick()
        assert unknown.exists() and worktree.exists()
        assert d.store.db.execute('SELECT state FROM cleanup_jobs').fetchone()[0]=='pending'
        d.store.apply(project,[issue(project,server_observed_at='2026-10-24T00:20:00Z')])
        assert d.store.queue()[0]['state']=='detached'  # Cleanup still owns artifacts.
        unknown.unlink()
        with d.store.db:
            d.store.db.execute('UPDATE cleanup_jobs SET next_retry_at=NULL')
        cleanup.tick()
        assert not worktree.exists()
        # No remote branch is deleted, and reconnect cannot overwrite it.
        d.store.apply(project,[issue(project,server_observed_at='2026-10-24T00:21:00Z')])
        assert d.store.confirm(project.repository,1,1)
        d.admit()
        assert not d.active
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0]==2
        latest = d.store.db.execute('SELECT * FROM attempts ORDER BY rowid DESC LIMIT 1').fetchone()
        assert latest['error_code'] == 'detach_remote_branch_conflict'
        assert not latest['codex_started']
        assert git(env[4], 'rev-parse', latest['branch']) == git(d.git.paths(project, 1)[0], 'rev-parse', latest['branch'])
    finally:
        d.lifecycle.cancel()
        d.close()


def test_custom_label_config_and_fingerprint(env):
    import yaml
    from dispatcher.initialization import fingerprint
    config,project,*_=env
    data=yaml.safe_load(config.source.read_text())
    data['dispatcher']={'emergency_poll_interval_seconds':7}
    data['projects'][0]['labels']={'emergency_stop':{'name':'halt-this-task','color':'abcdef','description':'Stop'}}
    config.source.write_text(yaml.safe_dump(data))
    custom=load(config.source)
    assert custom.emergency_poll_interval_seconds==7
    assert custom.projects[0].label('emergency_stop')==Label('halt-this-task','abcdef','Stop')
    assert fingerprint(custom.projects[0])!=fingerprint(project)


@pytest.mark.parametrize('reason', ['ready_removed','emergency_stop'])
def test_detach_during_preparation_never_launches_codex(env, reason):
    config,project,*_=env
    d=Dispatcher(config,Lifecycle(),adapter([issue(project)]))
    original=d.git.prepare
    def preparing(*args):
        result=original(*args)
        request_stop(d.store,reason)
        return result
    d.git.prepare=preparing
    try:
        register(d,project,[issue(project)])
        d.admit()
        assert not d.active
        assert d.execution.db.execute('SELECT codex_started FROM attempts').fetchone()[0]==0
        assert d.store.queue()[0]['state']=='detached'
        CleanupService(config,d.store).tick()
        assert not d.git.paths(project,1)[1].exists()
    finally:
        d.lifecycle.cancel()
        d.close()


def test_unconfirmed_stop_preserves_worktree_until_group_gone(env):
    config,project,control,*_=env
    control.write_text(json.dumps({'exec':'sleep'}))
    lifecycle=Lifecycle(force_grace_timeout_seconds=1)
    d=Dispatcher(config,lifecycle,adapter([issue(project)]))
    try:
        register(d,project,[issue(project)])
        d.admit()
        attempt=next(iter(d.active))
        child=d.active[attempt]['child']
        d.execution.update(attempt,process_identity='wrong-identity')
        request_stop(d.store,'emergency_stop')
        d.collect()
        assert group_members(child.pid)
        assert Path(d.execution.get(attempt)['worktree']).exists()
        stop=d.store.db.execute('SELECT * FROM stop_requests').fetchone()
        assert stop['process_status'] in ('pending','unconfirmed') and not stop['finalized_at']
        assert not d.store.db.execute('SELECT * FROM cleanup_jobs').fetchall()
        lifecycle.cancel()
        d.collect()
        assert d.store.queue()[0]['state']=='running'
        assert d.store.db.execute('SELECT error FROM stop_requests').fetchone()[0] == 'process_completion_unconfirmed'
        CleanupService(config,d.store).tick()
        assert Path(d.execution.get(attempt)['worktree']).exists()
    finally:
        lifecycle.cancel()
        d.close()


def test_upgrade_from_608_preserves_attempts_and_rolls_back(store):
    execution,attempt=running(store)
    path=store.db.execute('PRAGMA database_list').fetchone()[2]
    original=dict(execution.get(attempt))
    with store.db:
        store.db.execute('DROP TABLE stop_outbox')
        store.db.execute('DROP TABLE stop_requests')
        store.db.execute('DROP TABLE attempt_publications')
        store.db.execute('DROP TABLE control_observations')
        store.db.execute('DROP TABLE attempt_processes')
        store.db.execute('DELETE FROM schema_migrations WHERE version=609')
    reopened=Store(path)
    try:
        assert dict(ExecutionStore(reopened).get(attempt))==original
        from dispatcher.storage.migrations import upgrade609
        with pytest.raises(RuntimeError):
            with reopened.db:
                reopened.db.execute('BEGIN IMMEDIATE')
                upgrade609(reopened.db)
                request_in_transaction(reopened,reopened.queue()[0],'emergency_stop',{})
                raise RuntimeError('rollback')
        assert not reopened.db.execute('SELECT * FROM stop_requests').fetchall()
        assert reopened.queue()[0]['state']=='running'
    finally:
        reopened.close()


def test_restart_emergency_preserves_natural_drain(env):
    config,project,control,*_=env
    control.write_text(json.dumps({'exec':'sleep'}))
    registry=config.workspace/'.runtime'/'children.json'
    old=Lifecycle(registry=registry,force_grace_timeout_seconds=1)
    d=Dispatcher(config,old,adapter([issue(project)]))
    replacement=None
    try:
        register(d,project,[issue(project)])
        d.admit()
        attempt=next(iter(d.active))
        child=d.active[attempt]['child']
        request_stop(d.store,'emergency_stop')
        d.close()
        replacement=Lifecycle(registry=registry,force_grace_timeout_seconds=1)
        replacement.recover(lambda pid: True)
        d=Dispatcher(config,replacement,adapter([issue(project)]))
        d.recover_stops(); d.recover_stops()
        assert group_members(child.pid)
        assert d.store.queue()[0]['state']=='running'
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0]==1
        assert d.store.db.execute('SELECT method FROM stop_requests').fetchone()[0]=='not_required'
    finally:
        old.cancel()
        if replacement:
            replacement.cancel()
        d.close()


def test_delayed_poll_cannot_detach_a_reconnected_cycle(store):
    store.apply(PROJECT,[item()])
    request_stop(store)
    with store.db:
        store.db.execute("UPDATE cleanup_jobs SET state='completed'")
    store.apply(PROJECT,[item(server_observed_at='2026-10-24T00:11:00Z')])
    assert store.queue()[0]['state']=='discovered'
    store.apply(PROJECT,[item(labels=[],eligible=False,server_observed_at='2026-10-24T00:09:00Z')])
    assert store.queue()[0]['state']=='discovered'
    assert not store.stop_requested(store.queue()[0])


def test_emergency_overrides_ordinary_queue_commands(store):
    from dispatcher.queue import commands
    store.apply(PROJECT,[item()])
    before={1:dict(store.queue()[0])}
    task=item(labels=[PROJECT.task_label,PROJECT.label('emergency_stop').name,PROJECT.label('approve').name])
    observation={}
    store.apply(PROJECT,[task],observation=observation)
    api=Mock(spec=Adapter)
    assert commands(store,api,PROJECT,[task],before,observation)==[]
    assert not store.confirm(PROJECT.repository,1,1)
    assert not store.db.execute('SELECT * FROM attempts').fetchall()


def test_init_update_emergency_metadata_and_fingerprint(env):
    from dispatcher.initialization import initialize, fingerprint
    config,project,*_=env
    api=Mock(spec=Adapter)
    api.labels.return_value=[]
    api.request.return_value={}
    assert initialize(config,adapter=api)==[(project.id,True)]
    label=project.label('emergency_stop')
    assert any(c.args[1]=='POST' and c.args[2].get('name')==label.name for c in api.write.call_args_list)
    api.reset_mock()
    api.labels.return_value=[dict(name=label.name,color='000000',description='old')]
    assert initialize(config,update=True,adapter=api)==[(project.id,True)]
    from urllib.parse import quote
    assert any(c.args[0].endswith('/'+quote(label.name,safe='')) and c.args[1]=='PATCH' and
               c.args[2]==dict(color=label.color,description=label.description) for c in api.write.call_args_list)
    changed=replace(project,labels={**project.labels,'emergency_stop':replace(label,color='FFFFFF')})
    assert fingerprint(changed)!=fingerprint(project)


@pytest.mark.parametrize('reason', ['ready_removed','emergency_stop'])
def test_preparation_helper_groups_are_durable_and_addressable(env, reason):
    import subprocess
    from threading import Thread
    from dispatcher.workspace import GitWorkspaceManager
    config,project,*_=env
    store=Store(config.workspace/'.runtime'/'tasks.sqlite3')
    store.apply(project,[issue(project)])
    assert store.confirm(project.repository,1,1)
    execution=ExecutionStore(store)
    attempt=execution.reserve(store.queue()[0],'feature',1,store.guard(project.repository,1)['observation_token'])
    lifecycle=Lifecycle(force_grace_timeout_seconds=1)
    manager=GitWorkspaceManager(config,lifecycle,execution)
    manager.operation_attempt=attempt
    path=store.db.execute('PRAGMA database_list').fetchone()[2]
    errors=[]
    def controller():
        other=Store(path)
        try:
            deadline=time.monotonic()+5
            process=None
            while time.monotonic()<deadline:
                process=other.db.execute('SELECT * FROM attempt_processes').fetchone()
                if process: break
                time.sleep(.005)
            assert process
            request_stop(other,reason)
            if reason=='emergency_stop':
                stop=other.db.execute('SELECT * FROM stop_requests').fetchone()
                StopController(config,other,Mock()).signal(lifecycle,stop,process)
        except BaseException as exc:
            errors.append(exc)
        finally:
            other.close()
    worker=Thread(target=controller)
    worker.start()
    try:
        result=manager.run([sys.executable,'-c','import time;time.sleep(.4)'],
                           cwd=config.workspace,capture_output=True,text=True,timeout=5,stdin=subprocess.DEVNULL)
        worker.join(timeout=5)
        assert not worker.is_alive() and not errors
        assert result.returncode == 0
        assert store.db.execute('SELECT finished_at FROM attempt_processes').fetchone()[0]
        assert StopController(config,store,Mock()).finalize(store.queue()[0])
        assert store.queue()[0]['state']=='detached'
        assert not lifecycle.stopping
    finally:
        lifecycle.cancel()
        worker.join(timeout=5)
        store.close()
