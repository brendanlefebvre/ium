"""Rollback must never delete the backup unless the replacement actually ran.

Regression tests for the 2026-10-25 incident: qbittorrent's replacement was
created (taking the service name) but failed to start because its
``network_mode: container:<id>`` pointed at a recreated gluetun.  The rollback
rename then hit a 409 name conflict with the never-started replacement, and the
old code assumed a conflict meant the replacement was running — so it
force-removed the backup, the only working copy.

Invariant: the backup container is never removed unless ``start_container`` on
the replacement succeeded.
"""

import copy
import json
import logging
import re

import pytest

from ium import DockerImageUpdater
from tests.fake_docker import FakeResponse, fake_socket
from tests.test_socket_timeout_rollback import CONTAINER_INFO


def _no_such_container():
    return FakeResponse(500, b'{"message": "No such container: e51ca4"}')


def _conflict():
    return FakeResponse(
        409, b'{"message": "Conflict. The container name \\"/sonarr\\" is already in use"}'
    )


@pytest.fixture
def updater(tmp_path):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"
    config_file.write_text(json.dumps({
        "images": [{
            "image": "linuxserver/sonarr",
            "regex": r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
            "base_tag": "latest",
            "auto_update": True,
        }]
    }))
    state_file.write_text("{}")
    return DockerImageUpdater(str(config_file), str(state_file))


_RENAME_BACK = re.compile(r"/containers/[^/?]*_backup_[^/?]*/rename\?name=sonarr$")


def _is_rename_back(url):
    """Backup renamed back to the service name (not the forward rename)."""
    return bool(_RENAME_BACK.search(url))


def _make_handler(info=CONTAINER_INFO, create_fails=False, start_new_fails=False,
                  connect_fails=False, rename_back_conflicts=False,
                  start_old_fails=False):
    def handler(method, url, body):
        if method == "GET" and "/containers/sonarr/json" in url:
            return FakeResponse(200, json.dumps(info).encode())
        if method == "POST" and "/containers/create" in url:
            if create_fails:
                return _no_such_container()
            return FakeResponse(201, json.dumps({"Id": "new123"}).encode())
        if method == "POST" and "/containers/new123/start" in url:
            return _no_such_container() if start_new_fails else FakeResponse(204)
        if method == "POST" and "/containers/sonarr/start" in url and start_old_fails:
            return _no_such_container()
        if method == "POST" and "/networks/" in url and "/connect" in url:
            return _no_such_container() if connect_fails else FakeResponse(204)
        if method == "POST" and _is_rename_back(url) and rename_back_conflicts:
            return _conflict()
        return FakeResponse(204)
    return handler


def _run(updater, handler):
    patcher, calls = fake_socket(handler)
    with patcher:
        result = updater._update_container(
            "sonarr", "linuxserver/sonarr", "4.0.16.2944-ls299"
        )
    return result, calls


def _deletes(calls):
    return [(i, u) for i, (m, u) in enumerate(calls) if m == "DELETE"]


def _backup_deletes(calls):
    return [u for _, u in _deletes(calls) if "_backup_" in u]


def _replacement_deletes(calls):
    return [(i, u) for i, u in _deletes(calls) if "/containers/new123" in u]


def _rename_backs(calls):
    return [
        (i, u) for i, (m, u) in enumerate(calls)
        if m == "POST" and _is_rename_back(u)
    ]


def _old_restarts(calls):
    return [u for m, u in calls if m == "POST" and "/containers/sonarr/start" in u]


class TestStartFailure:
    def test_start_failure_removes_replacement_and_restores_backup(self, updater):
        result, calls = _run(updater, _make_handler(start_new_fails=True))

        assert result is False
        replacement = _replacement_deletes(calls)
        assert replacement, f"failed replacement was never removed; calls: {calls}"
        renames = _rename_backs(calls)
        assert renames, f"backup was never renamed back; calls: {calls}"
        assert renames[-1][0] > replacement[0][0], (
            f"rename-back must follow removal of the replacement; calls: {calls}"
        )
        assert _old_restarts(calls), f"old container never restarted; calls: {calls}"
        assert not _backup_deletes(calls), f"backup was deleted; calls: {calls}"

    def test_start_failure_with_rename_conflict_leaves_backup(self, updater):
        result, calls = _run(
            updater, _make_handler(start_new_fails=True, rename_back_conflicts=True)
        )

        assert result is False
        assert not _backup_deletes(calls), (
            f"backup must survive when the replacement never ran; calls: {calls}"
        )


class TestConnectNetworkFailure:
    def test_connect_failure_after_start_removes_stranded_backup(self, updater):
        info = copy.deepcopy(CONTAINER_INFO)
        info["NetworkSettings"]["Networks"] = {"bridge": {}, "media": {}}
        result, calls = _run(
            updater,
            _make_handler(info=info, connect_fails=True, rename_back_conflicts=True),
        )

        assert result is False
        assert any("/networks/media/connect" in u for _, u in calls), calls
        assert _backup_deletes(calls), (
            f"running replacement holds the name; stranded backup should go; calls: {calls}"
        )
        assert not _replacement_deletes(calls), (
            f"the running replacement must not be removed; calls: {calls}"
        )


class TestCreateFailure:
    def test_create_failure_deletes_nothing_and_restores(self, updater):
        result, calls = _run(updater, _make_handler(create_fails=True))

        assert result is False
        assert not _deletes(calls), f"nothing should be deleted; calls: {calls}"
        assert _rename_backs(calls), f"backup never renamed back; calls: {calls}"
        assert _old_restarts(calls), f"old container never restarted; calls: {calls}"


class TestStageLogging:
    """The error log must name the stage that failed, not always 'create'."""

    @pytest.fixture
    def captured(self, updater, caplog):
        # The ium logger does not propagate, so attach caplog's handler directly.
        updater.logger.addHandler(caplog.handler)
        caplog.set_level(logging.ERROR, logger=updater.logger.name)
        yield caplog
        updater.logger.removeHandler(caplog.handler)

    def test_start_failure_logs_start_stage(self, updater, captured):
        _run(updater, _make_handler(start_new_fails=True))
        assert "Failed to start new container" in captured.text

    def test_create_failure_logs_create_stage(self, updater, captured):
        _run(updater, _make_handler(create_fails=True))
        assert "Failed to create new container" in captured.text

    def test_rename_conflict_names_the_backup(self, updater, captured):
        _run(updater, _make_handler(start_new_fails=True, rename_back_conflicts=True))
        assert re.search(r"Rollback failed .*sonarr_backup_\d+ still holds", captured.text), (
            captured.text
        )

    def test_restored_container_failing_to_start_is_named_by_its_own_name(
        self, updater, captured
    ):
        # The backup shares the replacement's dead network parent, so it will
        # not start either.  By then it is back under the service name, so the
        # log must not send the operator looking for a backup that no longer
        # exists.
        result, calls = _run(
            updater, _make_handler(start_new_fails=True, start_old_fails=True)
        )
        assert result is False
        assert _rename_backs(calls), f"backup was never renamed back; calls: {calls}"
        assert not _backup_deletes(calls)
        assert "Rollback restored sonarr but it failed to start" in captured.text
        assert "_backup_" not in captured.text.split("Rollback restored")[-1]
