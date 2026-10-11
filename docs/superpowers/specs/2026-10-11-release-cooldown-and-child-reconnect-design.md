# Release-Age Cooldown and Dependent-Child Reconnect — Design

**Date:** 2026-10-11
**Status:** Approved
**Target version:** 1.5.0

## Background

On 2026-10-11 we read the auto-update code of Finsys/dockhand (v1.0.53) to see
whether it made ium redundant. It does not: Dockhand only auto-updates
same-tag digest drift and treats newer version tags as advisory, while ium's
purpose is to advance containers from one explicit version tag to the next.
Two Dockhand behaviours are worth adopting in ium. This spec covers both.
They are independent; neither depends on the other.

1. **Release-age cooldown** — hold an auto-update for a configurable number of
   hours after a new candidate first appears, so a bad release can be pulled
   upstream before ium applies it.
2. **Dependent-child reconnect** — after recreating a container, repair any
   other container that shared its network namespace by ID, which the recreate
   otherwise leaves pointing at a dead container.

A third Dockhand idea, temp-tag pull protection, was considered and dropped:
it exists because Dockhand pulls the *same* floating tag the container runs,
so the pull repoints that tag before a vulnerability scan can veto. ium pulls
a *different* concrete tag and has no scan step, so the problem does not
arise. See Out of scope.

## Decisions (settled during brainstorming)

| Question | Decision |
|---|---|
| Cooldown clock source | **First seen by ium**, recorded in state. Not the registry build timestamp, not Docker Hub `tag_last_pushed`. |
| Cooldown scope | Per-image config key only. No global default. |
| What the cooldown gates | Only an update that would otherwise **auto-apply right now**. Non-auto images, downgrades, and images without the key behave exactly as today. |
| Manual "Apply Update" in the web UI | **Bypasses** the cooldown. |
| Candidate changes mid-cooldown | New digest **replaces** the pending record and **restarts** the clock. |
| Notifications during cooldown | One `update_found` notification when the pending record is first created, carrying `deferred_until`. Nothing further until applied. |
| Child reconnect opt-out | **None.** Always on. |
| Child NetworkMode rewrite target | `container:<parent name>`, not the new ID, so children survive future recreations. |
| Child failure | Logged as a warning; **never** changes the parent's result. |
| Temp-tag pull protection | Dropped. A pre-cutover verification gate is a possible future task. |

## Part 1: Release-age cooldown

### 1.1 Config

New optional per-image key:

```json
{
  "image": "linuxserver/sonarr",
  "regex": "^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+-ls[0-9]+$",
  "base_tag": "latest",
  "auto_update": true,
  "min_release_age_hours": 48
}
```

- JSON schema (the `CONFIG_SCHEMA` dict in `ium.py`, image item properties):
  `"min_release_age_hours": {"type": "integer", "minimum": 0}`.
- Absent or `0` means **off**. Read in `check_and_update` alongside
  `keep_versions`, `stop_timeout`, `request_timeout`.
- Web UI config card (`static/js/app.js`): a small number input labelled
  "Min release age (hours)" next to the "keep versions" input, default `0`.
  When saving, omit the key from the JSON when the value is `0`, mirroring how
  `keep_versions` is omitted at its default of `3`. Track dirty state the same
  way the `keep_versions` input does.

### 1.2 State

`ImageState` (dataclass in `ium.py`) gains one field:

```python
@dataclass
class ImageState:
    base_tag: str
    tag: str
    digest: str
    last_updated: str
    pending: Optional[Dict[str, str]] = None   # {"tag", "digest", "first_seen"}
```

- `first_seen` is a UTC ISO-8601 timestamp produced by
  `datetime.now(timezone.utc).isoformat()`. UTC is deliberate: the age
  computation must not shift by an hour across DST.
- `_load_state` constructs `ImageState(**image_data)`; the default makes
  state files written before 1.5.0 load unchanged. `_save_state` uses
  `asdict`, which serialises the nested dict as-is. No migration step.
- `pending` is only ever set while an auto-update is deferred. It is cleared
  (the whole `ImageState` is rewritten without it) whenever `state.tag`
  advances, including by the manual apply handler in `webui.py`, which
  already constructs a fresh `ImageState`.

### 1.3 Flow in `check_and_update`

Today the method compares the resolved `(matching_tag, digest)` against
`saved_state.digest`. When they differ it enters the update branch, decides
`effective_auto_update` (auto_update and not a downgrade), notifies, and if
auto-applying pulls, recreates, cleans up, and writes state. Because state is
only written on success for auto-update images, a deferred update naturally
re-enters this branch every cycle. The cooldown slots in immediately after
`effective_auto_update` is computed and before any notification is sent.

Pseudocode for the new gate, applied in **both** the "new tag" branch and the
"image rebuilt under the same tag" branch (the pending record is keyed by
digest, so one helper serves both):

```
min_age_h = image_config.get('min_release_age_hours', 0)
deferred_until = None
newly_pending = False

if effective_auto_update and min_age_h > 0:
    now = utcnow()
    pending = saved_state.pending if saved_state else None
    if not pending or pending['digest'] != digest:
        pending = {'tag': matching_tag, 'digest': digest, 'first_seen': now.isoformat()}
        newly_pending = True
    first_seen = parse(pending['first_seen'])
    ready_at = first_seen + timedelta(hours=min_age_h)
    if now < ready_at:
        deferred_until = ready_at.isoformat()
        effective_auto_update = False      # do not apply this cycle
        # persist the pending record without advancing tag/digest
        self.state[image] = replace(saved_state, pending=pending)  # or a fresh
                                                                    # ImageState if none
```

Behaviour that follows from the gate:

- **First cycle a candidate appears** (`newly_pending`): log
  `UPDATE DEFERRED: <image> <old> -> <new> auto-applies after <deferred_until>`,
  emit the `update_found` progress event and the `update_found` notification
  exactly as today, plus `deferred_until` in both. `update_info` gains
  `deferred_until` (ISO string or absent) and keeps `auto_update: true` so the
  UI knows it will apply itself.
- **Later cycles, same digest, still cooling**: log the deferral line, emit the
  progress event with `deferred_until`, add to `updates_found`, but **do not**
  send a notification. Gate the `send_notifications` call on
  `newly_pending or deferred_until is None`.
- **Different digest mid-cooldown**: `pending['digest'] != digest` makes it
  `newly_pending` again — fresh record, fresh clock, one fresh notification.
- **Clock expired**: `effective_auto_update` stays true and the existing
  pull → `_update_containers` → cleanup → state-write path runs untouched.
  The state write constructs a new `ImageState` with no `pending`.
- **Image rebuilt branch**: identical treatment. Note this branch today uses
  `auto_update` rather than `effective_auto_update`; the implementor should
  introduce the same local name there so the gate reads identically.
- **Downgrade**: `effective_auto_update` is already false, so the gate is
  skipped and nothing is recorded. Unchanged from today.
- **Non-auto image**: gate skipped; state advances immediately as today.
- **Dry run**: `self.state` is mutated in memory but `_save_state` already
  returns early in dry-run mode, so no pending record reaches disk.
- **ium was down past the window**: `first_seen` is old, the gate passes on
  the first cycle after restart. Accepted consequence of the first-seen clock.

Where `saved_state` is `None` (first ever run for an image) and the gate
defers, write an `ImageState` whose `tag`/`digest` reflect the **currently
running** version (the `old_tag` already computed from the container, and
`digest=""` because the running image's registry digest is not known) and
`pending` holds the candidate. Do not write the candidate into `tag`/`digest`;
that would suppress the retry. An empty `digest` compares unequal to the
candidate next cycle, which is what re-enters the branch.

### 1.4 Notifications

`notify._build_payload` gains an optional `deferred_until: Optional[str] = None`
parameter included in the payload dict only when not `None`. `send_notifications`
threads it through. The ntfy formatter appends "auto-applies after <value>"
when present; the webhook template exposes `$deferred_until` (empty string when
absent). Existing payloads are byte-identical when the field is absent.

### 1.5 Web UI

- `displayUpdates` in `static/js/app.js`: when `update.deferred_until` is
  present, render the auto-update badge text as
  "Auto-applies after <local date/time>" (format with
  `new Date(iso).toLocaleString()`), and **also** render the "Apply Update"
  button, since manual apply bypasses the cooldown. Without
  `deferred_until`, rendering is unchanged.
- `api_apply_update` in `webui.py` needs no cooldown logic. It already pulls,
  recreates, and writes a fresh `ImageState`, which drops `pending`.
- Progress events: the `update_found` payload now may carry `deferred_until`;
  the Socket.IO consumer should pass it through to `displayUpdates`.

### 1.6 Tests (Part 1)

New file `tests/test_release_cooldown.py`. Use the existing fake Docker handler
pattern from `tests/fake_docker.py` and monkeypatch a clock (inject a `now`
callable or patch `datetime` in `ium`) so no test sleeps. Each case below is a
separate test and **must pass**; a skipped test here means a missing fixture,
not an acceptable outcome.

1. New candidate with `min_release_age_hours: 24` → no pull, no recreate,
   `state[image].pending` set with the candidate digest, `state[image].tag`
   unchanged, `update_info['deferred_until']` present, one notification sent.
2. Same candidate, clock advanced 23h → still deferred, **zero** notifications
   sent this cycle.
3. Same candidate, clock advanced 25h → pull and recreate happen, state
   advances, `pending` is `None`.
4. Different digest arrives at 20h → `pending` replaced, `first_seen` reset to
   now, one notification sent; at 20h + 23h still deferred; at 20h + 25h applied.
5. `auto_update: false` with the key set → state advances immediately,
   `pending` never set (unchanged behaviour).
6. Downgrade candidate with the key set → reported, not applied, `pending`
   never set.
7. Key absent or `0` → applied immediately (unchanged behaviour).
8. "Image rebuilt" branch (same tag, new digest) with the key set → deferred
   exactly like case 1, applied after the window.
9. State file written without a `pending` key loads successfully.
10. `dry_run=True` → `pending` set in memory, state file on disk untouched.
11. `webui` manual apply during cooldown (via `test_webui.py` style client) →
    recreate runs, resulting state has `pending is None`.
12. Schema rejects `min_release_age_hours: -1` and `"48"` (string).

## Part 2: Dependent-child reconnect

### 2.1 Problem

Recreating a container gives it a new ID. Any other container that shares its
network namespace is recorded by Docker as
`HostConfig.NetworkMode = "container:<64-hex id>"`. Compose produces this for
`network_mode: service:<name>` (the usual VPN-sidecar pattern). After the
parent is recreated the child still runs, but its stored reference is dead:
the next restart fails with
`joining network namespace of container: No such container`. ium's
`_build_create_config` already copies `NetworkMode` verbatim, so a child that
is itself updated by ium carries the stale reference forward.

### 2.2 Refactor: extract `_recreate_container`

`_update_container(container_name, image, tag, registry, stop_timeout,
request_timeout) -> bool` currently does two jobs: resolve the target image
reference and short-circuit if already there, then perform the recreate with
rollback. Split it:

```python
def _update_container(self, container_name, image, tag, registry=None,
                      stop_timeout=..., request_timeout=...) -> bool:
    # unchanged: compose pull_image/full_image, dry-run log,
    # fetch container_info, "already running target image" skip /
    # "on target image but stopped → start it" recovery
    return self._recreate_container(
        container_name, full_image, container_info,
        stop_timeout=stop_timeout, request_timeout=request_timeout)

def _recreate_container(self, container_name: str, full_image: str,
                        container_info: Dict[str, Any], *,
                        network_mode_override: Optional[str] = None,
                        start: bool = True,
                        stop_timeout: int = ..., request_timeout: int = ...) -> bool:
    # moved verbatim from _update_container: build create config, stop,
    # backup rename (with the rename-failure recovery), create, start,
    # connect extra networks, rollback on DockerAPIError, remove backup.
    # Two additions:
    #   - pass network_mode_override to _build_create_config
    #   - only call start_container when `start` is True
    #   - on success, call self._reconnect_dependent_children(old_id, container_name, ...)
```

- `_build_create_config` gains `network_mode_override: Optional[str] = None`.
  When given, it replaces `host_config['NetworkMode']` **before** the
  `is_container_network` / `shares_network_namespace` checks run, so hostname
  and port-binding suppression stay correct.
- `old_id` is `container_info['Id']`, which `_update_container` already has in
  hand before the rename.
- This is a move, not a rewrite. All existing tests covering stop failure,
  rename failure, socket timeout rollback, interrupted-update recovery, and
  multi-container update must pass unmodified after the refactor. Treat any
  needed test edit as a signal the refactor changed behaviour.

### 2.3 `_reconnect_dependent_children`

```python
def _reconnect_dependent_children(self, old_parent_id: str, parent_name: str,
                                  *, stop_timeout: int, request_timeout: int,
                                  _visited: Optional[Set[str]] = None) -> int:
    """Recreate containers whose NetworkMode pointed at the old parent ID.

    Returns the number of children repointed. Never raises.
    """
```

Algorithm:

1. Cycle guard: if `old_parent_id` is in `_visited`, return 0; add it.
2. `self.docker.list_containers(all=True)`. For each entry whose `Id` is not
   `old_parent_id`, `inspect_container(id)`. Skip on `DockerAPIError`.
3. Read `HostConfig.NetworkMode`. Continue unless it starts with
   `container:`. Let `ref` be the remainder. Match when
   `ref == old_parent_id`, or `len(ref) >= 12 and old_parent_id.startswith(ref)`.
   A `ref` that is a **name** never matches (names are not hex prefixes of the
   ID in practice; if one ever were, repointing to the same name is harmless).
4. For each match: log
   `Reconnecting dependent container <child> -> container:<parent_name>`.
   Call `self._recreate_container(child_name, child_config_image,
   child_info, network_mode_override=f"container:{parent_name}",
   start=(child State.Status == "running"), ...)`. The image is the child's
   own `Config.Image`, already local; nothing is pulled.
5. On success, increment the count. Capture the child's old ID before the
   call; if the child was itself a parent, recurse with `(old_child_id,
   child_name)` and the shared `_visited` set, adding the result.
6. Wrap each child in `try/except Exception` and log a warning on failure.
   The method must return normally no matter what.

Call site: inside `_recreate_container`, **after** the backup container has
been removed and success is certain, and **before** returning `True`. Wrap
the call itself in `try/except Exception` with a warning log, so a bug in the
reconnect code cannot turn a successful parent update into a reported
failure. Log the count when it is non-zero.

### 2.4 Isolation and rollback

- The parent's result is final before this step starts. No branch of the
  reconnect step may change the return value of the parent's
  `_recreate_container`.
- A child that fails mid-recreate is rolled back by the existing machinery
  inside its own `_recreate_container` call: renamed back and, if it was
  running, started. It still carries the dead reference, which is exactly the
  state it was in before. No worse than today.
- A child whose rollback also fails is logged with the same
  "check for `<name>_backup_<ts>` manually" guidance the parent path already
  emits.

### 2.5 Observability

Log lines only. No new progress events, history entries, or notifications in
this version. The web UI check log already streams the logger output.

### 2.6 Tests (Part 2)

New file `tests/test_dependent_children.py`, using `tests/fake_docker.py`.
The fake must be able to serve `list_containers(all=True)` and per-ID
`inspect_container` for several containers; extend it if it cannot. Each
case **must pass**:

1. Parent `p` (id `aaa…`) and child `c` with `NetworkMode: container:aaa…`,
   child running. Update `p` → `c` is stopped, renamed, recreated with
   `HostConfig.NetworkMode == "container:p"`, started; backup removed.
   Return value for `p` is `True`.
2. Same, child **stopped** → recreated with the name reference, **not**
   started.
3. Child referencing a different container ID → untouched (no stop, no
   rename, no create for it).
4. Child referencing the parent by **name** already → untouched.
5. Child whose `create_container` fails with `DockerAPIError` → child rolled
   back (renamed back, started), parent still returns `True`, warning logged.
6. Chain `A ← B ← C` (C joins B, B joins A). Update A → both B and C recreated,
   C ends with `container:B`.
7. Cycle guard: `_visited` pre-seeded with the parent ID returns 0 without
   listing containers.
8. Reconnect helper raising an unexpected exception (monkeypatched) → parent
   still returns `True`.
9. The full existing suite passes with no edits to pre-existing test files.

### 2.7 Live verification (must run, must pass)

Unit tests cannot prove that Docker accepts and preserves the name form of
`NetworkMode`. The implementor must run this against a real daemon and
record the output in the PR description:

```bash
docker run -d --name ium-parent-test nginx:1.25
docker run -d --name ium-child-test --network container:ium-parent-test alpine sleep 1d
docker inspect -f '{{.HostConfig.NetworkMode}}' ium-child-test   # note form
# configure ium for image nginx, regex ^1\.[0-9]+$, base_tag 1, auto_update true
python ium.py --check                                            # parent recreated
docker inspect -f '{{.HostConfig.NetworkMode}}' ium-child-test   # expect container:ium-parent-test
docker restart ium-child-test && docker ps --filter name=ium-child-test --format '{{.Status}}'  # expect Up
docker rm -f ium-child-test ium-parent-test
```

If the first `inspect` shows Docker already stored the **name**, the test
still proves the restart path. If it shows an ID, the test proves the repoint.
Either way the final restart must succeed. A failure here blocks the release.

## Implementation ordering

The two parts are independent and may be built in either order or in
parallel. Within Part 2 the order is fixed: 2.2 (refactor, full suite green,
no test edits) **before** 2.3 (new behaviour). Within Part 1, do 1.2 (state)
before 1.3 (flow), and 1.3 before 1.4/1.5. Bump `__version__` to `1.5.0` last.

## Compatibility

- Config files without `min_release_age_hours` validate and behave as before.
- State files without `pending` load as before. A state file written by
  1.5.0 and read by an older ium would fail `ImageState(**data)` on the
  unknown key; downgrading past 1.5.0 therefore requires deleting the
  `pending` keys from `state/image_update_state.json`. Note this in the
  release notes.
- Notification payloads are unchanged unless an update is deferred.
- No Docker Engine API changes; every call used already exists in
  `docker_api.py`.

## Out of scope

- **Pre-cutover verification gate** (future task): pull the new image, run an
  optional user-supplied check such as a command or a healthcheck wait, and
  only then recreate. This is the useful analogue of Dockhand's temp-tag
  protection for ium's pinned-tag model.
- Registry-derived release age (manifest config `created`, Docker Hub
  `tag_last_pushed`).
- A global default for `min_release_age_hours`.
- An opt-out for dependent-child reconnect.
- Progress events, history entries, or notifications for child reconnects.
- Vulnerability scanning.
