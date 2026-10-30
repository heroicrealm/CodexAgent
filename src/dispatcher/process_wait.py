"""Wait for command helpers naturally; deadlines never authorize a signal."""
import subprocess
import tempfile
import time

_lifecycle = None


def bind(lifecycle):
    """The daemon owns one Lifecycle; standalone commands have no force channel."""
    global _lifecycle
    _lifecycle = lifecycle


def run(argv, **kwargs):
    # subprocess.run(timeout=...) kills its child internally. Retain the caller's
    # timeout error contract, but report it only after the helper has exited.
    limit = kwargs.pop('timeout', None)
    started = time.monotonic()
    lifecycle = _lifecycle
    if lifecycle is None or not kwargs.get('capture_output'):
        result = subprocess.run(argv, **kwargs)
    else:
        # Register daemon API/probe helpers as services too, so an explicit
        # stop --force can interrupt a scheduler blocked on one of these calls.
        # Files avoid communicate()/poll() reaping a Lifecycle-owned leader.
        kwargs.pop('capture_output')
        text = kwargs.pop('text', False)
        data = kwargs.pop('input', None)
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error, tempfile.TemporaryFile() as source:
            if data is not None:
                source.write(data.encode() if isinstance(data, str) else data)
                source.seek(0)
                kwargs['stdin'] = source
            try:
                child = lifecycle.submit(argv, service=True, during_drain=True,
                                         stdout=output, stderr=error, **kwargs)
            except RuntimeError:
                if not lifecycle.forced:
                    raise
                # Force raced with admission of a helper: nothing was launched.
                empty = '' if text else b''
                return subprocess.CompletedProcess(argv, 125, empty, empty)
            while child in lifecycle._active():
                if lifecycle.forced:
                    lifecycle.cancel()
                time.sleep(.01)
            output.seek(0)
            error.seek(0)
            out, err = output.read(), error.read()
            result = subprocess.CompletedProcess(argv, child.returncode,
                out.decode(errors='replace') if text else out, err.decode(errors='replace') if text else err)
    if limit is not None and time.monotonic() - started >= limit:
        raise subprocess.TimeoutExpired(argv, limit, output=result.stdout, stderr=result.stderr)
    return result
