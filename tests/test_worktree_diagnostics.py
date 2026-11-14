"""Diagnostics operate exclusively on disposable local repositories."""
import json
from pathlib import Path
import subprocess

import pytest

from dispatcher.config import Config, Project
from dispatcher.tools.worktree_diagnostics import diagnose, main, paths


def git(*args):
    return subprocess.run(['git', *map(str, args)], check=True, capture_output=True).stdout


@pytest.fixture
def repository(tmp_path):
    checkout = tmp_path / 'repo'
    git('init', checkout)
    git('-C', checkout, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '--allow-empty', '-m', 'initial')
    project = Project('example', 'example/test', checkout=checkout)
    return Config(tmp_path / 'workspace', tmp_path / 'unused.sqlite', (project,)), project


def contents(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('linked', [2, 3])
def test_comparison_read_only(repository, tmp_path, linked):
    config, project = repository
    for i in range(linked):
        target = tmp_path / f'linked {i}\tю'
        git('-C', project.checkout, 'worktree', 'add', '--detach', target)
        (target / 'private-file').write_text('never read me')
    before = contents(tmp_path)
    report = diagnose(config, project, repeat=2)
    assert contents(tmp_path) == before
    for row in report['rounds']:
        assert row['parsed']['count'] == linked + 1
        assert row['parser_matches_raw']
        assert row['native_matches_instrumented']
        assert not row['metadata_changed']
        assert row['differences'] == []
    samples = [r for r in report['commands'] if 'count' in r]
    assert all(r['count'] == linked + 1 and r['returncode'] == 0 for r in samples)
    assert all(r['stdout_size'] and len(r['stdout_sha256']) == 64 for r in samples)
    raw = json.dumps(report)
    assert str(tmp_path) not in raw
    assert 'never read me' not in raw
    assert not config.database.exists()


def test_prunable_not_removed(repository, tmp_path):
    config, project = repository
    target = tmp_path / 'gone'
    git('-C', project.checkout, 'worktree', 'add', '--detach', target)
    target.rename(tmp_path / 'moved')
    before = contents(tmp_path)
    report = diagnose(config, project, 2)
    assert contents(tmp_path) == before
    assert all(r['parsed']['count'] == 2 for r in report['rounds'])
    assert all(not r['metadata_changed'] for r in report['rounds'])


def test_symlink_refused(repository, tmp_path):
    config, project = repository
    (project.checkout / '.git' / 'worktrees').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlink'):
        diagnose(config, project, 1)


def test_redirect_refused(repository, monkeypatch):
    config, project = repository
    monkeypatch.setenv('GIT_DIR', '/private/secret')
    with pytest.raises(ValueError, match='redirect'):
        diagnose(config, project, 1)


def test_cli_error_redacted(tmp_path, capsys):
    bad = tmp_path / 'config.yaml'
    bad.write_text('private-secret: [invalid')
    assert main(['--config', str(bad), '--project', 'test']) == 2
    assert 'private-secret' not in capsys.readouterr().out


def test_scanner_quoted_matches_nul():
    assert paths(b'worktree "/tmp/a\\t\\321\\216"\n', False) == paths(
        'worktree /tmp/a\tю\0'.encode(), True)


def test_empty_gitdir_explains_silent_omission(repository, tmp_path):
    config, project = repository
    target = tmp_path / 'linked'
    git('-C', project.checkout, 'worktree', 'add', '--detach', target)
    registration = next((project.checkout / '.git' / 'worktrees').iterdir())
    (registration / 'gitdir').write_bytes(b'')
    before = contents(tmp_path)
    report = diagnose(config, project, 1)
    assert len(report['rounds'][0]['before']['entries']) == 1
    assert report['rounds'][0]['parsed']['count'] == 1
    assert report['rounds'][0]['parser_matches_raw']
    assert contents(tmp_path) == before


def test_trace_environment_cannot_write(repository, tmp_path, monkeypatch):
    config, project = repository
    trace = tmp_path / 'trace'
    monkeypatch.setenv('GIT_TRACE', str(trace))
    with pytest.raises(ValueError, match='redirect'):
        diagnose(config, project, 1)
    assert not trace.exists()


def test_raw_mismatch_and_parser_failure_visible(repository, monkeypatch):
    config, project = repository
    actual = subprocess.run

    def transport(args, **kwargs):
        result = actual(args, **kwargs)
        if 'worktree' in args and 'list' in args and '-z' in args:
            result.stdout = result.stdout[:-1]  # independently injected truncated transport
        return result

    monkeypatch.setattr(subprocess, 'run', transport)
    report = diagnose(config, project, 1)
    assert report['rounds'][0]['parsed'] == {'error_type': 'GitError'}
    assert not report['rounds'][0]['parser_matches_raw']
    assert all(r['nul_records'] == 0 for r in report['commands']
               if 'count' in r and r['format'] == 'nul')


def test_metadata_race_visible(repository, tmp_path, monkeypatch):
    config, project = repository
    target = tmp_path / 'linked'
    git('-C', project.checkout, 'worktree', 'add', '--detach', target)
    head = next((project.checkout / '.git' / 'worktrees').iterdir()) / 'HEAD'
    actual = subprocess.run

    def transport(args, **kwargs):
        result = actual(args, **kwargs)
        if 'worktree' in args and 'list' in args:
            head.write_bytes(head.read_bytes())  # simulate another process touching metadata
        return result

    monkeypatch.setattr(subprocess, 'run', transport)
    report = diagnose(config, project, 1)
    assert report['rounds'][0]['metadata_changed']


def test_path_set_difference_visible(repository, tmp_path, monkeypatch):
    config, project = repository
    git('-C', project.checkout, 'worktree', 'add', '--detach', tmp_path / 'linked')
    actual = subprocess.run

    def transport(args, **kwargs):
        result = actual(args, **kwargs)
        if 'worktree' in args and 'list' in args and '-z' in args:
            separator = '\0\0' if isinstance(result.stdout, str) else b'\0\0'
            result.stdout = result.stdout.split(separator)[0] + separator
        return result

    monkeypatch.setattr(subprocess, 'run', transport)
    row = diagnose(config, project, 1)['rounds'][0]
    assert row['parsed']['count'] == 1
    assert row['parser_matches_raw']
    assert row['differences']
    assert all(len(d['only_baseline']) == 1 for d in row['differences'])


def test_cli_success(repository, tmp_path, capsys):
    _, project = repository
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'dispatcher:\n  root_path: {tmp_path}\nprojects:\n  - id: example\n    repository: example/test\n    checkout: ' +
                   str(project.checkout) + '\n')
    assert main(['--config', str(cfg), '--project', 'example', '--repeat', '1']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['rounds'][0]['parser_matches_raw']
