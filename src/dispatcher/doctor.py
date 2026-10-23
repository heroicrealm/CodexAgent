"""Read-only external diagnostics with injectable command boundaries."""
import os
import shutil
import subprocess
import sys
import tempfile
from dispatcher.github import authenticated
from dispatcher.runtime import prepare


def diagnose(config, which=shutil.which, run=subprocess.run):
    checks = [('PASS' if sys.version_info >= (3, 12) else 'FAIL', 'Python 3.12+')]
    for binary in ('git', 'gh', config.executable):
        found = which(binary)
        checks.append(('PASS' if found else 'FAIL', f'{binary}: available' if found else f'{binary}: install CLI or fix PATH'))
    if which('gh'):
        try:
            ok = authenticated(run)
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        checks.append(('PASS' if ok else 'FAIL', 'GitHub authentication' if ok else 'GitHub authentication: run gh auth login'))
    else:
        checks.append(('WARN', 'GitHub authentication: install gh first'))
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
