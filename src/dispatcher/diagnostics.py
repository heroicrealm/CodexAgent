"""Best-effort diagnostics with deliberately restricted, content-free fields."""
from datetime import datetime, timedelta, timezone
import re
import threading
from collections.abc import Mapping
from .storage import Storage

# Free text is intentionally excluded: arbitrary errors, URLs and bodies are unsafe.
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# Only route templates, not actual URL/query/path values.
_ROUTE = re.compile(r"/(?:[a-z-]+|\{[a-z_]+\})(?:/(?:[a-z-]+|\{[a-z_]+\}))*\Z")


class DiagnosticLogger:
    def __init__(self, storage: Storage, mode: str = "off", retention_days: int = 7):
        if mode not in {'off', 'api', 'verbose'} or retention_days <= 0:
            raise ValueError("Invalid diagnostic configuration")
        self.storage, self.mode, self.retention_days = storage, mode, retention_days
        self._last_cleanup: datetime | None = None
        self._lock = threading.Lock()
        self.cleanup()

    def cleanup(self, now: datetime | None = None) -> None:
        try:
            with self._lock:
                now = now or datetime.now(timezone.utc)
                if self._last_cleanup and now - self._last_cleanup < timedelta(days=1):
                    return
                self._last_cleanup = now  # also throttle failed attempts
                with self.storage.transaction() as db:
                    db.execute("DELETE FROM debug_operations WHERE julianday(created_at)<julianday(?)",
                               ((now - timedelta(days=self.retention_days)).isoformat(),))
        except Exception:
            pass

    def record(self, level: str, component: str, operation: str,
               context: Mapping[str, object], result: str) -> None:
        if self.mode == 'off' or level not in {'api', 'verbose'} or (self.mode == 'api' and level != 'api'):
            return
        try:
            if not all(isinstance(v, str) and _CODE.fullmatch(v) for v in (component, operation, result)):
                return
            safe: dict[str, object] = {}
            for key in ('issue_id', 'duration_ms', 'http_status'):
                value = context.get(key)
                if type(value) is int and value >= 0:
                    safe[key] = value
            for key, pattern in (('repository', _REPO), ('poll_id', _CODE), ('endpoint', _ROUTE)):
                value = context.get(key)
                if isinstance(value, str) and pattern.fullmatch(value):
                    safe[key] = value
            if context.get('method') in {'GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'HEAD'}:
                safe['method'] = context['method']
            self.cleanup()
            with self.storage.transaction() as db:
                columns = ['created_at', 'level', 'component', 'operation', 'result', *safe]
                values = [datetime.now(timezone.utc).isoformat(), level, component, operation, result, *safe.values()]
                db.execute(f"INSERT INTO debug_operations({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
        except Exception:
            pass
