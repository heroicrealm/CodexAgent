"""Fail-closed variant B boundary contract.

No production backend is installed yet. PGID/PPID/cwd and an empty /proc scan
are never accepted as proof. Delegation capabilities alone are not a boundary:
launch gating, migration protection, durable identity and recovery are required.
"""
import os
from pathlib import Path

from dispatcher.workspace import TaskError


def capabilities():
    """Read-only evidence, not a permission grant or containment certificate."""
    result = dict(backend='unimplemented', cgroup_v2=False, path=None,
                  delegated_writable=False, error=None)
    try:
        entry = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                     if line.startswith('0::'))
        mount = Path('/sys/fs/cgroup')
        path = mount / entry.lstrip('/')
        result.update(cgroup_v2=(mount / 'cgroup.controllers').is_file(), path=str(path))
        result['delegated_writable'] = bool(result['cgroup_v2'] and
            os.access(path, os.W_OK | os.X_OK) and all(
                os.access(path / name, os.W_OK) for name in ('cgroup.procs','cgroup.freeze','cgroup.kill')))
    except (OSError, StopIteration, ValueError):
        result['error'] = 'cgroup_capabilities_unavailable'
    return result


def require_control():
    """Future backend must supply a verifiable, non-reopenable empty boundary.

    Intentionally unconditional until that backend is implemented and tested.
    Neither writable cgroup files nor an operator boolean can bypass this guard.
    """
    raise TaskError('descendant_boundary_unverified', 'DISPATCHER')
