"""Draft and diagnostic contracts for the D1–D3 correction."""
import csv
import io
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dispatcher.github import Adapter
from dispatcher.rendering import render_queue
from dispatcher.storage import Store
from dispatcher.timing import Trace, attach, export
from test_stage04 import env, issue
from test_stage6433 import setup, raw
from test_stage6434 import monitor_setup, status, codes


@pytest.mark.parametrize('kind', ['issue', 'container', 'child'])
def test_draft_versions_and_approval_fence(env, kind):
    config, project, *_ = env
    store = Store(config.workspace / '.runtime' / 'draft.sqlite3')
    item = issue(project, body='Draft\n' + ('```yaml\ndispatcher: {issue_type: container}\n```' if kind == 'container' else ''))
    store.apply(project, [item])
    for n in range(3):
        item.update(title=f'Черновик {n}', body=item['body'] + '\nУточнение')
        store.apply(project, [item])
        row = store.get(project.repository, 1)
        assert row['state'] == 'discovered' and row['approved_version'] is None
        assert json.loads(row['snapshot'])['body'] == item['body']
    version = row['version']
    assert store.confirm(project.repository, 1, version)
    item['body'] += '\nLate edit'
    store.apply(project, [item])
    row = store.get(project.repository, 1)
    assert row['state'] == 'discovered' and row['approved_version'] is None
    assert not store.confirm(project.repository, 1, version)
    assert not store.db.execute('SELECT 1 FROM attempts').fetchone()
    store.close()


def test_draft_waits_through_edits_then_running_label_churn(env):
    project = env[1]
    store, api, monitor, row, pr, head, _ = monitor_setup(env, state='discovered', with_pr=False)
    with store.db as db:
        db.execute('UPDATE tasks SET work_branch=NULL,base_branch=NULL,approved_version=NULL WHERE issue_id=2')
    child = deepcopy(api.list_direct_children.return_value[0])
    api.list_direct_children.return_value = [child]
    for n in range(3):
        child['title'] = f'Draft {n}'
        child['body'] += '\nMore details'
        monitor.poll(project)
        assert status(store)['waiting_count'] == 1
        assert not codes(store)
    # Sync and approve the final draft before the independent child launch.
    item = issue(project, id=2, number=2, title=child['title'], body=child['body'], server_observed_at='2026-10-25T00:00:00Z')
    store.apply(project, [item], partial=True)
    assert json.loads(store.get(project.repository, 2)['snapshot'])['body'] == item['body']
    assert store.confirm(project.repository, 2, store.get(project.repository, 2)['version'])
    child['labels'].append(dict(name=project.label('approve').name))
    for state in ('pending', 'running', 'review'):
        with store.db as db:
            db.execute('UPDATE tasks SET state=? WHERE issue_id=2', (state,))
        child['updated_at'] = '2026-10-25T12:00:00Z'
        monitor.poll(project)
        assert not codes(store)
        assert status(store)['waiting_count'] == 1
    child['body'] += '\nUnapproved edit'
    monitor.poll(project)
    assert 'child_snapshot_stale' in codes(store)
    store.close()


def test_queue_has_no_tail():
    row = dict(number=1, repository='o/r', state='discovered', issue_type='container', run_codex=False,
               snapshot=json.dumps(dict(title='中文 🚀', body='Привет')))
    out = io.StringIO()
    render_queue([row], {'o/r': 'local'}, stream=out)
    assert out.getvalue().splitlines()[-1].endswith('┘')
    assert 'issue_type=' not in out.getvalue()
    out = io.StringIO()
    render_queue([row], {}, 'json', out)
    assert json.loads(out.getvalue())[0]['issue_type'] == 'container'


@pytest.mark.parametrize('enabled', [False, True])
def test_api_trace_readback_and_secret_exclusion(tmp_path, enabled):
    path = tmp_path / 'tasks.sqlite3'
    store = Store(path)
    calls = []
    secret = 'github_pat_NEVER_LOG_THIS'
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='HTTP/2 200 OK\n\n' + json.dumps(dict(body=secret)))
    api = Adapter(run=run)
    api.timing = Trace(path, enabled)
    api.timing_projects = {'o/r': 'one'}
    api.response_date = '2026-10-24T00:00:00Z'
    api.write('repos/o/r/issues/7', 'PATCH', dict(body=secret))
    assert api.response_date == '2026-10-24T00:00:00Z'
    rows = json.loads(export(path, 'one', format='json'))
    assert len(calls) == (2 if enabled else 1)
    assert secret not in export(path, 'one', format='json')
    if enabled:
        assert [r['event'] for r in rows] == ['github_request_start', 'github_response_success', 'github_request_start', 'github_response_success', 'github_readback_confirmed']
        assert all(r['request_id'] for r in rows if r['event'].startswith('github_response'))
        assert all(r['duration_ms'] >= 0 for r in rows if r['duration_ms'] is not None)
        assert 'github_web_visible' not in str(rows)
    else:
        assert rows == []
    store.close()


def test_trace_exports_filters_clear_and_failure(tmp_path):
    path = tmp_path / 'tasks.sqlite3'
    store = Store(path)
    trace = Trace(path, True)
    trace.emit('poll_empty', project_id='a', issue_number=1, duration_ms=1.25, cycle_id='cycle')
    trace.emit('poll_empty', project_id='b', issue_number=1)
    assert len(json.loads(export(path, 'a', 1, format='json'))) == 1
    assert len(list(csv.DictReader(io.StringIO(export(path, 'a', format='csv'))))) == 1
    assert 'UTC' in export(path, 'a')
    export(path, 'a', clear=True)
    assert json.loads(export(path, 'a', format='json')) == []
    assert len(json.loads(export(path, 'b', format='json'))) == 1
    Trace(tmp_path / 'missing' / 'database', True).emit('poll')
    store.close()


def test_empty_poll_enabled_and_disabled(env):
    from dispatcher.github.sync import SyncService
    from test_stage04 import adapter
    config, project, *_ = env
    store = Store(config.workspace / '.runtime' / 'timing.sqlite3')
    api = adapter([])
    SyncService(config, store, api).poll()
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    assert json.loads(export(path, project.id, format='json')) == []
    SyncService(replace(config, timing_enabled=True), store, api).poll()
    events = json.loads(export(path, project.id, format='json'))
    assert any(r['event'] == 'poll_empty' and r['cycle_id'] for r in events)
    assert any(r['event'] == 'poll' and r['duration_ms'] is not None for r in events)
    assert len({r['cycle_id'] for r in events if r['cycle_id']}) == 1
    store.close()


def test_retry_response_and_storage_failure(tmp_path):
    path = tmp_path / 'events.sqlite3'
    store = Store(path)
    replies = iter([SimpleNamespace(returncode=1, stdout='HTTP/2 429 Limited\n\n{}'),
                    SimpleNamespace(returncode=0, stdout='HTTP/2 200 OK\n\n{}')])
    api = Adapter(run=lambda *a, **kw: next(replies), sleep=lambda _: None)
    api.timing = Trace(path, True)
    api.timing_projects = {'o/r': 'a'}
    api.timing.projects = {'o/r': 'a'}
    assert api.request('repos/o/r/issues/1?token=PRIVATE') == {}
    events = json.loads(export(path, 'a', format='json'))
    assert any(e['event'] == 'github_response_failure' and e['http_status'] == 429 for e in events)
    assert any(e['event'] == 'github_retry' for e in events)
    assert 'PRIVATE' not in str(events)
    api.timing = Trace(tmp_path / 'unavailable' / 'db', True)
    api.run = lambda *a, **kw: SimpleNamespace(returncode=0, stdout='HTTP/2 200 OK\n\n{}')
    assert api.request('repos/o/r/issues/1') == {}
    store.close()


def test_preparation_refreshes_draft_without_losing_markdown(env):
    from test_stage6433 import controlled_setup
    store, api, service, remote, patches = controlled_setup(env)
    remote[2]['title'] = 'Новая редакция'
    remote[2]['body'] = '# New user Markdown\n\nТекст 🚀\n'
    remote[2]['updated_at'] = '2026-10-24T00:30:00Z'
    service.prepare(env[1], store.get(env[1].repository, 1))
    child = store.get(env[1].repository, 2)
    assert child['state'] == 'discovered' and child['approved_version'] is None
    assert remote[2]['body'].startswith('# New user Markdown\n\nТекст 🚀\n')
    assert patches == [2]
    assert store.get(env[1].repository, 1)['state'] == 'awaiting_children'
    store.close()


def test_two_merged_children_wait_for_editable_third_then_merge(env):
    from urllib.parse import parse_qs, urlsplit
    project = env[1]
    store, api, monitor, row, first_pr, _, _ = monitor_setup(env)
    third = issue(project, id=3, number=3, body='```yaml\ndispatcher: {base_branch: feature/container}\n```')
    draft = issue(project, id=4, number=4, body='Unfinished requirements')
    store.apply(project, [third, draft], partial=True)
    remotes = {2: api.list_direct_children.return_value[0], 3: raw(project, third), 4: raw(project, draft)}
    prs = {20: first_pr}
    def merged(number):
        pr = deepcopy(first_pr)
        pr.update(id=number*10, number=number*10)
        pr['head']['ref'] = f'feature/child{number}'
        prs[number*10] = pr
        with store.db as db:
            db.execute("UPDATE tasks SET state='done',work_branch=?,base_branch='feature/container' WHERE issue_id=?", (pr['head']['ref'], number))
            db.execute('INSERT INTO task_pull_requests VALUES(?,?,?,?,?,?,?,?,?)',
                (pr['id'], number, row['repository_id'], pr['number'], pr['head']['ref'], 'feature/container', 'abc', 'closed', '2026-10-23T00:00:00Z'))
    merged(3)
    original_request = api.request.side_effect
    api.request.side_effect = lambda endpoint: deepcopy(prs[int(endpoint.rsplit('/',1)[1])]) if '/pulls/' in endpoint else original_request(endpoint)
    def pages(endpoint):
        if '/pulls?' in endpoint:
            branch = parse_qs(urlsplit(endpoint).query)['head'][0].split(':',1)[1]
            return [deepcopy(pr) for pr in prs.values() if pr['head']['ref'] == branch]
        return []
    api.pages.side_effect = pages
    api.list_direct_children.side_effect = lambda *_: deepcopy(list(remotes.values()))
    for n in range(3):
        draft['body'] += f'\nRefinement {n}'
        draft['server_observed_at'] = '2026-10-25T00:00:00Z'
        store.apply(project, [draft], partial=True)
        remotes[4] = raw(project, draft)
        monitor.poll(project)
        assert status(store)['completed_count'] == 2
        assert status(store)['waiting_count'] == 1 and not codes(store)
        assert not status(store)['ready_for_integration']
    assert store.confirm(project.repository, 4, store.get(project.repository, 4)['version'])
    merged(4)
    monitor.poll(project)
    assert status(store)['ready_for_integration']
    assert status(store)['completed_count'] == 3 and not codes(store)
    store.close()


def test_labels_readback_and_outbox_correlator(env):
    config, project, *_ = env
    path = config.workspace / '.runtime' / 'outbox-timing.sqlite3'
    store = Store(path)
    api = Adapter(run=lambda *a, **kw: SimpleNamespace(returncode=0, stdout='HTTP/2 200 OK\n\n[{"name":"dispatcher:pending"}]'))
    attach(store, replace(config, timing_enabled=True), api)
    store.apply(project, [issue(project)])
    assert store.confirm(project.repository, 1, 1)
    row = store.get(project.repository, 1)
    key = 'pr:durable-operation'
    store.publication(key, row['current_iteration_id'])
    store.publication(key, row['current_iteration_id'], outcome='failed', retry=True)
    api.write(f'repos/{project.repository}/issues/1/labels', 'POST', {'labels':['dispatcher:pending']})
    events = json.loads(export(path, project.id, 1, limit=1000, format='json'))
    assert any(e['event'] == 'state_changed' and json.loads(e['details'])['state'] == 'pending' for e in events)
    assert any(e['event'] == 'outbox_enqueue' and e['operation_id'] == key and json.loads(e['details'])['retry'] for e in events)
    assert any(e['event'] == 'github_readback_confirmed' for e in events)
    store.close()


def test_timing_cli_and_strict_opt_in(env, capsys):
    import yaml
    from dispatcher.cli import main
    from dispatcher.config import load, ConfigError
    config, project, *_ = env
    assert not load(config.source).timing_enabled
    data = yaml.safe_load(config.source.read_text())
    data['diagnostics'] = {'timing_enabled': True}
    config.source.write_text(yaml.safe_dump(data))
    assert load(config.source).timing_enabled
    path = config.workspace / '.runtime' / 'tasks.sqlite3'
    store = Store(path)
    Trace(path, True).emit('poll_empty', project_id=project.id)
    store.close()
    assert main(['--config', str(config.source), 'timing', '--project', project.id, '--format', 'json']) == 0
    assert json.loads(capsys.readouterr().out)[0]['event'] == 'poll_empty'
    assert main(['--config', str(config.source), 'timing', '--project', project.id, '--clear']) == 0
    capsys.readouterr()
    assert main(['--config', str(config.source), 'timing', '--project', project.id, '--format', 'json']) == 0
    assert json.loads(capsys.readouterr().out) == []
    data['diagnostics']['timing_enabled'] = 'true'
    config.source.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load(config.source)
