"""Presentation and argparse checks: synthetic results only, no external services."""
import io
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from dispatcher.recovery_output import render_recovery


def identity(issue_id=6370045834, repository='owner/repo', number=41):
    return dict(issue_id=issue_id, repository=repository, issue_number=number)


def status(count=1):
    issues = [identity(), identity(9000000001, 'other/repo')][:count]
    return dict(format_version=2, recovery_required=False,
                quarantine=[dict(**i, reason_code=r, details='synthetic', created_at='date', updated_at='date')
                            for i in issues for r in ('execution-uncertain', 'local-worktree')],
                operations=[dict(**i, operation_id=f'op-{n}', action='retry',
                                 phase='completed' if n == 0 else 'archived',
                                 started_at='date', updated_at='date', github_comment_id=None)
                            for n, i in enumerate(issues)])


def render(value, mode='status', width=120, **kwargs):
    stream = io.StringIO()
    render_recovery(value, mode=mode, console=Console(file=stream, width=width), **kwargs)
    return stream.getvalue()


@pytest.mark.parametrize('count', [0, 1, 2])
def test_status_groups_and_identity(count):
    output = render(status(count))
    assert 'CODEX DISPATCHER · RECOVERY' in output
    assert f'Quarantined issues: {count}' in output
    assert '\x1b' not in output
    if not count:
        assert 'No quarantined issues' in output
        assert 'No recovery operations' in output
    else:
        assert 'QUARANTINED ISSUES' in output
        assert 'RECOVERY OPERATIONS' in output
        assert output.count('owner/repo') == 2  # one quarantine group plus one operation
        assert output.count('execution-uncertain') == count
        assert '6370045834' not in output
        assert '#41' in output
    if count == 2:
        assert output.count('other/repo') == 2
        assert 'Unfinished operations: 1' in output
        assert 'archived' in output and 'completed' in output


@pytest.mark.parametrize('width', [40, 80, 160])
def test_wrapping_and_literal_values(width):
    value = status()
    value['quarantine'][0]['repository'] = 'owner/[red]repository'
    output = render(value, width=width)
    compact = ''.join(output.replace('│', '').split())
    assert '[red]' in compact
    reasons = ''.join(line.split('│')[-2].strip() for line in output.splitlines() if line.count('│') == 4)
    assert 'execution-uncertain' in reasons
    assert 'local-worktree' in reasons
    assert '…' not in output


@pytest.mark.parametrize('mode', ['status', 'recover', 'accept', 'retry', 'cancel'])
def test_json_is_exact_document(mode):
    value = status()
    if mode == 'recover':
        value = [dict(**identity(), reasons=[], target='discovered')]
    output = render(value, mode=mode, as_json=True)
    assert json.loads(output) == value
    assert '\x1b' not in output


@pytest.mark.parametrize('dry_run', [True, False])
def test_recover_output(dry_run):
    output = render([dict(**identity(), reasons=['local-worktree'], target='running')],
                    mode='recover', dry_run=dry_run, status=status())
    assert ('DRY RUN / NO CHANGES' if dry_run else 'RECOVERY APPLIED') in output
    assert 'Global admission: READY' in output
    assert 'Quarantined issues: 1' in output
    assert 'PASS' not in output


def test_target_reasons_and_approve():
    value = dict(**identity(), action='retry', phase='completed', operation_id='operation-full',
                 recovery_required=True, quarantine=status(2)['quarantine'])
    value['quarantine'].append(dict(**identity(3, 'third/repo'), reason_code='unrelated'))
    output = render(value, mode='retry')
    assert 'operation-full' in output and 'New GitHub approve required' in output
    assert 'unrelated' not in output
    assert 'Global admission: BLOCKED' in output


@pytest.mark.parametrize('no_color', [False, True])
def test_real_console_tty_and_no_color(monkeypatch, no_color):
    class TTY(io.StringIO):
        def isatty(self):
            return True
    stream = TTY()
    monkeypatch.setattr('sys.stdout', stream)
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.delenv('FORCE_COLOR', raising=False)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '1')
    render_recovery(status(), mode='status')
    assert ('\x1b' in stream.getvalue()) is not no_color


@pytest.mark.parametrize('command', [ ['recover'], ['recover', '--dry-run'], ['recovery', 'status'],
                                    *[['recovery', a, 'p', '41'] for a in ('accept', 'retry', 'cancel')]])
@pytest.mark.parametrize('as_json', [False, True])
def test_cli_formats(command, as_json, monkeypatch, capsys):
    from dispatcher.__main__ import main
    value = status()
    operation = dict(**identity(), action=command[1] if command[0] == 'recovery' else 'retry',
                     phase='completed', operation_id='operation-full', started_at='date')
    value['operations'] = [operation]
    calls = []
    class Service:
        def __init__(self, *args):
            pass
        def status(self):
            return value
        def recover(self, dry_run):
            calls.append(dry_run)
            return [dict(**identity(), reasons=[], target='discovered')]
        def command(self, action, project, number):
            calls.append((action, project, number))
            return operation['operation_id']
    monkeypatch.setattr('dispatcher.__main__.load_config', lambda p: SimpleNamespace())
    monkeypatch.setattr('dispatcher.recovery.Recovery', Service)
    monkeypatch.setattr('dispatcher.github.GitHubAdapter', lambda *a: None)
    monkeypatch.setattr('dispatcher.git.Git', lambda *a: None)
    monkeypatch.setattr('sys.argv', ['cli', '--config', 'unused', 'db', *command, *(['--json'] if as_json else [])])
    main()
    captured = capsys.readouterr()
    assert not captured.err
    assert '\x1b' not in captured.out
    if as_json:
        result = json.loads(captured.out)
        row = result[0] if isinstance(result, list) else result
        if command != ['recovery', 'status']:
            assert row['issue_id'] == 6370045834
            assert row['issue_number'] == 41 and row['repository'] == 'owner/repo'
    else:
        assert 'CODEX DISPATCHER · RECOVERY' in captured.out
    if command[0] == 'recover':
        assert calls == [('--dry-run' in command)]
    elif command[1] != 'status':
        assert calls == [(command[1], 'p', 41)]


@pytest.mark.parametrize('command', [['recover'], ['recovery', 'status'],
                                    *[['recovery', a, 'p', '41'] for a in ('accept', 'retry', 'cancel')]])
@pytest.mark.parametrize('as_json', [False, True])
def test_cli_refusal(command, as_json, monkeypatch, capsys):
    from dispatcher.__main__ import main
    from dispatcher.storage import SchemaError
    def refuse(*a):
        raise SchemaError('synthetic refusal')
    monkeypatch.setattr('dispatcher.__main__.load_config', lambda p: None)
    class Service:
        def __init__(self, *args):
            pass
        recover = status = command = refuse
    monkeypatch.setattr('dispatcher.recovery.Recovery', Service)
    monkeypatch.setattr('dispatcher.github.GitHubAdapter', lambda *a: None)
    monkeypatch.setattr('dispatcher.git.Git', lambda *a: None)
    monkeypatch.setattr('sys.argv', ['cli', '--config', 'unused', 'db', *command, *(['--json'] if as_json else [])])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == 'FAIL synthetic refusal\n'


def test_cli_invalid_number(monkeypatch, capsys):
    from dispatcher.__main__ import main
    monkeypatch.setattr('sys.argv', ['cli', '--config', 'unused', 'db', 'recovery', 'retry', 'p', 'bad', '--json'])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not capsys.readouterr().out


def test_json_bypasses_tty_styles():
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=20)
    value = status(2)
    render_recovery(value, mode='status', as_json=True, console=console)
    assert json.loads(stream.getvalue()) == value
    assert '\x1b' not in stream.getvalue()
