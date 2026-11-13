"""Explicit, local-only worktree diagnostics; never used by recovery.

Only config --null --list, --version, rev-parse and worktree list are invoked.
No raw command output, config values or absolute repository paths are emitted.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import locale
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from unittest.mock import patch

from dispatcher.config import is_v1_path, load_config
from dispatcher.git import Git


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlink path refused')
    return path


def path_id(path):
    # Lexical normalization: do not follow paths supplied by Git.
    return 'path:' + digest(os.fsencode(os.path.abspath(path)))


def paths(raw, nul):
    """Independent scanner, deliberately separate from the adapter parser."""
    values = []
    for field in raw.split(b'\0' if nul else b'\n'):
        if field.startswith(b'worktree '):
            value = field[9:]
            # Git C-quotes special paths in the non-NUL format.
            if not nul and value.startswith(b'"'):
                value = re.sub(rb'\\([0-7]{3}|[abtnvfr\\"])',
                    lambda m: bytes([int(m[1], 8)]) if m[1].isdigit() else
                    {b'a': b'\a', b'b': b'\b', b't': b'\t', b'n': b'\n',
                     b'v': b'\v', b'f': b'\f', b'r': b'\r', b'\\': b'\\', b'"': b'"'}[m[1]],
                    value[1:-1])
                values.append(path_id(os.fsdecode(value)))
            else:
                values.append(path_id(os.fsdecode(value)))
    return sorted(values)


def directory_metadata(path):
    info = path.stat()
    return {'mode': stat.S_IMODE(info.st_mode), 'inode': info.st_ino,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
            'readable': os.access(path, os.R_OK), 'searchable': os.access(path, os.X_OK)}


def snapshot(common):
    root = safe_path(common / 'worktrees')
    records = []
    if root.exists():
        if not root.is_dir():
            raise ValueError('Unexpected registration directory')
        for entry in sorted(root.iterdir()):
            safe_path(entry)
            if not entry.is_dir():
                raise ValueError('Unexpected registration entry')
            files = []
            for child in sorted(entry.iterdir()):
                safe_path(child)
                info = child.lstat()
                record = {'name_hash': digest(os.fsencode(child.name)),
                          'type': stat.S_IFMT(info.st_mode), 'size': info.st_size,
                          'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
                          'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode)}
                # Read only small, known registration files, never index/user files.
                if child.name in {'gitdir', 'commondir', 'HEAD', 'locked', 'prunable'}:
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                        raise ValueError('Unexpected registration file')
                    data = child.read_bytes()
                    record.update(role=child.name, sha256=digest(data))
                files.append(record)
            records.append({'name_hash': digest(os.fsencode(entry.name)),
                            'directory': directory_metadata(entry), 'files': files})
    data = {'entries': records, 'registration_count': len(records),
            'directory': directory_metadata(root) if root.exists() else None}
    return {**data, 'sha256': digest(json.dumps(data, sort_keys=True).encode())}


def diagnose(config, project, repeat=5):
    if not 1 <= repeat <= 100:
        raise ValueError('Repeat must be between 1 and 100')
    checkout = safe_path(project.checkout or config.workspace / 'repos' / project.id)
    # This diagnostic intentionally accepts only a main checkout with a real .git.
    dotgit = safe_path(checkout / '.git')
    if is_v1_path(checkout) or not checkout.is_dir() or not dotgit.is_dir():
        raise ValueError('Expected existing main checkout with local .git directory')
    if (dotgit / 'commondir').exists():
        raise ValueError('External common directory refused')
    report = {'format': 1, 'python': platform.python_version(), 'platform': platform.system(),
              'adapter_file': str(Path(__import__('dispatcher.git', fromlist=['Git']).__file__)),
              'checkout': path_id(checkout), 'process_cwd': path_id(Path.cwd()),
              'environment_names': sorted(k for k in os.environ if k.startswith('GIT_') or
                  k in {'PATH', 'HOME', 'XDG_CONFIG_HOME', 'LANG', 'LC_ALL', 'LC_CTYPE'}),
              'commands': [], 'rounds': []}
    # Environment is inherited unchanged for every source. Redirecting Git's repo
    # discovery would make both safety and comparisons ambiguous.
    forbidden = {'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_CONFIG',
                 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_SYSTEM',
                 'GIT_CONFIG_GLOBAL', 'GIT_NAMESPACE', 'GIT_EXEC_PATH'}
    forbidden.update(k for k in os.environ if k.startswith('GIT_TRACE'))
    if forbidden.intersection(os.environ):
        raise ValueError('Repository/config redirect environment refused (names only): ' +
                         ','.join(sorted(forbidden.intersection(os.environ))))
    real_run = subprocess.run
    active = {'source': 'direct'}

    def observed(args, **kwargs):
        start = time.monotonic_ns()
        utc = datetime.now(timezone.utc).isoformat()
        text_mode = kwargs.pop('text', False)
        result = real_run(args, **kwargs)  # always capture original bytes
        out, err = result.stdout or b'', result.stderr or b''
        row = {'source': active['source'], 'utc': utc, 'start_ns': start,
               'end_ns': time.monotonic_ns(), 'returncode': result.returncode,
               'stdout_size': len(out), 'stderr_size': len(err),
               'stdout_sha256': digest(out), 'stderr_sha256': digest(err),
               'format': 'nul' if '-z' in args else 'text',
               'cwd': path_id(kwargs.get('cwd') or Path.cwd()),
               'nul_fields': out.count(b'\0'), 'nul_records': out.count(b'\0\0')}
        if 'worktree' in args and 'list' in args:
            row['paths'] = paths(out, '-z' in args)
            row['count'] = len(row['paths'])
        report['commands'].append(row)
        if text_mode:
            encoding = 'utf-8' if sys.flags.utf8_mode else locale.getencoding()
            result.stdout = out.decode(encoding).replace('\r\n', '\n').replace('\r', '\n')
            result.stderr = err.decode(encoding).replace('\r\n', '\n').replace('\r', '\n')
        return result

    def call(args, source, cwd=None):
        active['source'] = source
        return observed(args, capture_output=True, timeout=120, cwd=cwd)

    executables = list(dict.fromkeys(filter(None, [shutil.which('git'), '/usr/bin/git'])))
    report['executables'] = [{'path': p, 'realpath': str(Path(p).resolve())} for p in executables]
    # Validate discovery against the directory we are about to inspect.
    snapshot(dotgit)  # reject unexpected registration paths before invoking Git
    discovered = call(['git', '-C', str(checkout), 'rev-parse', '--absolute-git-dir'], 'git-dir')
    if discovered.returncode or Path(os.fsdecode(discovered.stdout).strip()) != dotgit:
        raise ValueError('Unexpected Git directory')
    cfg = call(['git', '-C', str(checkout), 'config', '--null', '--list', '--show-origin'], 'config-fingerprint')
    report['config_ok'] = cfg.returncode == 0
    for executable in executables:
        version = call([executable, '--version'], 'version:' + executable)
        report['executables'][executables.index(executable)]['version'] = (
            version.stdout.decode('ascii', errors='replace').strip()
            if version.returncode == 0 and version.stdout.startswith(b'git version ') else 'unavailable')
    adapter = Git(config)
    for number in range(repeat):
        before = snapshot(dotgit)
        config_before = call(['git', '-C', str(checkout), 'config', '--null', '--list',
                              '--show-origin'], 'config-before')
        offset = len(report['commands'])
        for executable in executables:
            for nul in (False, True):
                suffix = ['worktree', 'list', '--porcelain'] + (['-z'] if nul else [])
                for mode in ('-C', 'cwd'):
                    args = [executable] + (['-C', str(checkout)] if mode == '-C' else []) + suffix
                    call(args, f'direct:{executable}:{mode}', cwd=checkout if mode == 'cwd' else None)
        # Also run the native text transport without instrumentation, so the
        # diagnostic itself cannot conceal a text-vs-bytes discrepancy.
        native_commands = []
        for nul in (False, True):
            native_start = time.monotonic_ns()
            native_utc = datetime.now(timezone.utc).isoformat()
            result = adapter.command(['git', '-C', str(checkout), 'worktree', 'list',
                                      '--porcelain'] + (['-z'] if nul else []), 'worktree')
            native_commands.append({'utc': native_utc, 'start_ns': native_start,
                'end_ns': time.monotonic_ns(), 'count': len(paths(result.stdout.encode('utf-8'), nul)),
                'format': 'nul' if nul else 'text',
                'returncode': result.returncode,
                'paths': paths(result.stdout.encode('utf-8'), nul),
                'decoded_stdout_sha256': digest(result.stdout.encode('utf-8')),
                'decoded_stderr_sha256': digest(result.stderr.encode('utf-8'))})
        try:
            native_entries = adapter.worktrees(project)
            native_parsed = {'count': len(native_entries),
                'paths': sorted(path_id(e['worktree']) for e in native_entries)}
        except Exception as exc:
            native_parsed = {'error_type': type(exc).__name__}
        # Instrument only subprocess transport: original Git.command/run/worktrees
        # execute unchanged, including their exceptions and strict parser.
        with patch('dispatcher.git.subprocess.run', observed):
            for nul in (False, True):
                active['source'] = 'Git.command'
                adapter.command(['git', '-C', str(checkout), 'worktree', 'list', '--porcelain'] +
                                (['-z'] if nul else []), 'worktree')
            active['source'] = 'Git.run'
            adapter.run(checkout, 'worktree', 'list', '--porcelain', '-z', check=False)
            active['source'] = 'Git.worktrees'
            try:
                entries = adapter.worktrees(project)
                parsed = {'count': len(entries), 'paths': sorted(path_id(e['worktree']) for e in entries)}
            except Exception as exc:
                # Exception messages may contain private output.
                parsed = {'error_type': type(exc).__name__}
        config_after = call(['git', '-C', str(checkout), 'config', '--null', '--list',
                             '--show-origin'], 'config-after')
        after = snapshot(dotgit)
        samples = [r for r in report['commands'][offset:] if 'paths' in r]
        baseline = samples[0]['paths']
        differences = [{'sample': i, 'only_baseline': sorted(set(baseline) - set(r['paths'])),
                        'only_sample': sorted(set(r['paths']) - set(baseline))}
                       for i, r in enumerate(samples) if r['paths'] != baseline]
        report['rounds'].append({'number': number + 1, 'before': before, 'after': after,
                                 'metadata_changed': before != after,
                                 'config_changed': (config_before.returncode, config_before.stdout) !=
                                                   (config_after.returncode, config_after.stdout),
                                 'parsed': parsed, 'native_parsed': native_parsed,
                                 'native_commands': native_commands,
                                 'native_matches_instrumented': native_parsed == parsed,
                                 'differences': differences,
                                 'parser_matches_raw': parsed.get('paths') == samples[-1]['paths']})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--repeat', type=int, default=5)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        project = next(p for p in config.projects if p.id == args.project)
        report = diagnose(config, project, args.repeat)
    except Exception as exc:
        # Never include exception text from Git/config/filesystem.
        print(json.dumps({'error_type': type(exc).__name__, 'diagnostics_complete': False}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
