from datetime import datetime, timedelta, timezone
from dataclasses import replace
import pytest
from dispatcher.config import Config, Project, load_config
from dispatcher.diagnostics import DiagnosticLogger
from dispatcher.storage import Storage


def test_labels(tmp_path):
    p = Project('p', 'owner/new', {'review': 'custom-review'})
    q = Project('q', 'owner/other')
    c = Config(tmp_path / 'work', tmp_path / 'db', (p, q))
    assert c.effective_labels(p)['review'] == 'custom-review'
    assert c.effective_labels(q)['review'] == 'dispatcher:review'
    assert c.status_label(p, 'preparing') is None
    assert c.status_label(p, 'running') != c.status_label(p, 'manual-running')
    for labels in ({'bad': 'x'}, {'review': ''}, {'running': 'dispatcher:manual-running'}, {'approve': 'dispatcher:done'}):
        with pytest.raises(ValueError):
            replace(c, projects=(Project('bad', 'owner/new', labels),))
    with pytest.raises(ValueError):
        replace(c, workspace=__import__('pathlib').Path('V1/workspace'))
    assert load_config(__import__('pathlib').Path('config/config.example.yaml')).debug_mode == 'off'


@pytest.mark.parametrize('mode,expected', [('off', 0), ('api', 2), ('verbose', 3)])
def test_modes_and_safe_fields(tmp_path, mode, expected):
    store = Storage(tmp_path / 'db')
    log = DiagnosticLogger(store, mode)
    context = dict(poll_id='poll-1', repository='owner/new', issue_id=1, method='GET',
        endpoint='/repos/{owner}/{repo}/issues', duration_ms=10, http_status=200,
        token='secret', body='secret issue', stdout='secret output', error_message='secret error')
    log.record('api', 'GitHubAdapter', 'list_issues', context, 'ok')
    log.record('api', 'GitHubAdapter', 'list_issues', {**context, 'endpoint': 'https://host/?token=secret'}, 'retry')
    log.record('verbose', 'Scheduler', 'reserve', context, 'ok')
    with store.connection() as db:
        rows = db.execute('SELECT * FROM debug_operations').fetchall()
        assert len(rows) == expected
        assert 'secret' not in repr([tuple(r) for r in rows])
        assert all(r['poll_id'] == 'poll-1' for r in rows)
        assert db.execute("SELECT count(*) FROM debug_operations WHERE level='api' AND poll_id='poll-1'").fetchone()[0] == min(expected, 2)


def test_retention_throttling_and_failure(tmp_path):
    store = Storage(tmp_path / 'db')
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with store.transaction() as db:
        db.execute("INSERT INTO debug_operations(created_at,level,component,operation,result) VALUES (?,'api','a','b','ok')", (old,))
    log = DiagnosticLogger(store, 'verbose')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM debug_operations').fetchone()[0] == 0
        db.execute("INSERT INTO debug_operations(created_at,level,component,operation,result) VALUES (?,'api','a','b','ok')", (old,))
    log.cleanup()
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM debug_operations').fetchone()[0] == 1
    log.cleanup(datetime.now(timezone.utc) + timedelta(days=1, seconds=1))
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM debug_operations').fetchone()[0] == 0
        db.execute('DROP TABLE debug_operations')
    log.record('api', 'Adapter', 'read', {}, 'ok')
    log.cleanup(datetime.now(timezone.utc) + timedelta(days=3))
    # Main storage operations remain usable despite diagnostic failure.
    from dispatcher.models import IssueObservation
    store.register_issue(IssueObservation(1, 'owner/new', 1, 'title'), 'p')
    assert store.issue(1)
