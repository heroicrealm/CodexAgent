"""Read-only App Server adapter. Unknown protocols fail closed."""
import json
import queue
import subprocess
import threading
import time
from dispatcher.workspace import TaskError


def evaluate(payload, threshold):
    """Use every returned bucket conservatively; never infer a reset."""
    if not isinstance(payload, dict):
        raise ValueError
    windows, resets = [], []
    if 'ordinaryUsageAllowed' in payload:
        permission = payload['ordinaryUsageAllowed']
        if permission is None:
            return 'quota_unknown', resets
        if type(permission) is not bool:
            raise ValueError
        if permission is False:
            return 'quota_paused', resets
    buckets = [payload['rateLimits']]
    more = payload.get('rateLimitsByLimitId')
    if more is not None:
        if not isinstance(more, dict):
            raise ValueError
        buckets.extend(more.values())
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise ValueError
        if bucket.get('spendControlReached') is True or bucket.get('rateLimitReachedType'):
            return 'quota_paused', resets
        for key in ('primary', 'secondary', 'individualLimit'):
            window = bucket.get(key)
            if window is None:
                continue
            value = window['remainingPercent'] if key == 'individualLimit' else window['usedPercent']
            if type(value) not in (int, float) or not 0 <= value <= 100:
                raise ValueError
            percent = value if key == 'individualLimit' else 100 - value
            if not 0 <= percent <= 100:
                raise ValueError
            windows.append(percent)
            reset = window.get('resetsAt')
            if reset is not None:
                if type(reset) is not int:
                    raise ValueError
                resets.append(reset)
    if not windows:
        return 'quota_unknown', resets
    return ('quota_paused' if min(windows) < threshold else 'quota_available'), resets


class CodexQuotaProvider:
    def __init__(self, config, lifecycle, observation=lambda *_: None):
        self.config, self.lifecycle, self.observation = config, lifecycle, observation
        self.child = None
        self.messages = None
        self.reader = None
        self.sequence = 0
        self.state = 'quota_unknown' if config.quota_enabled else 'quota_available'
        self.next_check = 0
        self.failures = 0

    @staticmethod
    def read(stream, messages):
        try:
            while True:
                line = stream.readline(1048577)
                if not line or len(line) > 1048576:
                    break
                try:
                    value = json.loads(line)
                except ValueError:
                    break
                try:
                    messages.put(value, timeout=1)
                except queue.Full:
                    break
        finally:
            stream.close()
            try:
                messages.put(None, timeout=1)
            except queue.Full:
                pass

    def send(self, value):
        self.child.stdin.write(json.dumps(value) + '\n')
        self.child.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send(dict(id=request_id, method=method, params=params))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.lifecycle.stopping:
                raise ValueError
            if self.child not in self.lifecycle._active():
                raise ValueError
            try:
                reply = self.messages.get(timeout=.05)
            except queue.Empty:
                continue
            if not isinstance(reply, dict):
                raise ValueError
            if reply.get('id') == request_id:
                if 'error' in reply or 'result' not in reply:
                    raise ValueError
                return reply['result']
            # Server notifications do not constitute a fresh read response.
            # Unexpected requests get an explicit method-not-found response.
            if 'id' in reply and 'method' in reply:
                self.send(dict(id=reply['id'], error=dict(code=-32601, message='Read-only quota client')))
        raise ValueError

    def start(self):
        if self.child and self.child in self.lifecycle._active():
            return
        self.close()
        self.child = self.lifecycle.submit([self.config.executable, 'app-server'], service=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.messages = queue.Queue(maxsize=100)
        self.reader = threading.Thread(target=self.read, args=(self.child.stdout, self.messages), daemon=True)
        self.reader.start()
        self.rpc('initialize', dict(clientInfo=dict(name='codex_dispatcher', title='Codex Dispatcher', version='0.1.0')))
        self.send(dict(method='initialized', params={}))

    def check(self, fresh=False):
        if not self.config.quota_enabled:
            return True
        if time.monotonic() < self.next_check and (not fresh or self.state != 'quota_available'):
            return self.state == 'quota_available'
        try:
            self.start()
            payload = self.rpc('account/rateLimits/read', {})
            self.state, resets = evaluate(payload, self.config.quota_min_remaining_percent)
            self.failures = 0
            delay = self.config.quota_check_interval_seconds
            if self.state == 'quota_paused':
                future = [r - time.time() + self.config.quota_retry_after_reset_seconds for r in resets if r > time.time()]
                if future:
                    delay = min(delay, min(future))
            self.next_check = time.monotonic() + delay
            self.observation(self.state, 'rate_limits_read' if self.state != 'quota_unknown' else 'no_supported_windows')
        except (OSError, ValueError, KeyError, TypeError, TaskError, RuntimeError):
            self.close()
            self.state = 'quota_unknown'
            self.failures += 1
            self.next_check = time.monotonic() + min(60, 2 ** min(self.failures, 6))
            self.observation(self.state, 'app_server_unavailable_or_unsupported')
        return self.state == 'quota_available'

    def exhausted(self):
        self.state = 'quota_paused'
        self.next_check = time.monotonic() + self.config.quota_check_interval_seconds
        self.observation(self.state, 'quota_exhausted')

    def close(self):
        if self.child:
            self.child.stdin.close()
            if self.reader:
                self.reader.join(timeout=2)
            # A stubborn server may ignore stdin EOF. Let Lifecycle drain it
            # naturally (or an explicit force stop cancel it); closing a buffered
            # stream while reader holds its lock could block the force loop.
            if not self.reader or not self.reader.is_alive():
                self.child.stdout.close()
            self.child = None
