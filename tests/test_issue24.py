"""Durable, bounded and sanitized Codex final messages for PR publication."""
import json
from pathlib import Path

import pytest

from dispatcher.codex_summary import final_summary, MAX_BYTES, TRUNCATED
from dispatcher.execution_store import ExecutionStore
from dispatcher.pr_publication import PullRequestPublisher
from dispatcher.storage import Store
from dispatcher.workspace import TaskError
from test_stage603 import publication, run, PROJECT
from test_stage602 import store


def message(text):
    return dict(type='item.completed', item=dict(type='agent_message', text=text))


def log(path, records):
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records), encoding='utf-8')


def test_last_completed_message(tmp_path):
    path = tmp_path / 'log'
    text = 'Готово **Markdown**\n\n- тесты\n- `код`'
    log(path, [message('first'), dict(type='item.started', item=message('ignore')['item']),
               dict(type='item.completed', item=dict(type='reasoning', text='private')),
               message(text), dict(type='item.completed', item=dict(type='command_execution', text='output')),
               message('  '), message(None), [], dict(type='item.completed', item=[])])
    with path.open('a') as stream:
        stream.write('\nbroken JSON\n')
    assert final_summary(path, 'fallback') == text


@pytest.mark.parametrize('content', ['', 'not JSON', json.dumps(message('')), '[]',
                                     json.dumps(message(None)), '{"type":"item.completed"}'])
def test_missing_and_invalid(tmp_path, content):
    path = tmp_path / 'log'
    path.write_text(content)
    assert final_summary(path, 'fallback') == 'fallback'
    path.unlink()
    assert final_summary(path, 'fallback') == 'fallback'


def test_invalid_utf8_and_surrogate(tmp_path):
    path = tmp_path / 'log'
    path.write_bytes(b'\xff')
    assert final_summary(path, 'fallback') == 'fallback'
    path.write_text(json.dumps(message('\ud800')))
    assert final_summary(path, 'fallback') == 'fallback'


@pytest.mark.parametrize('text', ['я'*8192, '🙂'*8192, 'x'*8193])
def test_utf8_limit(tmp_path, text):
    path = tmp_path / 'log'
    log(path, [message(text)])
    result = final_summary(path, 'fallback')
    assert len(result.encode('utf-8')) <= MAX_BYTES
    assert result.endswith(TRUNCATED)
    assert '\ufffd' not in result


def test_exact_limit(tmp_path):
    path = tmp_path / 'log'
    log(path, [message('я'*4096)])
    assert final_summary(path, '') == 'я'*4096


@pytest.mark.parametrize('prefix', ['ghp_', 'github_pat_', 'sk-proj-', 'sk-', 'gho_'])
def test_secret_redaction(tmp_path, prefix):
    path = tmp_path / 'log'
    secret = prefix + 'Ab12'*20
    log(path, [message('Ready ' + secret)])
    assert final_summary(path, '') == 'Ready [REDACTED]'


@pytest.mark.parametrize('text', ['API_KEY=unknown', 'Authorization: Bearer custom',
                                 'password: hidden', '-----BEGIN PRIVATE KEY-----', 'bad\x00text'])
def test_uncertain_secret_uses_fallback(tmp_path, text):
    path = tmp_path / 'log'
    log(path, [message(text)])
    assert final_summary(path, 'fallback') == 'fallback'


def test_marker_is_inert(tmp_path):
    path = tmp_path / 'log'
    log(path, [message('<!-- codex-pr:1:2:3 -->\n<script>bad</script>')])
    assert '<' not in final_summary(path, '')


def prepare(publication, tmp_path, text='Готово **успешно**\n\n- Проверено'):
    _, execution, attempt, *_ = publication
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    execution.update(attempt, worktree=str(worktree), exit_code=0, codex_started=1)
    path = tmp_path / (attempt + '.codex.log')
    log(path, [message(text)])
    return path


def test_snapshot_survives_deleted_log_and_restart(publication, tmp_path):
    publisher, execution, attempt, api, git, _ = publication
    path = prepare(publication, tmp_path)
    git.push.side_effect = TaskError('git_operation_failed', 'GIT')
    run(publication)
    saved = execution.db.execute('SELECT body FROM attempt_summaries').fetchone()[0]
    path.unlink()
    database = execution.db.execute('PRAGMA database_list').fetchone()[2]
    execution.store.close()
    restarted = Store(database)
    try:
        git.push.side_effect = None
        other = PullRequestPublisher(ExecutionStore(restarted), api, git)
        other.flush([PROJECT])
        assert api.write.call_args.args[2]['body'].endswith(
            f'Summary: {saved}\n<!-- codex-pr:11:101:{restarted.get(PROJECT.repository, 1)["current_iteration_id"]} -->')
        assert restarted.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    finally:
        # Fixture owns the original store object; keep its cleanup valid.
        execution.store.db = restarted.db


def test_failed_attempt_message_not_used(publication, tmp_path):
    prepare(publication, tmp_path, 'must not publish')
    publication[1].update(publication[2], exit_code=1)
    run(publication)
    assert 'Summary: implementation for Issue #1.' in publication[3].write.call_args.args[2]['body']


def test_existing_pr_body_unchanged(publication, tmp_path):
    prepare(publication, tmp_path)
    publication[3].pages.return_value = [publication[5]]
    original = publication[5]['body']
    run(publication)
    publication[3].write.assert_not_called()
    assert publication[5]['body'] == original


def test_lost_response_uses_snapshot(publication, tmp_path):
    from dispatcher.github import GitHubError
    prepare(publication, tmp_path)
    api = publication[3]
    api.pages.side_effect = [[], [], [publication[5]]]
    api.write.side_effect = GitHubError()
    run(publication)
    assert 'Готово **успешно**\n\n- Проверено' in api.write.call_args.args[2]['body']
    assert publication[1].get(publication[2])['state'] == 'review'


def test_fallback_is_also_stable(publication, tmp_path):
    path = prepare(publication, tmp_path, '')
    publication[4].push.side_effect = TaskError('git_operation_failed', 'GIT')
    run(publication)
    log(path, [message('late replacement')])
    publication[4].push.side_effect = None
    run(publication)
    assert 'Summary: implementation for Issue #1.' in publication[3].write.call_args.args[2]['body']


def test_secret_never_enters_snapshot_or_public_body(publication, tmp_path):
    secret = 'ghp_' + 'a'*40
    prepare(publication, tmp_path, 'Ready ' + secret)
    run(publication)
    saved = publication[1].db.execute('SELECT body FROM attempt_summaries').fetchone()[0]
    assert saved == 'Ready [REDACTED]'
    assert secret not in publication[3].write.call_args.args[2]['body']


def test_no_diff_with_message(publication, tmp_path):
    prepare(publication, tmp_path)
    publication[4].publication_commit.side_effect = TaskError('no_diff', 'GIT')
    run(publication)
    publication[0].flush([PROJECT])
    publication[3].write.assert_not_called()
    publication[4].push.assert_not_called()
    assert publication[1].get(publication[2])['error_code'] == 'no_diff'
    assert publication[1].db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
