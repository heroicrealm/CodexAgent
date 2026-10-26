"""Variant B refusal acceptance. No kernel backend acceptance is claimed."""
from unittest.mock import Mock

import pytest

from dispatcher import containment
from dispatcher.detachment import StopController, request_in_transaction
from dispatcher.scheduler import Dispatcher
from dispatcher.runtime import Lifecycle
from dispatcher.workspace import TaskError
from test_stage04 import env, adapter, issue, register
from test_stage602 import store, PROJECT, running
from test_stage603 import publication

# Capture the actual gate before legacy fixtures inject their business-test mock.
REAL_REQUIRE_CONTROL = containment.require_control


def test_kernel_capabilities_are_not_a_boundary_certificate(monkeypatch):
    monkeypatch.setattr(containment, 'capabilities', lambda: dict(cgroup_v2=True,delegated_writable=True))
    with pytest.raises(TaskError, match='descendant_boundary_unverified'):
        REAL_REQUIRE_CONTROL()


def test_production_admission_refuses_without_backend(env, monkeypatch):
    config,project,*_=env
    monkeypatch.setattr(containment,'require_control',REAL_REQUIRE_CONTROL)
    d=Dispatcher(config,Lifecycle(),adapter([issue(project)]))
    try:
        register(d,project,[issue(project)])
        d.admit(); d.admit()
        assert not d.active
        assert not d.store.db.execute('SELECT * FROM attempts').fetchall()
        assert d.store.queue()[0]['state']=='pending'
        assert 'descendant_boundary_unverified' in (config.logs/'dispatcher.jsonl').read_text()
    finally:
        d.close()


def test_legacy_publication_does_not_resume_without_backend(publication, monkeypatch):
    publisher,execution,attempt,api,git,*_=publication
    monkeypatch.setattr(containment,'require_control',REAL_REQUIRE_CONTROL)
    publisher.publish(PROJECT,execution.get(attempt))
    git.publication_commit.assert_not_called()
    git.push.assert_not_called()
    api.write.assert_not_called()


def test_unverified_stop_is_durable_and_diagnostic_is_deliverable(store):
    execution,attempt=running(store)
    row=store.queue()[0]
    with store.db:
        request_in_transaction(store,row,'emergency_stop',{})
    api=Mock()
    api.pages.return_value=[]
    api.write.return_value={'id':51}
    controller=StopController(None,store,api)
    assert not controller.finalize(row,recovered=True)
    controller.diagnostics()
    saved=store.db.execute('SELECT * FROM stop_requests').fetchone()
    assert saved['error']=='descendant_boundary_unverified'
    assert saved['process_status']=='unconfirmed' and not saved['finalized_at']
    assert store.db.execute('SELECT delivered FROM stop_outbox').fetchone()[0]==1
    assert 'descendant_boundary_unverified' in api.write.call_args.args[2]['body']


@pytest.mark.skip(reason='BLOCKED: strong boundary backend is unimplemented; current WSL cgroup is not delegated')
@pytest.mark.parametrize('scenario', ['setsid','setpgid','leader_exit','reparent','ordinary_drain',
                                     'emergency_term_kill','restart','concurrent_attempts'])
def test_real_kernel_boundary_acceptance(scenario):
    # Explicitly unexecuted requirements, not simulated with a directory or /proc.
    raise AssertionError('Requires an implemented backend and delegated integration environment')


def test_old_pgid_confirmation_cannot_reconnect_or_release_unknown_boundary(store):
    execution,attempt=running(store)
    with store.db:
        request_in_transaction(store,store.queue()[0],'ready_removed',{'observed_at':'2026-10-26T10:00:00Z'})
        store.db.execute("UPDATE attempts SET state='completed'")
        store.db.execute("UPDATE tasks SET state='detached'")
        store.db.execute("UPDATE stop_requests SET finalized_at='2026-10-26T10:01:00Z',process_status='confirmed'")
        store.db.execute("UPDATE cleanup_jobs SET state='completed'")
    from test_stage602 import item
    store.apply(PROJECT,[item(server_observed_at='2026-10-26T10:02:00Z')])
    assert store.queue()[0]['state']=='detached'
    saved=store.db.execute('SELECT * FROM stop_requests').fetchone()
    assert saved['active'] and saved['finalized_at'] is None
    assert saved['error']=='descendant_boundary_unverified'


def test_child_launches_refused_before_exec_or_prompt_files(tmp_path):
    from dispatcher.workspace import GitWorkspaceManager
    from dispatcher.codex import CodexExecutor
    lifecycle=Mock()
    config=Mock()
    manager=GitWorkspaceManager(config,lifecycle,Mock())
    manager.operation_attempt='unknown'
    with pytest.raises(TaskError,match='descendant_boundary_unverified'):
        manager.run(['never-execute'])
    with pytest.raises(TaskError,match='descendant_boundary_unverified'):
        CodexExecutor(config,lifecycle).start({},'branch',[],tmp_path,tmp_path,'unknown')
    lifecycle.submit.assert_not_called()
    assert list(tmp_path.iterdir())==[]
