"""Offline transport for lifecycle tests; production HTTPS policy is tested separately."""
import subprocess
from dispatcher.git import Git, NETWORK_OPERATIONS


class LocalGit(Git):
    def verify_remote(self, directory, project=None):
        pass

    def command(self, args, operation):
        if operation in NETWORK_OPERATIONS:
            return subprocess.run(args, capture_output=True, text=True, timeout=120)
        return super().command(args, operation)
