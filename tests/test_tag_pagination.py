"""Tests for registry tag-list pagination (Link header following).

Spec: docs/superpowers/specs/2026-10-24-orphaned-image-and-blocked-downgrade-design.md, Part 5.
"""

from unittest.mock import patch, MagicMock

import pytest
import requests

import ium
from ium import DockerImageUpdater, _next_link

BASE = "https://ghcr.io"
TAGS_URL = "https://ghcr.io/v2/ns/repo/tags/list"


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
    return resp


def test_next_link_relative():
    assert _next_link('</v2/a/b/tags/list?last=x&n=0>; rel="next"', BASE) == \
        'https://ghcr.io/v2/a/b/tags/list?last=x&n=0'


def test_next_link_absolute():
    assert _next_link('<https://other.io/v2/a/tags/list?last=x>; rel="next"', BASE) == \
        'https://other.io/v2/a/tags/list?last=x'


def test_next_link_picks_next_among_several():
    header = '</u1>; rel="prev", </u2>; rel="next"'
    assert _next_link(header, BASE) == 'https://ghcr.io/u2'


def test_next_link_none_without_next():
    assert _next_link(None, BASE) is None
    assert _next_link('', BASE) is None
    assert _next_link('</u1>; rel="prev"', BASE) is None


def test_two_pages_joined(updater):
    link = '</v2/ns/repo/tags/list?last=b&n=0>; rel="next"'
    page2_url = "https://ghcr.io/v2/ns/repo/tags/list?last=b&n=0"
    responses = {
        TAGS_URL: _mock_response(headers={'Link': link}, json_data={'tags': ['a', 'b']}),
        page2_url: _mock_response(json_data={'tags': ['c']}),
    }
    with patch("ium.requests.request",
               side_effect=lambda method, url, **kw: responses[url]) as req:
        tags = updater._get_all_tags("ghcr.io", "ns", "repo", None)
    assert tags == ['a', 'b', 'c']
    assert [c.args[1] for c in req.call_args_list] == [TAGS_URL, page2_url]


def test_single_page_single_request(updater):
    with patch("ium.requests.request",
               return_value=_mock_response(json_data={'tags': ['a']})) as req:
        assert updater._get_all_tags("ghcr.io", "ns", "repo", None) == ['a']
    assert req.call_count == 1


def test_page_cap_warns(updater):
    def respond(method, url, **kw):
        return _mock_response(
            headers={'Link': '</v2/ns/repo/tags/list?last=z&n=0>; rel="next"'},
            json_data={'tags': ['t']})
    with patch("ium.requests.request", side_effect=respond) as req:
        with patch.object(updater, "logger") as log:
            tags = updater._get_all_tags("ghcr.io", "ns", "repo", None)
    assert req.call_count == ium.MAX_TAG_PAGES == 50
    assert len(tags) == 50
    log.warning.assert_called_once()
    assert "truncated at 50 pages" in log.warning.call_args.args[0]


@patch("ium.time.sleep")
def test_mid_way_error_returns_empty(mock_sleep, updater):
    calls = []

    def respond(method, url, **kw):
        calls.append(url)
        if len(calls) == 1:
            return _mock_response(
                headers={'Link': '</v2/ns/repo/tags/list?last=a&n=0>; rel="next"'},
                json_data={'tags': ['a']})
        raise requests.ConnectionError("boom")
    with patch("ium.requests.request", side_effect=respond):
        assert updater._get_all_tags("ghcr.io", "ns", "repo", None) == []
