"""Final regression: configuration boundaries, no live GitHub/Codex calls."""
import pytest

from dispatcher.preparation_error import parameters, record, blocked
from dispatcher.storage import Store
from dispatcher.workspace import GitWorkspaceManager, TaskError
from test_stage602 import store, PROJECT, item


@pytest.mark.parametrize(('body', 'expected'), [
    ('Requirements', {}),
    ("```codex-config\nbranch_name: '`id`'\n```", {'branch_name': '`id`'}),
    ('```yaml\nbranch_name: ignored\n```', {}),
    ('```codex-config\n{}\n```', {}),
    ('```codex-config\nbase_branch: release\n```', {'base_branch': 'release'}),
    ('```codex-config\nbranch_name: tasks/Исправить-API\n```', {'branch_name': 'tasks/Исправить-API'}),
    ('```codex-config\nbase_branch: release\nbranch_name: tasks/12345\n```',
     {'base_branch': 'release', 'branch_name': 'tasks/12345'}),
])
def test_configuration_partial_and_explicit_values(body, expected):
    assert parameters(body) == expected
    manager = GitWorkspaceManager.__new__(GitWorkspaceManager)
    manager.operation_guard = None
    import subprocess
    manager.run = subprocess.run
    for branch in expected.values():
        manager.validate_branch(branch)  # Real git check-ref-format, no network.


@pytest.mark.parametrize('value', ['null', 'true', '12345', '[]', '{}', "'../escape'",
    "'refs/heads/a..b'", "'@{-1}'", "'-option'", "'a.lock'", "'a b'", "'a@{b'",
    "'$(touch /tmp/should-not-run)'"])
def test_configuration_rejects_types_and_unsafe_refs(value):
    with pytest.raises(TaskError, match='invalid_codex_config'):
        parameters('```codex-config\nbranch_name: ' + value + '\n```')


def test_configuration_error_restart_requires_fresh_approval(store):
    body = '```codex-config\nbranch_name: [\n```'
    store.apply(PROJECT, [item(body=body)])
    assert store.confirm(PROJECT.repository, 1, 2)
    row = store.get(PROJECT.repository, 1)
    record(store, row, 'invalid_codex_config')
    record(store, row, 'invalid_codex_config')
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    recovered = Store(path)
    try:
        assert blocked(recovered, recovered.get(PROJECT.repository, 1))
        assert not recovered.confirm(PROJECT.repository, 1, 2)
        assert recovered.db.execute('SELECT count(*) FROM configuration_errors').fetchone()[0] == 1
        recovered.apply(PROJECT, [item(body='```codex-config\nbranch_name: tasks/fixed\n```')])
        current = recovered.get(PROJECT.repository, 1)
        assert current['state'] == 'discovered' and current['approved_version'] is None
        assert not blocked(recovered, current)
        assert not recovered.confirm(PROJECT.repository, 1, 2)
        assert recovered.confirm(PROJECT.repository, 1, 3)
        assert not recovered.db.execute('SELECT * FROM attempts').fetchall()
    finally:
        recovered.close()
