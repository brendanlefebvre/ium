"""Recovery when a previous update was interrupted after container create.

If create_container succeeded on the daemon but its response timed out, the
next cycle finds a container already carrying the target image that was never
started.  The retry short-circuit must not report success for it — it checked
the image but not the state, so a permanently-down container was reported as
"already running".
"""

import json
import pytest
from unittest.mock import patch

from docker_api import DockerAPIError
from ium import DockerImageUpdater


TARGET_TAG = "4.0.16.2944-ls299"
TARGET_IMAGE = f"linuxserver/sonarr:{TARGET_TAG}"


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


def _info(status: str) -> dict:
    return {
        "Config": {"Image": TARGET_IMAGE},
        "State": {"Status": status},
    }


class TestInterruptedUpdateRecovery:

    def test_container_created_but_not_started_is_started(self, updater):
        """A 'created' container on the target image must be started."""
        with patch.object(updater, '_get_container_config', return_value=_info('created')), \
             patch.object(updater.docker, 'start_container') as mock_start, \
             patch.object(updater.docker, 'stop_container') as mock_stop:

            result = updater._update_container('sonarr', 'linuxserver/sonarr', TARGET_TAG)

            mock_start.assert_called_once_with('sonarr')
            mock_stop.assert_not_called()
            assert result is True

    def test_exited_container_on_target_image_is_started(self, updater):
        """An 'exited' container on the target image must be started."""
        with patch.object(updater, '_get_container_config', return_value=_info('exited')), \
             patch.object(updater.docker, 'start_container') as mock_start:

            result = updater._update_container('sonarr', 'linuxserver/sonarr', TARGET_TAG)

            mock_start.assert_called_once_with('sonarr')
            assert result is True

    def test_running_container_on_target_image_is_left_alone(self, updater):
        """The existing skip path must not touch a healthy container."""
        with patch.object(updater, '_get_container_config', return_value=_info('running')), \
             patch.object(updater.docker, 'start_container') as mock_start, \
             patch.object(updater.docker, 'stop_container') as mock_stop:

            result = updater._update_container('sonarr', 'linuxserver/sonarr', TARGET_TAG)

            mock_start.assert_not_called()
            mock_stop.assert_not_called()
            assert result is True

    def test_failure_to_start_reports_failure(self, updater):
        """If the container cannot be started, do not claim success."""
        with patch.object(updater, '_get_container_config', return_value=_info('created')), \
             patch.object(updater.docker, 'start_container',
                          side_effect=DockerAPIError(0, "POST /start: timed out")):

            result = updater._update_container('sonarr', 'linuxserver/sonarr', TARGET_TAG)

            assert result is False, (
                "a container that could not be started must not be reported as updated"
            )
