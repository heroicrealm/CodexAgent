"""Read-only GitHub authorization diagnostic."""
import subprocess


def authenticated(run=subprocess.run):
    return run(['gh', 'auth', 'status'], capture_output=True, timeout=15).returncode == 0
