"""Canonical awaiting-children labels across configuration and GitHub delivery."""
from dataclasses import replace
from unittest.mock import Mock

import pytest
import yaml

from dispatcher.config import COLORS, Label, load
from dispatcher.github import Adapter
from dispatcher.initialization import fingerprint, initialize, ready


@pytest.mark.parametrize('override', [None, 'custom:children', {
    'name': 'custom:waiting', 'color': '123ABC', 'description': 'Custom waiting',
}])
def test_awaiting_children_contract(tmp_path, override):
    path = tmp_path / 'config.yaml'
    labels = {} if override is None else {'awaiting_children': override}
    path.write_text(yaml.safe_dump({'projects': [{
        'id': 'one', 'enabled': True, 'repository': 'owner/repo', 'labels': labels,
    }]}))
    config = load(path)
    project = config.projects[0]
    expected = (Label('dispatcher:awaiting-children', 'FBCA04') if override is None
                else Label(override, 'FBCA04') if isinstance(override, str)
                else Label(**override))
    assert project.label('awaiting_children') == expected
    assert replace(project, labels={}).label('awaiting_children').name == 'dispatcher:awaiting-children'
    # Preserve every other loader default, including the historical review_on_error spelling.
    for key in COLORS:
        if key != 'awaiting_children':
            name = ('codex-ready' if key == 'ready' else 'dispatcher:emergency-stop'
                    if key == 'emergency_stop' else f'dispatcher:{key}')
            assert project.label(key).name == name

    legacy = replace(project, labels={**project.labels,
        'awaiting_children': Label('dispatcher:awaiting_children', 'FBCA04')})
    assert fingerprint(legacy) != fingerprint(project)
    stale = replace(project, init_status='success', init_fingerprint=fingerprint(legacy))
    assert not ready(stale)

    api = Mock(spec=Adapter)
    # Existing legacy and unrelated user labels must never be renamed or removed.
    api.labels.return_value = [{'name': 'dispatcher:awaiting_children'}, {'name': 'user-label'}]
    assert initialize(config, adapter=api) == [('one', True)]
    writes = api.write.call_args_list
    assert all(call.args[1] == 'POST' for call in writes)
    assert [call.args[2] for call in writes if call.args[2]['name'] == expected.name] == [
        {'name': expected.name, 'color': expected.color, 'description': expected.description}]
    initialized = load(path).projects[0]
    assert ready(initialized)
    assert initialized.init_fingerprint == fingerprint(project)
    explicit = replace(project, labels={**project.labels, 'awaiting_children': expected})
    assert fingerprint(explicit) == initialized.init_fingerprint

    adapter = Adapter()
    current = [project.task_label, 'dispatcher:awaiting_children', 'user-label']
    adapter.pages = Mock(return_value=[{'name': name} for name in current])
    adapter.write = Mock()
    adapter.reconcile(initialized, {'number': 1, 'state': 'awaiting_children'})
    adapter.write.assert_called_once_with('repos/owner/repo/issues/1/labels', 'POST',
                                          {'labels': [expected.name]})
    adapter.pages.return_value.append({'name': expected.name})
    adapter.write.reset_mock()
    adapter.reconcile(initialized, {'number': 1, 'state': 'awaiting_children'})
    adapter.write.assert_not_called()


def test_other_label_overrides_are_preserved(tmp_path):
    path = tmp_path / 'config.yaml'
    labels = {key: {'name': f'custom:{key}', 'color': 'ABCDEF', 'description': key}
              for key in COLORS}
    path.write_text(yaml.safe_dump({'projects': [{
        'id': 'one', 'enabled': True, 'repository': 'owner/repo', 'labels': labels,
    }]}))
    project = load(path).projects[0]
    for key, value in labels.items():
        assert project.label(key) == Label(**value)
    assert project.task_label == labels['ready']['name']
