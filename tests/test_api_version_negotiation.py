"""Docker Engine API version negotiation.

Regression coverage for issue #25: API_VERSION was pinned to v1.41, but Docker
Engine 25+ refuses anything below 1.44 --

    Docker API error 400: client version 1.41 is too old.
    Minimum supported API version is 1.44

Every request failed, so container discovery returned nothing and updates were
reported against "unknown". Bumping the pin would instead break Docker 20.10,
whose ceiling is 1.41, so the version has to be negotiated per daemon.

GET /version is served on the unversioned path by every Engine and reports both
ApiVersion (newest supported) and MinAPIVersion (oldest supported).
"""

import json
import pytest

from docker_api import API_VERSION, DockerAPIError, DockerClient
from tests.fake_docker import FakeResponse, fake_socket


def version_payload(api_version: str, min_api_version: str) -> bytes:
    return json.dumps({
        "Version": "29.1.3",
        "ApiVersion": api_version,
        "MinAPIVersion": min_api_version,
    }).encode()


def handler_for(api_version: str, min_api_version: str):
    """Answer /version, and 200 with an empty object for anything else."""
    def handler(method, url, body):
        if url == "/version":
            return FakeResponse(200, version_payload(api_version, min_api_version))
        return FakeResponse(200, b"{}")
    return handler


def request_paths(calls):
    """Paths of the non-negotiation requests."""
    return [u for _, u in calls if u != "/version"]


class TestNegotiation:

    def test_uses_server_minimum_when_pin_is_too_old(self):
        """Docker 29: min 1.44 -- the old v1.41 pin must be raised to it."""
        patcher, calls = fake_socket(handler_for("1.52", "1.44"))
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            client.list_containers()

        assert request_paths(calls) == ["/v1.44/containers/json"]

    def test_uses_preferred_version_when_daemon_allows_it(self):
        """A daemon spanning our preferred version must get exactly that."""
        patcher, calls = fake_socket(handler_for("1.51", "1.24"))
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            client.list_containers()

        assert request_paths(calls) == [f"/{API_VERSION}/containers/json"]

    def test_caps_at_server_maximum_when_daemon_is_older(self):
        """Docker 19.03: ceiling 1.40, below our preferred 1.41."""
        patcher, calls = fake_socket(handler_for("1.40", "1.12"))
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            client.list_containers()

        assert request_paths(calls) == ["/v1.40/containers/json"]

    def test_version_probe_is_not_itself_versioned(self):
        """A versioned probe would 400 on the very daemons we are probing."""
        patcher, calls = fake_socket(handler_for("1.52", "1.44"))
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            client.list_containers()

        probes = [u for _, u in calls if "/version" in u]
        assert probes == ["/version"], f"probe must be unversioned, got {probes}"

    def test_negotiates_only_once_per_client(self):
        patcher, calls = fake_socket(handler_for("1.52", "1.44"))
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            client.list_containers()
            client.list_containers()
            client.inspect_container("sonarr")

        probes = [u for _, u in calls if u == "/version"]
        assert len(probes) == 1, f"negotiated {len(probes)} times, expected once"

    def test_falls_back_to_pinned_version_when_probe_fails(self):
        """An unreachable probe must not stop the client from trying."""
        def handler(method, url, body):
            if url == "/version":
                raise TimeoutError("timed out")
            return FakeResponse(200, b"{}")

        patcher, calls = fake_socket(handler)
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            client.list_containers()

        assert request_paths(calls) == [f"/{API_VERSION}/containers/json"]

    def test_malformed_probe_response_falls_back(self):
        """Garbage in ApiVersion must not crash the client."""
        def handler(method, url, body):
            if url == "/version":
                return FakeResponse(200, json.dumps(
                    {"ApiVersion": "banana", "MinAPIVersion": "also-banana"}
                ).encode())
            return FakeResponse(200, b"{}")

        patcher, calls = fake_socket(handler)
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            client.list_containers()

        assert request_paths(calls) == [f"/{API_VERSION}/containers/json"]

    def test_explicit_version_skips_negotiation(self):
        """An explicit override must be honoured without probing."""
        patcher, calls = fake_socket(handler_for("1.52", "1.44"))
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock", api_version="v1.47")
            client.list_containers()

        assert [u for _, u in calls] == ["/v1.47/containers/json"]


class TestNegotiationErrorsStillSurface:

    def test_real_api_errors_are_not_masked_by_negotiation(self):
        """A 404 on the actual call must still raise, post-negotiation."""
        def handler(method, url, body):
            if url == "/version":
                return FakeResponse(200, version_payload("1.52", "1.44"))
            return FakeResponse(404, json.dumps({"message": "No such container"}).encode())

        patcher, _ = fake_socket(handler)
        with patcher:
            client = DockerClient(socket_path="/nonexistent.sock")
            with pytest.raises(DockerAPIError) as exc:
                client.inspect_container("nope")
            assert exc.value.status == 404
