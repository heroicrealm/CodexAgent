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
    assert len(out.getvalue().splitlines()) == 5
    assert all(width(line) <= max(17, columns) for line in out.getvalue().splitlines())
    assert '\033' not in out.getvalue()


def test_empty():
    out = io.StringIO()
    render_queue([], {}, stream=out)
    lines = out.getvalue().splitlines()
    assert lines[0].startswith('┌') and lines[-1].endswith('┘')
    assert '│ Queue is empty. │' in lines


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
    assert json.loads(row['snapshot'])['body'] in unframe(output)
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
    assert item['body'] in unframe(capsys.readouterr().out)
    store = Store(config.workspace / '.runtime/tasks.sqlite3')
    # Add a repository absent from configuration; preserve Store ordering.
    with store.db:
        store.db.execute("INSERT INTO tasks(repository,number,issue_id,project_id,state,available,version,content_hash,snapshot,observed,approved_version,created_at,synced_at) SELECT 'aaa/hidden',number,issue_id+100,project_id,state,available,version,content_hash,snapshot,observed,approved_version,created_at,synced_at FROM tasks")
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
    assert '#2' in lines[2] and 'alpha' in lines[2]
    assert '#10' in lines[3] and 'Второй' in lines[3]
    assert '#1' in lines[4] and 'beta' in lines[4]


def unframe(output):
    return '\n'.join(line[2:-2].rstrip() for line in output.splitlines()
                     if line.startswith('│'))


def test_queue_single_frame(monkeypatch):
    monkeypatch.setenv('COLUMNS', '100')
    out = io.StringIO()
    render_queue([task(), task()], {'o/r': 'local'}, stream=out)
    lines = out.getvalue().splitlines()
    assert lines[0].startswith('┌') and lines[0].endswith('┐')
    assert lines[-1].startswith('└') and lines[-1].endswith('┘')
    assert lines[0].count('┬') == lines[-1].count('┴') == 3
    assert all(line.startswith('│') and line.endswith('│') and line.count('│') == 5
               for line in lines[1:-1])
    assert len({width(line) for line in lines}) == 1


@pytest.mark.parametrize('columns', [1, 6, 10, 25, 100])
def test_show_four_sections_and_lossless_wrap(monkeypatch, columns):
    monkeypatch.setenv('COLUMNS', str(columns))
    body = '# Заголовок\n\n' + '中文 e\u0301 🚀 текст ' * 30 + '\n\nend\n'
    out = io.StringIO()
    render_show(task(body=body), stream=out)
    lines = out.getvalue().splitlines()
    assert lines[0].startswith('┌') and lines[0].endswith('┐')
    assert lines[-1].startswith('└') and lines[-1].endswith('┘')
    separators = [i for i, line in enumerate(lines) if line.startswith('├')]
    assert len(separators) == 3
    assert all(line.endswith('┤') for line in lines if line.startswith('├'))
    assert len({width(line) for line in lines}) == 1
    assert all(width(line) <= max(6, columns) for line in lines)
    # Compare content without frame padding; whitespace-only logical lines survive.
    body_rows = lines[separators[1] + 1:separators[2]]
    recovered = ''.join(line[2:-2].rstrip() for line in body_rows)
    assert ''.join(recovered.split()) == ''.join(('Issue body:' + body).split())
    assert sum(line[2:-2].strip() == '' for line in body_rows) >= 3


def test_show_section_contents(monkeypatch):
    monkeypatch.setenv('COLUMNS', '100')
    row = dict(task(), version=2, approved_version=1, available=0,
               created_at='yesterday', synced_at='today')
    out = io.StringIO()
    render_show(row, stream=out)
    sections = out.getvalue().split('├')
    assert 'Project: local' in sections[0] and 'Repository: o/r' in sections[0]
    for label in ('Issue: #1', 'Title:', 'Status:', 'Branch:', 'Version: 2',
                  'Approved version: 1', 'Available: 0', 'Created:', 'Synced:'):
        assert label in sections[1]
    assert 'Issue body:' in sections[2]
    assert '└' in sections[3]
    assert 'Author:' not in out.getvalue()


@pytest.mark.parametrize('title', [None, 42, True, ['中文', None], {'key': 'value'}])
def test_nonstandard_titles_and_json(title):
    row = task(title=title)
    for renderer, value, args in [(render_queue, [row], ({'o/r': 'local'},)),
                                   (render_show, row, ())]:
        out = io.StringIO()
        renderer(value, *args, stream=out)
        assert '┌' in out.getvalue()
        out = io.StringIO()
        renderer(value, *args, format='json', stream=out)
        assert out.getvalue() == json.dumps(value, ensure_ascii=False) + '\n'


def test_colored_queue_aligned(monkeypatch):
    import re
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setenv('COLUMNS', '45')
    monkeypatch.delenv('NO_COLOR', raising=False)
    out = Terminal()
    render_queue([task()], {'o/r': 'local'}, stream=out)
    plain = re.sub(r'\x1b\[[0-9;]*m', '', out.getvalue())
    assert len({width(line) for line in plain.splitlines()}) == 1
    assert all(width(line) <= 45 for line in plain.splitlines())


def test_invalid_details_and_surrogates():
    out = io.StringIO()
    render_show(dict(task(body='bad\ud800\x1b\x00\tend'),
                     attempts=[None, 42, {'state': 'running'}], quota=42), stream=out)
    assert '\x1b' not in out.getvalue() and '\ud800' not in out.getvalue()
    out.getvalue().encode('utf-8')
    assert 'state=running' in out.getvalue()


def test_wrap_preserves_whitespace_and_blank_lines():
    from dispatcher.rendering import wrap_text
    assert list(wrap_text('a  \n\n中文\n', 2)) == ['a ', ' ', '', '中', '文', '']
    assert list(wrap_text('a\tb', 4)) == ['a   ', 'b']
    assert list(wrap_text('e\u0301e\u0301', 1)) == ['e\u0301', 'e\u0301']


def test_show_empty_body_is_available():
    out = io.StringIO()
    render_show(task(body=''), stream=out)
    assert '(body unavailable)' not in out.getvalue()
    assert 'Issue body:' in out.getvalue()
