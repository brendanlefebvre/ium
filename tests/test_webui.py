"""Tests for the ium web UI (webui.py).

Covers: status endpoints, CSRF protection, authentication, config CRUD,
check/daemon control, Socket.IO events, pattern detection, and AuthManager.
"""

import base64
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Set env vars BEFORE importing webui so module-level code uses temp paths
_FIXTURES_SET = False


def _ensure_env(tmp_path_factory):
    """Set env vars to temp paths so webui module-level init doesn't touch real files."""
    global _FIXTURES_SET
    if _FIXTURES_SET:
        return
    tmp = tmp_path_factory.mktemp("webui")
    config_path = tmp / "config.json"
    state_path = tmp / "state.json"
    config_path.write_text(json.dumps({
        "images": [{
            "image": "nginx",
            "regex": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
            "auto_update": False
        }]
    }))
    os.environ["CONFIG_FILE"] = str(config_path)
    os.environ["STATE_FILE"] = str(state_path)
    os.environ.pop("WEBUI_USER", None)
    os.environ.pop("WEBUI_PASSWORD", None)
    _FIXTURES_SET = True


@pytest.fixture(scope="session", autouse=True)
def _setup_env(tmp_path_factory):
    _ensure_env(tmp_path_factory)


# ---------------------------------------------------------------------------
# Import webui after env is configured
# ---------------------------------------------------------------------------

import webui as webui_mod
from ium import __version__


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

XHR_HEADER = {"X-Requested-With": "XMLHttpRequest"}


def _auth_header(user="admin", password="secret"):
    """Return a Basic auth header dict."""
    cred = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def _post_json(client, url, data, extra_headers=None):
    """POST JSON with the CSRF header included."""
    headers = {"Content-Type": "application/json", **XHR_HEADER}
    if extra_headers:
        headers.update(extra_headers)
    return client.post(url, data=json.dumps(data), headers=headers)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client(tmp_path):
    """Flask test client with a mock updater wired in."""
    # Create temp config/state files
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(json.dumps({
        "images": [{
            "image": "nginx",
            "regex": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
            "auto_update": False
        }]
    }))

    mock_updater = MagicMock()
    mock_updater.dry_run = True
    mock_updater.config = {
        "images": [{
            "image": "nginx",
            "regex": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
            "auto_update": False
        }]
    }
    mock_updater.state = {}
    mock_updater.config_file = config_path
    mock_updater.state_file = state_path

    # Reset global state
    webui_mod.updater = mock_updater
    webui_mod.is_checking = False
    webui_mod.daemon_running = False
    webui_mod.daemon_interval = 3600
    webui_mod.last_check_time = None
    webui_mod.last_updates = []
    webui_mod.update_history = []
    webui_mod.AUTH_ENABLED = False

    webui_mod.app.config["TESTING"] = True
    with webui_mod.app.test_client() as client:
        yield client

    # Cleanup
    webui_mod.daemon_running = False
    webui_mod.daemon_stop_event.set()


@pytest.fixture()
def auth_client(tmp_path):
    """Flask test client with authentication enabled."""
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(json.dumps({
        "images": [{
            "image": "nginx",
            "regex": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
            "auto_update": False
        }]
    }))

    mock_updater = MagicMock()
    mock_updater.dry_run = True
    mock_updater.config = {"images": [{"image": "nginx", "regex": "^[0-9]+$"}]}
    mock_updater.state = {}
    mock_updater.config_file = config_path
    mock_updater.state_file = state_path

    webui_mod.updater = mock_updater
    webui_mod.is_checking = False
    webui_mod.daemon_running = False
    webui_mod.AUTH_ENABLED = True
    webui_mod.AUTH_USER = "admin"
    webui_mod.AUTH_PASSWORD = "secret"

    webui_mod.app.config["TESTING"] = True
    with webui_mod.app.test_client() as client:
        yield client

    webui_mod.AUTH_ENABLED = False
    webui_mod.AUTH_USER = ""
    webui_mod.AUTH_PASSWORD = ""
    webui_mod.daemon_running = False
    webui_mod.daemon_stop_event.set()


@pytest.fixture()
def socketio_client():
    """Flask-SocketIO test client."""
    webui_mod.is_checking = False
    webui_mod.daemon_running = False
    webui_mod.last_check_time = None
    webui_mod.AUTH_ENABLED = False

    webui_mod.app.config["TESTING"] = True
    client = webui_mod.socketio.test_client(webui_mod.app)
    yield client
    client.disconnect()

    webui_mod.AUTH_ENABLED = False


# ===========================================================================
# TestStatusEndpoints
# ===========================================================================

class TestStatusEndpoints:
    def test_get_status(self, app_client):
        resp = app_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "updater_loaded" in data
        assert "dry_run" in data
        assert "is_checking" in data
        assert "daemon_running" in data
        assert data["updater_loaded"] is True

    def test_get_version(self, app_client):
        resp = app_client.get("/api/version")
        assert resp.status_code == 200
        assert resp.get_json()["version"] == __version__

    def test_get_config(self, app_client):
        resp = app_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "images" in data
        assert data["images"][0]["image"] == "nginx"

    def test_get_state(self, app_client):
        resp = app_client.get("/api/state")
        assert resp.status_code == 200
        assert resp.get_json() == {}

    def test_get_history(self, app_client):
        webui_mod.update_history = [{"ts": "1"}, {"ts": "2"}, {"ts": "3"}]
        resp = app_client.get("/api/history?limit=2")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        assert data[0]["ts"] == "2"

    def test_get_updates(self, app_client):
        resp = app_client.get("/api/updates")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["last_check"] is None
        assert data["updates"] == []


# ===========================================================================
# TestCSRF
# ===========================================================================

class TestCSRF:
    def test_post_without_xhr_header_rejected(self, app_client):
        resp = app_client.post(
            "/api/config",
            data=json.dumps({"images": []}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403
        assert "CSRF" in resp.get_json()["error"]

    def test_post_with_xhr_header_accepted(self, app_client):
        """POST with X-Requested-With should pass CSRF check (may fail on other validation)."""
        resp = _post_json(app_client, "/api/config", {"images": []})
        # Should NOT be 403 — it passes CSRF. May be 200 or 400 depending on schema.
        assert resp.status_code != 403

    def test_get_requests_not_affected(self, app_client):
        resp = app_client.get("/api/status")
        assert resp.status_code == 200


# ===========================================================================
# TestAuth
# ===========================================================================

class TestAuth:
    def test_no_auth_when_disabled(self, app_client):
        resp = app_client.get("/api/status")
        assert resp.status_code == 200

    def test_auth_required_when_enabled(self, auth_client):
        resp = auth_client.get("/api/status")
        assert resp.status_code == 401

    def test_auth_succeeds_with_correct_credentials(self, auth_client):
        resp = auth_client.get("/api/status", headers=_auth_header("admin", "secret"))
        assert resp.status_code == 200

    def test_auth_fails_with_wrong_credentials(self, auth_client):
        resp = auth_client.get("/api/status", headers=_auth_header("admin", "wrong"))
        assert resp.status_code == 401

    def test_health_bypass_auth_when_enabled(self, auth_client):
        resp = auth_client.get("/health")
        assert resp.status_code == 200

    def test_health_ok_when_auth_disabled(self, app_client):
        resp = app_client.get("/health")
        assert resp.status_code == 200


# ===========================================================================
# TestConfigEndpoint
# ===========================================================================

class TestConfigEndpoint:
    def test_save_valid_config(self, app_client, tmp_path):
        new_config = {
            "images": [{
                "image": "redis",
                "regex": "^[0-9]+\\.[0-9]+$",
                "auto_update": True
            }]
        }
        with patch.object(webui_mod, "load_updater", return_value=True):
            resp = _post_json(app_client, "/api/config", new_config)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "success"

    def test_save_invalid_schema(self, app_client):
        bad_config = {"images": "not_a_list"}
        resp = _post_json(app_client, "/api/config", bad_config)
        assert resp.status_code == 400

    def test_save_invalid_regex(self, app_client):
        bad_config = {
            "images": [{"image": "test", "regex": "[invalid"}]
        }
        resp = _post_json(app_client, "/api/config", bad_config)
        assert resp.status_code == 400
        assert "Invalid regex" in resp.get_json()["error"]

    def test_save_empty_body(self, app_client):
        resp = app_client.post(
            "/api/config",
            data="",
            headers={"Content-Type": "application/json", **XHR_HEADER},
        )
        assert resp.status_code == 400

    def test_updater_not_loaded(self, app_client):
        webui_mod.updater = None
        resp = _post_json(app_client, "/api/config", {"images": []})
        assert resp.status_code == 503


# ===========================================================================
# TestCheckEndpoint
# ===========================================================================

class TestCheckEndpoint:
    def test_trigger_check(self, app_client):
        with patch.object(webui_mod, "run_check"):
            resp = app_client.post("/api/check", headers=XHR_HEADER)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"

    def test_check_already_running(self, app_client):
        webui_mod.is_checking = True
        resp = app_client.post("/api/check", headers=XHR_HEADER)
        assert resp.status_code == 409


# ===========================================================================
# TestDaemonEndpoint
# ===========================================================================

class TestDaemonEndpoint:
    def test_start_daemon(self, app_client):
        with patch.object(webui_mod, "save_daemon_state"):
            resp = _post_json(app_client, "/api/daemon", {"action": "start", "interval": 120})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "started"
        # Cleanup
        webui_mod.daemon_running = False
        webui_mod.daemon_stop_event.set()
        if webui_mod.daemon_thread:
            webui_mod.daemon_thread.join(timeout=2)

    def test_stop_daemon(self, app_client):
        # Start daemon first
        webui_mod.daemon_running = True
        webui_mod.daemon_stop_event.clear()
        webui_mod.daemon_thread = threading.Thread(target=lambda: None)
        webui_mod.daemon_thread.start()
        webui_mod.daemon_thread.join()

        with patch.object(webui_mod, "save_daemon_state"):
            resp = _post_json(app_client, "/api/daemon", {"action": "stop"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "stopped"

    def test_start_when_already_running(self, app_client):
        webui_mod.daemon_running = True
        resp = _post_json(app_client, "/api/daemon", {"action": "start", "interval": 120})
        assert resp.status_code == 409
        webui_mod.daemon_running = False

    def test_stop_when_not_running(self, app_client):
        webui_mod.daemon_running = False
        resp = _post_json(app_client, "/api/daemon", {"action": "stop"})
        assert resp.status_code == 409

    def test_invalid_action(self, app_client):
        resp = _post_json(app_client, "/api/daemon", {"action": "restart"})
        assert resp.status_code == 400

    def test_interval_validation(self, app_client):
        resp = _post_json(app_client, "/api/daemon", {"action": "start", "interval": 10})
        assert resp.status_code == 400
        assert "60 seconds" in resp.get_json()["error"]

    def test_interval_validation_non_numeric(self, app_client):
        resp = _post_json(app_client, "/api/daemon", {"action": "start", "interval": "fast"})
        assert resp.status_code == 400


# ===========================================================================
# TestSocketIO
# ===========================================================================

class TestSocketIO:
    def test_connect_emits_status(self, socketio_client):
        received = socketio_client.get_received()
        event_names = [msg["name"] for msg in received]
        assert "connected" in event_names
        assert "status_update" in event_names

    def test_connect_rejected_without_auth(self):
        """When auth is enabled, unauthenticated SocketIO connections are rejected."""
        webui_mod.AUTH_ENABLED = True
        webui_mod.AUTH_USER = "admin"
        webui_mod.AUTH_PASSWORD = "secret"

        webui_mod.app.config["TESTING"] = True
        client = webui_mod.socketio.test_client(webui_mod.app)
        assert not client.is_connected()
        webui_mod.AUTH_ENABLED = False
        webui_mod.AUTH_USER = ""
        webui_mod.AUTH_PASSWORD = ""


# ===========================================================================
# TestDetectPatterns
# ===========================================================================

class TestDetectPatterns:
    def test_detect_patterns_valid_image(self, app_client):
        mock_updater = webui_mod.updater
        mock_updater._parse_image_reference.return_value = ("registry-1.docker.io", "library", "nginx")
        mock_updater._get_all_tags_by_date.return_value = ["1.24.0", "1.25.0", "latest"]

        with patch("webui.detect_tag_patterns", return_value=[{"regex": "^[0-9]+$", "label": "semver"}]):
            with patch("webui.detect_base_tags", return_value=["1.25.0"]):
                resp = _post_json(app_client, "/api/detect-patterns", {"image": "nginx"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert "patterns" in data
        assert data["total_tags"] == 3

    def test_detect_patterns_empty_image(self, app_client):
        resp = _post_json(app_client, "/api/detect-patterns", {"image": ""})
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"].lower()


# ===========================================================================
# TestAuthManager
# ===========================================================================

from ium import AuthManager


class TestAuthManager:
    def test_env_var_credentials_take_priority(self, tmp_path, monkeypatch):
        """Env vars override stored or generated credentials."""
        monkeypatch.setenv("WEBUI_USER", "envuser")
        monkeypatch.setenv("WEBUI_PASSWORD", "envpass")
        am = AuthManager(tmp_path)
        assert am.user == "envuser"
        assert am.password == "envpass"
        # No auth file should be written when using env vars
        assert not (tmp_path / AuthManager.AUTH_FILE).exists()

    def test_generates_credentials_on_first_run(self, tmp_path, monkeypatch):
        """Credentials are auto-generated and persisted when no env vars or stored creds."""
        monkeypatch.delenv("WEBUI_USER", raising=False)
        monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
        am = AuthManager(tmp_path)
        assert am.user == "admin"
        assert len(am.password) > 0
        auth_file = tmp_path / AuthManager.AUTH_FILE
        assert auth_file.exists()

    def test_generated_credentials_are_persisted(self, tmp_path, monkeypatch):
        """Generated credentials are stored in .auth.json."""
        monkeypatch.delenv("WEBUI_USER", raising=False)
        monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
        am1 = AuthManager(tmp_path)
        am2 = AuthManager(tmp_path)
        assert am1.user == am2.user
        assert am1.password == am2.password

    def test_stored_credentials_loaded_on_subsequent_runs(self, tmp_path, monkeypatch):
        """Second AuthManager instance loads the same credentials from disk."""
        monkeypatch.delenv("WEBUI_USER", raising=False)
        monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
        am1 = AuthManager(tmp_path)
        first_password = am1.password
        am2 = AuthManager(tmp_path)
        assert am2.password == first_password

    def test_auth_file_has_secure_permissions(self, tmp_path, monkeypatch):
        """Generated .auth.json must be owner-read-only (0600)."""
        monkeypatch.delenv("WEBUI_USER", raising=False)
        monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
        AuthManager(tmp_path)
        auth_file = tmp_path / AuthManager.AUTH_FILE
        mode = auth_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_env_var_does_not_overwrite_stored_credentials(self, tmp_path, monkeypatch):
        """Env-var credentials must not modify the stored .auth.json."""
        monkeypatch.delenv("WEBUI_USER", raising=False)
        monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
        # First: generate and store credentials
        am1 = AuthManager(tmp_path)
        stored_password = am1.password

        # Second: env vars set — stored file should be unchanged
        monkeypatch.setenv("WEBUI_USER", "override")
        monkeypatch.setenv("WEBUI_PASSWORD", "overridepass")
        am2 = AuthManager(tmp_path)
        assert am2.user == "override"
        assert am2.password == "overridepass"

        # The stored file should still hold original credentials
        import json as _json
        data = _json.loads((tmp_path / AuthManager.AUTH_FILE).read_text())
        assert data["password"] == stored_password

    def test_auth_file_content_structure(self, tmp_path, monkeypatch):
        """The .auth.json file must have version, username, and password fields."""
        monkeypatch.delenv("WEBUI_USER", raising=False)
        monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
        am = AuthManager(tmp_path)
        import json as _json
        data = _json.loads((tmp_path / AuthManager.AUTH_FILE).read_text())
        assert data["version"] == 1
        assert data["username"] == am.user
        assert data["password"] == am.password


# ---------------------------------------------------------------------------
# POST /api/apply-update
# ---------------------------------------------------------------------------

class TestApplyUpdate:
    """Tests for the manual apply-update endpoint."""

    def test_apply_update_missing_fields(self, app_client):
        """Returns 400 when required fields are missing."""
        resp = _post_json(app_client, '/api/apply-update', {})
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_apply_update_image_not_in_last_updates(self, app_client):
        """Returns 404 when no pending update exists for the image."""
        resp = _post_json(app_client, '/api/apply-update', {
            'image': 'nonexistent/image',
            'new_tag': '1.0.0'
        })
        assert resp.status_code == 404
        assert 'error' in resp.get_json()

    def test_apply_update_success(self, app_client, monkeypatch):
        """Returns 200 and applies the update when a valid update is pending."""
        import webui as webui_mod

        # Seed a pending update into last_updates
        monkeypatch.setattr(webui_mod, 'last_updates', [{
            'image': 'nginx',
            'base_tag': 'latest',
            'old_tag': '1.26.0',
            'new_tag': '1.27.0',
            'digest': 'sha256:abc123',
            'auto_update': False
        }])

        # Mock the updater methods to avoid real Docker calls
        mock_pull = MagicMock(return_value=True)
        mock_get_containers = MagicMock(return_value=[{'name': 'my-nginx'}])
        mock_update_containers = MagicMock(return_value={'my-nginx': True})
        mock_get_config = MagicMock(return_value={
            'image': 'nginx',
            'regex': r'^[0-9]+\.[0-9]+\.[0-9]+$',
            'base_tag': 'latest',
            'registry': None,
            'cleanup_old_images': False,
            'keep_versions': 3
        })

        monkeypatch.setattr(webui_mod.updater, '_pull_image', mock_pull)
        monkeypatch.setattr(webui_mod.updater, '_get_containers_for_image', mock_get_containers)
        monkeypatch.setattr(webui_mod.updater, '_update_containers', mock_update_containers)

        resp = _post_json(app_client, '/api/apply-update', {
            'image': 'nginx',
            'new_tag': '1.27.0'
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        mock_pull.assert_called()
        mock_update_containers.assert_called_once_with(['my-nginx'], 'nginx', '1.27.0', None)

    def test_apply_update_pull_failure(self, app_client, monkeypatch):
        """Returns 500 when image pull fails."""
        import webui as webui_mod

        monkeypatch.setattr(webui_mod, 'last_updates', [{
            'image': 'nginx', 'base_tag': 'latest',
            'old_tag': '1.26.0', 'new_tag': '1.27.0',
            'digest': 'sha256:abc', 'auto_update': False
        }])
        monkeypatch.setattr(webui_mod.updater, '_pull_image', MagicMock(return_value=False))

        resp = _post_json(app_client, '/api/apply-update', {
            'image': 'nginx', 'new_tag': '1.27.0'
        })
        assert resp.status_code == 500
        assert 'error' in resp.get_json()

    def test_apply_update_docker_unreachable(self, app_client, monkeypatch):
        """Returns 503 and leaves state untouched when containers cannot be listed."""
        import webui as webui_mod
        from docker_api import DockerAPIError

        monkeypatch.setattr(webui_mod, 'last_updates', [{
            'image': 'nginx', 'base_tag': 'latest',
            'old_tag': '1.26.0', 'new_tag': '1.27.0',
            'digest': 'sha256:abc', 'auto_update': False
        }])
        monkeypatch.setattr(webui_mod.updater, '_pull_image', MagicMock(return_value=True))
        monkeypatch.setattr(webui_mod.updater, '_get_containers_for_image',
                            MagicMock(side_effect=DockerAPIError(0, "boom")))
        mock_update_containers = MagicMock()
        monkeypatch.setattr(webui_mod.updater, '_update_containers', mock_update_containers)

        resp = _post_json(app_client, '/api/apply-update', {
            'image': 'nginx', 'new_tag': '1.27.0'
        })
        assert resp.status_code == 503
        assert 'unreachable' in resp.get_json()['error']
        mock_update_containers.assert_not_called()
        assert 'nginx' not in webui_mod.updater.state

    def test_apply_update_requires_csrf(self, app_client):
        """Rejects request without CSRF header."""
        resp = app_client.post('/api/apply-update',
                               data=json.dumps({'image': 'nginx', 'new_tag': '1.0.0'}),
                               content_type='application/json')
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/updates/dismiss
# ---------------------------------------------------------------------------

def _downgrade_pending(dismissed=False):
    return {
        'tag': '1.25.0',
        'digest': 'sha256:old',
        'first_seen': '2026-10-24T00:00:00',
        'reason': 'downgrade',
        'dismissed': dismissed,
        'last_notified': '2026-10-24T00:00:00',
    }


class TestDismissUpdate:
    """Tests for the pending-downgrade dismiss endpoint."""

    def test_dismiss_marks_pending_and_trims(self, app_client, monkeypatch):
        """200: pending marked dismissed, state saved, entry dropped, tabs told."""
        from ium import ImageState

        webui_mod.updater.state['nginx'] = ImageState(
            base_tag='latest', tag='1.26.0', digest='sha256:cur',
            last_updated='2026-10-24T00:00:00', pending=_downgrade_pending())
        other = {'image': 'redis', 'old_tag': '7.0.0', 'new_tag': '7.2.0'}
        monkeypatch.setattr(webui_mod, 'last_updates', [
            {'image': 'nginx', 'old_tag': '1.26.0', 'new_tag': '1.25.0',
             'downgrade': True, 'auto_update': False, 'repeat': True},
            other,
        ])
        mock_emit = MagicMock()
        monkeypatch.setattr(webui_mod.socketio, 'emit', mock_emit)

        resp = _post_json(app_client, '/api/updates/dismiss', {'image': 'nginx'})

        assert resp.status_code == 200
        assert resp.get_json() == {'success': True}
        assert webui_mod.updater.state['nginx'].pending['dismissed'] is True
        webui_mod.updater._save_state.assert_called_once()
        assert webui_mod.last_updates == [other]
        mock_emit.assert_called_once()
        args, kwargs = mock_emit.call_args
        assert args[0] == 'check_complete'
        assert args[1]['updates'] == [other]
        assert kwargs.get('namespace') == '/'

    def test_dismiss_404_without_pending(self, app_client):
        """404 when the image has no state, or state without a downgrade pending."""
        from ium import ImageState

        resp = _post_json(app_client, '/api/updates/dismiss', {'image': 'nginx'})
        assert resp.status_code == 404
        assert 'error' in resp.get_json()

        webui_mod.updater.state['nginx'] = ImageState(
            base_tag='latest', tag='1.26.0', digest='sha256:cur',
            last_updated='2026-10-24T00:00:00')
        resp = _post_json(app_client, '/api/updates/dismiss', {'image': 'nginx'})
        assert resp.status_code == 404
        assert 'error' in resp.get_json()
        webui_mod.updater._save_state.assert_not_called()

    def test_dismiss_requires_csrf(self, app_client):
        """Rejects request without CSRF header."""
        resp = app_client.post('/api/updates/dismiss',
                               data=json.dumps({'image': 'nginx'}),
                               content_type='application/json')
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# run_check history filtering
# ---------------------------------------------------------------------------

class TestRunCheckHistory:
    """Re-reported pending downgrades stay out of update_history."""

    def _run(self, monkeypatch, updates):
        webui_mod.updater.check_and_update = MagicMock(return_value=updates)
        monkeypatch.setattr(webui_mod.socketio, 'emit', MagicMock())
        monkeypatch.setattr(webui_mod, 'save_history', MagicMock())
        webui_mod.run_check()

    def test_repeat_update_not_appended_to_history(self, app_client, monkeypatch):
        update = {'image': 'nginx', 'old_tag': '1.26.0', 'new_tag': '1.25.0',
                  'downgrade': True, 'auto_update': False, 'repeat': True}
        self._run(monkeypatch, [update])
        assert webui_mod.update_history == []
        assert webui_mod.last_updates == [update]

    def test_first_detection_appended_to_history(self, app_client, monkeypatch):
        update = {'image': 'nginx', 'old_tag': '1.26.0', 'new_tag': '1.25.0',
                  'downgrade': True, 'auto_update': False, 'repeat': False}
        self._run(monkeypatch, [update])
        assert len(webui_mod.update_history) == 1
        assert webui_mod.update_history[0]['new_tag'] == '1.25.0'
