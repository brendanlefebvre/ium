"""Tests for container discovery functionality."""

import json
import pytest
from unittest.mock import Mock, patch
from ium import DockerImageUpdater
from docker_api import DockerAPIError


@pytest.fixture
def updater(tmp_path):
    """Create a DockerImageUpdater instance with test paths."""
    config_file = tmp_path / "config.json"
    state_file = tmp_path / "state.json"

    config = {
        "images": [{
            "image": "linuxserver/sonarr",
            "regex": r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
        }]
    }

    config_file.write_text(json.dumps(config))
    state_file.write_text("{}")

    return DockerImageUpdater(str(config_file), str(state_file))


class TestImageMatches:
    """Test the _image_matches helper method."""

    def test_exact_match(self, updater):
        """Test exact image name match."""
        assert updater._image_matches("nginx", "nginx")

    def test_tag_ignored(self, updater):
        """Test that tags are ignored in matching."""
        assert updater._image_matches("nginx", "nginx:alpine")
        assert updater._image_matches("nginx", "nginx:1.21")

    def test_registry_prefix_stripped(self, updater):
        """Test registry prefix handling."""
        assert updater._image_matches("linuxserver/sonarr", "lscr.io/linuxserver/sonarr:latest")
        assert updater._image_matches("linuxserver/sonarr", "ghcr.io/linuxserver/sonarr:4.0.0")

    def test_library_namespace(self, updater):
        """Test implicit library namespace."""
        assert updater._image_matches("postgres", "library/postgres:15")
        assert updater._image_matches("library/postgres", "postgres:15")

    def test_no_match_different_images(self, updater):
        """Test that different images don't match."""
        assert not updater._image_matches("nginx", "apache")
        assert not updater._image_matches("linuxserver/sonarr", "linuxserver/radarr")

    def test_portainer_standard(self, updater):
        """Test standard Portainer CE deployment."""
        assert updater._image_matches("portainer/portainer-ce", "portainer/portainer-ce:latest")
        assert updater._image_matches("portainer/portainer-ce", "portainer/portainer-ce:2.21.0")
        assert updater._image_matches("portainer/portainer-ce", "portainer/portainer-ce:2.21.0-alpine")

    def test_portainer_with_registry(self, updater):
        """Test Portainer from custom registries."""
        assert updater._image_matches("portainer/portainer-ce", "cr.portainer.io/portainer/portainer-ce:latest")
        assert updater._image_matches("portainer/portainer-ce", "docker.io/portainer/portainer-ce:2.21.0")
        assert updater._image_matches("portainer/portainer-ce", "index.docker.io/portainer/portainer-ce:latest")

    def test_digest_qualifier(self, updater):
        """Test images pinned with @sha256: digest."""
        assert updater._image_matches("portainer/portainer-ce", "portainer/portainer-ce:latest@sha256:abc123def456")
        assert updater._image_matches("nginx", "nginx:1.25@sha256:abc123def456")
        assert updater._image_matches("linuxserver/sonarr", "lscr.io/linuxserver/sonarr:latest@sha256:abc123")

    def test_localhost_registry_with_port(self, updater):
        """Test localhost registry with port number."""
        assert updater._image_matches("myapp", "localhost:5000/myapp:v1")
        assert updater._image_matches("myapp", "localhost:5000/myapp")
        assert updater._image_matches("org/myapp", "localhost:5000/org/myapp:v1")

    def test_registry_with_port(self, updater):
        """Test custom registry with port number."""
        assert updater._image_matches("myapp", "registry.local:5000/myapp:v1")
        assert updater._image_matches("org/myapp", "myregistry.io:5000/org/myapp:latest")


class TestGetContainersForImage:
    """Test the _get_containers_for_image method."""

    def test_no_containers(self, updater):
        """Test when no containers exist."""
        with patch.object(updater.docker, 'list_containers', return_value=[]):
            containers = updater._get_containers_for_image("nginx")
            assert containers == []

    def test_single_container(self, updater):
        """Test finding a single container."""
        api_containers = [{
            "Id": "abc123",
            "Names": ["/sonarr"],
            "Image": "linuxserver/sonarr:4.0.0.740",
            "State": "running"
        }]

        with patch.object(updater.docker, 'list_containers', return_value=api_containers):
            containers = updater._get_containers_for_image("linuxserver/sonarr")

            assert len(containers) == 1
            assert containers[0]['name'] == 'sonarr'
            assert containers[0]['id'] == 'abc123'
            assert containers[0]['state'] == 'running'
            assert containers[0]['image_ref'] == 'linuxserver/sonarr:4.0.0.740'

    def test_multiple_containers(self, updater):
        """Test finding multiple containers with same image."""
        api_containers = [
            {
                "Id": "abc123",
                "Names": ["/sonarr-hd"],
                "Image": "linuxserver/sonarr:4.0.0.740",
                "State": "running"
            },
            {
                "Id": "def456",
                "Names": ["/sonarr-4k"],
                "Image": "linuxserver/sonarr:4.0.0.740",
                "State": "running"
            },
        ]

        with patch.object(updater.docker, 'list_containers', return_value=api_containers):
            containers = updater._get_containers_for_image("linuxserver/sonarr")

            assert len(containers) == 2
            assert containers[0]['name'] == 'sonarr-hd'
            assert containers[1]['name'] == 'sonarr-4k'

    def test_stopped_containers_included(self, updater):
        """Test that stopped containers are also returned."""
        api_containers = [{
            "Id": "abc123",
            "Names": ["/sonarr"],
            "Image": "linuxserver/sonarr:4.0.0.740",
            "State": "exited"
        }]

        with patch.object(updater.docker, 'list_containers', return_value=api_containers):
            containers = updater._get_containers_for_image("linuxserver/sonarr")

            assert len(containers) == 1
            assert containers[0]['state'] == 'exited'

    def test_filters_non_matching_containers(self, updater):
        """Test that only matching containers are returned."""
        api_containers = [
            {
                "Id": "abc123",
                "Names": ["/sonarr"],
                "Image": "linuxserver/sonarr:4.0.0.740",
                "State": "running"
            },
            {
                "Id": "def456",
                "Names": ["/radarr"],
                "Image": "linuxserver/radarr:6.0.0.123",
                "State": "running"
            },
        ]

        with patch.object(updater.docker, 'list_containers', return_value=api_containers):
            containers = updater._get_containers_for_image("linuxserver/sonarr")

            assert len(containers) == 1
            assert containers[0]['name'] == 'sonarr'



class TestDiscoveryFailureDefers:
    """A container-list failure defers the image instead of recording success."""

    SEED_DIGEST = 'sha256:old'

    @pytest.fixture
    def updater(self, tmp_path):
        config_file = tmp_path / "config.json"
        state_file = tmp_path / "state.json"
        config_file.write_text(json.dumps({
            "images": [{
                "image": "linuxserver/sonarr",
                "regex": r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$",
                "auto_update": True,
            }]
        }))
        state_file.write_text(json.dumps({
            "linuxserver/sonarr": {
                "base_tag": "latest",
                "tag": "4.0.0.740-ls1",
                "digest": self.SEED_DIGEST,
                "last_updated": "2026-10-01T00:00:00",
            }
        }))
        return DockerImageUpdater(str(config_file), str(state_file))

    def test_list_error_raises(self, updater):
        with patch.object(updater.docker, 'list_containers',
                          side_effect=DockerAPIError(0, "boom")):
            with pytest.raises(DockerAPIError):
                updater._get_containers_for_image("linuxserver/sonarr")

    def test_list_error_skips_image(self, updater):
        events = []
        with patch.object(updater, 'find_matching_tag',
                          return_value=('4.0.1.100-ls1', 'sha256:new')), \
             patch.object(updater.docker, 'list_containers',
                          side_effect=DockerAPIError(0, "boom")), \
             patch.object(updater, '_pull_image', return_value=True) as mock_pull:
            updates = updater.check_and_update(
                progress_callback=lambda e, d: events.append((e, d)))

        assert mock_pull.call_count == 0
        assert updates == []
        saved = updater.state['linuxserver/sonarr']
        assert saved.digest == self.SEED_DIGEST
        assert saved.tag == '4.0.0.740-ls1'
        errors = [d for e, d in events if e == 'check_error']
        assert len(errors) == 1
        assert errors[0]['reason'] == 'docker_unreachable'
        assert errors[0]['image'] == 'linuxserver/sonarr'
        assert not any(e == 'update_found' for e, _ in events)

    def test_unchanged_digest_makes_no_docker_call(self, updater):
        events = []
        with patch.object(updater, 'find_matching_tag',
                          return_value=('4.0.0.740-ls1', self.SEED_DIGEST)), \
             patch.object(updater.docker, 'list_containers') as mock_list, \
             patch.object(updater, '_pull_image', return_value=True) as mock_pull:
            updater.check_and_update(
                progress_callback=lambda e, d: events.append((e, d)))

        mock_list.assert_not_called()
        assert mock_pull.call_count == 0
        assert any(e == 'no_update' for e, _ in events)


class TestUpdateContainers:
    """Test the _update_containers method."""

    def test_all_containers_succeed(self, updater):
        """Test updating multiple containers successfully."""
        with patch.object(updater, '_update_container') as mock_update:
            mock_update.return_value = True

            results = updater._update_containers(
                ['sonarr-hd', 'sonarr-4k'],
                'linuxserver/sonarr',
                '4.0.16.2944-ls299'
            )

            assert results == {'sonarr-hd': True, 'sonarr-4k': True}
            assert mock_update.call_count == 2

    def test_partial_failure(self, updater):
        """Test when some containers fail to update."""
        with patch.object(updater, '_update_container') as mock_update:
            # First succeeds, second fails
            mock_update.side_effect = [True, False]

            results = updater._update_containers(
                ['sonarr-hd', 'sonarr-4k'],
                'linuxserver/sonarr',
                '4.0.16.2944-ls299'
            )

            assert results == {'sonarr-hd': True, 'sonarr-4k': False}

    def test_all_containers_fail(self, updater):
        """Test when all containers fail to update."""
        with patch.object(updater, '_update_container') as mock_update:
            mock_update.return_value = False

            results = updater._update_containers(
                ['sonarr-hd', 'sonarr-4k'],
                'linuxserver/sonarr',
                '4.0.16.2944-ls299'
            )

            assert results == {'sonarr-hd': False, 'sonarr-4k': False}

    def test_single_container(self, updater):
        """Test updating a single container."""
        with patch.object(updater, '_update_container') as mock_update:
            mock_update.return_value = True

            results = updater._update_containers(
                ['sonarr'],
                'linuxserver/sonarr',
                '4.0.16.2944-ls299'
            )

            assert results == {'sonarr': True}
            assert mock_update.call_count == 1


class TestOrphanedContainerFallback:
    """Containers whose image tag was re-pointed are listed by bare image ID."""

    ORPHAN = {'Id': 'c1', 'Names': ['/portainer'], 'Image': '3fe380ead685',
              'State': 'running'}

    def _inspect(self, cfg_image):
        return {'Config': {'Image': cfg_image}, 'Image': 'sha256:3fe380ead685'}

    def test_orphan_found_via_inspect(self, updater):
        with patch.object(updater.docker, 'list_containers', return_value=[self.ORPHAN]), \
             patch.object(updater.docker, 'inspect_container',
                          return_value=self._inspect('portainer/portainer-ce:2.45.0')):
            with patch.object(updater.logger, 'warning') as warn:
                result = updater._get_containers_for_image('portainer/portainer-ce')
        assert result == [{'name': 'portainer', 'id': 'c1', 'state': 'running',
                           'image_ref': 'portainer/portainer-ce:2.45.0',
                           'orphaned': True}]
        assert any('matched via inspect' in c.args[0] for c in warn.call_args_list)

    def test_sha256_prefixed_id_also_treated_as_id(self, updater):
        orphan = dict(self.ORPHAN, Image='sha256:' + 'a' * 64)
        with patch.object(updater.docker, 'list_containers', return_value=[orphan]), \
             patch.object(updater.docker, 'inspect_container',
                          return_value=self._inspect('portainer/portainer-ce:2.45.0')) as insp:
            result = updater._get_containers_for_image('portainer/portainer-ce')
        insp.assert_called_once_with('portainer')
        assert len(result) == 1

    def test_orphan_of_other_repo_not_matched(self, updater):
        with patch.object(updater.docker, 'list_containers', return_value=[self.ORPHAN]), \
             patch.object(updater.docker, 'inspect_container',
                          return_value=self._inspect('linuxserver/sonarr:1')) as insp:
            result = updater._get_containers_for_image('portainer/portainer-ce')
        assert result == []
        assert insp.call_count == 1

    def test_named_containers_not_inspected(self, updater):
        named = dict(self.ORPHAN, Image='portainer/portainer-ce:2.45.0')
        with patch.object(updater.docker, 'list_containers', return_value=[named]), \
             patch.object(updater.docker, 'inspect_container') as insp:
            result = updater._get_containers_for_image('portainer/portainer-ce')
        assert len(result) == 1
        assert insp.call_count == 0

    def test_inspect_error_propagates(self, updater):
        with patch.object(updater.docker, 'list_containers', return_value=[self.ORPHAN]), \
             patch.object(updater.docker, 'inspect_container',
                          side_effect=DockerAPIError(500, 'boom')):
            with pytest.raises(DockerAPIError):
                updater._get_containers_for_image('portainer/portainer-ce')
