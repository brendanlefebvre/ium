"""Per-image timeout configuration.

The 2026-07-29 incident timed out stopping a heavy container: the socket
budget for POST /containers/{name}/stop was hardcoded at grace + 30s = 40s,
and Synology needed longer.  Two knobs are configurable per image:

  stop_timeout    -- Docker's SIGTERM grace period (the `t` query param)
  request_timeout -- socket read budget for container lifecycle calls

Both must reach the Docker client, not just the config schema.
"""

import json
import pytest
from unittest.mock import patch
from jsonschema import ValidationError, validate

from docker_api import DockerClient
from ium import CONFIG_SCHEMA, DockerImageUpdater


@pytest.fixture
def updater(tmp_path):
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"
    config_file.write_text(json.dumps({
        "images": [{
            "image": "linuxserver/calibre",
            "regex": r"^v[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
            "base_tag": "latest",
            "auto_update": True,
            "stop_timeout": 60,
            "request_timeout": 180,
        }]
    }))
    state_file.write_text("{}")
    return DockerImageUpdater(str(config_file), str(state_file))


CONTAINER_INFO = {
    "Id": "abc123" * 10,
    "Config": {
        "Image": "linuxserver/calibre:v9.10.0-ls400",
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


class TestConfigSchema:

    def test_schema_accepts_timeout_keys(self):
        config = {"images": [{
            "image": "linuxserver/calibre",
            "regex": r"^v.*$",
            "stop_timeout": 60,
            "request_timeout": 180,
        }]}
        validate(instance=config, schema=CONFIG_SCHEMA)

    @pytest.mark.parametrize("key", ["stop_timeout", "request_timeout"])
    @pytest.mark.parametrize("bad_value", ["60", 0, -5, 1.5])
    def test_schema_rejects_invalid_timeouts(self, key, bad_value):
        """Typos and nonsense values must fail validation, not reach Docker."""
        config = {"images": [{
            "image": "linuxserver/calibre",
            "regex": r"^v.*$",
            key: bad_value,
        }]}
        with pytest.raises(ValidationError):
            validate(instance=config, schema=CONFIG_SCHEMA)


class TestDockerClientTimeouts:
    """stop_container must separate the grace period from the socket budget."""

    def test_stop_socket_budget_is_grace_plus_request_timeout(self):
        captured = {}

        def fake_request(method, path, body=None, query=None, timeout=30, stream=False):
            captured['query'] = query
            captured['timeout'] = timeout

        client = DockerClient(socket_path="/nonexistent.sock")
        with patch.object(client, '_request', side_effect=fake_request):
            client.stop_container("calibre", timeout=60, request_timeout=180)

        assert captured['query'] == {"t": "60"}, "grace period must reach Docker"
        assert captured['timeout'] == 240, (
            "socket budget must be grace + request_timeout so a slow daemon "
            "cannot expire the read before the grace period elapses"
        )

    def test_stop_socket_budget_defaults_preserve_old_behaviour(self):
        captured = {}

        def fake_request(method, path, body=None, query=None, timeout=30, stream=False):
            captured['timeout'] = timeout

        client = DockerClient(socket_path="/nonexistent.sock")
        with patch.object(client, '_request', side_effect=fake_request):
            client.stop_container("calibre")

        assert captured['timeout'] == 40, "default stays grace(10) + 30"


class TestTimeoutsReachDockerClient:
    """The configured values must be applied to the actual Docker calls."""

    def test_update_container_applies_both_timeouts(self, updater):
        with patch.object(updater, '_get_container_config', return_value=CONTAINER_INFO), \
             patch.object(updater.docker, 'stop_container') as mock_stop, \
             patch.object(updater.docker, 'rename_container'), \
             patch.object(updater.docker, 'create_container', return_value='new123') as mock_create, \
             patch.object(updater.docker, 'start_container') as mock_start, \
             patch.object(updater.docker, 'remove_container'):

            result = updater._update_container(
                'calibre', 'linuxserver/calibre', 'v9.11.0-ls414',
                stop_timeout=60, request_timeout=180,
            )

        assert result is True
        assert mock_stop.call_args.kwargs.get('timeout') == 60
        assert mock_stop.call_args.kwargs.get('request_timeout') == 180
        assert mock_create.call_args.kwargs.get('timeout') == 180
        assert mock_start.call_args.kwargs.get('timeout') == 180

    def test_update_containers_forwards_timeouts(self, updater):
        with patch.object(updater, '_update_container', return_value=True) as mock_one:
            updater._update_containers(
                ['calibre'], 'linuxserver/calibre', 'v9.11.0-ls414',
                stop_timeout=60, request_timeout=180,
            )

        assert mock_one.call_args.kwargs.get('stop_timeout') == 60
        assert mock_one.call_args.kwargs.get('request_timeout') == 180


class TestConfigValuesReachUpdate:
    """Values in config.json must flow through check_and_update."""

    def test_configured_timeouts_are_passed_through(self, updater):
        containers = [{
            'name': 'calibre', 'id': 'abc123', 'state': 'running',
            'image_ref': 'linuxserver/calibre:v9.10.0-ls400',
        }]

        with patch.object(updater, '_get_containers_for_image', return_value=containers), \
             patch.object(updater, 'find_matching_tag',
                          return_value=('v9.11.0-ls414', 'sha256:newdigest')), \
             patch.object(updater, '_get_container_current_tag', return_value='v9.10.0-ls400'), \
             patch.object(updater, '_pull_image', return_value=True), \
             patch.object(updater, '_update_containers',
                          return_value={'calibre': True}) as mock_update:

            updater.check_and_update()

        assert mock_update.call_args.kwargs.get('stop_timeout') == 60
        assert mock_update.call_args.kwargs.get('request_timeout') == 180

    def test_defaults_used_when_config_omits_them(self, tmp_path):
        config_file = tmp_path / "config.json"
        state_file = tmp_path / "state.json"
        config_file.write_text(json.dumps({
            "images": [{
                "image": "linuxserver/calibre",
                "regex": r"^v[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
                "auto_update": True,
            }]
        }))
        state_file.write_text("{}")
        upd = DockerImageUpdater(str(config_file), str(state_file))

        containers = [{
            'name': 'calibre', 'id': 'abc123', 'state': 'running',
            'image_ref': 'linuxserver/calibre:v9.10.0-ls400',
        }]

        with patch.object(upd, '_get_containers_for_image', return_value=containers), \
             patch.object(upd, 'find_matching_tag',
                          return_value=('v9.11.0-ls414', 'sha256:newdigest')), \
             patch.object(upd, '_get_container_current_tag', return_value='v9.10.0-ls400'), \
             patch.object(upd, '_pull_image', return_value=True), \
             patch.object(upd, '_update_containers',
                          return_value={'calibre': True}) as mock_update:

            upd.check_and_update()

        assert mock_update.call_args.kwargs.get('stop_timeout') == 10
        assert mock_update.call_args.kwargs.get('request_timeout') == 60
