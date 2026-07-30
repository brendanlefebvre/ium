# Image Update Manager (ium)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker Pulls](https://img.shields.io/docker/pulls/brendanl79/ium)](https://hub.docker.com/r/brendanl79/ium)
[![Docker Image Version](https://img.shields.io/docker/v/brendanl79/ium?sort=semver)](https://hub.docker.com/r/brendanl79/ium)

Docker image auto-updater that tracks version-specific tags via regex patterns, compares manifest digests to detect updates, and recreates containers preserving all settings with rollback on failure.

## Why not just use Watchtower or Diun?

- **Version-aware updates, not just "latest" roulette.** Watchtower watches a fixed tag — if your container runs `nginx:latest`, it pulls whatever `latest` points to, including breaking major-version jumps. ium uses regex patterns to find the *specific version tag* (e.g., `4.3.2.7890-ls225`) that matches your base tag's digest, so you always know exactly what version is running and updates stay within your defined pattern.
- **Checks for updates without pulling images.** ium compares manifest digests via lightweight HEAD requests to the registry API. No images are downloaded just to check whether an update exists, saving bandwidth and time — especially across a large image list.
- **Built-in rollback on failure.** When an auto-update fails (container won't start), ium automatically restores the previous container. Watchtower has no rollback mechanism — a bad update leaves you with a stopped container and no easy way back.
- **Notification *and* action in one tool.** Diun only notifies you that a new tag exists — you still have to update containers yourself. ium can run in notify-only mode (`auto_update: false`) or handle the full update cycle including pull, recreate, and rollback, per image.
- **Web UI included.** Neither Watchtower nor Diun ships a built-in web interface. ium includes a real-time Web UI (Socket.IO) for managing images, viewing update status, and triggering checks — no Portainer or external dashboard needed.

**Where the others may suit you better:** Diun has a broader notification ecosystem (Slack, Telegram, Discord, Matrix, and more) and supports Kubernetes/Swarm/Nomad. If you only need notifications across orchestrators, Diun is a good choice. Note that Watchtower was [archived in December 2025](https://github.com/containrrr/watchtower) and is no longer maintained.

## Quick Start

### Docker Hub (recommended)

```bash
mkdir -p ium/config ium/state && cd ium
echo '{"images": []}' > config/config.json
docker run -d --name ium -p 5050:5050 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v ./config:/config -v ./state:/state \
  brendanl79/ium:latest
```

Or with docker-compose — save the [docker-compose.yml](https://github.com/BrendanL79/ium/blob/main/docker-compose.yml) from this repo, then:

```bash
docker-compose up -d              # Web UI at http://localhost:5050
```

A CLI-only image is also available as `brendanl79/ium-cli`.

### Build from source

```bash
git clone https://github.com/BrendanL79/ium.git && cd ium
mkdir -p config state
echo '{"images": []}' > config/config.json
docker-compose up -d --build
```

Use the Web UI to add images via **Add from Preset** (19 built-in) or **+ Add Image** (auto-detects patterns from registry).

## How It Works

1. Queries registry APIs directly (no images pulled for checking)
2. Compares manifest digest of your `base_tag` (e.g., `latest`, `stable`, `lts`) against saved state
3. Finds the version-specific tag matching your regex with the same digest
4. If `auto_update: true`: pulls image, recreates containers, rolls back on failure

## Configuration

```json
{
  "images": [{
    "image": "linuxserver/sonarr",
    "regex": "^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$",
    "base_tag": "latest",
    "auto_update": false,
    "cleanup_old_images": true,
    "keep_versions": 3,
    "registry": "ghcr.io",
    "stop_timeout": 10,
    "request_timeout": 60
  }]
}
```

Only `image` and `regex` are required. Containers are auto-detected — no need to specify container names.

### Timeouts

| Key | Default | Meaning |
|-----|---------|---------|
| `stop_timeout` | `10` | Seconds Docker waits after SIGTERM before SIGKILL when stopping the container |
| `request_timeout` | `60` | Seconds to wait for the Docker daemon to answer a container lifecycle call |

Raise both for containers that are slow to stop, or on slow storage such as a
NAS. The stop request gets `stop_timeout + request_timeout` seconds in total,
so the socket cannot expire before the grace period has even elapsed.

If a request does time out, ium never assumes the worst: the daemon often
completes the operation after the socket gave up. It inspects the container and
continues the update if it really did stop, restores the previous container if a
later step failed, and starts a container that a previous run created but never
started. A container is never left renamed or half-migrated.

### Common Patterns

| Pattern | Regex | Examples |
|---------|-------|---------|
| Semver | `^[0-9]+\.[0-9]+\.[0-9]+$` | portainer-ce, jellyfin, n8n |
| Semver (v-prefix) | `^v[0-9]+\.[0-9]+\.[0-9]+$` | homarr, mealie |
| LinuxServer | `^v[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$` | bazarr, calibre, tautulli |
| LinuxServer 4-part | `^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-ls[0-9]+$` | sonarr, radarr, prowlarr |

## Deployment

| Mode | Command |
|------|---------|
| Web UI (default) | `docker-compose up -d` |
| CLI daemon | `docker-compose --profile cli up -d ium-cli` |
| Both | `docker-compose --profile cli up -d` |
| Standalone | `python ium.py config/config.json --dry-run` |

All modes default to `auto_update: false` per image.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_FILE` | `/config/config.json` | Config path |
| `STATE_FILE` | `/state/image_update_state.json` | State path |
| `DRY_RUN` | `false` | Dry-run mode |
| `DAEMON` / `CHECK_INTERVAL` | `true` / `3600` | CLI daemon settings |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `WEBUI_USER` / `WEBUI_PASSWORD` | (disabled) | Optional basic auth |

## Security

- Docker socket access required (same as Portainer, Watchtower, Diun)
- Set `WEBUI_USER`/`WEBUI_PASSWORD` to enable basic auth; unset = unrestricted (suitable for trusted LANs)
- Always test with `DRY_RUN=true` first

## Testing

```bash
pip install -r requirements.txt && pytest tests/
```

## NAS Deployment

See [nas-setup.md](https://github.com/BrendanL79/ium/blob/main/nas-setup.md).
