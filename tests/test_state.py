"""Tests for ImageState serialization and round-tripping."""

import json
import pytest
from dataclasses import asdict

from ium import ImageState


class TestImageStateSerialization:
    """ImageState ↔ dict ↔ JSON round-trips."""

    def test_asdict(self, sample_state):
        d = asdict(sample_state)
        assert d == {
            "base_tag": "latest",
            "tag": "v8.16.2-ls374",
            "digest": "sha256:abc123",
            "last_updated": "2025-01-01T00:00:00",
            "pending": None,
            "resolve_failure": None,
        }

    def test_legacy_dict_loads(self):
        legacy = {"base_tag": "latest", "tag": "v1", "digest": "sha256:x",
                  "last_updated": "2025-01-01T00:00:00"}
        state = ImageState(**legacy)
        assert state.pending is None
        assert state.resolve_failure is None

    def test_pending_round_trips(self):
        state = ImageState(
            base_tag="latest", tag="v1", digest="sha256:x",
            last_updated="2025-01-01T00:00:00",
            pending={"tag": "v2", "digest": "sha256:y", "seen": "2025-01-02T00:00:00"},
        )
        restored = ImageState(**json.loads(json.dumps(asdict(state))))
        assert restored == state
        assert restored.pending["tag"] == "v2"

    def test_round_trip_dict(self, sample_state):
        d = asdict(sample_state)
        restored = ImageState(**d)
        assert restored == sample_state

    def test_round_trip_json(self, sample_state):
        json_str = json.dumps(asdict(sample_state))
        restored = ImageState(**json.loads(json_str))
        assert restored == sample_state

    def test_full_state_file_round_trip(self):
        """Simulate a multi-image state file round-trip."""
        state = {
            "linuxserver/calibre": ImageState(
                base_tag="latest", tag="v8.16.2-ls374",
                digest="sha256:aaa", last_updated="2025-01-01T00:00:00",
            ),
            "plexinc/pms-docker": ImageState(
                base_tag="latest", tag="1.42.2.10156-f737b826c",
                digest="sha256:bbb", last_updated="2025-01-02T00:00:00",
            ),
        }
        # Serialize
        serialized = {img: asdict(s) for img, s in state.items()}
        json_str = json.dumps(serialized, indent=2)

        # Deserialize
        loaded = json.loads(json_str)
        restored = {img: ImageState(**d) for img, d in loaded.items()}

        assert restored == state


class TestImageStateConstruction:
    """Edge cases in ImageState construction."""

    def test_missing_field_raises(self):
        with pytest.raises(TypeError):
            ImageState(base_tag="latest", tag="v1.0.0", digest="sha256:x")
            # missing last_updated

    def test_extra_field_raises(self):
        with pytest.raises(TypeError):
            ImageState(
                base_tag="latest", tag="v1.0.0",
                digest="sha256:x", last_updated="now",
                extra_field="bad",
            )

    def test_none_values_allowed(self):
        """Dataclass doesn't enforce types at runtime, so None is accepted."""
        state = ImageState(base_tag=None, tag=None, digest=None, last_updated=None)
        d = asdict(state)
        assert all(v is None for v in d.values())

    def test_equality(self):
        a = ImageState("latest", "v1.0.0", "sha256:abc", "2025-01-01")
        b = ImageState("latest", "v1.0.0", "sha256:abc", "2025-01-01")
        assert a == b

    def test_inequality_on_digest(self):
        a = ImageState("latest", "v1.0.0", "sha256:abc", "2025-01-01")
        b = ImageState("latest", "v1.0.0", "sha256:def", "2025-01-01")
        assert a != b


def test_updater_now_returns_datetime(tmp_path):
    from datetime import datetime
    from ium import DockerImageUpdater
    cfg = tmp_path / "config.json"
    cfg.write_text('{"images": []}')
    updater = DockerImageUpdater(str(cfg), str(tmp_path / "state.json"))
    assert isinstance(updater._now(), datetime)
