"""Read-only external diagnostics with injectable command boundaries."""
import os
import shutil
import subprocess
import sys
import tempfile
from dispatcher.github import authenticated
from dispatcher.runtime import prepare


def ssh_diagnose(config, which, run, environ):
    """Inspect existing project clones only; owner/name does not specify transport."""
    ssh, unknown = False, False
    for project in config.projects:
        if not project.enabled:
            continue
        clone = config.workspace / project.id
        if not (clone / '.git').exists():
            unknown = True
            continue
        try:
            result = run(['git', '-C', str(clone), 'remote', '-v'],
                         capture_output=True, timeout=15)
            output = result.stdout
            if isinstance(output, bytes):
                output = output.decode(errors='replace')
            if result.returncode or not output.strip():
                unknown = True
                continue
            for line in output.splitlines():
                parts = line.split()
                if len(parts) < 2:
                    unknown = True
                    continue
                url = parts[1]
                if url.startswith('ssh://') or (':' in url and '://' not in url):
                    ssh = True
                elif not url.startswith(('https://', 'http://', 'file://', '/')):
                    unknown = True
        except (OSError, subprocess.TimeoutExpired):
            unknown = True
    checks = []
    if unknown:
        checks.append(('WARN', 'Git transport: unknown for some projects; inspect clone remotes and Git URL rewrites'))
    severity = 'FAIL' if ssh else 'WARN'
    advice = 'SSH Agent: start ssh-agent and run ssh-add locally; GitHub API authentication is independent'
    if not environ.get('SSH_AUTH_SOCK'):
        checks.append((severity if ssh or unknown else 'PASS',
                       advice if ssh or unknown else 'SSH Agent: not required by detected remotes'))
    elif not which('ssh-add'):
        checks.append((severity, 'SSH Agent: install OpenSSH client and run ssh-add locally'))
    else:
        try:
            result = run(['ssh-add', '-l'], capture_output=True, timeout=15)
            checks.append(('PASS' if result.returncode == 0 else severity,
                           'SSH Agent: reachable with loaded key' if result.returncode == 0 else advice))
        except (OSError, subprocess.TimeoutExpired):
            checks.append((severity, advice))
    return checks


def diagnose(config, which=shutil.which, run=subprocess.run, environ=None):
    checks = [('PASS' if sys.version_info >= (3, 12) else 'FAIL', 'Python 3.12+')]
    for binary in ('git', config.github_executable, config.executable):
        found = which(binary)
        if not found:
            checks.append(('FAIL', f'{binary}: install CLI or fix PATH'))
            continue
        try:
            result = run([binary, '--version'], capture_output=True, timeout=15)
            ok = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        checks.append(('PASS' if ok else 'FAIL', f'{binary}: version check passed' if ok else
                       f'{binary}: version check failed; check executable and dependencies'))
    if which(config.github_executable):
        try:
            ok = authenticated(run, config.github_executable)
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        checks.append(('PASS' if ok else 'FAIL', 'GitHub authentication' if ok else 'GitHub authentication: run gh auth login'))
    else:
        checks.append(('WARN', 'GitHub authentication: install gh first'))
    checks.extend(ssh_diagnose(config, which, run, os.environ if environ is None else environ))
    try:
        prepare(config)
        for path in (config.workspace, config.logs):
            with tempfile.TemporaryFile(dir=path):
                pass
        checks.append(('PASS', 'Directories: writable'))
    except OSError:
        checks.append(('FAIL', 'Directories: check ownership and permissions'))
    checks.append(('PASS', 'YAML configuration valid'))
    return checks
