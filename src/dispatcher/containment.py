"""Optional stronger-containment capability diagnostics, never an admission gate."""
import os
from pathlib import Path


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
    """Compatibility shim: optional cgroups do not gate Lifecycle execution."""
    return None
