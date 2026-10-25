"""Stage 4 acceptance: real local Git/processes, no credentials or remote models."""
import hashlib
import io
import json
import os
import runpy
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
import yaml

from dispatcher.attachments import IssueAttachmentManager
from dispatcher.codex.quota import CodexQuotaProvider, evaluate
from dispatcher.config import ConfigError, load
from dispatcher.execution_store import ExecutionStore
from dispatcher.github import Adapter, GitHubError
from dispatcher.initialization import save_result
from dispatcher.publisher import TaskResultPublisher
from dispatcher.runtime import Lifecycle, identity, prepare
from dispatcher.scheduler import Dispatcher
from dispatcher.storage import Store
from dispatcher.workspace import GitWorkspaceManager, TaskError, TaskWorkspaceCleanup


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path):
    executable = tmp_path / 'codex-stub'
    control = tmp_path / 'control.json'
    control.write_text('{}')
    executable.write_text(f'#!{sys.executable}\n' + '''
import json, pathlib, subprocess, sys, time
control = pathlib.Path(__file__).with_name('control.json')
def settings(): return json.loads(control.read_text())
if '--version' in sys.argv:
    print('codex-cli test'); sys.exit(0)
if '--help' in sys.argv:
    print('--dangerously-bypass-approvals-and-sandbox'); sys.exit(0)
if 'app-server' in sys.argv:
    with control.with_name('server-starts').open('a') as stream: stream.write('start\\n')
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request: continue
        if request['method'] == 'initialize':
            print(json.dumps({'id':request['id'],'result':{}}), flush=True)
        elif settings().get('server') == 'crash': sys.exit(3)
        elif settings().get('server') == 'unsupported':
            print(json.dumps({'id':request['id'],'error':{'code':-32601}}), flush=True)
        else:
            print(json.dumps({'method':'account/rateLimits/updated','params':{}}), flush=True)
            print(json.dumps({'id':-1,'result':{}}), flush=True)
            print(json.dumps({'id':request['id'],'result':{'rateLimits':{'primary':{'usedPercent':settings().get('used',20)}}}}), flush=True)
    sys.exit(0)
prompt = sys.stdin.read()
pathlib.Path('received-prompt').write_text(prompt)
pathlib.Path('received-prompt').unlink()
mode = settings().get('exec','success')
if mode == 'sleep': time.sleep(60)
if mode == 'fail': print('SECRET'); sys.exit(1)
if mode == 'quota':
    print(json.dumps({'type':'turn.failed','error':{'code':'usage_limit_reached'}})); sys.exit(1)
if mode == 'noop':
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'NO_OP: already implemented and tested'}})); sys.exit(0)
pathlib.Path('result.txt').write_text('implemented')
if mode != 'dirty':
    subprocess.run(['git','add','result.txt'],check=True)
    subprocess.run(['git','commit','-m','Implement task'],check=True)
''')
    executable.chmod(0o700)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(dict(paths=dict(workspace=str(tmp_path / 'work'), logs=str(tmp_path / 'logs')),
        codex=dict(executable=str(executable), quota=dict(enabled=False)),
        projects=[dict(id='one', enabled=True, repository='owner/repo', security=dict(allowed_authors=['alice']))])))
    config = load(path)
    save_result(config, config.projects[0], True)
    config = load(path)
    prepare(config)
    project = config.projects[0]
    seed, remote = tmp_path / 'seed', tmp_path / 'remote.git'
    seed.mkdir()
    git(seed, 'init', '-b', 'main')
    git(seed, 'config', 'user.name', 'Test')
    git(seed, 'config', 'user.email', 'test@example.invalid')
    (seed / 'initial').write_text('initial')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'Initial')
    git(tmp_path, 'clone', '--bare', str(seed), str(remote))
    repo = config.workspace / project.id / 'repository'
    git(tmp_path, 'clone', '--no-checkout', str(remote), str(repo))
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    return config, project, control, seed, remote


def issue(project, **changes):
    item = dict(id=1, repository_id=11 if project.repository == 'owner/repo' else 12, server_observed_at='2026-10-24T00:00:00Z', number=1, project_id=project.id, repository=project.repository,
                title='Task', body='Requirements', author='alice', target_branch='main', eligible=True,
                state='open', labels=[project.task_label], updated_at='2026-10-24T00:00:00Z')
    item.update(changes)
    return item


def adapter(items):
    result = Mock(spec=Adapter)
    result.fetch.return_value = items
    result.pages.return_value = []
    result.repository_identity.return_value = 11
    result.response_date = '2026-10-24T00:10:00Z'
    result.write.return_value = {'id': 123}
    original_write = result.write
    def write(endpoint, method, payload):
        if endpoint.endswith('/pulls'):
            result._pr = dict(id=301, number=2, body=payload['body'], state='open', draft=False,
                              merged=False, changed_files=1, base=dict(ref='main', repo=dict(id=11)),
                              head=dict(ref=payload['head'], repo=dict(id=11)))
            return result._pr
        return {'id': 123}
    original_write.side_effect = write
    def request(endpoint):
        pr = dict(result._pr)
        import re
        pr['head']['sha'] = re.search(r'SHA: ([0-9a-f]+)', pr['body'])[1]
        return pr
    result.request.side_effect = request
    return result


def register(dispatcher, project, items):
    dispatcher.store.apply(project, items)
    for item in items:
        assert dispatcher.store.confirm(project.repository, item['number'], 1)


def finish(dispatcher):
    deadline = time.monotonic() + 8
    while dispatcher.active and time.monotonic() < deadline:
        dispatcher.collect()
        time.sleep(.01)
    assert not dispatcher.active


@pytest.mark.parametrize('branch', ['existing', 'new-branch', 'main'])
def test_worktree_existing_missing_default_and_push(env, branch):
    config, project, _, seed, remote = env
    if branch == 'existing':
        git(seed, 'branch', branch)
        git(seed, 'push', str(remote), branch)
    manager = GitWorkspaceManager(config)
    item = issue(project, target_branch=branch)
    actual = manager.branch(project, item)
    assert actual == ('codex/issue-1' if branch == 'main' else branch)
    worktree, initial = manager.prepare(project, item, actual)
    (worktree / 'new').write_text('new')
    git(worktree, 'add', '.')
    git(worktree, 'commit', '-m', 'Task')
    sha = manager.verify(worktree, actual, initial)
    manager.push(project, worktree, actual)
    assert git(remote, 'rev-parse', actual) == sha
    assert git(seed, 'status', '--porcelain') == ''
    assert not (seed / 'new').exists()
    with pytest.raises(TaskError):
        manager.prepare(project, item, actual)


@pytest.mark.parametrize('mode,state,code', [
    ('success','review',None), ('fail','failed','codex_failed'), ('dirty','review',None),
    ('noop','failed','no_diff'), ('quota','interrupted','quota_exhausted')])
def test_executor_pipeline_and_final_comment(env, mode, state, code):
    config, project, control, _, remote = env
    control.write_text(json.dumps(dict(exec=mode)))
    api = adapter([issue(project)])
    lifecycle = Lifecycle()
    dispatcher = Dispatcher(config, lifecycle, api)
    try:
        register(dispatcher, project, api.fetch.return_value)
        dispatcher.admit()
        finish(dispatcher)
        row = dispatcher.execution.db.execute('SELECT * FROM attempts').fetchone()
        assert row['state'] == state and row['error_code'] == code
        assert Path(row['worktree']).exists()
        assert (Path(row['worktree']).parent / 'attachments' / 'manifest.json').exists()
        assert row['codex_started'] == 1 and row['duration'] >= 0
        if state == 'review':
            assert row['pushed'] == 1
            assert git(remote, 'rev-parse', row['branch']) == row['result_sha']
        dispatcher.publisher.flush()
        body = api.write.call_args.args[2]['body']
        assert 'Started:' in body and 'Finished:' in body and 'Duration:' in body and 'UTC' in body
        assert 'SECRET' not in body and str(config.workspace) not in body
        dispatcher.publisher.flush()
        assert api.write.call_count == (2 if state == 'review' else 1)
        assert (Path(row['worktree']).parent / f'{row["attempt_id"]}.codex.log').stat().st_mode & 0o777 == 0o600
    finally:
        lifecycle.cancel()
        dispatcher.close()


def test_approved_explained_noop(env):
    config, project, control, _, _ = env
    control.write_text(json.dumps(dict(exec='noop')))
    api = adapter([issue(project, body='No-op allowed: If already implemented explain why.')])
    dispatcher = Dispatcher(config, Lifecycle(), api)
    try:
        register(dispatcher, project, api.fetch.return_value)
        dispatcher.admit()
        finish(dispatcher)
        assert dispatcher.store.get(project.repository, 1)['state'] == 'failed'
    finally:
        dispatcher.close()


@pytest.mark.parametrize('change', ['edit','unavailable','untrusted','network','stop'])
def test_admission_rechecks_approval_and_github(env, change):
    config, project, _, _, _ = env
    item = issue(project)
    api = adapter([item])
    dispatcher = Dispatcher(config, Lifecycle(), api)
    try:
        register(dispatcher, project, [item])
        if change == 'edit': api.fetch.return_value = [dict(item, body='Changed')]
        if change == 'unavailable': api.fetch.return_value = []
        if change == 'untrusted': api.fetch.return_value = [dict(item, author='mallory', eligible=False)]
        if change == 'network': api.fetch.side_effect = GitHubError()
        if change == 'stop': dispatcher.lifecycle.request_stop()
        dispatcher.admit()
        assert not dispatcher.active
        assert not dispatcher.execution.db.execute('SELECT * FROM attempts').fetchall()
    finally:
        dispatcher.close()


def test_concurrent_atomic_reservation_and_branch_lock(env):
    config, project, _, _, _ = env
    path = config.workspace / '.runtime' / 'tasks.sqlite3'
    store = Store(path)
    execution = ExecutionStore(store)
    store.apply(project, [issue(project), issue(project, number=2, id=2)])
    assert store.confirm(project.repository, 1, 1)
    assert store.confirm(project.repository, 2, 1)
    row = dict(store.get(project.repository, 1))
    token = store.guard(project.repository, 1)['observation_token']
    def reserve(_):
        other = Store(path)
        try: return ExecutionStore(other).reserve(row, 'task', 2, token)
        finally: other.close()
    try:
        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(reserve, range(2)))
        assert sum(v is not None for v in values) == 1
        second = store.get(project.repository, 2)
        assert execution.reserve(second, 'task', 2, store.guard(project.repository, 2)['observation_token']) is None
        assert execution.reserve(second, 'other', 1, store.guard(project.repository, 2)['observation_token']) is None
    finally: store.close()


def test_max_parallel_timeout_force_and_graceful_stop(env):
    config, project, control, _, _ = env
    control.write_text(json.dumps(dict(exec='sleep')))
    config = replace(config, max_parallel_tasks=2, timeout_seconds=1)
    api = adapter([issue(project), issue(project, number=2, id=2), issue(project, number=3, id=3)])
    lifecycle = Lifecycle(2, 1)
    dispatcher = Dispatcher(config, lifecycle, api)
    try:
        register(dispatcher, project, api.fetch.return_value)
        dispatcher.admit()  # one project gets one slot per admission round
        dispatcher.admit()
        dispatcher.admit()
        assert len(dispatcher.active) == 2
        lifecycle.request_stop()
        dispatcher.admit()
        assert dispatcher.store.get(project.repository, 3)['state'] == 'pending'
        finish(dispatcher)
        assert {r['error_code'] for r in dispatcher.execution.db.execute('SELECT * FROM attempts')} == {'codex_timeout'}
    finally:
        lifecycle.cancel()
        dispatcher.close()


def test_force_interrupts_and_keeps_artifacts(env):
    config, project, control, _, _ = env
    control.write_text(json.dumps(dict(exec='sleep')))
    lifecycle = Lifecycle(force_grace_timeout_seconds=1)
    dispatcher = Dispatcher(config, lifecycle, adapter([issue(project)]))
    try:
        register(dispatcher, project, [issue(project)])
        dispatcher.admit()
        lifecycle.request_stop(True)
        finish(dispatcher)
        row = dispatcher.execution.db.execute('SELECT * FROM attempts').fetchone()
        assert row['state'] == 'interrupted' and row['error_code'] == 'force_stop'
        assert Path(row['worktree']).exists()
        assert not lifecycle.children
    finally:
        lifecycle.cancel()
        dispatcher.close()


def test_push_conflict_preserves_local_commit(env):
    config, project, _, seed, remote = env
    dispatcher = Dispatcher(config, Lifecycle(), adapter([issue(project)]))
    try:
        register(dispatcher, project, [issue(project)])
        dispatcher.admit()
        (seed / 'other').write_text('competing')
        git(seed, 'add', '.')
        git(seed, 'commit', '-m', 'Competing')
        git(seed, 'push', str(remote), 'HEAD:refs/heads/codex/issue-1')
        finish(dispatcher)
        row = dispatcher.execution.db.execute('SELECT * FROM attempts').fetchone()
        assert row['state'] == 'running' and row['recovery_diagnostic'] == 'non_fast_forward'
        assert dispatcher.store.db.execute("SELECT outcome FROM publication_operations WHERE operation_key LIKE 'pr:%'").fetchone()[0] == 'failed'
        assert row['result_sha'] == git(Path(row['worktree']), 'rev-parse', 'HEAD')
        assert row['result_sha'] != git(remote, 'rev-parse', row['branch'])
    finally: dispatcher.close()


class Response(io.BytesIO):
    status = 200
    def __init__(self, data=b'file', url='https://github.com/user-attachments/files/123/file.txt', headers=None):
        super().__init__(data)
        self.url = url
        self.headers = headers or {}
    def geturl(self): return self.url


@pytest.mark.parametrize('failure', [None,403,404,'oversize','path','redirect','html','count'])
def test_attachments_manifest_auth_limits_and_failure(env, failure):
    config, _, _, _, _ = env
    config = replace(config, attachment_max_bytes=8, attachment_max_files=2)
    opener = Mock()
    url = 'https://github.com/user-attachments/files/123/file.txt'
    body = f'![image]({url}) [file]({url}?second) [external](https://example.com/a.txt)'
    opener.open.side_effect = lambda request, **_: Response(url=request.full_url)
    if failure in (403,404): opener.open.side_effect = HTTPError(url, failure, 'SECRET', {}, None)
    if failure == 'oversize': opener.open.side_effect = lambda *a, **k: Response(b'0123456789')
    if failure == 'path': body = '![x](https://github.com/user-attachments/files/123/%2e%2e%2fevil)'
    if failure == 'redirect': opener.open.side_effect = HTTPError(url, 302, 'redirect', {'Location':'https://evil.invalid/file'}, None)
    if failure == 'html': opener.open.side_effect = lambda *a, **k: Response(headers={'Content-Type':'text/html'})
    if failure == 'count': body += f' [extra]({url}?third)'
    manager = IssueAttachmentManager(config, opener)
    manager.token = Mock(return_value='test-private-token')
    task = config.workspace / 'one' / 'tasks' / 'issue-1'
    if failure:
        with pytest.raises(TaskError, match='attachment_download_failed'):
            manager.prepare(body, task)
    else:
        paths = manager.prepare(body, task)
        assert len(paths) == 2 and len(set(paths)) == 2
        assert opener.open.call_args.args[0].get_header('Authorization') == 'Bearer test-private-token'
    manifest = json.loads((task / 'attachments' / 'manifest.json').read_text())
    assert 'test-private-token' not in str(manifest)
    if not failure:
        assert all(entry['sha256'] == hashlib.sha256(b'file').hexdigest() for entry in manifest)


def test_attachment_redirect_drops_token_and_symlink_rejected(env):
    config, _, _, _, _ = env
    opener = Mock()
    url = 'https://github.com/user-attachments/files/123/file'
    cdn = 'https://objects.githubusercontent.com/file?signature=private'
    opener.open.side_effect = [HTTPError(url, 302, '', {'Location':cdn}, None), Response(url=cdn)]
    manager = IssueAttachmentManager(config, opener)
    manager.token = lambda: 'test-token'
    task = config.workspace / 'one' / 'tasks' / 'issue-1'
    manager.prepare(url, task)
    assert opener.open.call_args_list[1].args[0].get_header('Authorization') is None
    task2 = config.workspace / 'one' / 'tasks' / 'issue-2'
    task2.mkdir()
    (task2 / 'attachments').symlink_to(task / 'attachments')
    with pytest.raises(TaskError): manager.prepare(url, task2)


def test_one_failed_attachment_never_starts_codex(env):
    config, project, _, _, _ = env
    item = issue(project, body='![x](https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc)')
    dispatcher = Dispatcher(config, Lifecycle(), adapter([item]))
    dispatcher.attachments.token = lambda: 'test-token'
    dispatcher.attachments.opener = Mock()
    dispatcher.attachments.opener.open.side_effect = HTTPError('', 403, 'SECRET', {}, None)
    dispatcher.executor.start = Mock()
    try:
        register(dispatcher, project, [item])
        dispatcher.admit()
        dispatcher.executor.start.assert_not_called()
        row = dispatcher.execution.db.execute('SELECT * FROM attempts').fetchone()
        assert row['error_code'] == 'attachment_download_failed' and row['codex_started'] == 0
        body = dispatcher.execution.db.execute('SELECT body FROM result_outbox').fetchone()[0]
        assert '[ATTACHMENT]' in body and 'Codex was not started' in body
    finally: dispatcher.close()


@pytest.mark.parametrize('used,state', [(89,'quota_available'),(90,'quota_available'),(91,'quota_paused')])
def test_quota_threshold_and_all_windows(used, state):
    assert evaluate({'rateLimits':{'primary':{'usedPercent':used}}},10)[0] == state
    assert evaluate({'rateLimits':{'primary':{'usedPercent':0},'secondary':{'usedPercent':99}}},10)[0] == 'quota_paused'
    assert evaluate({'rateLimits':{}},10)[0] == 'quota_unknown'
    assert evaluate({'rateLimits':{'primary':{'usedPercent':0}},
                     'rateLimitsByLimitId':{'extra':{'primary':{'usedPercent':100}}}},10)[0] == 'quota_paused'


@pytest.mark.parametrize('server', ['ok','crash','unsupported'])
def test_app_server_managed_rpc_restart_and_disabled(env, server):
    config, _, control, _, _ = env
    config = replace(config, quota_enabled=True)
    lifecycle = Lifecycle()
    quota = CodexQuotaProvider(config, lifecycle)
    try:
        assert quota.check(fresh=True)
        first = quota.child.pid
        assert quota.check(fresh=True) and quota.child.pid == first
        control.write_text(json.dumps(dict(server=server, used=91)))
        assert not quota.check(fresh=True)
        assert quota.state == ('quota_paused' if server == 'ok' else 'quota_unknown')
        assert not quota.check(fresh=True)
        control.write_text(json.dumps(dict(used=90)))
        quota.next_check = 0
        assert quota.check(fresh=True)
        if server != 'ok': assert quota.child.pid != first
    finally:
        quota.close()
        assert lifecycle._active() == []
    disabled = CodexQuotaProvider(replace(config, quota_enabled=False), lifecycle)
    assert disabled.check() and disabled.child is None


def test_quota_pause_does_not_cancel_running_exec(env):
    config, project, control, _, _ = env
    config = replace(config, quota_enabled=True)
    control.write_text(json.dumps(dict(exec='sleep')))
    lifecycle = Lifecycle()
    dispatcher = Dispatcher(config, lifecycle, adapter([issue(project),issue(project, id=2, number=2)]))
    try:
        register(dispatcher, project, dispatcher.adapter.fetch.return_value)
        dispatcher.admit()
        child = next(iter(dispatcher.active.values()))['child']
        control.write_text(json.dumps(dict(exec='sleep', used=99)))
        assert not dispatcher.quota.check(fresh=True)
        assert child in lifecycle._active()
        lifecycle.request_stop(True)
        finish(dispatcher)
    finally:
        lifecycle.cancel()
        dispatcher.close()


def test_outbox_uncertain_write_recovery_and_migration(env):
    config, project, _, _, _ = env
    dispatcher = Dispatcher(config, Lifecycle(), adapter([issue(project)]))
    register(dispatcher, project, [issue(project)])
    dispatcher.admit()
    finish(dispatcher)
    attempt = dispatcher.execution.db.execute('SELECT * FROM attempts').fetchone()
    body = dispatcher.execution.db.execute('SELECT body FROM result_outbox').fetchone()[0]
    api = dispatcher.adapter
    api.write.side_effect = GitHubError()
    dispatcher.publisher.flush()
    assert dispatcher.execution.db.execute('SELECT published FROM result_outbox').fetchone()[0] == 0
    dispatcher.close()
    store = Store(config.workspace / '.runtime' / 'tasks.sqlite3')
    try:
        execution = ExecutionStore(store)
        ExecutionStore(store)
        assert [r[0] for r in store.db.execute('SELECT version FROM schema_migrations ORDER BY version')] == [1,2,3,602,604,605]
        assert store.get(project.repository,1)['approved_version'] == 1
        api.pages.return_value = [dict(id=456,body=body)]
        TaskResultPublisher(execution,api).flush()
        assert api.write.call_count == 2
        assert execution.get(attempt['attempt_id'])['state'] == 'review'
        assert store.db.execute('SELECT comment_id FROM result_outbox').fetchone()[0] == '456'
    finally: store.close()


def test_crash_running_never_reexecutes_and_cleanup_requires_done(env):
    config, project, _, _, _ = env
    dispatcher = Dispatcher(config, Lifecycle(), adapter([issue(project)]))
    register(dispatcher,project,[issue(project)])
    row = dispatcher.store.get(project.repository,1)
    token = dispatcher.store.guard(project.repository,1)['observation_token']
    attempt = dispatcher.execution.reserve(row,'codex/issue-1',1,token)
    dispatcher.close()
    dispatcher = Dispatcher(config,Lifecycle(),adapter([issue(project)]))
    try:
        assert dispatcher.execution.get(attempt)['state'] == 'interrupted'
        dispatcher.admit()
        assert not dispatcher.active
        assert dispatcher.execution.db.execute('SELECT count(*) FROM result_outbox').fetchone()[0] == 1
    finally: dispatcher.close()


@pytest.mark.parametrize('case',['clean','dirty','partial','symlink','traversal','review'])
def test_done_cleanup_is_conservative_and_repeatable(env, case):
    config, project, _, _, _ = env
    manager = GitWorkspaceManager(config)
    item = issue(project)
    worktree,_ = manager.prepare(project,item,'codex/issue-1')
    task = worktree.parent
    attachments = task / 'attachments'
    attachments.mkdir()
    (attachments / 'manifest.json').write_text('[]')
    row = dict(number=1,state='done')
    if case == 'dirty': (worktree / 'dirty').write_text('preserve')
    if case == 'partial': manager.git(config.workspace / project.id / 'repository','worktree','remove',str(worktree))
    if case == 'symlink': (attachments / 'escape').symlink_to(config.source)
    if case == 'traversal': row['number'] = '../outside'
    if case == 'review': row['state'] = 'review'
    cleanup = TaskWorkspaceCleanup(manager)
    if case in ('clean','partial'):
        cleanup.cleanup(project,row)
        cleanup.cleanup(project,row)
        assert not task.exists()
    else:
        with pytest.raises(TaskError): cleanup.cleanup(project,row)
        assert attachments.exists()


def test_recovery_verified_process_group_and_unrelated_process(env):
    config, _, _, _, _ = env
    registry = config.workspace / '.runtime' / 'children.json'
    old = Lifecycle(registry=registry)
    child = old.submit([sys.executable,'-c','import time; time.sleep(60)'])
    unrelated = subprocess.Popen(['sleep','60'],start_new_session=True)
    try:
        new = Lifecycle(force_grace_timeout_seconds=1,registry=registry)
        new.recover()
        assert identity(child.pid) is None
        assert unrelated.poll() is None
        assert not registry.exists()
    finally:
        child.wait()
        unrelated.terminate()
        unrelated.wait()


@pytest.mark.parametrize('section', [dict(codex=dict(quota=dict(min_remaining_percent=101))),
    dict(codex=dict(quota=dict(enabled='yes'))),dict(codex=dict(timeout_seconds=0)),
    dict(attachments=dict(max_bytes=-1)),dict(codex=dict(execution_mode='interactive'))])
def test_stage04_configuration_rejects_invalid_values(env, section):
    config,*_ = env
    data = yaml.safe_load(config.source.read_text())
    data.update(section)
    config.source.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError): load(config.source)


def write_github_state_stub(gh):
    gh.write_text(f'#!{sys.executable}\n' + '''
import json,os,pathlib,sys,tempfile
path=pathlib.Path(__file__).with_name('github-state.json')
state=json.loads(path.read_text())
method=sys.argv[sys.argv.index('--method')+1]
endpoint=next(arg for arg in sys.argv if arg.startswith('repos/'))
if method=='GET':
    if endpoint=='repos/owner/repo': result=dict(id=11,full_name='owner/repo')
    elif '/pulls?' in endpoint: result=[state['pr']] if 'pr' in state else []
    elif '/pulls/' in endpoint: result=state['pr']
    elif '/comments' in endpoint: result=state['comments']
    elif '/labels' in endpoint: result=state['issue']['labels']
    else: result=[state['issue']]
else:
    payload=json.load(sys.stdin)
    if endpoint.endswith('/pulls'):
        import re
        sha=re.search(r'SHA: ([0-9a-f]+)',payload['body'])[1]
        result=dict(id=301,number=2,body=payload['body'],state='open',draft=False,merged=False,changed_files=1,
                    head=dict(ref=payload['head'],sha=sha,repo=dict(id=11)),base=dict(ref=payload['base'],repo=dict(id=11)))
        state['pr']=result
    elif '/comments' in endpoint:
        result=dict(id=len(state['comments'])+1,body=payload['body'])
        state['comments'].append(result)
    elif method=='POST':
        state['issue']['labels'] += [dict(name=name) for name in payload['labels']]
        result={}
    else:
        from urllib.parse import unquote
        name=unquote(endpoint.rsplit('/',1)[-1])
        state['issue']['labels']=[x for x in state['issue']['labels'] if x['name']!=name]
        result={}
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                                         prefix=path.name+'.',suffix='.tmp',delete=False) as stream:
            temporary=pathlib.Path(stream.name)
            json.dump(state,stream)
        os.replace(temporary,path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
print('HTTP/2 200 OK\\nDate: Sat, 24 Oct 2026 '+('00:10:00' if '/pulls/' in endpoint else '00:00:00')+' GMT\\n\\n'+json.dumps(result))
''')
    gh.chmod(0o700)


def daemon_comment_diagnostic(argv, config, state):
    """Bounded diagnostics, evaluated only on failure and still inside stop's try/finally."""
    try:
        status = subprocess.run([*argv, 'status'], capture_output=True, text=True, timeout=5)
        status_text = f'exit={status.returncode}, stdout={status.stdout!r}, stderr={status.stderr!r}'
    except (OSError, subprocess.TimeoutExpired) as exc:
        status_text = type(exc).__name__
    try:
        with (config.logs / 'dispatcher.jsonl').open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            log = stream.read(4096).decode('utf-8', errors='replace')
    except OSError as exc:
        log = type(exc).__name__
    return f'Expected one comment within 10s; GitHub state={state!r}; daemon status={status_text}; log tail={log}'


@pytest.mark.parametrize('failure', [None, 'write', 'replace'])
def test_github_state_stub_publishes_complete_json(tmp_path, monkeypatch, failure):
    """Read during a deliberately partial write of the actual generated stub."""
    path = tmp_path / 'github-state.json'
    original = dict(issue={'labels': []}, comments=[])
    path.write_text(json.dumps(original))
    gh = tmp_path / 'gh-stub'
    write_github_state_stub(gh)
    streams = []
    replacements = []
    replace_file = os.replace

    def partial_dump(value, stream):
        streams.append(stream)
        encoded = json.dumps(value)
        midpoint = len(encoded) // 2
        stream.write(encoded[:midpoint])
        stream.flush()
        # A reader opening the published path while the write is incomplete
        # must still see the entire previous document, with no parse retries.
        assert json.loads(path.read_text()) == original
        if failure == 'write':
            raise OSError('injected write failure')
        stream.write(encoded[midpoint:])

    def publish(source, destination):
        source, destination = Path(source), Path(destination)
        assert source.parent == destination.parent == tmp_path
        assert source != destination
        assert streams[-1].closed
        assert json.loads(path.read_text()) == original
        assert json.loads(source.read_text())['comments'] == [dict(id=1, body='result')]
        replacements.append(source)
        if failure == 'replace':
            raise OSError('injected replace failure')
        replace_file(source, destination)

    monkeypatch.setattr(json, 'dump', partial_dump)
    monkeypatch.setattr(os, 'replace', publish)
    monkeypatch.setattr(sys, 'argv', [str(gh), '--method', 'POST', 'repos/owner/repo/issues/1/comments'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'body': 'result'})))
    if failure:
        with pytest.raises(OSError, match='injected'):
            runpy.run_path(str(gh), run_name='__main__')
        assert json.loads(path.read_text()) == original
    else:
        runpy.run_path(str(gh), run_name='__main__')
        assert json.loads(path.read_text())['comments'] == [dict(id=1, body='result')]
        assert len(replacements) == 1
    assert streams and all(stream.closed for stream in streams)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['gh-stub', 'github-state.json']


def test_daemon_comment_diagnostic_handles_status_timeout(tmp_path, monkeypatch):
    from types import SimpleNamespace
    config = SimpleNamespace(logs=tmp_path)
    (tmp_path / 'dispatcher.jsonl').write_text('x' * 5000 + '\nlast daemon event\n')
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 5
        raise subprocess.TimeoutExpired(args[0], 5)
    monkeypatch.setattr(subprocess, 'run', timeout)
    diagnostic = daemon_comment_diagnostic(['dispatcher'], config, {'comments': []})
    assert 'within 10s' in diagnostic and "'comments': []" in diagnostic
    assert 'TimeoutExpired' in diagnostic and 'last daemon event' in diagnostic
    assert len(diagnostic) < 4500


def test_real_daemon_start_poll_execute_comment_stop(env):
    """Exercise actual CLI daemon and stub gh over its real subprocess boundary."""
    config, project, _, _, remote = env
    api_state = config.source.parent / 'github-state.json'
    raw = dict(id=1,number=1,title='Task',body='Requirements',user=dict(login='alice'),
               labels=[dict(name=project.task_label)],state='open',updated_at='2026-10-24T00:00:00Z',
               url='https://api.github.com/repos/owner/repo/issues/1',html_url='https://github.com/owner/repo/issues/1')
    api_state.write_text(json.dumps(dict(issue=raw,comments=[])))
    gh = config.source.parent / 'gh-stub'
    write_github_state_stub(gh)
    data = yaml.safe_load(config.source.read_text())
    data['executables'] = dict(github=str(gh))
    data['dispatcher'] = dict(poll_interval_seconds=1)
    config.source.write_text(yaml.safe_dump(data))
    config = load(config.source)
    store = Store(config.workspace / '.runtime' / 'tasks.sqlite3')
    store.apply(project,[issue(project)])
    assert store.confirm(project.repository,1,1)
    store.close()
    argv = [sys.executable,'-m','dispatcher','--config',str(config.source)]
    result = subprocess.run([*argv,'start'],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    try:
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            state=json.loads(api_state.read_text())
            if state['comments']: break
            time.sleep(.05)
        assert len(state['comments'])==1, daemon_comment_diagnostic(argv, config, state)
        assert 'Task completed successfully.' in state['comments'][0]['body']
        assert git(remote,'rev-parse','codex/issue-1')
    finally:
        result=subprocess.run([*argv,'stop'],capture_output=True,text=True,timeout=10)
        assert result.returncode == 0, result.stderr
    result=subprocess.run([*argv,'status'],capture_output=True,text=True,timeout=5)
    assert result.stdout.strip()=='stopped'


def test_default_branch_different_from_config_and_invalid_refs(env):
    config, project, _, _, remote = env
    git(remote,'branch','production','main')
    git(remote,'symbolic-ref','HEAD','refs/heads/production')
    manager=GitWorkspaceManager(config)
    assert manager.branch(project,issue(project,target_branch='production'))=='codex/issue-1'
    worktree,sha=manager.prepare(project,issue(project),'codex/issue-1')
    assert sha==git(remote,'rev-parse','production')
    for branch in ('../escape','-option','a..b','x\nmain','main.lock'):
        with pytest.raises(TaskError): manager.validate_branch(branch)
    with pytest.raises(TaskError): manager.push(project,worktree,'production')


def test_origin_change_and_rewritten_history_are_rejected(env):
    config, project, _, _, remote=env
    manager=GitWorkspaceManager(config)
    worktree,sha=manager.prepare(project,issue(project),'codex/issue-1')
    with pytest.raises(TaskError,match='origin_changed'):
        manager.push(project,worktree,'codex/issue-1',str(remote)+'-unexpected')
    git(worktree,'checkout','--orphan','other')
    git(worktree,'commit','-m','Orphan')
    git(worktree,'branch','-m','codex/issue-1-orphan')
    with pytest.raises(TaskError,match='rewritten_history'):
        manager.verify(worktree,'codex/issue-1-orphan',sha)


def test_quota_reset_read_and_invalid_data(env):
    config,_,_,_,_=env
    lifecycle=Lifecycle()
    quota=CodexQuotaProvider(replace(config,quota_enabled=True),lifecycle)
    quota.start=Mock()
    quota.rpc=Mock(return_value=dict(rateLimits=dict(primary=dict(usedPercent=99,resetsAt=int(time.time())+2))))
    assert not quota.check()
    assert 0 < quota.next_check-time.monotonic() <= 63
    quota.next_check=0
    quota.rpc.return_value=dict(rateLimits=dict(primary=dict(usedPercent=90)))
    assert quota.check()
    quota.rpc.return_value=dict(rateLimits=dict(primary=dict(usedPercent='SECRET')))
    assert not quota.check(fresh=True)
    assert quota.state=='quota_unknown'


def test_quota_network_error_is_not_exhaustion(tmp_path):
    from dispatcher.codex import CodexExecutor
    path=tmp_path/'events'
    path.write_text(json.dumps(dict(type='error',error=dict(code='network_error',message='quota_exhausted HTTP 429')))+'\n')
    assert not CodexExecutor.quota_exhausted(path)


def test_stale_observation_cannot_reserve(env):
    config, project, _, _, _=env
    store=Store(config.workspace/'.runtime'/'tasks.sqlite3')
    try:
        execution=ExecutionStore(store)
        store.apply(project,[issue(project)])
        store.confirm(project.repository,1,1)
        row=store.get(project.repository,1)
        token=store.guard(project.repository,1)['observation_token']
        store.apply(project,[issue(project)])
        assert execution.reserve(row,'task',1,token) is None
        assert store.get(project.repository,1)['state']=='pending'
    finally: store.close()


def test_launch_gate_never_executes_without_durable_registry(env):
    config,*_=env
    lifecycle=Lifecycle(registry=config.workspace/'.runtime'/'children.json')
    marker=config.workspace/'must-not-exist'
    lifecycle.save_registry=Mock(side_effect=OSError('disk failure SECRET'))
    with pytest.raises(OSError):
        lifecycle.submit([sys.executable,'-c',f'from pathlib import Path; Path({str(marker)!r}).touch()'])
    assert not marker.exists()
    assert not lifecycle.children


def test_crash_after_push_inspects_remote_without_reexecution(env):
    config,project,_,_,_=env
    dispatcher=Dispatcher(config,Lifecycle(),adapter([issue(project)]))
    register(dispatcher,project,[issue(project)])
    row=dispatcher.store.get(project.repository,1)
    attempt=dispatcher.execution.reserve(row,'codex/issue-1',1,dispatcher.store.guard(project.repository,1)['observation_token'])
    worktree,sha=dispatcher.git.prepare(project,issue(project),'codex/issue-1')
    origin=dispatcher.git.git(worktree,'remote','get-url','origin').stdout.strip()
    dispatcher.execution.update(attempt,worktree=str(worktree),initial_sha=sha,origin_url=origin)
    (worktree/'changed').write_text('changed')
    git(worktree,'add','.')
    git(worktree,'commit','-m','Task')
    dispatcher.git.push(project,worktree,'codex/issue-1',origin)
    dispatcher.close() # emulate crash between push and result transaction
    dispatcher=Dispatcher(config,Lifecycle(),adapter([issue(project)]))
    try:
        row=dispatcher.execution.get(attempt)
        assert row['state']=='interrupted' and row['error_code']=='crash_recovery'
        assert row['remote_sha']==row['result_sha']==git(worktree,'rev-parse','HEAD')
        assert row['recovery_diagnostic']=='remote_matches_local'
        dispatcher.admit()
        assert not dispatcher.active
    finally: dispatcher.close()


def test_unknown_backend_permission_does_not_infer_quota_recovery():
    payload=dict(rateLimits=dict(primary=dict(usedPercent=0)),ordinaryUsageAllowed=None)
    assert evaluate(payload,10)[0]=='quota_unknown'
    payload['ordinaryUsageAllowed']=False
    assert evaluate(payload,10)[0]=='quota_paused'
    payload['ordinaryUsageAllowed']=True
    assert evaluate(payload,10)[0]=='quota_available'
    payload['rateLimits']['primary']['usedPercent']=True
    with pytest.raises(ValueError): evaluate(payload,10)


def test_scheduler_project_round_robin(env):
    from dispatcher.initialization import fingerprint
    config,project,_,_,remote=env
    second=replace(project,id='two',repository='owner/second')
    second=replace(second,init_fingerprint=fingerprint(second))
    config=replace(config,projects=(project,second))
    prepare(config)
    repo=config.workspace/second.id/'repository'
    git(config.source.parent,'clone','--no-checkout',str(remote),str(repo))
    git(repo,'config','user.name','Test')
    git(repo,'config','user.email','test@example.invalid')
    first_items=[issue(project),issue(project,id=2,number=2)]
    second_items=[issue(second,id=3)]
    api=adapter([])
    api.fetch.side_effect=lambda p: first_items if p.id==project.id else second_items
    dispatcher=Dispatcher(config,Lifecycle(),api)
    try:
        register(dispatcher,project,first_items)
        register(dispatcher,second,second_items)
        dispatcher.admit()
        finish(dispatcher)
        assert dispatcher.store.get(project.repository,1)['state']=='review'
        dispatcher.admit()
        finish(dispatcher)
        # Same remote branch has first task's commit; stub's second commit may
        # be no-op, but admission must nevertheless have selected project two.
        assert dispatcher.store.get(second.repository,1)['state'] in ('review','failed')
        assert dispatcher.store.get(project.repository,2)['state']=='pending'
    finally:
        dispatcher.lifecycle.cancel()
        dispatcher.close()


def test_comment_never_includes_raw_error_token_or_private_path():
    from dispatcher.publisher import comment_body
    body=comment_body(dict(category='SECRET',state='failed',branch='ghp_SUPERSECRET',result_sha=None,
        error_code='/private/path/SECRET',codex_started=0,duration=4,attempt_id='a'*32,
        started_at='2026-10-24T00:00:00+00:00',finished_at='2026-10-24T00:00:04+00:00'))
    assert 'SECRET' not in body and '/private/' not in body
    assert 'Branch: [redacted]' in body and 'Reason: internal_error' in body


def test_partial_attachment_success_still_blocks_execution(env):
    config,project,_,_,_=env
    url='https://github.com/user-attachments/files/123/file.txt'
    item=issue(project,body=f'[first]({url}) [second]({url}?second)')
    dispatcher=Dispatcher(config,Lifecycle(),adapter([item]))
    dispatcher.attachments.token=lambda:'test-token'
    opener=Mock()
    opener.open.side_effect=[Response(),HTTPError(url,404,'SECRET',{},None)]
    dispatcher.attachments.opener=opener
    dispatcher.executor.start=Mock()
    try:
        register(dispatcher,project,[item])
        dispatcher.admit()
        dispatcher.executor.start.assert_not_called()
        _,task,_=dispatcher.git.paths(project,1)
        manifest=json.loads((task/'attachments'/'manifest.json').read_text())
        assert [m['status'] for m in manifest]==['downloaded','failed']
        assert Path(manifest[0]['local_path']).read_bytes()==b'file'
    finally: dispatcher.close()


def test_malformed_external_payloads_fail_safely(env, tmp_path):
    from dispatcher.codex import CodexExecutor
    with pytest.raises(ValueError): evaluate([],10)
    path=tmp_path/'malformed.log'
    path.write_text('[]\nnull\n{"item": []}\n')
    assert not CodexExecutor.quota_exhausted(path)
    assert not CodexExecutor.confirmed_noop(dict(body='No-op allowed: explanation'),path)
    config,project,_,_,_=env
    dispatcher=Dispatcher(config,Lifecycle(),adapter([issue(project)]))
    try:
        register(dispatcher,project,[issue(project)])
        dispatcher.admit()
        finish(dispatcher)
        dispatcher.adapter.write.reset_mock()
        dispatcher.adapter.pages.return_value=['malformed']
        dispatcher.publisher.flush()
        assert dispatcher.execution.db.execute('SELECT published FROM result_outbox').fetchone()[0]==0
        dispatcher.adapter.write.assert_not_called()
    finally: dispatcher.close()


def test_service_clone_uses_configured_gh_and_destination(env):
    import shutil
    config,project,_,_,remote=env
    repo=config.workspace/project.id/'repository'
    shutil.rmtree(repo)
    gh=config.source.parent/'gh-clone-stub'
    gh.write_text(f'#!{sys.executable}\n'+f'''
import json,subprocess,sys
if sys.argv[1:3]==['repo','view']:
    print(json.dumps({{'defaultBranchRef':{{'name':'main'}}}}))
else:
    assert sys.argv[1:4]==['repo','clone','owner/repo']
    assert sys.argv[5:]==['--','--no-checkout']
    subprocess.run(['git','clone','--no-checkout',{str(remote)!r},sys.argv[4]],check=True)
''')
    gh.chmod(0o700)
    manager=GitWorkspaceManager(replace(config,github_executable=str(gh)))
    assert manager.branch(project,issue(project))=='codex/issue-1'
    worktree,_=manager.prepare(project,issue(project),'codex/issue-1')
    assert (worktree/'initial').exists()
    assert repo.stat().st_mode & 0o777==0o700


@pytest.mark.parametrize('url', [
    'https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc',
    'https://github.com/user-attachments/files/123/report.pdf',
    'https://github.com/owner/repo/files/123/report.pdf?download=1',
    'https://github.com/owner/repo/assets/123/12345678-1234-1234-1234-123456789abc',
    'https://user-images.githubusercontent.com/123/456-abc.png',
    'https://private-user-images.githubusercontent.com/123/456-abc.png?jwt=signed',
])
def test_attachment_url_formats(url):
    assert IssueAttachmentManager.recognized(url)
    manager = IssueAttachmentManager(None)
    assert manager.urls(f'[file]({url}) <img src="{url}">') == [url]


@pytest.mark.parametrize('url', [
    'https://github.com/owner/repo/blob/main/files/report.pdf',
    'https://github.com/owner/repo/tree/main/assets/image.png',
    'https://github.com/owner/repo/blob/main/README.md',
    'https://github.com/owner/repo/blob/main/src/main.py',
    'https://github.com/owner/repo/releases/download/v1/report.pdf',
    'https://github.com/owner/repo/files/arbitrary/report.pdf',
    'https://github.com/user-attachments/assets/not-a-uuid',
    'https://github.com/user-attachments/files/123',
    'https://github.com/user-attachments/anything',
    'https://user-images.githubusercontent.com/arbitrary/path',
    'https://private-user-images.githubusercontent.com/',
    'https://example.com/files/123/report.pdf',
    'https://github.com.evil.invalid/user-attachments/files/123/report.pdf',
    'https://objects.githubusercontent.com/github-production-release-asset/file',
    'http://github.com/user-attachments/files/123/report.pdf',
    'https://token@github.com/user-attachments/files/123/report.pdf',
    'https://github.com:invalid/user-attachments/files/123/report.pdf',
    'https://github.com:444/user-attachments/files/123/report.pdf',
    'https://[broken',
])
def test_non_attachment_urls(url):
    assert not IssueAttachmentManager.recognized(url)
    assert IssueAttachmentManager(None).urls(f'[link]({url})') == []


def test_repository_file_link_does_not_download_or_block_codex(env):
    config, project, _, _, _ = env
    item = issue(project, body='Read https://github.com/owner/repo/blob/main/files/spec.md')
    dispatcher = Dispatcher(config, Lifecycle(), adapter([item]))
    dispatcher.attachments.download = Mock(side_effect=AssertionError('unexpected download'))
    dispatcher.attachments.token = Mock(side_effect=AssertionError('unexpected token lookup'))
    try:
        register(dispatcher, project, [item])
        dispatcher.admit()
        finish(dispatcher)
        row = dispatcher.execution.db.execute('SELECT * FROM attempts').fetchone()
        assert row['state'] == 'review' and row['codex_started'] == 1
        dispatcher.attachments.download.assert_not_called()
        dispatcher.attachments.token.assert_not_called()
    finally:
        dispatcher.close()


@pytest.mark.parametrize('case', [
    'normal', 'partial', 'unknown', 'other_attempt', 'artifact_symlink',
    'attachment_unknown', 'bad_manifest', 'bad_registry', 'ignored', 'failed', 'interrupted',
])
def test_cleanup_registered_artifacts_and_preservation(env, case, monkeypatch):
    from dispatcher.workspace import register_artifacts
    config, project, _, seed, remote = env
    manager = GitWorkspaceManager(config)
    worktree, sha = manager.prepare(project, issue(project), 'codex/issue-1')
    task = worktree.parent
    attempt = 'a' * 32
    names = [f'{attempt}.prompt', f'{attempt}.codex.log', 'dispatcher.tmp']
    register_artifacts(config.workspace, task, *names)
    for name in names:
        (task / name).write_text('owned')
    attachments = task / 'attachments'
    attachments.mkdir()
    attachment = attachments / '001-report.txt'
    attachment.write_text('data')
    (attachments / 'manifest.json').write_text(json.dumps([dict(local_path=str(attachment))]))
    row = dict(number=1, state='done', attempt_id=attempt)
    if case == 'unknown': (task / 'keep.txt').write_text('user data')
    if case == 'other_attempt': (task / ('b' * 32 + '.prompt')).write_text('other task')
    if case == 'artifact_symlink':
        (task / names[0]).unlink()
        (task / names[0]).symlink_to(seed / 'initial')
    if case == 'attachment_unknown': (attachments / 'keep.txt').write_text('user data')
    if case == 'bad_manifest':
        (attachments / 'manifest.json').write_text(json.dumps([dict(local_path=str(seed / 'initial'))]))
    if case == 'bad_registry': (task / '.dispatcher-artifacts.jsonl').write_text('"../outside"\n')
    if case == 'ignored':
        (worktree / '.gitignore').write_text('ignored\n')
        git(worktree, 'add', '.gitignore')
        git(worktree, 'commit', '-m', 'Ignore file')
        (worktree / 'ignored').write_text('preserve')
    if case in ('failed', 'interrupted'): row['state'] = case
    cleanup = TaskWorkspaceCleanup(manager)
    if case == 'partial':
        original = Path.unlink
        def fail_once(path, *args, **kwargs):
            if path == task / names[0]:
                raise OSError('simulated interruption after worktree and attachments removal')
            return original(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'unlink', fail_once)
            with pytest.raises(OSError): cleanup.cleanup(project, row)
        assert not worktree.exists() and not attachments.exists()
    if case in ('normal', 'partial'):
        cleanup.cleanup(project, row)
        cleanup.cleanup(project, row)
        assert not task.exists()
        assert git(config.workspace / project.id / 'repository', 'rev-parse', 'codex/issue-1') == sha
        assert git(remote, 'rev-parse', 'main') == sha
    else:
        with pytest.raises(TaskError): cleanup.cleanup(project, row)
        assert worktree.exists() and attachment.exists()
        assert (seed / 'initial').read_text() == 'initial'


def test_cleanup_legacy_attempt_artifacts(env):
    config, project, _, _, _ = env
    manager = GitWorkspaceManager(config)
    worktree, _ = manager.prepare(project, issue(project), 'codex/issue-1')
    attempt = 'a' * 32
    for suffix in ('.prompt', '.codex.log'):
        (worktree.parent / (attempt + suffix)).write_text('legacy')
    TaskWorkspaceCleanup(manager).cleanup(project, dict(number=1, state='done', attempt_id=attempt))
    assert not worktree.parent.exists()


def test_real_executor_artifacts_cleanup_after_done(env):
    config, project, _, _, remote = env
    dispatcher = Dispatcher(config, Lifecycle(), adapter([issue(project)]))
    try:
        register(dispatcher, project, [issue(project)])
        dispatcher.admit()
        finish(dispatcher)
        row = dict(dispatcher.execution.db.execute('SELECT * FROM attempts').fetchone())
        assert row['state'] == 'review'
        task = Path(row['worktree']).parent
        registry = task / '.dispatcher-artifacts.jsonl'
        assert registry.stat().st_mode & 0o777 == 0o600
        assert {json.loads(line) for line in registry.read_text().splitlines()} == {
            row['attempt_id'] + '.prompt', row['attempt_id'] + '.codex.log'}
        row['number'] = 1
        row['state'] = 'done'
        cleanup = TaskWorkspaceCleanup(dispatcher.git)
        cleanup.cleanup(project, row)
        cleanup.cleanup(project, row)
        assert not task.exists()
        assert git(remote, 'rev-parse', row['branch']) == row['result_sha']
    finally:
        dispatcher.close()


def test_artifact_registration_does_not_claim_existing_files(env):
    from dispatcher.workspace import register_artifacts, safe_path
    config, project, _, seed, _ = env
    _, task, _ = GitWorkspaceManager(config).paths(project, 1)
    task.mkdir(parents=True)
    (task / 'foreign.prompt').write_text('preserve')
    with pytest.raises(TaskError, match='artifact_already_exists'):
        register_artifacts(config.workspace, task, 'foreign.prompt')
    assert not (task / '.dispatcher-artifacts.jsonl').exists()
    for path in (seed / 'initial', task / '..' / 'outside', config.workspace):
        with pytest.raises(TaskError, match='unsafe_path'):
            safe_path(config.workspace, path)
