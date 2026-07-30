"""Docker Engine API client over Unix socket.

Replaces Docker CLI subprocess calls with direct HTTP requests to the
Docker Engine API via the mounted /var/run/docker.sock Unix socket.
No external dependencies — uses only Python stdlib (http.client, socket).
"""

import http.client
import json
import logging
import os
import socket
import urllib.parse
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Docker Engine API version — compatible with Docker 20.10+
API_VERSION = "v1.41"


class DockerAPIError(Exception):
    """Error from the Docker Engine API."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Docker API error {status}: {message}")


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection subclass that connects via a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: int = 30):
        # host is unused for the actual connection but required by HTTPConnection
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


class DockerClient:
    """Client for the Docker Engine API over Unix socket."""

    def __init__(self, socket_path: Optional[str] = None):
        if socket_path is None:
            host = os.environ.get("DOCKER_HOST", "")
            if host:
                socket_path = host.replace("unix://", "")
            else:
                socket_path = "/var/run/docker.sock"
        self._socket_path = socket_path

    def _request(self, method: str, path: str, body: Any = None,
                 query: Optional[Dict[str, str]] = None,
                 timeout: int = 30, stream: bool = False) -> Any:
        """Send an HTTP request to the Docker Engine API.

        Creates a fresh connection per call (Docker socket is local so
        the overhead is negligible and avoids stale-connection issues).

        Returns parsed JSON for most calls.  When *stream* is True the
        response body is consumed line-by-line and the last status JSON
        object is returned (used for ``POST /images/create``).
        """
        url = f"/{API_VERSION}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        headers: Dict[str, str] = {}
        encoded_body: Optional[bytes] = None

        if body is not None:
            encoded_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        conn = UnixHTTPConnection(self._socket_path, timeout=timeout)
        try:
            conn.request(method, url, body=encoded_body, headers=headers)
            response = conn.getresponse()

            if stream:
                # Streaming response (e.g. image pull) — read all chunks,
                # check for error objects in the NDJSON stream.
                data = response.read().decode("utf-8", errors="replace")
                if response.status >= 400:
                    raise DockerAPIError(response.status, data.strip())
                # Check for error in streamed JSON lines
                for line in data.strip().split("\n"):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if "error" in obj:
                            raise DockerAPIError(
                                response.status or 500,
                                obj.get("errorDetail", {}).get("message", obj["error"])
                            )
                    except json.JSONDecodeError:
                        continue
                return None

            raw = response.read().decode("utf-8", errors="replace")

            if response.status == 204:
                return None

            if response.status >= 400:
                # Try to extract message from JSON error body
                try:
                    err = json.loads(raw)
                    msg = err.get("message", raw)
                except (json.JSONDecodeError, AttributeError):
                    msg = raw
                raise DockerAPIError(response.status, msg)

            if not raw:
                return None

            return json.loads(raw)
        finally:
            conn.close()

    # ── Image operations ──────────────────────────────────────────

    def pull_image(self, image: str, tag: str) -> None:
        """Pull an image from a registry.

        Equivalent to ``docker pull image:tag``.
        Uses a 300 s timeout for large images.
        """
        self._request(
            "POST", "/images/create",
            query={"fromImage": image, "tag": tag},
            timeout=300,
            stream=True,
        )

    def list_images(self, reference: str) -> List[Dict[str, Any]]:
        """List images matching a reference filter.

        Returns list of dicts with keys like ``Id``, ``RepoTags``, ``Created``.
        """
        filters = json.dumps({"reference": [reference]})
        result = self._request("GET", "/images/json", query={"filters": filters})
        return result or []

    def remove_image(self, image_ref: str, timeout: int = 120) -> bool:
        """Remove an image.  Returns True on success, False on 404/409."""
        try:
            self._request("DELETE", f"/images/{image_ref}", timeout=timeout)
            return True
        except DockerAPIError as e:
            if e.status in (404, 409):
                return False
            raise

    # ── Container operations ──────────────────────────────────────

    def list_containers(self, all: bool = False) -> List[Dict[str, Any]]:
        """List containers.

        Returns list of dicts with keys like ``Id``, ``Names`` (list with
        ``/`` prefix), ``Image``, ``State``.
        """
        query = {}
        if all:
            query["all"] = "true"
        result = self._request("GET", "/containers/json", query=query)
        return result or []

    def inspect_container(self, name: str) -> Dict[str, Any]:
        """Inspect a container (equivalent to ``docker inspect``).

        Returns the full container JSON — no ``[0]`` unwrap needed.
        """
        return self._request("GET", f"/containers/{name}/json")

    def stop_container(self, name: str, timeout: int = 10) -> None:
        """Stop a container."""
        self._request(
            "POST", f"/containers/{name}/stop",
            query={"t": str(timeout)},
            timeout=timeout + 30,
        )

    def rename_container(self, id_or_name: str, new_name: str) -> None:
        """Rename a container."""
        self._request("POST", f"/containers/{id_or_name}/rename",
                       query={"name": new_name})

    def create_container(self, name: str, config: Dict[str, Any]) -> str:
        """Create a container.  Returns the new container ID."""
        result = self._request(
            "POST", "/containers/create",
            body=config,
            query={"name": name},
        )
        return result["Id"]

    def start_container(self, name: str) -> None:
        """Start an existing container."""
        self._request("POST", f"/containers/{name}/start")

    def remove_container(self, name: str, force: bool = False, timeout: int = 30) -> None:
        """Remove a container."""
        query = {"force": "true"} if force else None
        self._request("DELETE", f"/containers/{name}", query=query, timeout=timeout)

    # ── Network operations ────────────────────────────────────────

    def connect_network(self, network: str, container_id: str) -> None:
        """Connect a container to a network."""
        self._request(
            "POST", f"/networks/{network}/connect",
            body={"Container": container_id},
        )
