"""Human output boundaries, including terminal capabilities and legacy snapshots."""
import io
import json

import pytest

from dispatcher.rendering import render_queue, render_show, width
from test_cli_unicode import unicode_task
from dispatcher.cli import main
from dispatcher.storage import Store


class Terminal(io.StringIO):
    def isatty(self):
        return True


def task(title='Написать 中文 🚀', body='# План\n\nПервый абзац\n- пункт\n```py\nx = 1\n```'):
    return dict(number=1, repository='o/r', project_id='local', state='running',
                snapshot=json.dumps(dict(title=title, body=body, target_branch='feature')))


@pytest.mark.parametrize('tty,no_color,term,colored', [
    (True, False, 'xterm', True), (False, False, 'xterm', False),
    (True, True, 'xterm', False), (True, False, 'dumb', False),
    (True, False, '', False)])
def test_terminal_colors(monkeypatch, tty, no_color, term, colored):
    monkeypatch.setenv('TERM', term)
    monkeypatch.delenv('NO_COLOR', raising=False)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    stream = Terminal() if tty else io.StringIO()
    render_queue([task()], {'o/r': 'configured'}, stream=stream)
    assert ('\033[' in stream.getvalue()) == colored
    assert 'running' in stream.getvalue()
    stream.seek(0); stream.truncate()
    render_queue([task()], {}, 'json', stream)
    assert '\033' not in stream.getvalue()
    assert json.loads(stream.getvalue()) == [task()]


@pytest.mark.parametrize('columns', [1, 10, 25, 80])
def test_narrow_unicode_table(monkeypatch, columns):
    monkeypatch.setenv('COLUMNS', str(columns))
    out = io.StringIO()
    render_queue([task('Очень длинный 中文 e\u0301 🚀' * 30), dict(task(), number=2)],
                 {'o/r': 'проект'}, stream=out)
    assert len(out.getvalue().splitlines()) == 3
    assert all(width(line) <= max(7, columns) for line in out.getvalue().splitlines())
    assert '\033' not in out.getvalue()


def test_empty():
    out = io.StringIO()
    render_queue([], {}, stream=out)
    assert out.getvalue() == 'Queue is empty.\n'


@pytest.mark.parametrize('raw', [None, 'broken', '[]', 'null', '{}', '{"body": 42}'])
def test_missing_snapshot(raw):
    out = io.StringIO()
    render_show(dict(task(), snapshot=raw), stream=out)
    assert '(body unavailable)' in out.getvalue()


def test_complete_body_and_sections():
    row = task()
    row.update(decisions=[dict(action='approve', source='cli')],
               command_diagnostics=[dict(reason='stale_version')],
               attempts=[dict(result_sha='abc', origin_url='https://secret@host')],
               quota=[dict(state='available')], result_publication=[dict(published=1)])
    out = io.StringIO()
    render_show(row, stream=out)
    output = out.getvalue()
    assert json.loads(row['snapshot'])['body'] in output
    for text in ('approve', 'stale_version', 'abc', 'available', 'published=1'):
        assert text in output
    assert 'secret' not in output
    assert '\033' not in output


def test_terminal_controls_are_neutralized():
    out = io.StringIO()
    render_show(task(body='text\033[31m\x00\nnext'), stream=out)
    assert '\033' not in out.getvalue() and '\x00' not in out.getvalue()


def test_cli_offline_and_configured_projects(unicode_task, monkeypatch, capsys):
    config, item, _ = unicode_task
    monkeypatch.setattr('dispatcher.github.Adapter.__init__', lambda *_: pytest.fail('network adapter'))
    assert main(['--config', str(config.source), 'show', 'ru-project', '1']) == 0
    assert item['body'] in capsys.readouterr().out
    store = Store(config.workspace / '.runtime/tasks.sqlite3')
    # Add a repository absent from configuration; preserve Store ordering.
    with store.db:
        store.db.execute("INSERT INTO tasks SELECT 'aaa/hidden',number,issue_id,project_id,state,available,version,content_hash,snapshot,observed,approved_version,created_at,synced_at FROM tasks")
    store.close()
    assert main(['--config', str(config.source), 'queue']) == 0
    output = capsys.readouterr().out
    assert 'ru-project' in output and output.count('#1') == 1


def test_show_json_populated_sections(unicode_task, capsys):
    from dispatcher.execution_store import ExecutionStore
    config, item, _ = unicode_task
    store = Store(config.workspace / '.runtime/tasks.sqlite3')
    ExecutionStore(store)
    with store.db:
        store.db.execute("INSERT INTO decisions(repository,number,version,source,action,result,created_at) VALUES('owner/repo',1,1,'cli','approve','applied','now')")
        store.db.execute("INSERT INTO command_quarantine(repository,number,action,event_id,reason) VALUES('owner/repo',1,'approve','e1','stale_version')")
        store.db.execute("INSERT INTO attempts(attempt_id,repository,number,version,branch,state,started_at,result_sha) VALUES('a1','owner/repo',1,1,'feature','review','now','abc')")
        store.db.execute("INSERT INTO result_outbox(attempt_id,body,published,comment_id) VALUES('a1','private',1,'c1')")
        store.db.execute("INSERT INTO quota_observation VALUES(1,'available','now','ok')")
    expected = dict(store.get('owner/repo', 1))
    expected['decisions'] = [dict(r) for r in store.db.execute('SELECT * FROM decisions')]
    expected['command_diagnostics'] = [dict(r) for r in store.db.execute('SELECT action,event_id,reason FROM command_quarantine')]
    expected['attempts'] = [dict(r) for r in store.db.execute('SELECT * FROM attempts')]
    expected['quota'] = [dict(r) for r in store.db.execute('SELECT * FROM quota_observation')]
    expected['result_publication'] = [dict(attempt_id='a1', published=1, comment_id='c1')]
    store.close()
    assert main(['--config', str(config.source), 'show', 'ru-project', '1', '--format', 'json']) == 0
    actual = json.loads(capsys.readouterr().out)
    for key in ('snapshot', 'observed'):
        assert isinstance(actual[key], str)
        actual[key] = json.loads(actual[key])
        expected[key] = json.loads(expected[key])
    assert actual == expected


def test_queue_multiple_projects_order(monkeypatch):
    monkeypatch.setenv('COLUMNS', '120')
    rows = [dict(task('Первый'), repository='a/repo', number=2),
            dict(task('Второй'), repository='a/repo', number=10),
            dict(task('Третий'), repository='b/repo', number=1)]
    out = io.StringIO()
    render_queue(rows, {'a/repo': 'alpha', 'b/repo': 'beta'}, stream=out)
    lines = out.getvalue().splitlines()
    assert '#2' in lines[1] and 'alpha' in lines[1]
    assert '#10' in lines[2] and 'Второй' in lines[2]
    assert '#1' in lines[3] and 'beta' in lines[3]
