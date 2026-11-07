from pathlib import Path
from urllib.parse import quote

import pytest

from dispatcher.output import public_result
from test_execution_cleanup import environment, start, gitcmd


def test_public_output_committed_paths_and_preserved_stdout(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    executor.prepare(eid)
    execution = store.execution(eid)
    root = Path(execution['worktree_path'])
    names = ['readme.md', 'папка/файл #?% (v2).md', 'deleted.md']
    for name in names:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('committed')
    executor.run_once(eid)
    pub.publish(eid)
    sha = store.execution(eid)['result_commit']
    (root / 'deleted.md').unlink()
    (root / 'untracked.md').write_text('local only')
    (root / 'escape.md').symlink_to(config.projects[0].checkout / 'base.txt')
    raw = '\n'.join([
        *(f'[{name}](<{root / name}>)' for name in names),
        f'[untracked]({root}/untracked.md)',
        f'[escape]({root}/escape.md)',
        f'[foreign]({root.parent}/other/readme.md)',
        f'[missing]({root}/missing.md)',
        f'[line]({root}/readme.md:12:3)',
        '[web](https://example.org/home/user/readme.md)',
        '[relative](docs/readme.md)',
        'Ordinary text',
        'See `/home/private/project/local.txt`.',
    ])
    result_file = config.workspace / 'results' / f'{eid}.txt'
    result_file.write_text(raw)
    pub.phase_output(1)
    body = store.issue(1)['output_body']
    assert f'Result commit: [{sha}](https://github.com/owner/new/commit/{sha})' in body
    assert 'https://github.com/owner/new/pull/' in body
    for name in names:
        assert f'/blob/{sha}/{quote(name, safe="/")}' in body
    assert str(root) not in body and '/home/private' not in body
    assert '\nuntracked\nescape\nforeign\nmissing\n' in body
    assert '[web](https://example.org/home/user/readme.md)' in body
    assert '[relative](docs/readme.md)' in body
    assert 'Ordinary text' in body and '`local.txt`' in body
    assert result_file.read_text() == raw
    before = len(api.calls)
    pub.phase_output(1)
    assert len(api.calls) == before
    pr = next(iter(api.prs.values()))
    pr['merged_at'] = '2026-11-07T10:00:00+00:00'
    pr['state'] = 'closed'
    pr['head']['sha'] = 'f' * 40
    poll.poll_project(config.projects[0])
    assert store.issue(1)['state'] == 'done'
    assert store.execution(eid)['result_commit'] == sha
    body = store.issue(1)['output_body']
    assert f'/commit/{sha}' in body and 'State: `done`' in body
    before = len(api.calls)
    pub.phase_output(1)
    assert len(api.calls) == before
    assert runner.calls == 1 and len(api.prs) == 1
    # Local checkout remains sufficient after worktree removal.
    gitcmd(config.projects[0].checkout, 'worktree', 'remove', '--force', str(root))
    pub.phase_output(1)
    assert f'/blob/{sha}/readme.md' in store.issue(1)['output_body']


@pytest.mark.parametrize('sha,pushed', [(None, 1), ('abc', 1), ('x' * 40, 1), ('a' * 40, 0)])
def test_unconfirmed_commit_and_paths(environment, sha, pushed):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    executor.prepare(eid)
    root = store.execution(eid)['worktree_path']
    store.update_execution(eid, result_commit=sha, push_completed=pushed)
    result = config.workspace / 'results' / f'{eid}.txt'
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(f'[base]({root}/base.txt)')
    pub.phase_output(1)
    body = store.issue(1)['output_body']
    assert 'Result commit:' not in body and '/blob/' not in body
    assert body.endswith('base') and root not in body


def test_manual_output(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment, 'dispatcher:\n  run_codex: false\n')
    executor.execute(eid)
    sha = store.execution(eid)['result_commit']
    assert f'/commit/{sha}' in store.issue(1)['output_body']
    assert 'Group branch: `manual/task-0001`' in store.issue(1)['output_body']
    assert runner.calls == 0


def test_render_relative_external_and_traversal(tmp_path):
    execution = dict(result_commit='a' * 40, push_completed=1, worktree_path=str(tmp_path))
    text = '[external](https://example.com/a?q=1#x) [relative](a.md) plain text'
    assert public_result(text, execution, 'o/r', lambda *_: True) == text
    assert public_result(f'[escape]({tmp_path}/../escape)', execution, 'o/r', lambda *_: True) == 'escape'


def test_latest_successful_publication_and_failed_retry(environment, monkeypatch):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment)
    executor.execute(eid)
    old_sha = store.execution(eid)['result_commit']
    root = Path(store.execution(eid)['worktree_path'])
    (root / 'result.txt').write_text('updated result')
    pub.publish(eid)
    new_sha = store.execution(eid)['result_commit']
    assert new_sha != old_sha
    assert f'/commit/{new_sha}' in store.issue(1)['output_body']
    assert f'/commit/{old_sha}' not in store.issue(1)['output_body']
    (root / 'unpublished.md').write_text('not pushed')
    def failed(*args):
        raise RuntimeError('push failed')
    monkeypatch.setattr(git, 'commit_push', failed)
    with pytest.raises(RuntimeError, match='push failed'):
        pub.publish(eid)
    pub.phase_output(1)
    assert store.execution(eid)['result_commit'] == new_sha
    assert f'/commit/{new_sha}' in store.issue(1)['output_body']
    assert runner.calls == 1 and len(api.prs) == 1


@pytest.mark.parametrize('number,branch,failed', [
    (13, None, False), (12, None, True),
    (13, 'feature/task-custom.v2', False),
    (12, 'feature/task-custom.v2', True),
])
def test_branch_output_review_done_and_error(environment, number, branch, failed):
    from dispatcher.execution import Executor
    from dispatcher.services import Scheduler
    from test_execution_cleanup import FakeRunner
    from test_services import approve, issue

    config, store, api, gh, git, groups, state, pub, poll = environment
    api.issues = {number: {**issue(1), 'number': number}}
    yaml = 'dispatcher: {}\n' if branch is None else f'dispatcher:\n  branch: {branch}\n'
    approve(api, number=number, yaml=yaml)
    poll.poll_project(config.projects[0])
    eid = Scheduler(store, groups, state).reserve(1).execution_id
    runner = FakeRunner(store, code=1 if failed else 0)
    Executor(config, store, git, state, pub, runner).execute(eid)
    branch = branch or f'codex/task-{number:04d}'
    expected_state = 'error' if failed else 'review'
    body = store.issue(1)['output_body']
    assert f'State: `{expected_state}`. Group branch: `{branch}`.' in body
    assert '[withheld]' not in body
    assert store.execution(eid)['push_completed']
    if not failed:
        assert len(api.prs) == 1 and 'Result commit:' in body
        pr = next(iter(api.prs.values()))
        pr.update(merged_at='2026-11-08T10:00:00+00:00', state='closed')
        poll.poll_project(config.projects[0])
        body = store.issue(1)['output_body']
        assert f'State: `done`. Group branch: `{branch}`.' in body
        assert 'Result commit:' in body
    else:
        assert not api.prs and 'Agent final result' not in body
    before = len(api.calls)
    pub.phase_output(1)
    pub.phase_output(1)
    assert len(api.calls) == before and runner.calls == 1


def test_manual_branch_output_review_and_done(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    executor, eid, runner = start(environment, 'dispatcher:\n  run_codex: false\n')
    executor.execute(eid)
    branch = 'manual/task-0001'
    api.prs[600] = dict(id=600, number=600, state='open', merged_at=None,
                       head={'ref': branch, 'repo': {'full_name': 'owner/new'}},
                       base={'ref': 'main'})
    poll.poll_project(config.projects[0])
    assert f'State: `review`. Group branch: `{branch}`.' in store.issue(1)['output_body']
    api.prs[600].update(state='closed', merged_at='2026-11-08T10:00:00+00:00')
    poll.poll_project(config.projects[0])
    assert f'State: `done`. Group branch: `{branch}`.' in store.issue(1)['output_body']
    before = len(api.calls)
    pub.phase_output(1)
    assert len(api.calls) == before and runner.calls == 0


def test_secret_shaped_branch_remains_withheld_in_output(environment):
    config, store, api, gh, git, groups, state, pub, poll = environment
    branch = 'feature/sk-proj-' + 'aB3_' * 20
    executor, eid, runner = start(environment, f'dispatcher:\n  branch: {branch}\n')
    executor.execute(eid)
    assert store.issue(1)['state'] == 'review'
    assert 'Group branch: `[withheld]`' in store.issue(1)['output_body']
    assert branch not in store.issue(1)['output_body']
