from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from dispatcher.config import Config, Project
from dispatcher.diagnostics import DiagnosticLogger
from dispatcher.github import GitHubAdapter, GitHubError
from dispatcher.input import INPUT_MARKER, InputError, parse_input
from dispatcher.models import IssueObservation, State, PRObservation, PRStatus
from dispatcher.services import GroupService, StateService, Scheduler, PollService, ObservationBatch
from dispatcher.execution import Publisher
from dispatcher.storage import Storage


def issue(i, labels=('dispatcher:codex-ready',), **kw):
    return dict(id=i, number=i, title=f'Task {i}', body=f'Body {i}', state='open',
                labels=[{'name': v} for v in labels], repository_url='https://api.github.com/repos/owner/new', **kw)


class FakeAPI:
    def __init__(self):
        self.issues = {1: issue(1)}
        self.comments = {}
        self.parents = {}
        self.children = {}
        self.prs = {}
        self.calls = []
        self.fail = None
        self.next_id = 500
        self.polls = 0
        self.on_poll = None

    def __call__(self, method, endpoint, payload):
        from urllib.parse import urlsplit, parse_qs, unquote
        self.calls.append((method, endpoint, payload))
        if self.fail and self.fail(method, endpoint, payload):
            raise GitHubError(503)
        url = urlsplit(endpoint)
        parts = url.path.split('/')[3:]
        query = parse_qs(url.query)
        if parts == ['issues']:
            self.polls += 1
            if self.on_poll:
                self.on_poll()
            return [v for v in self.issues.values() if v['state'] == 'open']
        if parts == ['pulls']:
            if method == 'GET':
                branch = query['head'][0].split(':', 1)[1]
                return [p for p in self.prs.values() if p['head']['ref'] == branch
                        and (query['state'][0] == 'all' or p['state'] == query['state'][0])]
            self.next_id += 1
            value = dict(id=self.next_id, number=self.next_id, state='open', merged_at=None,
                         head={'ref': payload['head'], 'repo': {'full_name': 'owner/new'}}, base={'ref': payload['base']})
            self.prs[value['number']] = value
            return value
        if parts[0] == 'pulls':
            return self.prs[int(parts[1])]
        if parts[:2] == ['issues', 'comments']:
            comment = next((c for values in self.comments.values() for c in values if c['id'] == int(parts[2])), None)
            if comment is None:
                raise GitHubError(404)
            comment['body'] = payload['body']
            return comment
        number = int(parts[1])
        if len(parts) == 2:
            if number not in self.issues:
                raise GitHubError(404)
            if method == 'PATCH':
                self.issues[number].update(payload)
            return self.issues[number]
        if parts[2] == 'parent':
            if number not in self.parents:
                raise GitHubError(404)
            return self.issues[self.parents[number]]
        if parts[2] == 'sub_issues':
            return [self.issues[i] for i in self.children.get(number, [])]
        if parts[2] == 'comments':
            if method == 'GET':
                return self.comments.get(number, [])
            self.next_id += 1
            comment = dict(id=self.next_id, body=payload['body'])
            self.comments.setdefault(number, []).append(comment)
            return comment
        if parts[2] == 'labels':
            if method == 'GET':
                return self.issues[number]['labels']
            if method == 'DELETE':
                self.issues[number]['labels'] = [v for v in self.issues[number]['labels'] if v['name'] != unquote(parts[3])]
            else:
                names = {v['name'] for v in self.issues[number]['labels']} | set(payload['labels'])
                self.issues[number]['labels'] = [{'name': v} for v in sorted(names)]
            return self.issues[number]['labels']
        raise AssertionError((method, endpoint))


@pytest.fixture
def app(tmp_path):
    config = Config(tmp_path / 'work', tmp_path / 'db', (Project('p', 'owner/new'),), debug_mode='verbose')
    storage = Storage(config.database)
    api = FakeAPI()
    gh = GitHubAdapter(config, DiagnosticLogger(storage, 'verbose'), api, retries=0)
    groups = GroupService(storage)
    state = StateService(storage, groups, config)
    publisher = Publisher(config, storage, gh, None, state)
    poll = PollService(config, storage, gh, state, publisher, gh.logger)
    return config, storage, api, gh, groups, state, publisher, poll


def approve(api, number=1, yaml='dispatcher: {}\n'):
    api.issues[number]['labels'] = [{'name': 'dispatcher:codex-ready'}, {'name': 'dispatcher:approve'}]
    api.comments[number] = [c for c in api.comments.get(number, []) if INPUT_MARKER not in c['body']] + [
        dict(id=number + 100, body=INPUT_MARKER + '\n```yaml\n' + yaml + '```')]


@pytest.mark.parametrize('raw', ['bad', 'dispatcher:\n  run_codex: 1\n', 'dispatcher:\n  correction_for: true\n',
    'dispatcher:\n  wrong: 1\n', 'dispatcher:\n  branch: ../bad\n', 'dispatcher: {}\nother: {}\n',
    'dispatcher:\n  correction_for: 1\n  base_branch: main\n', 'dispatcher:\n  run_codex: true\n  run_codex: false\n'])
def test_input_invalid(raw):
    with pytest.raises(InputError):
        parse_input(INPUT_MARKER + '\n```yaml\n' + raw + '```')


def test_registration_never_reads_input_then_approve_snapshots(app):
    config, store, api, gh, groups, state, pub, poll = app
    api.comments[1] = [dict(id=101, body='malformed INPUT')]
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'discovered'
    assert not any('/comments' in endpoint for method, endpoint, _ in api.calls if method == 'GET')
    approve(api)
    api.issues[1]['body'] = 'Fresh approved body'
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'pending'
    assert store.issue(1)['approved_body'] == 'Fresh approved body'
    snapshot = store.issue(1)['approved_input']
    api.comments[1][0]['body'] = 'Edited after approval'
    api.issues[1]['body'] = 'Edited issue'
    before = len([c for c in api.calls if '/comments?' in c[1]])
    poll.poll_project(config.projects[0])
    assert store.issue(1)['approved_input'] == snapshot
    assert len([c for c in api.calls if '/comments?' in c[1]]) == before
    assert len(store.rows('SELECT * FROM state_history')) == 2
    assert all(r['poll_id'] for r in store.rows("SELECT * FROM debug_operations WHERE level='api'"))


def test_approve_invalid_and_missing_source_leave_discovered(app):
    config, store, api, *_, poll = app
    approve(api, yaml='dispatcher:\n  run_codex: nope\n')
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'discovered'
    assert store.issue(1)['approved_at'] is None
    approve(api, yaml='dispatcher:\n  correction_for: 999\n')
    poll.poll_project(config.projects[0])
    assert store.issue(1)['execution_group_id'] == 1
    assert store.issue(1)['approved_input'] is None


def test_correction_binds_only_at_approve_and_group_atomic(app):
    config, store, api, gh, groups, state, pub, poll = app
    approve(api)
    poll.poll_project(config.projects[0])
    state.transition(1, State.PENDING, State.PREPARING, 'test')
    state.transition(1, State.PREPARING, State.ERROR, 'test')
    for n in (2, 3):
        api.issues[n] = issue(n)
    poll.poll_project(config.projects[0])
    assert store.issue(2)['execution_group_id'] == 2
    approve(api, 2, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    assert store.issue(2)['state'] == 'discovered'
    assert store.issue(2)['execution_group_id'] == 2
    state.transition(1, State.ERROR, State.NEED_FIX, 'permission')
    approve(api, 2, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    assert store.issue(2)['execution_group_id'] == 1
    assert store.issue(2)['correction_of_issue_id'] == 1
    approve(api, 3, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    assert store.issue(3)['state'] == 'discovered'
    assert store.issue(3)['execution_group_id'] == 3
    assert store.issue(3)['approved_input'] is None
    assert store.group_branches(2) == ('main', 'codex/task-1')


def test_virtual_children_late_links_and_readiness(app):
    config, store, api, gh, groups, state, pub, poll = app
    api.issues[1] = issue(1, labels=())
    api.issues[2] = issue(2)
    api.parents[2] = 1
    api.children[1] = [2]
    poll.poll_project(config.projects[0])
    assert store.issue(1)['is_virtual']
    approve(api, 1)
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'awaiting-children'
    state.transition(2, State.DISCOVERED, State.CANCELLED, 'child-cancelled')
    assert store.issue(1)['state'] == 'pending'
    execution = Scheduler(store, groups, state).reserve(1)
    assert execution
    api.issues[3] = issue(3)
    api.parents[3] = 1
    api.children[1] = [2, 3]
    poll.poll_project(config.projects[0])
    assert not store.rows('SELECT * FROM issue_relations WHERE child_issue_id=3')
    assert store.issue(1)['state'] == 'preparing'
    api.issues[3]['labels'] = [{'name': 'dispatcher:discovered'}]
    poll.poll_project(config.projects[0])
    assert store.issue(3)['is_ready'] == 0
    assert not store.rows('SELECT * FROM issue_relations WHERE child_issue_id=3')
    assert store.issue(1)['state'] == 'preparing'


def test_partial_reads_preserve_whole_project(app):
    config, store, api, gh, groups, state, pub, poll = app
    api.issues[2] = issue(2)
    api.parents[2] = 1
    api.children[1] = [2]
    poll.poll_project(config.projects[0])
    before = [tuple(r) for r in store.rows('SELECT * FROM issue_relations')]
    api.issues[3] = issue(3)
    api.fail = lambda method, endpoint, _: '/3/sub_issues' in endpoint
    with pytest.raises(GitHubError):
        poll.poll_project(config.projects[0])
    assert store.issue(3) is None
    assert [tuple(r) for r in store.rows('SELECT * FROM issue_relations')] == before


def test_scheduler_slot_and_concurrent_reservations(app):
    config, store, api, gh, groups, state, pub, poll = app
    api.issues[2] = issue(2)
    approve(api, 1)
    approve(api, 2)
    poll.poll_project(config.projects[0])
    scheduler = Scheduler(store, groups, state)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(scheduler.reserve, [1, 2]))
    assert sum(r is not None for r in results) == 1
    assert len(store.rows('SELECT * FROM executions')) == 1


def test_state_forbids_transitions_and_execution_cancel(app):
    config, store, api, gh, groups, state, pub, poll = app
    approve(api)
    poll.poll_project(config.projects[0])
    with pytest.raises(ValueError):
        state.transition(1, State.PENDING, State.DONE, 'invalid')
    Scheduler(store, groups, state).reserve(1)
    with pytest.raises(ValueError):
        state.transition(1, State.PREPARING, State.CANCELLED, 'invalid')


def test_api_pagination_retry_and_no_post_retry(app, monkeypatch):
    config, store, api, gh, *_ = app
    monkeypatch.setattr('dispatcher.github.time.sleep', lambda _: None)
    calls = []
    def transport(method, endpoint, payload):
        calls.append((method, endpoint))
        if len(calls) == 1:
            raise GitHubError(503)
        return list(range(100)) if 'page=1' in endpoint.split('per_page=')[0] else []
    # Distinct controlled paging transport, including a visible retry.
    def pages(method, endpoint, payload):
        calls.append((method, endpoint))
        if len(calls) == 1:
            raise GitHubError(503)
        return list(range(100)) if endpoint.endswith('&page=1') else [100]
    gh.transport, gh.retries = pages, 1
    assert len(gh.pages('repos/owner/new/issues', poll_id='poll-test')) == 101
    assert len(calls) == 3
    assert len(store.rows("SELECT * FROM debug_operations WHERE poll_id='poll-test'")) == 3
    gh.transport = lambda *_: (_ for _ in ()).throw(GitHubError(503))
    with pytest.raises(GitHubError):
        gh.request('POST', 'repos/owner/new/pulls')


def test_labels_inheritance_and_preparing_no_running(app):
    config, store, api, gh, *_ = app
    project = replace(config.projects[0], labels={'running': 'custom-run', 'manual-running': 'custom-manual'})
    api.issues[1]['labels'] = [{'name': 'user-owned'}, {'name': 'dispatcher:pending'}]
    gh.sync_labels(project, 1, 'preparing', frozenset(['user-owned', 'dispatcher:pending']))
    assert api.issues[1]['labels'] == [{'name': 'user-owned'}]
    gh.sync_labels(project, 1, 'manual-running', frozenset(['user-owned']))
    assert {v['name'] for v in api.issues[1]['labels']} == {'user-owned', 'custom-manual'}


def test_terminal_pr_with_closed_issue_and_conflict(app):
    config, store, api, gh, groups, state, pub, poll = app
    approve(api)
    poll.poll_project(config.projects[0])
    state.transition(1, State.PENDING, State.PREPARING, 'test')
    state.transition(1, State.PREPARING, State.RUNNING, 'test')
    pr = PRObservation(700, 1, 'owner/new', 700, 'codex/task-1', 'main', PRStatus.OPEN)
    state.apply_pr(pr)
    api.prs[700] = dict(id=700, number=700, state='closed', merged_at='2026-11-07T10:00:00+00:00',
                       head={'ref': 'codex/task-1'}, base={'ref': 'different'})
    api.issues[1]['state'] = 'closed'
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'done'
    assert api.issues[1]['state_reason'] == 'completed'
    before = len(store.rows('SELECT * FROM state_history'))
    poll.poll_project(config.projects[0])
    assert len(store.rows('SELECT * FROM state_history')) == before


def test_merge_conflict_and_ready_removed_with_pr_block_parent(app):
    config, store, api, gh, groups, state, pub, poll = app
    api.issues[2] = issue(2)
    api.parents[2] = 1
    api.children[1] = [2]
    approve(api, 1)
    approve(api, 2)
    poll.poll_project(config.projects[0])
    state.transition(2, State.PENDING, State.PREPARING, 'test')
    state.transition(2, State.PREPARING, State.RUNNING, 'test')
    pr = PRObservation(700, 2, 'owner/new', 700, 'codex/task-2', 'main', PRStatus.OPEN)
    state.apply_pr(pr)
    api.prs[700] = dict(id=700, number=700, state='open', merged_at=None,
                       head={'ref': 'codex/task-2'}, base={'ref': 'main'})
    api.issues[2]['labels'] = [{'name': 'dispatcher:review'}]
    poll.poll_project(config.projects[0])
    assert store.issue(2)['is_ready'] == 0
    assert store.issue(1)['state'] == 'awaiting-children'
    assert {v['name'] for v in api.issues[2]['labels']} == {'dispatcher:review'}
    assert 'child-ready-removed-with-open-pr' in groups.evaluate(1).reasons
    state.apply_pr(replace(pr, status=PRStatus.MERGED, base_branch='different'))
    assert store.issue(2)['state'] == 'done'
    assert store.issue(1)['state'] == 'awaiting-children'
    assert 'child-merge-target-conflict' in groups.evaluate(1).reasons
    with store.transaction() as db:
        db.execute('DELETE FROM issue_relations WHERE parent_issue_id=1')
        state.refresh_db(db)
    assert store.issue(1)['state'] == 'pending'


def test_concurrent_corrections_only_one_atomic_approval(app):
    config, store, api, gh, groups, state, pub, poll = app
    approve(api)
    poll.poll_project(config.projects[0])
    state.transition(1, State.PENDING, State.PREPARING, 'test')
    state.transition(1, State.PREPARING, State.ERROR, 'test')
    state.transition(1, State.ERROR, State.NEED_FIX, 'permission')
    for n in (2, 3):
        api.issues[n] = issue(n)
    poll.poll_project(config.projects[0])
    from dispatcher.github import InputComment
    comment = InputComment(101, INPUT_MARKER + '\n```yaml\ndispatcher:\n  correction_for: 1\n```')
    def approve_one(n):
        try:
            return state.approve(config.projects[0], gh.issue_observation(api.issues[n], 'owner/new'),
                                 comment, gh.issue_observation(api.issues[1], 'owner/new'))
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve_one, (2, 3)))
    assert sum(results) == 1
    rows = [store.issue(n) for n in (2, 3)]
    assert sum(r['approved_at'] is not None and r['execution_group_id'] == 1 for r in rows) == 1
    assert sum(r['approved_at'] is None and r['execution_group_id'] in (2, 3) for r in rows) == 1


def test_duplicate_inputs_reject_approval_but_register(app):
    config, store, api, gh, groups, state, pub, poll = app
    approve(api)
    api.comments[1].append(dict(id=102, body=api.comments[1][0]['body']))
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'discovered'
    assert store.issue(1)['approved_at'] is None


def test_need_fix_not_reused_after_a_new_failed_attempt(app):
    config, store, api, gh, groups, state, pub, poll = app
    approve(api)
    poll.poll_project(config.projects[0])
    state.transition(1, State.PENDING, State.PREPARING, 'test')
    state.transition(1, State.PREPARING, State.ERROR, 'test')
    state.transition(1, State.ERROR, State.NEED_FIX, 'permission')
    api.issues[2] = issue(2)
    approve(api, 2, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    state.transition(2, State.PENDING, State.PREPARING, 'test')
    state.transition(2, State.PREPARING, State.ERROR, 'test')
    api.issues[3] = issue(3)
    approve(api, 3, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    assert store.issue(3)['state'] == 'discovered'
    assert store.issue(3)['execution_group_id'] == 3
    state.transition(2, State.ERROR, State.NEED_FIX, 'new-permission')
    approve(api, 3, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    assert store.issue(3)['execution_group_id'] == 1


def test_multiple_projects_same_issue_numbers_and_label_overrides(tmp_path):
    projects = (Project('p', 'owner/new'), Project('q', 'other/new', {'approve': 'custom-approve', 'pending': 'custom-pending'}))
    config = Config(tmp_path / 'work', tmp_path / 'db', projects)
    store = Storage(config.database)
    first, second = FakeAPI(), FakeAPI()
    first.issues = {1: issue(100)}
    first.issues[1]['number'] = 1
    second.issues = {1: issue(200, labels=('dispatcher:codex-ready', 'custom-approve'))}
    second.issues[1]['number'] = 1
    gh = GitHubAdapter(config, DiagnosticLogger(store),
        lambda method, endpoint, payload: (first if '/owner/new/' in endpoint else second)(method, endpoint, payload))
    groups = GroupService(store)
    state = StateService(store, groups, config)
    pub = Publisher(config, store, gh, None, state)
    poll = PollService(config, store, gh, state, pub, gh.logger)
    poll.poll_project(projects[0])
    poll.poll_project(projects[1])
    assert store.issue(100)['state'] == 'discovered'
    assert store.issue(200)['state'] == 'pending'
    assert store.issue(200)['repository'] == 'other/new'
    assert 'custom-pending' in {v['name'] for v in second.issues[1]['labels']}
    assert not any('/comments?' in endpoint for method, endpoint, _ in first.calls)


def test_group_terminal_finishes_each_direct_child_once(app):
    config, store, api, gh, groups, state, pub, poll = app
    api.issues[3] = issue(3)
    api.parents[1] = 3
    api.children[3] = [1]
    approve(api, 1)
    approve(api, 3)
    poll.poll_project(config.projects[0])
    assert store.issue(3)['state'] == 'awaiting-children'
    state.transition(1, State.PENDING, State.PREPARING, 'test')
    state.transition(1, State.PREPARING, State.ERROR, 'test')
    state.transition(1, State.ERROR, State.NEED_FIX, 'permission')
    api.issues[2] = issue(2)
    api.parents[2] = 3
    api.children[3] = [1, 2]
    approve(api, 2, 'dispatcher:\n  correction_for: 1\n')
    poll.poll_project(config.projects[0])
    state.transition(2, State.PENDING, State.PREPARING, 'test')
    state.transition(2, State.PREPARING, State.RUNNING, 'test')
    state.apply_pr(PRObservation(700, 2, 'owner/new', 700, 'codex/task-1', 'main', PRStatus.OPEN))
    state.apply_pr(PRObservation(700, 2, 'owner/new', 700, 'codex/task-1', 'main', PRStatus.MERGED))
    assert store.issue(1)['state'] == store.issue(2)['state'] == 'done'
    assert store.issue(3)['state'] == 'pending'
    count = len(store.rows('SELECT * FROM state_history'))
    state.apply_pr(PRObservation(700, 2, 'owner/new', 700, 'codex/task-1', 'main', PRStatus.MERGED))
    assert len(store.rows('SELECT * FROM state_history')) == count


def test_baseline_poll_api_budget_has_no_comment_reads(app):
    config, store, api, gh, groups, state, pub, poll = app
    poll.poll_project(config.projects[0])
    api.calls.clear()
    poll.poll_project(config.projects[0])
    # One issue page, one parent read, one direct-children page; no INPUT/OUTPUT.
    assert len(api.calls) == 3
    assert all(call[0] == 'GET' for call in api.calls)


def test_v1_path_guard_does_not_depend_on_package_location(tmp_path, monkeypatch):
    monkeypatch.setattr('dispatcher.config.__file__', str(tmp_path / 'installed/lib/dispatcher/config.py'))
    with pytest.raises(ValueError, match='V1'):
        Config(tmp_path / 'project/V1/workspace', tmp_path / 'db', ())
    with pytest.raises(ValueError, match='V1'):
        Config(tmp_path / 'work', tmp_path / 'db',
               (Project('p', 'owner/new', checkout=tmp_path / 'elsewhere/v1/checkout'),))


@pytest.mark.parametrize('custom_labels', [False, True])
def test_registered_child_without_ready_still_blocks_and_keeps_status(app, custom_labels):
    config, store, api, gh, groups, state, pub, poll = app
    if custom_labels:
        project = replace(config.projects[0], labels={
            'codex-ready': 'custom-ready', 'discovered': 'custom-discovered'})
        config = replace(config, projects=(project,))
        gh.config = state.config = poll.config = pub.config = config
    project = config.projects[0]
    labels = config.effective_labels(project)
    api.issues[2] = issue(2, labels=(labels['codex-ready'], 'user-label'))
    api.parents[2] = 1
    api.children[1] = [2]
    approve(api, 1)
    if custom_labels:
        api.issues[1]['labels'][0]['name'] = labels['codex-ready']
    poll.poll_project(project)
    assert store.issue(1)['state'] == 'awaiting-children'
    api.issues[2]['labels'] = [v for v in api.issues[2]['labels'] if v['name'] != labels['codex-ready']]
    history = [tuple(r) for r in store.rows('SELECT * FROM state_history ORDER BY id')]
    api.calls.clear()
    for _ in range(2):
        poll.poll_project(project)
        assert store.issue(1)['state'] == 'awaiting-children'
        assert store.issue(2)['state'] == 'discovered'
        assert store.issue(2)['is_virtual'] == 0 and store.issue(2)['is_ready'] == 0
        assert groups.evaluate(1).reasons == ('unfinished-child',)
        assert {v['name'] for v in api.issues[2]['labels']} == {labels['discovered'], 'user-label'}
    assert [tuple(r) for r in store.rows('SELECT * FROM state_history ORDER BY id')] == history
    assert store.rows('SELECT * FROM issue_relations WHERE parent_issue_id=1 AND child_issue_id=2')
    assert not any('/labels' in endpoint for _, endpoint, _ in api.calls)
    assert Scheduler(store, groups, state).reserve(1) is None


def test_missing_status_is_restored_without_restoring_registration_label(app):
    config, store, api, gh, groups, state, pub, poll = app
    project = replace(config.projects[0], labels={'discovered': 'custom-discovered'})
    config = replace(config, projects=(project,))
    gh.config = state.config = poll.config = pub.config = config
    poll.poll_project(project)
    api.issues[1]['labels'] = [{'name': 'user-label'}]
    api.calls.clear()
    poll.poll_project(project)
    assert store.issue(1)['state'] == 'discovered' and store.issue(1)['is_ready'] == 0
    assert {v['name'] for v in api.issues[1]['labels']} == {'user-label', 'custom-discovered'}
    writes = [call for call in api.calls if '/labels' in call[1]]
    assert len(writes) == 1
    assert writes[0] == ('POST', 'repos/owner/new/issues/1/labels', {'labels': ['custom-discovered']})
    api.calls.clear()
    poll.poll_project(project)
    assert not any('/labels' in endpoint for _, endpoint, _ in api.calls)
    assert len(store.rows('SELECT * FROM state_history')) == 1


@pytest.mark.parametrize('terminal', [State.DONE, State.CANCELLED])
def test_child_without_ready_can_approve_and_complete_dependency(app, terminal):
    config, store, api, gh, groups, state, pub, poll = app
    project = config.projects[0]
    api.issues[2] = issue(2)
    api.parents[2] = 1
    api.children[1] = [2]
    approve(api, 1)
    poll.poll_project(project)
    api.issues[2]['labels'] = [{'name': 'dispatcher:discovered'}]
    poll.poll_project(project)
    assert store.issue(1)['state'] == 'awaiting-children'
    # Approve an already registered child without re-adding codex-ready.
    api.issues[2]['labels'].append({'name': 'dispatcher:approve'})
    poll.poll_project(project)
    assert store.issue(2)['state'] == 'pending' and store.issue(2)['approved_at']
    assert store.issue(2)['is_ready'] == 0
    if terminal == State.CANCELLED:
        assert state.transition(2, State.PENDING, State.CANCELLED, 'user-command')
    else:
        state.transition(2, State.PENDING, State.PREPARING, 'test')
        state.transition(2, State.PREPARING, State.RUNNING, 'test')
        pr = PRObservation(700, 2, 'owner/new', 700, 'codex/task-2', 'main', PRStatus.OPEN)
        state.apply_pr(pr)
        state.apply_pr(replace(pr, status=PRStatus.MERGED))
    assert store.issue(2)['state'] == terminal and store.issue(2)['is_ready'] == 0
    assert store.issue(1)['state'] == 'pending'
    assert groups.evaluate(1).eligible
    poll.poll_project(project)
    assert 'dispatcher:codex-ready' not in {v['name'] for v in api.issues[2]['labels']}
    assert store.issue(1)['state'] == 'pending'


def test_removed_ready_child_relation_can_be_removed_before_preparing(app):
    config, store, api, gh, groups, state, pub, poll = app
    project = config.projects[0]
    api.issues[2] = issue(2)
    api.parents[2] = 1
    api.children[1] = [2]
    approve(api, 1)
    poll.poll_project(project)
    api.issues[2]['labels'] = [{'name': 'dispatcher:discovered'}]
    poll.poll_project(project)
    assert store.issue(1)['state'] == 'awaiting-children'
    del api.parents[2]
    api.children[1] = []
    poll.poll_project(project)
    assert store.issue(1)['state'] == 'pending'
    assert not store.rows('SELECT * FROM issue_relations WHERE parent_issue_id=1')
    assert store.issue(2)['state'] == 'discovered' and store.issue(2)['is_ready'] == 0
    history = [tuple(r) for r in store.rows('SELECT * FROM state_history ORDER BY id')]
    poll.poll_project(project)
    assert [tuple(r) for r in store.rows('SELECT * FROM state_history ORDER BY id')] == history


def test_virtual_child_relation_does_not_block_real_parent(app):
    config, store, api, gh, groups, state, pub, poll = app
    store.register_issue(IssueObservation(1, 'owner/new', 1, 'Real parent'), 'p')
    store.register_child(IssueObservation(2, 'owner/new', 2, 'Real child'),
                         IssueObservation(3, 'owner/new', 3, 'Virtual container'), 'p')
    with store.transaction() as db:
        db.execute('INSERT INTO issue_relations VALUES (1,3)')
    assert groups.evaluate(1).eligible


def test_null_child_state_is_not_terminal_even_without_ready(app):
    config, store, api, gh, groups, state, pub, poll = app
    store.register_child(IssueObservation(2, 'owner/new', 2, 'Child'),
                         IssueObservation(1, 'owner/new', 1, 'Parent'), 'p')
    store.register_issue(IssueObservation(1, 'owner/new', 1, 'Parent'), 'p')
    # The normative CHECK disallows this for real Issues. Exercise the defensive
    # readiness rule directly against an anomalous row without treating NULL as done.
    with store.connection() as db:
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('UPDATE issues SET state=NULL,is_ready=0 WHERE issue_id=2')
    assert groups.evaluate(1).reasons == ('unfinished-child',)
