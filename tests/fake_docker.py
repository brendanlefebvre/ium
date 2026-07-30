"""A fake Docker socket for exercising DockerClient without a daemon.

Patches docker_api.UnixHTTPConnection, so the real DockerClient._request runs:
URL construction, status handling and error translation are all under test.
The handler receives (method, url, body) and either returns a FakeResponse or
raises — raising from getresponse() reproduces a read timeout in the same place
the production traceback showed one.
"""

from unittest.mock import patch


class FakeResponse:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeConn:
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
    """Return (patcher, calls) — use the patcher as a context manager.

    *calls* accumulates (method, url) tuples for every request issued.
    """
    calls: list[tuple[str, str]] = []
    patcher = patch(
        "docker_api.UnixHTTPConnection",
        lambda socket_path, timeout=30: FakeConn(handler, calls),
    )
    return patcher, calls
