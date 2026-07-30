"""A failed stop request does not mean the container is still running.

In the 2026-07-29 incident the stop request for a heavy container exceeded its
socket budget (43s against a 40s deadline), but the daemon went on to stop the
container anyway.  Abandoning the update on the failed request left the
container down until the next cycle, so before giving up we inspect the actual
state: if the container really is stopped, the update can proceed.
"""

import json
import pytest
from unittest.mock import patch

from docker_api import DockerAPIError
from ium import DockerImageUpdater


TARGET_TAG = "v9.11.0-ls414"
STOP_TIMED_OUT = DockerAPIError(0, "POST /v1.41/containers/calibre/stop: timed out")


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
        }]
    }))
    state_file.write_text("{}")
    return DockerImageUpdater(str(config_file), str(state_file))


def running_info(status: str = "running") -> dict:
    return {
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
        "State": {"Status": status},
    }


class TestStopFailureRecovery:

    def _run(self, updater, post_stop_state):
        """Attempt an update where the stop request fails.

        *post_stop_state* is what inspecting the container afterwards returns.
        """
        with patch.object(updater, '_get_container_config',
                          side_effect=[running_info(), post_stop_state]), \
             patch.object(updater.docker, 'stop_container',
                          side_effect=STOP_TIMED_OUT), \
             patch.object(updater.docker, 'rename_container') as mock_rename, \
             patch.object(updater.docker, 'create_container',
                          return_value='new123') as mock_create, \
             patch.object(updater.docker, 'start_container'), \
             patch.object(updater.docker, 'remove_container'):

            result = updater._update_container(
                'calibre', 'linuxserver/calibre', TARGET_TAG
            )

        return result, mock_rename, mock_create

    def test_update_proceeds_when_container_did_stop(self, updater):
        """The daemon finished the stop late — carry on with the update."""
        result, mock_rename, mock_create = self._run(updater, running_info('exited'))

        assert result is True
        mock_rename.assert_called_once()
        mock_create.assert_called_once()

    def test_update_proceeds_when_container_is_dead(self, updater):
        result, mock_rename, _ = self._run(updater, running_info('dead'))

        assert result is True
        mock_rename.assert_called_once()

    def test_update_aborts_when_container_still_running(self, updater):
        """Renaming a live container would be destructive — refuse."""
        result, mock_rename, mock_create = self._run(updater, running_info('running'))

        assert result is False
        mock_rename.assert_not_called()
        mock_create.assert_not_called()

    def test_update_aborts_when_container_is_paused(self, updater):
        """'paused' is not stopped; treat it as still running."""
        result, mock_rename, _ = self._run(updater, running_info('paused'))

        assert result is False
        mock_rename.assert_not_called()

    def test_update_aborts_when_state_cannot_be_determined(self, updater):
        """If the follow-up inspect also fails, do not guess."""
        result, mock_rename, _ = self._run(updater, None)

        assert result is False
        mock_rename.assert_not_called()

    def test_successful_stop_does_not_inspect_again(self, updater):
        """The extra inspect must only happen on the failure path."""
        with patch.object(updater, '_get_container_config',
                          side_effect=[running_info()]) as mock_inspect, \
             patch.object(updater.docker, 'stop_container'), \
             patch.object(updater.docker, 'rename_container'), \
             patch.object(updater.docker, 'create_container', return_value='new123'), \
             patch.object(updater.docker, 'start_container'), \
             patch.object(updater.docker, 'remove_container'):

            result = updater._update_container(
                'calibre', 'linuxserver/calibre', TARGET_TAG
            )

        assert result is True
        assert mock_inspect.call_count == 1
