"""Tests for registry override handling in the update flow.

Regression coverage for the bug where _update_container built the image
reference without the registry prefix (e.g. homarr-labs/homarr:v1.9.0)
even though the image was pulled and stored as ghcr.io/homarr-labs/homarr:v1.9.0,
causing Docker to fail with "No such image" on container creation and leaving
state unsaved so the update was re-detected on every check cycle.
"""

import json
import pytest
from unittest.mock import patch, call

from ium import DEFAULT_REQUEST_TIMEOUT, DEFAULT_STOP_TIMEOUT, DockerImageUpdater


@pytest.fixture
def ghcr_updater(tmp_path):
    """DockerImageUpdater configured with a ghcr.io registry override."""
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"

    config = {
        "images": [{
            "image": "homarr-labs/homarr",
            "regex": r"^v[0-9]+\.[0-9]+\.[0-9]+$",
            "base_tag": "latest",
            "auto_update": True,
            "registry": "ghcr.io",
        }]
    }

    config_file.write_text(json.dumps(config))
    state_file.write_text("{}")

    return DockerImageUpdater(str(config_file), str(state_file))


# ---------------------------------------------------------------------------
# _update_container: image reference construction
# ---------------------------------------------------------------------------

class TestUpdateContainerImageReference:
    """_update_container must pass a registry-prefixed image to Docker."""

    def _patch_docker_ops(self, updater, captured_image):
        """Return a context-manager stack that captures the image arg to _build_create_config."""

        def capture_build_config(container_name, image, container_info):
            captured_image.append(image)
            return {"Image": image}, []

        return [
            patch.object(updater, "_get_container_config",
                         return_value={"Id": "abc123", "Image": "sha256:old"}),
            patch.object(updater, "_build_create_config",
                         side_effect=capture_build_config),
            patch.object(updater.docker, "stop_container"),
            patch.object(updater.docker, "rename_container"),
            patch.object(updater.docker, "create_container", return_value="newid"),
            patch.object(updater.docker, "start_container"),
            patch.object(updater.docker, "remove_container"),
        ]

    def test_registry_override_prefixes_image(self, ghcr_updater):
        """Container create receives ghcr.io/image:tag, not bare image:tag.

        Before the fix, Docker would reject the call with 'No such image'
        because the locally cached image was keyed under the full registry path.
        """
        captured = []
        patches = self._patch_docker_ops(ghcr_updater, captured)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = ghcr_updater._update_container(
                "homarr", "homarr-labs/homarr", "v1.9.0", registry="ghcr.io"
            )

        assert result is True
        assert captured == ["ghcr.io/homarr-labs/homarr:v1.9.0"]

    def test_no_registry_uses_bare_image(self, ghcr_updater):
        """Without a registry override the image reference is unchanged."""
        captured = []
        patches = self._patch_docker_ops(ghcr_updater, captured)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = ghcr_updater._update_container(
                "sonarr", "linuxserver/sonarr", "4.0.16.2944-ls299"
            )

        assert result is True
        assert captured == ["linuxserver/sonarr:4.0.16.2944-ls299"]

    def test_already_registry_prefixed_image_not_double_prefixed(self, ghcr_updater):
        """Image already containing the registry host must not be double-prefixed."""
        captured = []
        patches = self._patch_docker_ops(ghcr_updater, captured)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = ghcr_updater._update_container(
                "homarr", "ghcr.io/homarr-labs/homarr", "v1.9.0", registry="ghcr.io"
            )

        assert result is True
        assert captured == ["ghcr.io/homarr-labs/homarr:v1.9.0"]


# ---------------------------------------------------------------------------
# _update_containers: registry forwarding
# ---------------------------------------------------------------------------

class TestUpdateContainersRegistryForwarding:
    """_update_containers must thread registry through to every _update_container call."""

    def test_registry_forwarded_to_single_container(self, ghcr_updater):
        with patch.object(ghcr_updater, "_update_container", return_value=True) as mock_update:
            ghcr_updater._update_containers(
                ["homarr"], "homarr-labs/homarr", "v1.9.0", registry="ghcr.io"
            )

        mock_update.assert_called_once_with(
            "homarr", "homarr-labs/homarr", "v1.9.0", "ghcr.io",
            stop_timeout=DEFAULT_STOP_TIMEOUT,
            request_timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def test_registry_forwarded_to_every_container(self, ghcr_updater):
        with patch.object(ghcr_updater, "_update_container", return_value=True) as mock_update:
            ghcr_updater._update_containers(
                ["homarr-a", "homarr-b"], "homarr-labs/homarr", "v1.9.0", registry="ghcr.io"
            )

        assert mock_update.call_count == 2
        for c in mock_update.call_args_list:
            # positional: (container_name, image, tag, registry)
            assert c.args[3] == "ghcr.io"


# ---------------------------------------------------------------------------
# check_and_update: end-to-end registry threading and state behaviour
# ---------------------------------------------------------------------------

class TestCheckAndUpdateRegistryFlow:
    """End-to-end tests for how registry config flows through check_and_update."""

    _containers = [
        {"name": "homarr", "id": "abc", "state": "running",
         "image_ref": "ghcr.io/homarr-labs/homarr:v1.8.0"},
    ]

    def test_registry_passed_to_update_containers(self, ghcr_updater):
        """check_and_update must forward the config registry to _update_containers."""
        with patch.object(ghcr_updater, "_get_containers_for_image", return_value=self._containers), \
             patch.object(ghcr_updater, "find_matching_tag", return_value=("v1.9.0", "sha256:new")), \
             patch.object(ghcr_updater, "_get_container_current_tag", return_value="v1.8.0"), \
             patch.object(ghcr_updater, "_pull_image", return_value=True), \
             patch.object(ghcr_updater, "_update_containers",
                          return_value={"homarr": True}) as mock_update:
            ghcr_updater.check_and_update()

        mock_update.assert_called_once_with(
            ["homarr"], "homarr-labs/homarr", "v1.9.0", "ghcr.io",
            stop_timeout=DEFAULT_STOP_TIMEOUT,
            request_timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    def test_failed_update_does_not_save_state(self, ghcr_updater):
        """When container creation fails, state is NOT saved so the update is
        retried next cycle (this is intentional — the image may now be correct
        on disk after the pull, so a retry makes sense)."""
        with patch.object(ghcr_updater, "_get_containers_for_image", return_value=self._containers), \
             patch.object(ghcr_updater, "find_matching_tag", return_value=("v1.9.0", "sha256:new")), \
             patch.object(ghcr_updater, "_get_container_current_tag", return_value="v1.8.0"), \
             patch.object(ghcr_updater, "_pull_image", return_value=True), \
             patch.object(ghcr_updater, "_update_containers", return_value={"homarr": False}):
            ghcr_updater.check_and_update()

        assert "homarr-labs/homarr" not in ghcr_updater.state

    def test_successful_update_saves_state_stopping_redetection(self, ghcr_updater):
        """When the update succeeds, state is saved and the second cycle finds
        no update — the re-detection loop is broken."""
        with patch.object(ghcr_updater, "_get_containers_for_image", return_value=self._containers), \
             patch.object(ghcr_updater, "find_matching_tag", return_value=("v1.9.0", "sha256:new")), \
             patch.object(ghcr_updater, "_get_container_current_tag", return_value="v1.8.0"), \
             patch.object(ghcr_updater, "_pull_image", return_value=True), \
             patch.object(ghcr_updater, "_update_containers", return_value={"homarr": True}):
            ghcr_updater.check_and_update()

        # State must reflect the new version
        saved = ghcr_updater.state["homarr-labs/homarr"]
        assert saved.tag == "v1.9.0"
        assert saved.digest == "sha256:new"

        # Second cycle: same digest → no update reported, no pull attempted
        with patch.object(ghcr_updater, "_get_containers_for_image", return_value=self._containers), \
             patch.object(ghcr_updater, "find_matching_tag", return_value=("v1.9.0", "sha256:new")), \
             patch.object(ghcr_updater, "_pull_image") as mock_pull:
            updates = ghcr_updater.check_and_update()

        assert updates == []
        mock_pull.assert_not_called()
