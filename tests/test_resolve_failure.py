"""Resolver failure streaks and escalation (spec Part 7, §7.1, §7.2, §7.4, §7.6).

Every failing cycle bumps a per-image ``resolve_failure`` record; nothing is
sent before 24 h of continuous failure, then daily through day 7, then
weekly.  Success clears the record and, only if a failure was notified,
sends one ``resolve_recovered``.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from docker_api import DockerAPIError
from ium import DockerImageUpdater, ImageState, ResolveFailure

IMAGE = 'linuxserver/qbittorrent'
TAG = '5.0.0-r0-ls400'
DIGEST = 'sha256:d400'
BASE_DIGEST = 'sha256:base'
NOW = datetime(2026, 10, 24, 12, 0, 0)
NOW_ISO = NOW.isoformat()
CONTAINERS = [{'name': 'qbittorrent', 'id': 'abc123', 'state': 'running',
               'image_ref': f'{IMAGE}:{TAG}'}]


def _make(tmp_path, dry_run=False, auto_update=True):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"
    config_file.write_text(json.dumps({
        "images": [{
            "image": IMAGE,
            "regex": r"^[0-9]+\.[0-9]+\.[0-9]+-r[0-9]+-ls[0-9]+$",
            "base_tag": "latest",
            "auto_update": auto_update,
        }]
    }))
    if not state_file.exists():
        state_file.write_text("{}")
    return DockerImageUpdater(str(config_file), str(state_file), dry_run=dry_run)


@pytest.fixture
def updater(tmp_path):
    return _make(tmp_path)


def _seed(updater, resolve_failure=None, tag=TAG, digest=DIGEST, pending=None):
    updater.state[IMAGE] = ImageState(
        base_tag='latest', tag=tag, digest=digest,
        last_updated='2026-10-01T00:00:00', pending=pending,
        resolve_failure=resolve_failure)


def _fail(updater, now=NOW, failure=ResolveFailure.NO_REGEX_MATCH, callback=None):
    """Run one cycle in which the resolver fails with ``failure``."""
    def fake_find(*args, **kwargs):
        updater._last_resolve_failure = failure
        updater._last_base_digest = BASE_DIGEST
        return None

    with patch.object(updater, 'find_matching_tag', side_effect=fake_find), \
         patch('ium.send_notifications') as notify, \
         patch.object(updater, '_now', return_value=now):
        updater.check_and_update(progress_callback=callback)
    return notify


def _succeed(updater, candidate=(TAG, DIGEST), now=NOW):
    with patch.object(updater, 'find_matching_tag', return_value=candidate), \
         patch.object(updater, '_get_containers_for_image', return_value=CONTAINERS), \
         patch.object(updater, '_get_container_current_tag', return_value=TAG), \
         patch.object(updater, '_pull_image', return_value=True), \
         patch.object(updater, '_update_containers',
                      return_value={'qbittorrent': True}) as upd, \
         patch('ium.send_notifications') as notify, \
         patch.object(updater, '_now', return_value=now), \
         patch.object(updater.logger, 'info', wraps=updater.logger.info) as info:
        updates = updater.check_and_update()
    updater.info_lines = [str(c.args[0]) for c in info.call_args_list]
    return updates, notify, upd


def _reload(updater):
    return updater._load_state()[IMAGE]


def _sends(notify, event='resolve_failure'):
    return [c for c in notify.call_args_list if c.kwargs.get('event') == event]


def test_first_failure_creates_record(updater):
    _seed(updater)
    notify = _fail(updater)

    notify.assert_not_called()
    state = _reload(updater)
    assert state.resolve_failure == {
        'reason': 'NO_REGEX_MATCH', 'since': NOW_ISO,
        'cycles': 1, 'last_notified': None}
    # tag/digest/last_updated preserved on a failing cycle
    assert state.tag == TAG
    assert state.digest == DIGEST
    assert state.last_updated == '2026-10-01T00:00:00'


def test_failing_cycle_preserves_pending(updater):
    pending = {'tag': 'x', 'digest': 'sha256:x', 'first_seen': NOW_ISO,
               'reason': 'downgrade', 'dismissed': False, 'last_notified': None}
    _seed(updater, pending=pending)
    _fail(updater)
    assert _reload(updater).pending == pending


def test_reason_updates_since_stays(updater):
    _seed(updater)
    _fail(updater)
    _fail(updater, now=NOW + timedelta(hours=1),
          failure=ResolveFailure.NO_DIGEST_MATCH)

    rf = _reload(updater).resolve_failure
    assert rf['reason'] == 'NO_DIGEST_MATCH'
    assert rf['since'] == NOW_ISO
    assert rf['cycles'] == 2


def test_no_notify_before_24h(updater):
    _seed(updater)
    _fail(updater)
    notify = _fail(updater, now=NOW + timedelta(hours=23))
    notify.assert_not_called()
    assert _reload(updater).resolve_failure['last_notified'] is None


@pytest.mark.parametrize('tick_hours', [1, 12])
def test_daily_then_weekly(updater, tick_hours):
    """Walk a failing image hourly (or every 12 h) through day 15."""
    _seed(updater)
    # (hours since start) -> cumulative resolve_failure sends at that point
    table = {
        23: 0, 24: 1, 36: 1, 47: 1, 48: 2, 72: 3, 96: 4, 120: 5, 144: 6,
        167: 6, 168: 7, 192: 7, 300: 7, 335: 7, 336: 8, 360: 8,
    }
    total = 0
    for h in range(0, 361, tick_hours):
        notify = _fail(updater, now=NOW + timedelta(hours=h))
        sent = _sends(notify)
        total += len(sent)
        for c in sent:
            assert c.kwargs['old_version'] == TAG
            assert c.kwargs['new_version'] == 'latest'
            assert c.kwargs['digest'] == BASE_DIGEST
            assert c.kwargs['auto_update'] is True
            assert IMAGE in c.kwargs['message']
            assert 'latest' in c.kwargs['message']
            assert ResolveFailure.NO_REGEX_MATCH.value in c.kwargs['message']
        if h in table:
            assert total == table[h], f"at {h} h"

    rf = _reload(updater).resolve_failure
    assert rf['since'] == NOW_ISO
    assert rf['last_notified'] == (NOW + timedelta(hours=336)).isoformat()


def test_recovered_after_notify(updater):
    _seed(updater, resolve_failure={
        'reason': 'NO_REGEX_MATCH', 'since': NOW_ISO, 'cycles': 30,
        'last_notified': (NOW + timedelta(hours=24)).isoformat()})
    new_tag, new_digest = '5.0.1-r0-ls401', 'sha256:d401'
    _, notify, _ = _succeed(updater, candidate=(new_tag, new_digest),
                            now=NOW + timedelta(hours=30))

    recovered = _sends(notify, 'resolve_recovered')
    assert len(recovered) == 1
    kw = recovered[0].kwargs
    assert kw['new_version'] == new_tag
    assert kw['old_version'] == TAG
    assert kw['digest'] == new_digest
    assert kw['auto_update'] is True
    assert IMAGE in kw['message'] and new_tag in kw['message']
    assert 'RESOLVED AGAIN: linuxserver/qbittorrent after 1d 6h' in updater.info_lines
    state = _reload(updater)
    assert state.resolve_failure is None
    assert state.tag == new_tag


def test_recovered_without_notify_is_silent(updater):
    _seed(updater, resolve_failure={
        'reason': 'NO_REGEX_MATCH', 'since': NOW_ISO, 'cycles': 3,
        'last_notified': None})
    _, notify, _ = _succeed(updater, now=NOW + timedelta(hours=3))

    assert _sends(notify, 'resolve_recovered') == []
    assert 'RESOLVED AGAIN: linuxserver/qbittorrent after 3h 0m' in updater.info_lines
    assert _reload(updater).resolve_failure is None


@pytest.mark.parametrize('auto_update', [True, False])
def test_success_unchanged_digest_still_clears(tmp_path, auto_update):
    updater = _make(tmp_path, auto_update=auto_update)
    _seed(updater, resolve_failure={
        'reason': 'NO_DIGEST_MATCH', 'since': NOW_ISO, 'cycles': 40,
        'last_notified': (NOW + timedelta(hours=24)).isoformat()})
    updates, notify, upd = _succeed(updater, now=NOW + timedelta(hours=40))

    assert updates == []
    upd.assert_not_called()
    recovered = _sends(notify, 'resolve_recovered')
    assert len(recovered) == 1
    assert recovered[0].kwargs['auto_update'] is auto_update
    state = _reload(updater)
    assert state.resolve_failure is None
    assert state.tag == TAG
    assert state.digest == DIGEST
    assert state.last_updated == '2026-10-01T00:00:00'


def test_clear_survives_docker_unreachable(updater):
    """The clear runs before the Part 1.1 deferral ``continue``."""
    _seed(updater, resolve_failure={
        'reason': 'NO_REGEX_MATCH', 'since': NOW_ISO, 'cycles': 2,
        'last_notified': None})
    with patch.object(updater, 'find_matching_tag',
                      return_value=('5.0.1-r0-ls401', 'sha256:d401')), \
         patch.object(updater, '_get_containers_for_image',
                      side_effect=DockerAPIError(0, 'down')), \
         patch('ium.send_notifications'), \
         patch.object(updater, '_now', return_value=NOW):
        updater.check_and_update()
    state = _reload(updater)
    assert state.resolve_failure is None
    assert state.tag == TAG


def test_failing_since_in_check_error(updater):
    _seed(updater)
    events = []

    def cb(event, data):
        if event == 'check_error':
            events.append(data)

    _fail(updater, callback=cb)
    _fail(updater, now=NOW + timedelta(hours=1), callback=cb)

    assert 'failing_since' not in events[0]
    assert 'failing_cycles' not in events[0]
    assert events[0]['reason'] == 'no_regex_match'
    assert events[1]['failing_since'] == NOW_ISO
    assert events[1]['failing_cycles'] == 2


def test_never_resolved_image_gets_placeholder_state(updater):
    assert IMAGE not in updater.state
    _fail(updater)
    state = _reload(updater)
    assert state.tag == 'unknown'
    assert state.digest == ''
    assert state.base_tag == 'latest'
    assert state.last_updated == NOW_ISO
    assert state.resolve_failure['cycles'] == 1


def test_never_resolved_notification_uses_unknown(updater):
    _fail(updater)
    notify = _fail(updater, now=NOW + timedelta(hours=24))
    sent = _sends(notify)
    assert len(sent) == 1
    assert sent[0].kwargs['old_version'] == 'unknown'


def test_dry_run_persists_nothing(tmp_path):
    seeded = _make(tmp_path)
    _seed(seeded)
    seeded._save_state()
    state_file = tmp_path / "state.json"
    before = state_file.read_bytes()

    dry = _make(tmp_path, dry_run=True)
    _fail(dry)
    _fail(dry, now=NOW + timedelta(hours=25))
    assert state_file.read_bytes() == before


def test_internal_failure_bookkeeping(updater):
    _seed(updater)
    _fail(updater, failure=None)  # defensive path: no reason recorded
    assert _reload(updater).resolve_failure['reason'] == 'INTERNAL'


def test_record_round_trips(updater):
    _seed(updater)
    _fail(updater)
    first = _reload(updater)
    updater.state = updater._load_state()
    updater._save_state()
    assert _reload(updater) == first
