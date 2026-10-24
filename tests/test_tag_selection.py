"""Tests for safe tag selection.

Regression coverage for the 2026-05-24 incident where a transient codeberg
error made ium "update" forgejo/forgejo from 15.0.2 to 9.0.3: the base-tag
digest HEAD failed, find_matching_tag fell back to "latest matching tag",
and the lexicographic sort ranked "9.0.3" above "15.0.2".

Spec: docs/superpowers/specs/2026-06-04-tag-selection-safety-design.md
"""

import json
import re
from unittest.mock import patch, MagicMock

import pytest
import requests

from ium import (
    DockerImageUpdater, DigestStatus, ResolveFailure, _natural_sort_key,
)


@pytest.fixture
def updater(tmp_path):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"
    config_file.write_text('{"images": []}')
    state_file.write_text("{}")
    return DockerImageUpdater(str(config_file), str(state_file))


def _mock_response(status_code=200, headers=None, json_data=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        # response= is required: _get_manifest_digest_head inspects
        # e.response.status_code to classify 404 vs other failures.
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status_code} error", response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestNaturalSortKey:
    """_natural_sort_key orders digit runs numerically, text runs lexically."""

    def test_multi_digit_major_sorts_numerically(self):
        # The May 24 bug: "9.0.3" > "15.0.2" as strings.
        assert _natural_sort_key("9.0.3") < _natural_sort_key("15.0.2")

    def test_reverse_sort_puts_highest_major_first(self):
        tags = ["9.0.3", "15.0.2", "12.0.1", "15.0.1"]
        tags.sort(key=_natural_sort_key, reverse=True)
        assert tags == ["15.0.2", "15.0.1", "12.0.1", "9.0.3"]

    def test_different_digit_widths_within_component(self):
        # 3.10.0 is newer than 3.9.0; lexicographic gets this wrong.
        assert _natural_sort_key("3.9.0") < _natural_sort_key("3.10.0")

    def test_numeric_suffix_runs(self):
        # linuxserver -lsNNN suffixes cross digit-width boundaries too.
        assert (_natural_sort_key("6.1.1.10360-ls99")
                < _natural_sort_key("6.1.1.10360-ls301"))

    def test_v_prefix_tags(self):
        assert (_natural_sort_key("v9.8.0-ls399")
                < _natural_sort_key("v9.10.0-ls400"))

    def test_four_part_versions(self):
        assert (_natural_sort_key("6.1.1.9999-ls301")
                < _natural_sort_key("6.1.1.10360-ls301"))

    def test_equal_tags_equal_keys(self):
        assert _natural_sort_key("15.0.2") == _natural_sort_key("15.0.2")

    def test_heterogeneous_shapes_are_comparable(self):
        # Must never raise TypeError (bare int < str comparison).
        tags = ["latest", "15.0.2", "v2", "2025.11.1"]
        sorted(tags, key=_natural_sort_key)  # no exception
        assert _natural_sort_key("latest") != _natural_sort_key("15.0.2")


class TestDigestStatus:
    """_get_manifest_digest_head returns (digest, status) and classifies failures.

    404 -> NOT_FOUND (the tag does not exist; fallback is legitimate).
    Everything else -> ERROR (transient/auth/protocol; result unknown).
    """

    URL_ARGS = ("codeberg.org", "forgejo", "forgejo", "15", "tok")

    @patch("ium.requests.request")
    def test_success_returns_digest_and_ok(self, mock_request, updater):
        mock_request.return_value = _mock_response(
            200, headers={"Docker-Content-Digest": "sha256:abc"}
        )
        digest, status = updater._get_manifest_digest_head(*self.URL_ARGS)
        assert digest == "sha256:abc"
        assert status is DigestStatus.OK

    @patch("ium.requests.request")
    def test_404_is_not_found(self, mock_request, updater):
        mock_request.return_value = _mock_response(404)
        digest, status = updater._get_manifest_digest_head(*self.URL_ARGS)
        assert digest is None
        assert status is DigestStatus.NOT_FOUND

    @patch("ium.time.sleep")
    @patch("ium.requests.request")
    def test_persistent_503_is_error(self, mock_request, _sleep, updater):
        # _request_with_retry tries 4 times (initial + 3 retries) then
        # returns the failing response; raise_for_status -> HTTPError(503).
        mock_request.side_effect = [_mock_response(503)] * 4
        digest, status = updater._get_manifest_digest_head(*self.URL_ARGS)
        assert digest is None
        assert status is DigestStatus.ERROR
        assert mock_request.call_count == 4

    @patch("ium.time.sleep")
    @patch("ium.requests.request")
    def test_connection_error_is_error(self, mock_request, _sleep, updater):
        mock_request.side_effect = requests.ConnectionError("boom")
        digest, status = updater._get_manifest_digest_head(*self.URL_ARGS)
        assert digest is None
        assert status is DigestStatus.ERROR

    @patch("ium.requests.request")
    def test_401_is_error_not_not_found(self, mock_request, updater):
        # Expired/invalid token must not look like a missing tag.
        mock_request.return_value = _mock_response(401)
        digest, status = updater._get_manifest_digest_head(*self.URL_ARGS)
        assert digest is None
        assert status is DigestStatus.ERROR

    @patch("ium.requests.request")
    def test_200_without_digest_header_is_error(self, mock_request, updater):
        # A 200 missing Docker-Content-Digest previously looked identical
        # to "tag not found".  It is a protocol anomaly: ERROR.
        mock_request.return_value = _mock_response(200, headers={})
        digest, status = updater._get_manifest_digest_head(*self.URL_ARGS)
        assert digest is None
        assert status is DigestStatus.ERROR


FORGEJO_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"


class TestFindMatchingTagDecisionTable:
    """One test per row of the spec's decision table.

    | Base tag resolution        | Behavior                       |
    |----------------------------|--------------------------------|
    | OK + digest match          | return (tag, digest)           |
    | OK + no match              | None (skip cycle)              |
    | NOT_FOUND (true 404)       | fallback: newest matching tag  |
    | ERROR                      | None (skip cycle)              |

    These tests patch the internal helpers, not HTTP: the HTTP-level
    classification is covered by TestDigestStatus, and the full HTTP path
    by TestMay24Regression.
    """

    TAGS = ["latest", "9.0.3", "12.0.1", "15.0.1", "15.0.2"]

    def _setup(self, updater, digest_map):
        """Wire updater so _get_manifest_digest_head serves from digest_map.

        digest_map: tag -> (digest, DigestStatus); unlisted tags -> NOT_FOUND.
        """
        updater._get_docker_token = MagicMock(return_value="tok")
        updater._get_all_tags = MagicMock(return_value=list(self.TAGS))
        updater.compiled_patterns[FORGEJO_PATTERN] = re.compile(FORGEJO_PATTERN)

        def head(registry, namespace, repo, tag, token):
            return digest_map.get(tag, (None, DigestStatus.NOT_FOUND))

        updater._get_manifest_digest_head = MagicMock(side_effect=head)

    def test_base_ok_with_digest_match_returns_it(self, updater):
        self._setup(updater, {
            "15": ("sha256:current", DigestStatus.OK),
            "15.0.2": ("sha256:current", DigestStatus.OK),
            "15.0.1": ("sha256:older", DigestStatus.OK),
            "12.0.1": ("sha256:old", DigestStatus.OK),
            "9.0.3": ("sha256:ancient", DigestStatus.OK),
        })
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org"
        )
        assert result == ("15.0.2", "sha256:current")

    def test_base_ok_no_match_skips_cycle(self, updater):
        # Mid-release race: base repointed, version tag not pushed yet.
        self._setup(updater, {
            "15": ("sha256:brand-new", DigestStatus.OK),
            "15.0.2": ("sha256:current", DigestStatus.OK),
            "15.0.1": ("sha256:older", DigestStatus.OK),
            "12.0.1": ("sha256:old", DigestStatus.OK),
            "9.0.3": ("sha256:ancient", DigestStatus.OK),
        })
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org"
        )
        assert result is None  # no guessing; retried next cycle

    def test_base_ok_per_tag_errors_skip_cycle(self, updater):
        # Per-tag HEADs failed transiently: matches may have been missed.
        self._setup(updater, {
            "15": ("sha256:current", DigestStatus.OK),
            "15.0.2": (None, DigestStatus.ERROR),
            "15.0.1": (None, DigestStatus.ERROR),
            "12.0.1": (None, DigestStatus.ERROR),
            "9.0.3": (None, DigestStatus.ERROR),
        })
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org"
        )
        assert result is None

    def test_base_not_found_falls_back_to_natural_newest(self, updater):
        # True 404 on the base tag: fallback fires, and the natural sort
        # must pick 15.0.2 — the old lexicographic sort picked 9.0.3.
        self._setup(updater, {
            "15": (None, DigestStatus.NOT_FOUND),
            "15.0.2": ("sha256:current", DigestStatus.OK),
            "9.0.3": ("sha256:ancient", DigestStatus.OK),
        })
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org"
        )
        assert result == ("15.0.2", "sha256:current")

    def test_base_not_found_fallback_digest_error_skips_cycle(self, updater):
        self._setup(updater, {
            "15": (None, DigestStatus.NOT_FOUND),
            "15.0.2": (None, DigestStatus.ERROR),
        })
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org"
        )
        assert result is None

    def test_base_error_skips_cycle_before_tag_listing(self, updater):
        # The May 24 trigger: transient error on the base tag.  Must skip
        # without even listing tags — there is nothing safe to do.
        self._setup(updater, {
            "15": (None, DigestStatus.ERROR),
        })
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org"
        )
        assert result is None
        updater._get_all_tags.assert_not_called()


def _make_updater(tmp_path, state):
    """Updater with the forgejo config and the given persisted state dict."""
    config = {
        "images": [{
            "image": "forgejo/forgejo",
            "regex": FORGEJO_PATTERN,
            "base_tag": "15",
            "registry": "codeberg.org",
            "auto_update": True,
            "cleanup_old_images": False,
        }]
    }
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"
    config_file.write_text(json.dumps(config))
    state_file.write_text(json.dumps(state))
    return DockerImageUpdater(str(config_file), str(state_file))


CURRENT_STATE = {
    "forgejo/forgejo": {
        "base_tag": "15",
        "tag": "15.0.2",
        "digest": "sha256:current",
        "last_updated": "2026-05-24T00:00:00",
    }
}


class TestDowngradeGuard:
    """A candidate older than the current version is reported, never applied."""

    @patch("ium.send_notifications")
    def test_downgrade_reported_but_not_applied(self, mock_notify, tmp_path):
        u = _make_updater(tmp_path, CURRENT_STATE)
        u.find_matching_tag = MagicMock(return_value=("9.0.3", "sha256:ancient"))
        u._get_containers_for_image = MagicMock(return_value=[])
        u._pull_image = MagicMock(return_value=True)

        updates = u.check_and_update()

        assert len(updates) == 1
        info = updates[0]
        assert info["new_tag"] == "9.0.3"
        assert info["downgrade"] is True
        # Effective auto_update is off: webui history derives
        # applied=... from this field.
        assert info["auto_update"] is False
        u._pull_image.assert_not_called()
        # State does NOT advance to the refused tag (spec Part 4): the
        # candidate is held as a pending downgrade record instead.
        assert u.state["forgejo/forgejo"].tag == "15.0.2"
        assert u.state["forgejo/forgejo"].pending["reason"] == "downgrade"
        assert u.state["forgejo/forgejo"].pending["tag"] == "9.0.3"
        # Notification still goes out, marked not-auto-applied.
        assert mock_notify.call_args.kwargs["auto_update"] is False

    @patch("ium.send_notifications")
    def test_upgrade_applies_normally(self, mock_notify, tmp_path):
        u = _make_updater(tmp_path, CURRENT_STATE)
        u.find_matching_tag = MagicMock(return_value=("15.0.3", "sha256:new"))
        u._get_containers_for_image = MagicMock(return_value=[])
        u._pull_image = MagicMock(return_value=True)

        updates = u.check_and_update()

        assert len(updates) == 1
        info = updates[0]
        assert info["downgrade"] is False
        assert info["auto_update"] is True
        u._pull_image.assert_called()
        assert u.state["forgejo/forgejo"].tag == "15.0.3"

    @patch("ium.send_notifications")
    def test_unknown_old_tag_is_not_blocked(self, mock_notify, tmp_path):
        # No state and no containers -> old_tag == 'unknown': nothing to
        # compare against, guard must not fire.
        u = _make_updater(tmp_path, {})
        u.find_matching_tag = MagicMock(return_value=("9.0.3", "sha256:ancient"))
        u._get_containers_for_image = MagicMock(return_value=[])
        u._pull_image = MagicMock(return_value=True)

        updates = u.check_and_update()

        assert len(updates) == 1
        assert updates[0]["downgrade"] is False
        assert updates[0]["auto_update"] is True
        u._pull_image.assert_called()


class TestMay24Regression:
    """Replay of the 2026-05-24 incident, HTTP level.

    forgejo/forgejo tracked with base_tag '15' on codeberg.org; the tag
    list contains 9.0.3 and 15.0.x; the base-tag HEAD hits a persistent
    503.  Before the fix: fallback + lexicographic sort selected 9.0.3
    and 'applied' it.  After: the cycle is skipped, state untouched.
    """

    @patch("ium.send_notifications")
    @patch("ium.time.sleep")
    @patch("ium.requests.request")
    def test_transient_base_error_skips_cycle(
        self, mock_request, _sleep, mock_notify, tmp_path
    ):
        u = _make_updater(tmp_path, CURRENT_STATE)
        u._get_docker_token = MagicMock(return_value="tok")
        u._get_containers_for_image = MagicMock(return_value=[])
        u._pull_image = MagicMock(return_value=True)

        # HEAD /v2/forgejo/forgejo/manifests/15 -> 503, all 4 attempts.
        mock_request.side_effect = [_mock_response(503)] * 4

        updates = u.check_and_update()

        assert updates == []
        mock_notify.assert_not_called()
        u._pull_image.assert_not_called()
        # State untouched: still on 15.0.2.
        assert u.state["forgejo/forgejo"].tag == "15.0.2"
        assert u.state["forgejo/forgejo"].digest == "sha256:current"
        # Only the base-tag HEAD was attempted - no tag listing happened.
        assert all("/manifests/15" in c.args[1] for c in mock_request.call_args_list)


class TestResolveFailureReason:
    """Every failing find_matching_tag branch records a specific reason (spec §6).

    Each case asserts the recorded member directly, then drives the same
    registry picture through check_and_update and checks that the
    check_error payload carries the member's text and lower-cased name.
    """

    TAGS = TestFindMatchingTagDecisionTable.TAGS

    def _wire(self, u, digest_map, tags=None):
        """Serve HEADs from digest_map (unlisted -> NOT_FOUND) and list *tags*."""
        u._get_docker_token = MagicMock(return_value="tok")
        u._get_all_tags = MagicMock(
            return_value=list(self.TAGS if tags is None else tags))
        u.compiled_patterns[FORGEJO_PATTERN] = re.compile(FORGEJO_PATTERN)

        def head(registry, namespace, repo, tag, token):
            return digest_map.get(tag, (None, DigestStatus.NOT_FOUND))

        u._get_manifest_digest_head = MagicMock(side_effect=head)

    @staticmethod
    def _check_errors(u):
        events = []
        u.check_and_update(progress_callback=lambda e, d: events.append((e, d)))
        return [d for e, d in events if e == 'check_error']

    def _assert_payload(self, errors, member):
        assert len(errors) == 1
        payload = errors[0]
        assert payload['image'] == "forgejo/forgejo"
        assert payload['base_tag'] == "15"
        assert payload['reason'] == member.name.lower()
        assert member.value in payload['error']
        assert payload['error'] == (
            f"Could not determine version for forgejo/forgejo:15: {member.value}")

    def _assert_reason(self, updater, tmp_path, member, digest_map, tags=None):
        self._wire(updater, digest_map, tags)
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org")
        assert result is None
        assert updater._last_resolve_failure is member

        # Same registry picture, through check_and_update.
        u = _make_updater(tmp_path, CURRENT_STATE)
        self._wire(u, digest_map, tags)
        self._assert_payload(self._check_errors(u), member)

    def test_registry_error(self, updater, tmp_path):
        self._assert_reason(updater, tmp_path, ResolveFailure.REGISTRY_ERROR, {
            "15": (None, DigestStatus.ERROR),
        })
        assert updater._last_base_digest is None

    @patch("ium.time.sleep")
    @patch("ium.requests.request")
    def test_registry_error_http_level(self, mock_request, _sleep, tmp_path):
        # The May 24 replay at HTTP level now names the real cause.
        u = _make_updater(tmp_path, CURRENT_STATE)
        u._get_docker_token = MagicMock(return_value="tok")
        mock_request.side_effect = [_mock_response(503)] * 4
        self._assert_payload(self._check_errors(u), ResolveFailure.REGISTRY_ERROR)

    def test_no_tags(self, updater, tmp_path):
        self._assert_reason(updater, tmp_path, ResolveFailure.NO_TAGS, {
            "15": ("sha256:current", DigestStatus.OK),
        }, tags=[])

    def test_no_regex_match(self, updater, tmp_path):
        self._assert_reason(updater, tmp_path, ResolveFailure.NO_REGEX_MATCH, {
            "15": ("sha256:current", DigestStatus.OK),
        }, tags=["latest", "nightly"])

    def test_no_digest_match(self, updater, tmp_path):
        self._assert_reason(updater, tmp_path, ResolveFailure.NO_DIGEST_MATCH, {
            "15": ("sha256:brand-new", DigestStatus.OK),
            "15.0.2": ("sha256:current", DigestStatus.OK),
            "15.0.1": ("sha256:older", DigestStatus.OK),
            "12.0.1": ("sha256:old", DigestStatus.OK),
            "9.0.3": ("sha256:ancient", DigestStatus.OK),
        })
        # The base digest is exposed for Part 7's diagnosis.
        assert updater._last_base_digest == "sha256:brand-new"

    def test_no_fallback_digest(self, updater, tmp_path):
        self._assert_reason(updater, tmp_path, ResolveFailure.NO_FALLBACK_DIGEST, {
            "15": (None, DigestStatus.NOT_FOUND),
            "15.0.2": (None, DigestStatus.ERROR),
        })
        assert updater._last_base_digest is None

    def test_internal_on_pattern_cache_miss(self, updater, tmp_path):
        self._wire(updater, {"15": ("sha256:current", DigestStatus.OK)})
        del updater.compiled_patterns[FORGEJO_PATTERN]
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org")
        assert result is None
        assert updater._last_resolve_failure is ResolveFailure.INTERNAL

        u = _make_updater(tmp_path, CURRENT_STATE)
        self._wire(u, {"15": ("sha256:current", DigestStatus.OK)})
        del u.compiled_patterns[FORGEJO_PATTERN]
        self._assert_payload(self._check_errors(u), ResolveFailure.INTERNAL)

    def test_none_failure_is_treated_as_internal(self, tmp_path):
        # Defensive: a None result with no recorded reason still says something.
        u = _make_updater(tmp_path, CURRENT_STATE)
        u.find_matching_tag = MagicMock(return_value=None)
        self._assert_payload(self._check_errors(u), ResolveFailure.INTERNAL)

    def test_success_leaves_none(self, updater):
        # A prior failure must not leak into a later successful call.
        self._wire(updater, {"15": (None, DigestStatus.ERROR)})
        updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org")
        assert updater._last_resolve_failure is ResolveFailure.REGISTRY_ERROR

        self._wire(updater, {
            "15": ("sha256:current", DigestStatus.OK),
            "15.0.2": ("sha256:current", DigestStatus.OK),
        })
        result = updater.find_matching_tag(
            "forgejo/forgejo", "15", FORGEJO_PATTERN, "codeberg.org")
        assert result == ("15.0.2", "sha256:current")
        assert updater._last_resolve_failure is None
        assert updater._last_base_digest == "sha256:current"

    def test_attributes_initialised_in_init(self, updater):
        assert updater._last_resolve_failure is None
        assert updater._last_base_digest is None

    def test_refined_members_are_format_templates(self):
        # A later task formats these; the plain members carry no placeholders.
        assert ResolveFailure.REGEX_STALE.value.format(
            tags="v1, v2", regex="^x$", base_tag="stable") == (
            "stable is currently `v1, v2`, which the regex `^x$` does not match")
        assert ResolveFailure.ROLLING_BASE.value.format(base_tag="latest") == (
            "no tag names the current `latest` digest; it is probably a "
            "rolling build. Try a release-line base tag (for example `v3`, "
            "`lts`, `stable`)")
        plain = set(ResolveFailure) - {ResolveFailure.REGEX_STALE,
                                       ResolveFailure.ROLLING_BASE}
        assert all('{' not in m.value and '}' not in m.value for m in plain)
        assert [m.name for m in ResolveFailure] == [
            "REGISTRY_ERROR", "NO_TAGS", "NO_REGEX_MATCH", "NO_DIGEST_MATCH",
            "NO_FALLBACK_DIGEST", "INTERNAL", "REGEX_STALE", "ROLLING_BASE"]
