"""Integration tests for multi-container update scenarios."""

import json
import pytest
from unittest.mock import Mock, patch, call
from ium import DEFAULT_REQUEST_TIMEOUT, DEFAULT_STOP_TIMEOUT, DockerImageUpdater
from tests.fake_docker import FakeResponse, fake_socket


@pytest.fixture
def updater(tmp_path):
    """Create a DockerImageUpdater instance with test paths."""
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"

    config = {
        "images": [{
            "image": "linuxserver/sonarr",
            "regex": r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
            "base_tag": "latest",
            "auto_update": True,
        }]
    }

    config_file.write_text(json.dumps(config))
    state_file.write_text("{}")

    return DockerImageUpdater(str(config_file), str(state_file))


class TestMultiContainerUpdate:
    """Test updating multiple containers with the same image."""

    def test_two_containers_both_updated(self, updater):
        """Test that both containers get updated when update is available."""
        # Mock container discovery
        containers = [
            {'name': 'sonarr-hd', 'id': 'abc123', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
            {'name': 'sonarr-4k', 'id': 'def456', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
        ]

        with patch.object(updater, '_get_containers_for_image', return_value=containers), \
             patch.object(updater, 'find_matching_tag', return_value=('4.0.16.2944-ls299', 'sha256:newdigest')), \
             patch.object(updater, '_get_container_current_tag', return_value='4.0.0.740-ls290'), \
             patch.object(updater, '_pull_image', return_value=True), \
             patch.object(updater, '_update_containers', return_value={'sonarr-hd': True, 'sonarr-4k': True}) as mock_update:

            updates = updater.check_and_update()

            # Verify update was called with both container names (registry=None for Docker Hub)
            mock_update.assert_called_once_with(
                ['sonarr-hd', 'sonarr-4k'],
                'linuxserver/sonarr',
                '4.0.16.2944-ls299',
                None,
                stop_timeout=DEFAULT_STOP_TIMEOUT,
                request_timeout=DEFAULT_REQUEST_TIMEOUT,
            )

            # Verify update was detected
            assert len(updates) == 1
            assert updates[0]['old_tag'] == '4.0.0.740-ls290'
            assert updates[0]['new_tag'] == '4.0.16.2944-ls299'

    def test_partial_update_failure_does_not_update_state(self, updater):
        """Test that state is NOT updated when some containers fail, so the update is retried."""
        containers = [
            {'name': 'sonarr-hd', 'id': 'abc123', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
            {'name': 'sonarr-4k', 'id': 'def456', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
        ]

        with patch.object(updater, '_get_containers_for_image', return_value=containers), \
             patch.object(updater, 'find_matching_tag', return_value=('4.0.16.2944-ls299', 'sha256:newdigest')), \
             patch.object(updater, '_get_container_current_tag', return_value='4.0.0.740-ls290'), \
             patch.object(updater, '_pull_image', return_value=True), \
             patch.object(updater, '_update_containers', return_value={'sonarr-hd': True, 'sonarr-4k': False}):

            updater.check_and_update()

            # State should NOT be updated — partial failure triggers retry next cycle
            assert 'linuxserver/sonarr' not in updater.state

    def test_no_containers_image_only_update(self, updater):
        """Test updating image when no containers exist."""
        with patch.object(updater, '_get_containers_for_image', return_value=[]), \
             patch.object(updater, 'find_matching_tag', return_value=('4.0.16.2944-ls299', 'sha256:newdigest')), \
             patch.object(updater, '_pull_image', return_value=True), \
             patch.object(updater, '_update_containers') as mock_update:

            updates = updater.check_and_update()

            # Should not call _update_containers when no containers found
            mock_update.assert_not_called()

            # State should still be updated
            assert 'linuxserver/sonarr' in updater.state
            assert updater.state['linuxserver/sonarr'].tag == '4.0.16.2944-ls299'

    def test_version_detection_from_first_container(self, updater):
        """Test that version is detected from first container when no saved state."""
        containers = [
            {'name': 'sonarr-hd', 'id': 'abc123', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
            {'name': 'sonarr-4k', 'id': 'def456', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
        ]

        with patch.object(updater, '_get_containers_for_image', return_value=containers), \
             patch.object(updater, 'find_matching_tag', return_value=('4.0.16.2944-ls299', 'sha256:newdigest')), \
             patch.object(updater, '_get_container_current_tag', return_value='4.0.0.740-ls290') as mock_get_tag, \
             patch.object(updater, '_pull_image', return_value=True), \
             patch.object(updater, '_update_containers', return_value={'sonarr-hd': True, 'sonarr-4k': True}):

            updates = updater.check_and_update()

            # Should query first container for current version
            mock_get_tag.assert_called_once_with('sonarr-hd', 'linuxserver/sonarr', updater.config['images'][0]['regex'], None)

            # Update should use detected version as old_tag
            assert updates[0]['old_tag'] == '4.0.0.740-ls290'


class TestNoAutoUpdate:
    """Test behavior when auto_update is false."""

    def test_containers_not_updated_when_auto_update_false(self, updater):
        """Test that containers aren't updated when auto_update is false."""
        # Modify config to disable auto_update
        updater.config['images'][0]['auto_update'] = False

        containers = [
            {'name': 'sonarr', 'id': 'abc123', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
        ]

        with patch.object(updater, '_get_containers_for_image', return_value=containers), \
             patch.object(updater, 'find_matching_tag', return_value=('4.0.16.2944-ls299', 'sha256:newdigest')), \
             patch.object(updater, '_get_container_current_tag', return_value='4.0.0.740-ls290'), \
             patch.object(updater, '_pull_image') as mock_pull, \
             patch.object(updater, '_update_containers') as mock_update:

            updates = updater.check_and_update()

            # Should not pull or update
            mock_pull.assert_not_called()
            mock_update.assert_not_called()

            # But state should still be updated to prevent re-reporting
            assert 'linuxserver/sonarr' in updater.state
            assert updates[0]['auto_update'] is False


class TestImageCleanup:
    """Test image cleanup after updates."""

    def test_cleanup_after_successful_update(self, updater):
        """Test that old images are cleaned up after successful update."""
        updater.config['images'][0]['cleanup_old_images'] = True
        updater.config['images'][0]['keep_versions'] = 3

        containers = [
            {'name': 'sonarr', 'id': 'abc123', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
        ]

        with patch.object(updater, '_get_containers_for_image', return_value=containers), \
             patch.object(updater, 'find_matching_tag', return_value=('4.0.16.2944-ls299', 'sha256:newdigest')), \
             patch.object(updater, '_get_container_current_tag', return_value='4.0.0.740-ls290'), \
             patch.object(updater, '_pull_image', return_value=True), \
             patch.object(updater, '_update_containers', return_value={'sonarr': True}), \
             patch.object(updater, '_cleanup_old_images') as mock_cleanup:

            updater.check_and_update()

            # Should call cleanup after successful update
            mock_cleanup.assert_called_once_with('linuxserver/sonarr', 3)

    def test_no_cleanup_after_failed_update(self, updater):
        """Test that cleanup doesn't happen if update fails."""
        updater.config['images'][0]['cleanup_old_images'] = True

        containers = [
            {'name': 'sonarr', 'id': 'abc123', 'state': 'running', 'image_ref': 'linuxserver/sonarr:4.0.0.740'},
        ]

        with patch.object(updater, '_get_containers_for_image', return_value=containers), \
             patch.object(updater, 'find_matching_tag', return_value=('4.0.16.2944-ls299', 'sha256:newdigest')), \
             patch.object(updater, '_get_container_current_tag', return_value='4.0.0.740-ls290'), \
             patch.object(updater, '_pull_image', return_value=True), \
             patch.object(updater, '_update_containers', return_value={'sonarr': False}), \
             patch.object(updater, '_cleanup_old_images') as mock_cleanup:

            updater.check_and_update()

            # Should NOT call cleanup after failed update
            mock_cleanup.assert_not_called()


class TestSkipAlreadyUpdated:
    """Containers already running the target image should be skipped on retry."""

    def test_skip_container_already_on_target_image(self, updater):
        """A container already running the target image is skipped (returns True)."""
        container_info = {
            'Image': 'sha256:' + 'a' * 64,
            'Config': {'Image': 'linuxserver/sonarr:4.0.16.2944-ls299'},
            'State': {'Status': 'running'},
        }
        with patch.object(updater, '_get_container_config', return_value=container_info), \
             patch.object(updater.docker, 'list_images', return_value=[{'Id': 'sha256:' + 'a' * 64}]), \
             patch.object(updater.docker, 'stop_container') as mock_stop:

            result = updater._update_container('sonarr', 'linuxserver/sonarr', '4.0.16.2944-ls299')
            assert result is True
            mock_stop.assert_not_called()

    def test_update_container_with_different_image(self, updater):
        """A container on a different image proceeds with the update."""
        container_info = {
            'Id': 'abc123' * 10,
            'Config': {
                'Image': 'linuxserver/sonarr:4.0.0.740-ls290',
                'Hostname': 'abc123abc1',
                'User': '', 'WorkingDir': '',
                'Env': ['PATH=/usr/bin:/bin'],
                'Cmd': None, 'Labels': {},
            },
            'HostConfig': {
                'RestartPolicy': {'Name': '', 'MaximumRetryCount': 0},
                'NetworkMode': 'default',
                'PortBindings': None,
                'Privileged': False, 'CapAdd': None, 'CapDrop': None,
                'Devices': None, 'Memory': 0, 'CpuShares': 0,
                'CpuQuota': 0, 'SecurityOpt': None, 'Runtime': '',
            },
            'Mounts': [],
            'NetworkSettings': {'Networks': {}},
        }
        with patch.object(updater, '_get_container_config', return_value=container_info), \
             patch.object(updater.docker, 'list_images', return_value=[]), \
             patch.object(updater.docker, 'stop_container'), \
             patch.object(updater.docker, 'rename_container'), \
             patch.object(updater.docker, 'create_container', return_value='new123'), \
             patch.object(updater.docker, 'start_container'), \
             patch.object(updater.docker, 'remove_container'):

            result = updater._update_container('sonarr', 'linuxserver/sonarr', '4.0.16.2944-ls299')
            assert result is True


# ---------------------------------------------------------------------------
# Spec Part 3: the skip check compares image IDs, not reference strings
# ---------------------------------------------------------------------------

SKIP_TARGET_TAG = '4.0.16.2944-ls299'
SKIP_TARGET_IMAGE = f'linuxserver/sonarr:{SKIP_TARGET_TAG}'
ID_A = 'sha256:' + 'a' * 64
ID_B = 'sha256:' + 'b' * 64


def _skip_container_info(image_id: str, status: str) -> dict:
    """A container whose Config.Image string already equals the target."""
    return {
        'Id': 'abc123' * 10,
        'Image': image_id,
        'Config': {
            'Image': SKIP_TARGET_IMAGE,
            'Hostname': 'abc123abc1',
            'User': '', 'WorkingDir': '',
            'Env': ['PATH=/usr/bin:/bin'],
            'Cmd': None, 'Labels': {},
        },
        'HostConfig': {
            'RestartPolicy': {'Name': '', 'MaximumRetryCount': 0},
            'NetworkMode': 'default',
            'PortBindings': None,
            'Privileged': False, 'CapAdd': None, 'CapDrop': None,
            'Devices': None, 'Memory': 0, 'CpuShares': 0,
            'CpuQuota': 0, 'SecurityOpt': None, 'Runtime': '',
        },
        'Mounts': [],
        'NetworkSettings': {'Networks': {}},
        'State': {'Status': status},
    }


class TestSkipCheckById:
    """Skip only when the container is on the image ID the target tag resolves to.

    Runs the real DockerClient request path over a fake socket.
    """

    @staticmethod
    def _run(updater, container_info, local_images):
        def handler(method, url, body):
            if method == 'GET' and '/containers/sonarr/json' in url:
                return FakeResponse(200, json.dumps(container_info).encode())
            if method == 'GET' and '/images/json' in url:
                return FakeResponse(200, json.dumps(local_images).encode())
            if method == 'POST' and '/containers/create' in url:
                return FakeResponse(201, json.dumps({'Id': 'new123'}).encode())
            return FakeResponse(204)

        patcher, calls = fake_socket(handler)
        with patcher:
            result = updater._update_container('sonarr', 'linuxserver/sonarr', SKIP_TARGET_TAG)
        creates = [u for m, u in calls if m == 'POST' and '/containers/create' in u]
        starts = [u for m, u in calls if m == 'POST' and u.endswith('/containers/sonarr/start')]
        stops = [u for m, u in calls if m == 'POST' and '/containers/sonarr/stop' in u]
        lookups = [u for m, u in calls if m == 'GET' and '/images/json' in u]
        return result, creates, starts, stops, lookups, calls

    def test_same_id_running_skips(self, updater):
        result, creates, starts, stops, lookups, calls = self._run(
            updater, _skip_container_info(ID_A, 'running'), [{'Id': ID_A}])
        assert result is True
        assert lookups, f'target image ID was never looked up; calls were: {calls}'
        assert not creates, f'a container on the target ID was recreated; calls were: {calls}'
        assert not stops and not starts, f'a healthy container was touched; calls were: {calls}'

    def test_same_id_exited_starts(self, updater):
        result, creates, starts, stops, lookups, calls = self._run(
            updater, _skip_container_info(ID_A, 'exited'), [{'Id': ID_A}])
        assert result is True
        assert starts, f'exited container on the target ID was not started; calls were: {calls}'
        assert not creates, f'exited container on the target ID was recreated; calls were: {calls}'
        assert not stops, f'exited container was stopped; calls were: {calls}'

    def test_different_id_recreates(self, updater):
        """Regression: Config.Image equals the target string, but the tag was re-pushed.

        The string check said "already running" while the container sat on an
        older image ID, so same-tag re-pushes were never applied.
        """
        result, creates, starts, stops, lookups, calls = self._run(
            updater, _skip_container_info(ID_A, 'running'), [{'Id': ID_B}])
        assert creates, (
            f'container on a stale image ID was skipped instead of recreated; calls were: {calls}'
        )

    def test_missing_local_tag_recreates(self, updater):
        result, creates, starts, stops, lookups, calls = self._run(
            updater, _skip_container_info(ID_A, 'running'), [])
        assert lookups, f'target image ID was never looked up; calls were: {calls}'
        assert creates, (
            f'an unresolvable target tag must not be treated as already running; calls were: {calls}'
        )

    def test_image_lookup_failure_touches_nothing(self, updater):
        """If the target ID cannot be read, neither skip nor recreate: report failure."""
        def handler(method, url, body):
            if method == 'GET' and '/containers/sonarr/json' in url:
                return FakeResponse(200, json.dumps(_skip_container_info(ID_A, 'running')).encode())
            if method == 'GET' and '/images/json' in url:
                raise TimeoutError('timed out')
            return FakeResponse(204)

        patcher, calls = fake_socket(handler)
        with patcher:
            result = updater._update_container('sonarr', 'linuxserver/sonarr', SKIP_TARGET_TAG)
        assert result is False
        assert not [c for c in calls if c[0] == 'POST'], (
            f'container was touched without knowing the target ID; calls were: {calls}'
        )
