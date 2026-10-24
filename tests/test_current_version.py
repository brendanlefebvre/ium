"""The current version comes from the running container, not saved state (spec Part 2)."""

import json
from unittest.mock import Mock, patch

import pytest

from docker_api import DockerAPIError
from ium import DockerImageUpdater, ImageState

IMAGE = 'linuxserver/sonarr'
CONTAINERS = [{'name': 'sonarr', 'id': 'abc123', 'state': 'running',
               'image_ref': f'{IMAGE}:latest'}]


@pytest.fixture
def updater(tmp_path):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"
    config = {
        "images": [{
            "image": IMAGE,
            "regex": r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
            "base_tag": "latest",
            "auto_update": True,
        }]
    }
    config_file.write_text(json.dumps(config))
    state_file.write_text("{}")
    return DockerImageUpdater(str(config_file), str(state_file))


def _seed(updater, tag, digest='sha256:olddigest'):
    updater.state[IMAGE] = ImageState(base_tag='latest', tag=tag, digest=digest,
                                      last_updated='2026-10-23T00:00:00')


def _run(updater, candidate, containers, current_tag=None, lookup_side_effect=None,
         progress_callback=None):
    lookup_kwargs = ({'side_effect': lookup_side_effect} if lookup_side_effect
                     else {'return_value': current_tag})
    with patch.object(updater, 'find_matching_tag', return_value=(candidate, 'sha256:newdigest')), \
         patch.object(updater, '_get_containers_for_image', return_value=containers), \
         patch.object(updater, '_get_container_current_tag', **lookup_kwargs) as lookup, \
         patch.object(updater, '_pull_image', return_value=True) as pull, \
         patch.object(updater, '_update_containers',
                      side_effect=lambda names, *a, **k: {n: True for n in names}) as update:
        updates = updater.check_and_update(progress_callback=progress_callback)
    return updates, lookup, pull, update


def test_container_tag_beats_saved_state(updater):
    _seed(updater, '4.0.0.700-ls1')
    updates, lookup, _, _ = _run(updater, '4.0.16.2944-ls330', CONTAINERS,
                                 current_tag='4.0.15.2941-ls329')
    assert updates[0]['old_tag'] == '4.0.15.2941-ls329'
    lookup.assert_called_once_with('sonarr', IMAGE, updater.config['images'][0]['regex'], None)


def test_orphan_container_is_unknown(updater):
    # Saved state already names the candidate, but the container's image has
    # no regex-matching local tag (orphaned): it must still be recreated.
    _seed(updater, '4.0.16.2944-ls330')
    updates, _, _, update = _run(updater, '4.0.16.2944-ls330', CONTAINERS, current_tag=None)
    assert updates[0]['old_tag'] == 'unknown'
    assert updates[0]['downgrade'] is False
    update.assert_called_once()


def test_no_containers_falls_back_to_state(updater):
    _seed(updater, '4.0.15.2941-ls329')
    updates, lookup, _, _ = _run(updater, '4.0.16.2944-ls330', [])
    assert updates[0]['old_tag'] == '4.0.15.2941-ls329'
    lookup.assert_not_called()


def test_sonarr_repush_hits_downgrade_guard(updater):
    # Saved state says ls1 (from a bad re-push); the container is really on
    # ls329; the registry offers ls1 again under a new digest.
    _seed(updater, '4.0.0.700-ls1')
    updates, _, _, update = _run(updater, '4.0.0.700-ls1', CONTAINERS,
                                 current_tag='4.0.15.2941-ls329')
    assert updates[0]['old_tag'] == '4.0.15.2941-ls329'
    assert updates[0]['downgrade'] is True
    assert updates[0]['auto_update'] is False
    update.assert_not_called()


def test_lookup_error_defers(updater):
    _seed(updater, '4.0.15.2941-ls329')
    callback = Mock()
    updates, _, pull, update = _run(
        updater, '4.0.16.2944-ls330', CONTAINERS,
        lookup_side_effect=DockerAPIError(0, 'connection reset'),
        progress_callback=callback)
    assert updates == []
    pull.assert_not_called()
    update.assert_not_called()
    errors = [c.args[1] for c in callback.call_args_list if c.args[0] == 'check_error']
    assert len(errors) == 1
    assert errors[0]['reason'] == 'docker_unreachable'
    assert updater.state[IMAGE].tag == '4.0.15.2941-ls329'
