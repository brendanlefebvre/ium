"""Socket-level failures must behave like Docker API errors.

Regression tests for the 2026-07-29 incident: a read timeout on the Docker
socket escaped as a bare ``TimeoutError``, which no handler caught.  The
container had already been stopped and renamed to a backup by that point, so
rollback never ran and the service was left down.

The timeout is injected at ``getresponse()`` — the same place the production
traceback showed it (``http.client.begin`` -> ``_read_status`` -> socket recv).
"""

import json
import pytest
from unittest.mock import patch

from docker_api import DockerAPIError, DockerClient
from ium import DockerImageUpdater


# ---------------------------------------------------------------------------
# Fake Docker socket
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeConn:
    """Stands in for UnixHTTPConnection, driven by a handler callable."""

    def __init__(self, handler, calls):
        self._handler = handler
        self._calls = calls
        self._pending = None

    def request(self, method, url, body=None, headers=None):
        self._pending = (method, url, body)
        self._calls.append((method, url))

    def getresponse(self):
        return self._handler(*self._pending)

    def close(self):
        pass


def fake_socket(handler):
    """Patch DockerClient's transport.  Returns (patcher, calls list)."""
    calls: list[tuple[str, str]] = []
    patcher = patch(
        "docker_api.UnixHTTPConnection",
        lambda socket_path, timeout=30: _FakeConn(handler, calls),
    )
    return patcher, calls


CONTAINER_INFO = {
    "Id": "abc123" * 10,
    "Config": {
        "Image": "linuxserver/sonarr:4.0.0.740-ls290",
        "Hostname": "abc123abc1",
        "User": "", "WorkingDir": "",
        "Env": ["PATH=/usr/bin:/bin"],
        "Cmd": None, "Labels": {},
    },
    "HostConfig": {
        "RestartPolicy": {"Name": "", "MaximumRetryCount": 0},
        "NetworkMode": "default",
        "PortBindings": None,
        "Privileged": False, "CapAdd": None, "CapDrop": None,
        "Devices": None, "Memory": 0, "CpuShares": 0,
        "CpuQuota": 0, "SecurityOpt": None, "Runtime": "",
    },
    "Mounts": [],
    "NetworkSettings": {"Networks": {}},
    "State": {"Status": "running"},
}


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


# ---------------------------------------------------------------------------
# Transport layer: socket errors must surface as DockerAPIError
# ---------------------------------------------------------------------------

class TestTransportErrorTranslation:
    """DockerClient must not leak raw socket exceptions to its callers."""

    def test_read_timeout_raises_docker_api_error(self):
        def handler(method, url, body):
            raise TimeoutError("timed out")

        patcher, _ = fake_socket(handler)
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            with pytest.raises(DockerAPIError):
                client.inspect_container("sonarr")

    def test_connection_error_raises_docker_api_error(self):
        def handler(method, url, body):
            raise ConnectionRefusedError("connection refused")

        patcher, _ = fake_socket(handler)
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            with pytest.raises(DockerAPIError):
                client.inspect_container("sonarr")

    def test_timeout_message_identifies_the_request(self):
        """The error must name the failing call so logs are actionable."""
        def handler(method, url, body):
            raise TimeoutError("timed out")

        patcher, _ = fake_socket(handler)
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            with pytest.raises(DockerAPIError) as exc:
                client.start_container("sonarr")
            assert "/containers/sonarr/start" in str(exc.value)


# ---------------------------------------------------------------------------
# Symptom 1: the stopped container must be restored
# ---------------------------------------------------------------------------

class TestRollbackOnSocketTimeout:
    """A timeout after stop+rename must roll the old container back."""

    def _handler_timing_out_on(self, target_fragment):
        def handler(method, url, body):
            if method == "GET" and "/containers/sonarr/json" in url:
                return _FakeResponse(200, json.dumps(CONTAINER_INFO).encode())
            if target_fragment in url:
                raise TimeoutError("timed out")
            if method == "POST" and "/containers/create" in url:
                return _FakeResponse(201, json.dumps({"Id": "new123"}).encode())
            return _FakeResponse(204)
        return handler

    def test_create_timeout_restores_old_container(self, updater):
        patcher, calls = fake_socket(self._handler_timing_out_on("/containers/create"))
        with patcher:
            result = updater._update_container(
                "sonarr", "linuxserver/sonarr", "4.0.16.2944-ls299"
            )

        assert result is False, "a failed update must report failure, not raise"

        renamed_back = [
            u for m, u in calls
            if m == "POST" and "/rename" in u and "name=sonarr" in u and "_backup_" in u
        ]
        assert renamed_back, (
            f"backup was never renamed back to 'sonarr'; calls were: {calls}"
        )

        restarted = [
            u for m, u in calls if m == "POST" and u.endswith("/containers/sonarr/start")
        ]
        assert restarted, f"old container was never restarted; calls were: {calls}"

    def test_start_timeout_does_not_leave_container_stopped(self, updater):
        patcher, calls = fake_socket(self._handler_timing_out_on("/start"))
        with patcher:
            result = updater._update_container(
                "sonarr", "linuxserver/sonarr", "4.0.16.2944-ls299"
            )

        assert result is False
        renamed_back = [
            u for m, u in calls
            if m == "POST" and "/rename" in u and "name=sonarr" in u and "_backup_" in u
        ]
        assert renamed_back, (
            f"backup was never renamed back after start timeout; calls were: {calls}"
        )


# ---------------------------------------------------------------------------
# Symptom 2: one timeout must not abort the rest of the cycle
# ---------------------------------------------------------------------------

class TestUpdateLoopSurvivesTimeout:
    """A timeout on one container must not skip the containers after it."""

    def test_second_container_still_updated_after_first_times_out(self, updater):
        def handler(method, url, body):
            if method == "GET" and "/containers/sonarr-hd/json" in url:
                return _FakeResponse(200, json.dumps(CONTAINER_INFO).encode())
            if method == "GET" and "/containers/sonarr-4k/json" in url:
                return _FakeResponse(200, json.dumps(CONTAINER_INFO).encode())
            # Only the first container's create times out.
            if "/containers/create" in url and "name=sonarr-hd" in url:
                raise TimeoutError("timed out")
            if method == "POST" and "/containers/create" in url:
                return _FakeResponse(201, json.dumps({"Id": "new123"}).encode())
            return _FakeResponse(204)

        patcher, calls = fake_socket(handler)
        with patcher:
            results = updater._update_containers(
                ["sonarr-hd", "sonarr-4k"], "linuxserver/sonarr", "4.0.16.2944-ls299"
            )

        assert set(results) == {"sonarr-hd", "sonarr-4k"}, (
            f"loop aborted early; only got {set(results)}"
        )
        assert results["sonarr-hd"] is False
        assert results["sonarr-4k"] is True
