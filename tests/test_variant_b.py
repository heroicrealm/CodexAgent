"""Portable natural drain supersedes mandatory variant B containment."""
from unittest.mock import Mock

import pytest

from dispatcher import containment
from dispatcher.detachment import StopController, request_in_transaction
from dispatcher.scheduler import Dispatcher
from dispatcher.runtime import Lifecycle, group_members
from dispatcher.timeout_diagnostic import TimeoutDiagnostics
from dispatcher.github import GitHubError
from test_stage04 import env, adapter, issue, register
from test_stage602 import store, PROJECT, running


def test_execution_without_cgroup_delegation(env, monkeypatch):
    config, project, *_ = env
    monkeypatch.setattr(containment, 'capabilities', lambda: dict(cgroup_v2=False, delegated_writable=False))
    # Any obsolete production gate call is a regression.
    monkeypatch.setattr(containment, 'require_control', lambda: pytest.fail('obsolete gate'))
    lifecycle = Lifecycle()
    d = Dispatcher(config, lifecycle, adapter([issue(project)]))
    try:
        register(d, project, [issue(project)])
        d.admit()
        assert len(d.active) == 1
    finally:
        lifecycle.cancel()
        d.close()


@pytest.mark.parametrize('failure', ['outage', 'lost_response'])
def test_timeout_outbox_survives_restart_and_lost_response(store, failure):
    execution, attempt = running(store)
    api = Mock()
    comments = []
    api.pages.side_effect = lambda endpoint: list(comments)
    def write(endpoint, method, payload):
        if failure == 'lost_response':
            comments.append(dict(id=51, **payload))
        raise GitHubError(kind='transport')
    api.write.side_effect = write
    outbox = TimeoutDiagnostics(store, api)
    outbox.record(attempt, 20, 10)
    outbox.record(attempt, 30, 10)
    outbox.flush()
    assert store.db.execute('SELECT delivered FROM timeout_outbox').fetchone()[0] == 0
    from dispatcher.storage import Store
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    reopened = Store(path)
    try:
        def success(endpoint, method, payload):
            comments.append(dict(id=52, **payload))
            return comments[-1]
        api.write.side_effect = success
        recovered = TimeoutDiagnostics(reopened, api)
        recovered.flush(); recovered.flush()
        assert len(comments) == 1
        saved = reopened.db.execute('SELECT * FROM timeout_outbox').fetchone()
        assert saved['delivered'] and saved['elapsed_seconds'] == 20
        assert execution.get(attempt)['state'] == 'running'
        assert reopened.queue()[0]['state'] == 'running'
    finally:
        reopened.close()


@pytest.mark.parametrize('reason', [None, 'ready_removed', 'emergency_stop'])
def test_deadline_and_labels_never_signal_codex(env, monkeypatch, reason):
    import json
    import time
    from pathlib import Path
    from test_stage04 import finish
    from test_detachment import request_stop
    config, project, control, *_ = env
    Path(config.executable).write_text(Path(config.executable).read_text().replace('time.sleep(60)', 'time.sleep(.4)'))
    control.write_text(json.dumps({'exec': 'sleep'}))
    lifecycle = Lifecycle()
    d = Dispatcher(config, lifecycle, adapter([issue(project)]))
    try:
        register(d, project, [issue(project)])
        d.admit()
        attempt = next(iter(d.active))
        d.active[attempt]['deadline'] = 0
        if reason:
            request_stop(d.store, reason)
        with monkeypatch.context() as guard:
            guard.setattr('os.killpg', lambda *args: pytest.fail('unexpected signal'))
            d.collect(); d.collect()
            assert d.execution.get(attempt)['state'] == 'running'
            assert group_members(d.active[attempt]['child'].pid)
            assert d.store.db.execute('SELECT count(*) FROM timeout_outbox').fetchone()[0] == 1
            finish(d)
        saved = d.execution.get(attempt)
        assert saved['exit_code'] == 0
        assert saved['error_code'] != 'codex_timeout'
        assert d.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
    finally:
        lifecycle.cancel()
        d.close()


def test_force_stop_during_detach_is_not_natural_success(env):
    import json
    from test_detachment import request_stop
    from test_stage04 import finish
    config, project, control, *_ = env
    control.write_text(json.dumps({'exec': 'sleep'}))
    lifecycle = Lifecycle(force_grace_timeout_seconds=1)
    d = Dispatcher(config, lifecycle, adapter([issue(project)]))
    try:
        register(d, project, [issue(project)])
        d.admit()
        attempt = next(iter(d.active))
        request_stop(d.store, 'emergency_stop')
        lifecycle.request_stop(True)
        finish(d)
        saved = d.execution.get(attempt)
        assert saved['state'] == 'interrupted' and saved['error_code'] == 'force_stop'
        assert not saved['pushed']
    finally:
        lifecycle.cancel()
        d.close()


def test_helper_deadline_waits_without_signals(monkeypatch):
    import subprocess
    import sys
    from dispatcher.process_wait import run
    with monkeypatch.context() as guard:
        guard.setattr('os.kill', lambda *args: pytest.fail('unexpected helper signal'))
        guard.setattr('os.killpg', lambda *args: pytest.fail('unexpected helper group signal'))
        with pytest.raises(subprocess.TimeoutExpired) as error:
            run([sys.executable, '-c', 'import time; time.sleep(.05); print("natural exit")'],
                timeout=.001, capture_output=True, text=True)
        assert error.value.output.strip() == 'natural exit'


def test_force_interrupts_daemon_helper_without_waiting_for_deadline():
    import sys
    import time
    from threading import Thread
    from dispatcher.process_wait import bind, run
    lifecycle = Lifecycle(force_grace_timeout_seconds=1)
    bind(lifecycle)
    def force():
        time.sleep(.1)
        lifecycle.request_stop(True)
    thread = Thread(target=force)
    thread.start()
    try:
        result = run([sys.executable, '-c', 'import time; time.sleep(60)'],
                     capture_output=True, text=True, timeout=120)
        assert result.returncode < 0
        assert lifecycle.forced and not lifecycle.children
    finally:
        thread.join()
        bind(None)
        lifecycle.cancel()
