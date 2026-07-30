"""A failed rename request leaves the container's identity ambiguous.

The rename to <name>_backup_<epoch> happens after the container is stopped but
before the replacement exists, so it is the most dangerous call in the update to
get a timeout on:

  * if the daemon applied it late, the container is now the backup and nothing
    answers to the original name -- later cycles inspect the original, get 404,
    and give up forever
  * if the daemon never applied it, the container is stopped under its own name
    and creating a replacement would collide with it

Neither can be assumed, so both names are inspected before deciding.
"""

import json
import pytest
from unittest.mock import patch

from docker_api import DockerAPIError
from ium import DockerImageUpdater


TARGET_TAG = "v9.11.0-ls414"
RENAME_TIMED_OUT = DockerAPIError(0, "POST /v1.41/containers/calibre/rename: timed out")
NOT_FOUND = DockerAPIError(404, "No such container")


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


def container_info() -> dict:
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
        "State": {"Status": "running"},
    }


class TestRenameFailureRecovery:

    def _run(self, updater, inspect_side_effect):
        """Attempt an update whose rename request fails.

        *inspect_side_effect* answers the post-rename existence probes, which
        query the backup name first and then the original name.
        """
        with patch.object(updater, '_get_container_config',
                          return_value=container_info()), \
             patch.object(updater.docker, 'stop_container'), \
             patch.object(updater.docker, 'rename_container',
                          side_effect=RENAME_TIMED_OUT) as mock_rename, \
             patch.object(updater.docker, 'inspect_container',
                          side_effect=inspect_side_effect), \
             patch.object(updater.docker, 'create_container',
                          return_value='new123') as mock_create, \
             patch.object(updater.docker, 'start_container') as mock_start, \
             patch.object(updater.docker, 'remove_container'):

            result = updater._update_container(
                'calibre', 'linuxserver/calibre', TARGET_TAG
            )

        return result, mock_create, mock_start, mock_rename

    def test_update_continues_when_rename_actually_landed(self, updater):
        """Backup exists and the original is gone: the rename did happen."""
        # probe order: backup_name -> exists, container_name -> 404
        result, mock_create, mock_start, _ = self._run(
            updater, [container_info(), NOT_FOUND]
        )

        assert result is True
        mock_create.assert_called_once()

    def test_original_is_restarted_when_rename_did_not_land(self, updater):
        """Original still present: abort, but do not leave it stopped."""
        # probe order: backup_name -> 404, container_name -> exists
        result, mock_create, mock_start, _ = self._run(
            updater, [NOT_FOUND, container_info()]
        )

        assert result is False
        mock_create.assert_not_called()
        mock_start.assert_called_once_with('calibre', timeout=60)

    def test_aborts_without_creating_when_state_is_indeterminate(self, updater):
        """Neither name resolvable: refuse to guess, never create a replacement."""
        transport_down = DockerAPIError(0, "GET /json: timed out")
        result, mock_create, mock_start, _ = self._run(
            updater, [transport_down, transport_down]
        )

        assert result is False
        mock_create.assert_not_called()
        mock_start.assert_not_called()

    def test_does_not_probe_when_rename_succeeds(self, updater):
        """The existence probes must only run on the failure path."""
        with patch.object(updater, '_get_container_config',
                          return_value=container_info()), \
             patch.object(updater.docker, 'stop_container'), \
             patch.object(updater.docker, 'rename_container'), \
             patch.object(updater.docker, 'inspect_container') as mock_inspect, \
             patch.object(updater.docker, 'create_container', return_value='new123'), \
             patch.object(updater.docker, 'start_container'), \
             patch.object(updater.docker, 'remove_container'):

            result = updater._update_container(
                'calibre', 'linuxserver/calibre', TARGET_TAG
            )

        assert result is True
        mock_inspect.assert_not_called()
