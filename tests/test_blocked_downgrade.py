"""Blocked downgrade stays pending (spec Part 4, §4.1-4.3).

A candidate older than the running version is reported but never applied,
and must never advance saved state: a ``pending`` record is written instead,
re-reported every cycle (``repeat``), notified at most once per 24 h,
silenced by ``dismissed``, and dropped when the registry moves on.
"""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from ium import DockerImageUpdater, ImageState

IMAGE = 'linuxserver/sonarr'
CURRENT = '4.0.20.3014-ls329'
CURRENT_DIGEST = 'sha256:d329'
DOWN = '4.0.20.3014-ls1'
DOWN_DIGEST = 'sha256:d1'
NOW = datetime(2026, 10, 24, 12, 0, 0)
NOW_ISO = NOW.isoformat()
CONTAINERS = [{'name': 'sonarr', 'id': 'abc123', 'state': 'running',
               'image_ref': f'{IMAGE}:{CURRENT}'}]


def _make(tmp_path, dry_run=False):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"
    config_file.write_text(json.dumps({
        "images": [{
            "image": IMAGE,
            "regex": r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
            "base_tag": "latest",
            "auto_update": True,
        }]
    }))
    if not state_file.exists():
        state_file.write_text("{}")
    return DockerImageUpdater(str(config_file), str(state_file), dry_run=dry_run)


@pytest.fixture
def updater(tmp_path):
    return _make(tmp_path)


def _seed(updater, pending=None):
    updater.state[IMAGE] = ImageState(
        base_tag='latest', tag=CURRENT, digest=CURRENT_DIGEST,
        last_updated='2026-10-01T00:00:00', pending=pending)


def _downgrade_pending(first_seen=NOW_ISO, last_notified=NOW_ISO, dismissed=False):
    return {'tag': DOWN, 'digest': DOWN_DIGEST, 'first_seen': first_seen,
            'reason': 'downgrade', 'dismissed': dismissed,
            'last_notified': last_notified}


def _run(updater, candidate=(DOWN, DOWN_DIGEST), now=NOW):
    with patch.object(updater, 'find_matching_tag', return_value=candidate), \
         patch.object(updater, '_get_containers_for_image', return_value=CONTAINERS), \
         patch.object(updater, '_get_container_current_tag', return_value=CURRENT), \
         patch.object(updater, '_pull_image', return_value=True) as pull, \
         patch.object(updater, '_update_containers',
                      return_value={'sonarr': True}) as upd, \
         patch('ium.send_notifications') as notify, \
         patch.object(updater, '_now', return_value=now):
        updates = updater.check_and_update()
    return updates, notify, upd, pull


def _reload(updater):
    return updater._load_state()[IMAGE]


def test_first_downgrade_writes_pending_not_tag(updater):
    _seed(updater)
    updates, notify, upd, pull = _run(updater)

    state = _reload(updater)
    assert state.tag == CURRENT
    assert state.digest == CURRENT_DIGEST
    assert state.pending == _downgrade_pending()
    assert notify.call_count == 1
    assert notify.call_args.kwargs['event'] == 'update_found'
    assert len(updates) == 1
    assert updates[0]['repeat'] is False
    assert updates[0]['downgrade'] is True
    assert updates[0]['auto_update'] is False
    upd.assert_not_called()
    pull.assert_not_called()


def test_second_cycle_repeats_without_notify(updater):
    _seed(updater, pending=_downgrade_pending())
    updates, notify, upd, _ = _run(updater, now=NOW + timedelta(hours=1))

    assert len(updates) == 1
    assert updates[0]['repeat'] is True
    assert updates[0]['downgrade'] is True
    notify.assert_not_called()
    upd.assert_not_called()
    state = _reload(updater)
    assert state.tag == CURRENT
    assert state.pending['first_seen'] == NOW_ISO
    assert state.pending['last_notified'] == NOW_ISO


def test_renotifies_after_24h(updater):
    _seed(updater, pending=_downgrade_pending())
    later = NOW + timedelta(hours=25)
    updates, notify, upd, _ = _run(updater, now=later)

    assert notify.call_count == 1
    assert updates[0]['repeat'] is True
    upd.assert_not_called()
    state = _reload(updater)
    assert state.pending['last_notified'] == later.isoformat()
    assert state.pending['first_seen'] == NOW_ISO
    assert state.tag == CURRENT


def test_dismissed_is_silent(updater):
    _seed(updater, pending=_downgrade_pending(dismissed=True))
    updates, notify, upd, _ = _run(updater, now=NOW + timedelta(hours=48))

    assert updates == []
    notify.assert_not_called()
    upd.assert_not_called()
    state = _reload(updater)
    assert state.tag == CURRENT
    assert state.digest == CURRENT_DIGEST
    assert state.pending == _downgrade_pending(dismissed=True)


def test_registry_moves_on_clears_pending(updater):
    _seed(updater, pending=_downgrade_pending())
    updates, notify, upd, _ = _run(
        updater, candidate=('4.0.20.3014-ls330', 'sha256:d330'))

    upd.assert_called_once()
    assert updates[0]['new_tag'] == '4.0.20.3014-ls330'
    assert updates[0]['downgrade'] is False
    state = _reload(updater)
    assert state.tag == '4.0.20.3014-ls330'
    assert state.digest == 'sha256:d330'
    assert state.pending is None


def test_different_downgrade_digest_resets(updater):
    _seed(updater, pending=_downgrade_pending(dismissed=True))
    later = NOW + timedelta(hours=2)
    updates, notify, upd, _ = _run(
        updater, candidate=('4.0.20.3014-ls2', 'sha256:d2'), now=later)

    assert notify.call_count == 1
    assert len(updates) == 1
    assert updates[0]['repeat'] is False
    upd.assert_not_called()
    state = _reload(updater)
    assert state.tag == CURRENT
    assert state.pending == {
        'tag': '4.0.20.3014-ls2', 'digest': 'sha256:d2',
        'first_seen': later.isoformat(), 'reason': 'downgrade',
        'dismissed': False, 'last_notified': later.isoformat()}


def test_unknown_reason_pending_left_alone(updater):
    cooldown = {'tag': '4.0.20.3014-ls330', 'digest': 'sha256:d330',
                'first_seen': NOW_ISO, 'reason': 'cooldown'}
    _seed(updater, pending=cooldown)
    updates, notify, upd, _ = _run(
        updater, candidate=('4.0.20.3014-ls330', 'sha256:d330'))

    upd.assert_called_once()
    assert updates[0]['downgrade'] is False
    assert notify.call_count == 1
    state = _reload(updater)
    assert state.tag == '4.0.20.3014-ls330'
    assert state.pending is None


def test_first_run_downgrade_writes_placeholder(updater):
    assert IMAGE not in updater.state
    updates, notify, upd, _ = _run(updater)

    upd.assert_not_called()
    assert notify.call_count == 1
    assert updates[0]['repeat'] is False
    state = _reload(updater)
    assert state.tag == CURRENT
    assert state.digest == ''
    assert state.last_updated == NOW_ISO
    assert state.pending == _downgrade_pending()


def test_dry_run_persists_nothing(tmp_path):
    seeded = _make(tmp_path)
    _seed(seeded)
    seeded._save_state()
    state_file = tmp_path / "state.json"
    before = state_file.read_bytes()

    dry = _make(tmp_path, dry_run=True)
    assert dry.state[IMAGE].tag == CURRENT
    _, _, upd, _ = _run(dry)

    upd.assert_not_called()
    assert state_file.read_bytes() == before


def test_state_round_trip(updater):
    _seed(updater)
    _run(updater)
    first = _reload(updater)

    updater.state = updater._load_state()
    updater._save_state()
    second = _reload(updater)

    assert asdict(first) == asdict(second)
    assert second.pending == _downgrade_pending()
