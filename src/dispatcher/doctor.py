"""Read-only external diagnostics with injectable command boundaries."""
import os
import shutil
import subprocess
import sys
from dispatcher.github import authenticated, Adapter, GitHubError
from dispatcher.config import COLORS
from dispatcher.initialization import ready


def ssh_diagnose(config, which, run, environ):
    """Inspect existing project clones only; owner/name does not specify transport."""
    ssh, unknown = False, False
    for project in config.projects:
        if not project.enabled:
            continue
        clone = config.workspace / project.id / 'repository'
        if not (clone / '.git').exists():
            clone = config.workspace / project.id  # Stage 1 legacy diagnostic path
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
    from dispatcher.containment import capabilities
    boundary = capabilities()
    checks.append(('FAIL', 'Descendant containment: backend unimplemented; launches and cleanup blocked; ' +
        ('cgroup v2 delegation not writable' if not boundary['delegated_writable'] else 'delegation alone is not a verified boundary')))
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
    # Read-only: inspect the closest existing parent, without creating directories.
    writable = True
    for path in (config.workspace, config.logs):
        while not path.exists() and path != path.parent:
            path = path.parent
        writable &= os.access(path, os.W_OK | os.X_OK)
    checks.append(('PASS' if writable else 'FAIL', 'Directories: writable' if writable else 'Directories: check ownership and permissions'))
    adapter = Adapter(config.github_executable, run=run, attempts=1)
    for project in config.projects:
        if not project.enabled:
            continue
        checks.append(('PASS' if ready(project) else 'FAIL', f'{project.id}: initialization ' + ('current' if ready(project) else 'missing, failed or stale; run init')))
        try:
            repository = adapter.request(f'repos/{project.repository}')
            if not isinstance(repository, dict):
                raise GitHubError(kind='invalid_response')
            checks.append(('PASS', f'{project.id}: repository accessible (read access)'))
            permissions = repository.get('permissions')
            if not isinstance(permissions, dict):
                permissions = {}
            flags = [key for key in ('push', 'maintain', 'admin') if permissions.get(key) is True]
            checks.append(('INFO', f'{project.id}: GitHub repository permission flags: ' + (', '.join(flags) if flags else 'none reported')))
        except (GitHubError, KeyError, TypeError, AttributeError):
            checks.append(('FAIL', f'{project.id}: repository read access cannot be verified'))
        else:
            try:
                labels = {v['name'].casefold() for v in adapter.labels(project)}
                missing = any(project.label(k).name.casefold() not in labels for k in COLORS)
                checks.append(('FAIL' if missing else 'PASS', f'{project.id}: required labels ' + ('missing; run init' if missing else 'present')))
            except (GitHubError, KeyError, TypeError, AttributeError):
                checks.append(('FAIL', f'{project.id}: required labels cannot be verified'))
        checks.append(('WARN', f'{project.id}: label write permission is not confirmed by read-only checks; '
                       'run init (or init --update for existing labels) to attempt the required operations'))
    checks.append(('PASS', 'YAML configuration valid'))
    return checks
