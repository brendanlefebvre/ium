"""Resolver failure streaks, escalation and diagnosis (spec Part 7, §7.1-§7.4, §7.6).

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
from ium import DigestStatus, DockerImageUpdater, ImageState, ResolveFailure

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


# --- Diagnosis (spec §7.3) -------------------------------------------------

REGEX = r"^[0-9]+\.[0-9]+\.[0-9]+-r[0-9]+-ls[0-9]+$"
STALE_TAG = '5.3.0_v2.1.2-ls484'   # what qbittorrent's `latest` really is
HUB_TAGS = ['5.0.0-r0-ls399', '5.0.0-r0-ls400', 'latest', STALE_TAG]


def _rf(since, cycles, diag=None, last_notified=None):
    rf = {'reason': 'NO_DIGEST_MATCH', 'since': since.isoformat(),
          'cycles': cycles, 'last_notified': last_notified}
    if diag is not None:
        rf['diag'] = diag
    return rf


def _diag(checked_at, outcome='transient', sharing=(), base_digest=BASE_DIGEST):
    return {'base_digest': base_digest, 'checked_at': checked_at.isoformat(),
            'outcome': outcome, 'sharing_tags': list(sharing)}


def _diag_fail(updater, now=NOW, tags=HUB_TAGS, sharing=(STALE_TAG,),
               base_digest=BASE_DIGEST, failure=ResolveFailure.NO_DIGEST_MATCH):
    """One failing cycle with the diagnosis sweep's registry calls faked.

    ``sharing`` lists the candidate tags whose digest equals the base digest.
    Returns (notify mock, tag-listing mock, HEADed tags, check_error payloads).
    """
    def fake_find(*args, **kwargs):
        updater._last_resolve_failure = failure
        updater._last_base_digest = base_digest
        return None

    def fake_head(registry, namespace, repo, tag, token):
        heads.append(tag)
        return (base_digest if tag in sharing else f'sha256:{tag}', DigestStatus.OK)

    heads, events = [], []
    with patch.object(updater, 'find_matching_tag', side_effect=fake_find), \
         patch.object(updater, '_get_all_tags_by_date', return_value=list(tags)) as listing, \
         patch.object(updater, '_get_manifest_digest_head', side_effect=fake_head), \
         patch.object(updater, '_get_docker_token', return_value='tok'), \
         patch('ium.send_notifications') as notify, \
         patch.object(updater, '_now', return_value=now):
        updater.check_and_update(
            progress_callback=lambda e, d: events.append(d) if e == 'check_error' else None)
    return notify, listing, heads, events


# Gate: rf is seeded with cycles one short; the run bumps it.

def test_gate_cycle1_no(updater):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(hours=3), 0))
    _, listing, _, _ = _diag_fail(updater)
    listing.assert_not_called()


def test_gate_cycle2_90min_no(updater):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(minutes=90), 1))
    _, listing, _, _ = _diag_fail(updater)
    listing.assert_not_called()


def test_gate_cycle2_2h_yes(updater):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(hours=2), 1))
    _, listing, _, _ = _diag_fail(updater)
    assert listing.call_count == 1
    assert _reload(updater).resolve_failure['diag']['checked_at'] == NOW_ISO


def test_gate_same_digest_within_24h_no(updater):
    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=48), 40, diag=_diag(NOW - timedelta(hours=23, minutes=59))))
    _, listing, _, _ = _diag_fail(updater)
    listing.assert_not_called()

    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=48), 40, diag=_diag(NOW - timedelta(hours=24))))
    _, listing, _, _ = _diag_fail(updater)
    assert listing.call_count == 1


def test_gate_digest_changed_5h_no(updater):
    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=48), 40,
        diag=_diag(NOW - timedelta(hours=5), base_digest='sha256:older')))
    _, listing, _, _ = _diag_fail(updater)
    listing.assert_not_called()


def test_gate_digest_changed_6h_yes(updater):
    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=48), 40,
        diag=_diag(NOW - timedelta(hours=6), base_digest='sha256:older')))
    _, listing, _, _ = _diag_fail(updater)
    assert listing.call_count == 1
    assert _reload(updater).resolve_failure['diag']['base_digest'] == BASE_DIGEST


@pytest.mark.parametrize('failure', [f for f in ResolveFailure
                                     if f is not ResolveFailure.NO_DIGEST_MATCH])
def test_other_failures_never_sweep(updater, failure):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(hours=30), 30))
    _, listing, heads, _ = _diag_fail(updater, failure=failure)
    listing.assert_not_called()
    assert heads == []
    assert 'diag' not in _reload(updater).resolve_failure


def test_unknown_base_digest_never_sweeps(updater):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(hours=30), 30))
    _, listing, _, _ = _diag_fail(updater, base_digest=None)
    listing.assert_not_called()


# Outcomes

def test_qbittorrent_regex_stale(updater):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(hours=3), 2))
    notify, listing, _, events = _diag_fail(updater)

    assert listing.call_count == 1
    rf = _reload(updater).resolve_failure
    assert rf['diag'] == {'base_digest': BASE_DIGEST, 'checked_at': NOW_ISO,
                          'outcome': 'regex_stale', 'sharing_tags': [STALE_TAG]}
    assert rf['reason'] == 'NO_DIGEST_MATCH'  # raw reason stays stored
    assert rf['last_notified'] == NOW_ISO
    sent = _sends(notify)
    assert len(sent) == 1
    kw = sent[0].kwargs
    assert STALE_TAG in kw['message'] and REGEX in kw['message']
    assert kw['old_version'] == TAG
    assert kw['new_version'] == 'latest'
    assert kw['digest'] == BASE_DIGEST
    assert kw['auto_update'] is True
    refined = ResolveFailure.REGEX_STALE.value.format(tags=STALE_TAG, regex=REGEX)
    assert events[0]['reason'] == 'regex_stale'
    assert refined in events[0]['error']


def test_gluetun_rolling_base(updater):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(hours=3), 2))
    notify, _, _, events = _diag_fail(updater, sharing=())

    diag = _reload(updater).resolve_failure['diag']
    assert diag['outcome'] == 'rolling_base'
    assert diag['sharing_tags'] == []
    sent = _sends(notify)
    assert len(sent) == 1
    assert 'rolling' in sent[0].kwargs['message']
    assert '`latest`' in sent[0].kwargs['message']
    assert events[0]['reason'] == 'rolling_base'


def test_transient_no_notify(updater):
    _seed(updater, resolve_failure=_rf(NOW - timedelta(hours=3), 2))
    notify, _, _, events = _diag_fail(updater, sharing=(STALE_TAG, '5.0.0-r0-ls400'))

    rf = _reload(updater).resolve_failure
    assert rf['diag']['outcome'] == 'transient'
    assert rf['last_notified'] is None
    notify.assert_not_called()
    assert events[0]['reason'] == 'no_digest_match'
    assert ResolveFailure.NO_DIGEST_MATCH.value in events[0]['error']


def test_same_finding_no_second_notify(updater):
    last = (NOW - timedelta(hours=1)).isoformat()
    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=30), 30, last_notified=last,
        diag=_diag(NOW - timedelta(hours=24), 'regex_stale', [STALE_TAG])))
    notify, listing, _, _ = _diag_fail(updater)

    assert listing.call_count == 1  # it did re-sweep
    notify.assert_not_called()
    rf = _reload(updater).resolve_failure
    assert rf['last_notified'] == last
    assert rf['diag']['checked_at'] == NOW_ISO


def test_new_sharing_set_notifies_again(updater):
    last = (NOW - timedelta(hours=1)).isoformat()
    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=30), 30, last_notified=last,
        diag=_diag(NOW - timedelta(hours=24), 'regex_stale', ['5.3.0_v2.1.2-ls483'])))
    notify, _, _, _ = _diag_fail(updater)

    sent = _sends(notify)
    assert len(sent) == 1
    assert STALE_TAG in sent[0].kwargs['message']
    assert _reload(updater).resolve_failure['last_notified'] == NOW_ISO


def test_refined_reason_persists_between_sweeps(updater):
    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=5), 5,
        diag=_diag(NOW - timedelta(hours=1), 'regex_stale', [STALE_TAG])))
    notify, listing, _, events = _diag_fail(updater)

    listing.assert_not_called()
    notify.assert_not_called()
    refined = ResolveFailure.REGEX_STALE.value.format(tags=STALE_TAG, regex=REGEX)
    assert refined in events[0]['error']
    assert events[0]['reason'] == 'regex_stale'


def test_raw_reason_after_digest_moves_before_sweep(updater):
    _seed(updater, resolve_failure=_rf(
        NOW - timedelta(hours=5), 5,
        diag=_diag(NOW - timedelta(hours=1), 'regex_stale', [STALE_TAG])))
    _, listing, _, events = _diag_fail(updater, base_digest='sha256:moved')

    listing.assert_not_called()  # 6 h floor
    assert events[0]['reason'] == 'no_digest_match'
    assert ResolveFailure.NO_DIGEST_MATCH.value in events[0]['error']


def test_sweep_does_not_mutate_saved_record(updater):
    old = _diag(NOW - timedelta(hours=25), 'regex_stale', ['5.3.0_v2.1.2-ls483'])
    seeded = _rf(NOW - timedelta(hours=30), 30, diag=old)
    _seed(updater, resolve_failure=seeded)
    _diag_fail(updater)
    assert seeded['diag'] is old
    assert old['sharing_tags'] == ['5.3.0_v2.1.2-ls483']
    assert old['checked_at'] == (NOW - timedelta(hours=25)).isoformat()
    assert seeded['cycles'] == 30


# Candidate set (unit level)

def _diagnose(updater, tags=None, all_tags=None, registry_override=None, sharing=()):
    heads = []

    def fake_head(registry, namespace, repo, tag, token):
        heads.append(tag)
        return (BASE_DIGEST if tag in sharing else f'sha256:{tag}', DigestStatus.OK)

    with patch.object(updater, '_get_manifest_digest_head', side_effect=fake_head), \
         patch.object(updater, '_get_docker_token', return_value='tok'), \
         patch.object(updater, '_get_all_tags', return_value=list(all_tags or [])) as plain, \
         patch.object(updater, '_now', return_value=NOW):
        if tags is not None:
            with patch.object(updater, '_get_all_tags_by_date', return_value=list(tags)):
                diag = updater._diagnose_base_tag(IMAGE, 'latest', REGEX,
                                                  registry_override, BASE_DIGEST)
        else:
            diag = updater._diagnose_base_tag(IMAGE, 'latest', REGEX,
                                              registry_override, BASE_DIGEST)
    return diag, heads, plain


def test_hub_candidates_last_100_of_date_order(updater):
    tags = [f'old{i}' for i in range(150)]
    _, heads, _ = _diagnose(updater, tags=tags)
    assert sorted(heads) == sorted(tags[-100:])


def test_non_hub_candidates_last_100_of_list(updater):
    tags = [f'v{i}' for i in range(150)]
    _, heads, plain = _diagnose(updater, all_tags=tags, registry_override='ghcr.io')
    assert plain.call_args.args[0] == 'ghcr.io'
    assert sorted(heads) == sorted(tags[-100:])


def test_base_tag_excluded(updater):
    diag, heads, _ = _diagnose(updater, tags=['a', 'latest', 'b'], sharing=('latest', 'b'))
    assert 'latest' not in heads
    assert sorted(heads) == ['a', 'b']
    assert diag['sharing_tags'] == ['b']


def test_sharing_tags_sorted_and_capped(updater):
    tags = [f't{i}' for i in range(9, 0, -1)]
    diag, _, _ = _diagnose(updater, tags=tags, sharing=tuple(tags))
    assert diag == {'base_digest': BASE_DIGEST, 'checked_at': NOW_ISO,
                    'outcome': 'regex_stale',
                    'sharing_tags': ['t1', 't2', 't3', 't4', 't5']}


def test_head_errors_are_not_counted_as_sharing(updater):
    def fake_head(registry, namespace, repo, tag, token):
        return (BASE_DIGEST, DigestStatus.ERROR)

    with patch.object(updater, '_get_manifest_digest_head', side_effect=fake_head), \
         patch.object(updater, '_get_docker_token', return_value='tok'), \
         patch.object(updater, '_get_all_tags_by_date', return_value=['a', 'b']), \
         patch.object(updater, '_now', return_value=NOW):
        diag = updater._diagnose_base_tag(IMAGE, 'latest', REGEX, None, BASE_DIGEST)
    assert diag['sharing_tags'] == []
    # Incomplete evidence never claims a rolling build.
    assert diag['outcome'] == 'transient'


def test_empty_tag_listing_is_transient(updater):
    diag, heads, _ = _diagnose(updater, tags=[])
    assert heads == []
    assert diag['outcome'] == 'transient'


# A then B

def test_a_send_delays_first_b_send(updater):
    """A at hour 3 counts as a send: no B at 24 h, first B at 27 h (§7.4)."""
    sends = {}
    for h in [0, 1, 3, 24, 26, 27]:
        notify, _, _, _ = _diag_fail(updater, now=NOW + timedelta(hours=h))
        sends[h] = _sends(notify)

    assert [h for h, s in sends.items() if s] == [3, 27]
    assert len(sends[3]) == 1 and len(sends[27]) == 1
    refined = ResolveFailure.REGEX_STALE.value.format(tags=STALE_TAG, regex=REGEX)
    assert f"{refined}. Failing since" in sends[27][0].kwargs['message']
    rf = _reload(updater).resolve_failure
    assert rf['last_notified'] == (NOW + timedelta(hours=27)).isoformat()
