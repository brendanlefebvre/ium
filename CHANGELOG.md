# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [1.5.0] - 2026-10-24

### Fixed
- Containers whose image tag was re-pointed to a newer image (so `docker ps` reports a bare image ID) are now discovered by inspecting their original image reference, instead of being missed while the update was recorded as applied
- A Docker error during container discovery now defers the image to the next cycle (no pull, no state write, no notification) instead of being treated as "no containers" and recorded as a success
- The current version is read from the running container's actual image, not from saved state, whenever a container exists
- The current-tag lookup propagates Docker errors and honours per-image registry overrides
- The rebuilt-branch skip check compares image IDs rather than image strings, so a container is skipped only when it already runs the target image
- A downgrade refused by the downgrade guard no longer advances saved state, so a later cycle cannot silently recreate the container on the refused tag
- Registry tag listing follows `Link` pagination headers (capped at 50 pages); ghcr.io returns only 100 tags per page, which hid newer versions
- Rollback no longer deletes the backup container when the replacement was created but failed to start; the replacement is removed and the backup is renamed back and started. Previously a start failure (e.g. a `network_mode: container:<id>` pointing at a recreated container) left the service down with its only working copy removed.
- Registry tag pagination only follows `Link: rel="next"` targets on the same scheme and host as the tags endpoint, so the bearer token is never sent elsewhere.

### Added
- "Ignore until registry changes" action in the Web UI pending list (`POST /api/updates/dismiss`), which silences a pending downgrade in the UI and in notifications until the resolved digest changes
- Resolver failures carry a specific reason (e.g. no tag matches the regex, no tag shares the base tag's digest) to the log and the Web UI, replacing the generic "no matching tags found"
- Failure-streak tracking for resolver failures: a notification after 24 hours of continuous failure, daily through day 7, then weekly; check-log error lines show how long the failure has persisted
- Diagnosis of persistent digest mismatches: ium inspects the 100 most recently pushed tags (Docker Hub) or the last 100 listed tags (other registries) to report what the base tag actually points to (regex out of date vs. a rolling base tag), notifying once per distinct finding

### Changed
- A refused downgrade now stays in the pending list every cycle until it is applied, dismissed, or the registry moves; its notification repeats at most once per 24 hours. Repeats are kept out of update history
- Notifications: two new event names, `resolve_failure` and `resolve_recovered`, and a new free-text `message` field in the payload, available to webhook templates. Webhook consumers that switch on `event` will see the new values
- State file: two new optional per-image fields, `pending` and `resolve_failure`. Existing state files load unchanged. Rolling back to 1.4.0 after running 1.5.0 makes 1.4.0 discard every image entry (1.5.0 writes `pending` and `resolve_failure` on all of them, as `null` when unset), so current versions are re-reported once; any downgrade that was pending is then subject to the pre-1.5.0 behaviour again, where a refused downgrade advances saved state

## [1.0.0] - 2026-02-12

### Changed
- Rebranded from "dum" (Docker Update Manager) to "ium" (Image Update Manager)
- Renamed `dum.py` to `ium.py`
- Renamed state file from `docker_update_state.json` to `image_update_state.json`
- Updated Docker Compose service names (`dum` → `ium`, `dum-cli` → `ium-cli`, `dum-net` → `ium-net`)
- Updated Docker Hub image names (`brendanl79/dum` → `brendanl79/ium`, `brendanl79/dum-cli` → `brendanl79/ium-cli`)
- Multi-platform Docker images (linux/amd64, linux/arm64)

### Fixed
- Digest-only image updates (image rebuilt under the same tag) are now correctly detected and applied instead of being falsely reported as "already up to date"

### Added
- Core updater engine with registry API integration (Docker Hub, ghcr.io, lscr.io, gcr.io, private registries)
- Web UI with real-time Socket.IO updates, card-based config editor, and update history
- 19 built-in image presets (LinuxServer, Jellyfin, Plex, Portainer, etc.)
- Auto-detect tag patterns from any registry, sorted by push recency
- Auto-detect base tags (latest, stable, lts, etc.)
- Auto-populate container name from image name
- Dry-run mode (enabled by default) for safe testing
- Flexible base tag tracking (latest, stable, lts, major versions, etc.)
- Regex-based version tag matching with live validation and test input
- Container management with full settings preservation and automatic rollback on failure
- State persistence with atomic file writes and cross-platform file locking
- Current version detection via container image inventory
- Optional basic authentication (WEBUI_USER/WEBUI_PASSWORD environment variables)
- Docker Compose profiles for CLI dry-run, CLI production, Web UI dry-run, and Web UI production
- Health checks in Docker images
- Persistent update history (max 500 entries)
- GitHub Actions CI/CD for automated testing and Docker Hub publishing
- MIT License
