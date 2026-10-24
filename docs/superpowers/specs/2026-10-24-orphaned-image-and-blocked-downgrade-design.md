# Orphaned-Image Discovery, Blocked-Downgrade Visibility, and Honest Resolver Failures — Design

**Date:** 2026-10-24
**Status:** Approved
**Target version:** 1.5.0 (no new config keys; minor bump because Parts 4 and 7 add user-visible behaviour and two notification events). The release-age cooldown and dependent-child reconnect spec, previously targeted at 1.5.0, moves to 1.6.0.

## Background

On 2026-10-24 the portainer container on the production NAS was found running
2.45.0 while Docker Hub's `latest` had moved through 2.45.1, 2.45.2 and
2.45.3. ium's history showed all three as *applied*. A second container,
sonarr, had been recreated on a tag the downgrade guard had refused three
days earlier. Three other images (homarr, qbittorrent, gluetun) reported
"no matching tags found" every cycle. The investigation found four code
defects behind portainer and sonarr, one code defect behind homarr, and a
misleading UI message that hid the (config-side) causes behind qbittorrent
and gluetun. This spec fixes all six and adds two heuristics (Part 7) that would have warned about the config-side cases. It does not touch the features
in `2026-10-11-release-cooldown-and-child-reconnect-design.md`, but the
state-file `pending` field it adds is deliberately the one that spec already reserves
(see §5.2) so the two compose.

### The portainer chain (reconstructed from history, state, and local repro)

1. **Aug 26.** ium updated portainer 2.39.6 → 2.45.0. The container was
   created with `Config.Image = portainer/portainer-ce:2.45.0`, image ID
   `3fe380…`.
2. **Aug 27, three hours later.** Portainer re-pushed `2.45.0` with a new
   digest. ium took the "image rebuilt" branch of `check_and_update`
   (`ium.py` ≈1702), pulled `2.45.0` again, and Docker re-pointed the local
   `2.45.0` tag at the new ID. Docker permits this even though a running
   container uses the old ID; the old image became `<none>`. (Verified
   locally: `docker tag` onto an in-use tag succeeds silently; `docker rmi`
   of a sole in-use tag is refused with 409, so ium's cleanup could not have
   done this.)
3. The rebuilt branch then called `_update_containers`. The skip check in
   `_update_container` (`ium.py` ≈1138) compares the container's
   `Config.Image` **string** with the target string. They were equal, so it
   logged "already running … skipping", returned `True`, and state advanced.
4. **Sep 16, Oct 7, Oct 22.** Because the container's tag no longer resolved
   to its image ID, the Docker container list (`GET /containers/json`)
   reported it by bare image ID instead of by name. (Verified locally: after
   the re-point, `docker ps` shows `ab3fe4defd29` where it showed
   `iumtest/foo:1`.) `_get_containers_for_image` matches on that field by
   name only, found nothing, and `check_and_update` logged "No containers
   found … image updated only", set `update_ok = True`, and wrote state.
   Three releases were recorded as applied without touching the container.
5. Oct 22's cleanup removed the re-pushed `2.45.0` image as the fourth-oldest
   version, so only `2.45.1`–`2.45.3` and the orphan `<none>` remained.

### The sonarr chain

1. **Oct 20.** `latest` resolved to `4.0.20.3014-ls1`; saved tag was
   `…-ls329`. The downgrade guard (`ium.py` ≈1623) correctly set
   `effective_auto_update = False`. But the state write at ≈1690 is gated on
   `not auto_update or update_ok`, and `update_ok` was still `True` because
   no update was attempted, so state advanced to `ls1`.
2. **Oct 23.** linuxserver re-pushed `ls1` with a new digest. Saved tag now
   equalled the candidate tag, so ium took the rebuilt branch, which has no
   downgrade check. The container's `Config.Image` was still `…-ls329`, so
   the string skip check did not fire and the container was recreated on
   `ls1`. (In this instance `ls1` was linuxserver's build-counter reset and
   functionally newer, so no harm resulted. The mechanism is still wrong: ium
   applied a change it had explicitly refused.)

### The homarr/qbittorrent/gluetun triage

- **homarr** (`ghcr.io/homarr-labs/homarr`, 255 tags): ghcr.io returns 100
  tags per page with a `Link: <…>; rel="next"` header. `_get_all_tags`
  (`ium.py` ≈701) issues one request and ignores `Link`, so `v2.8.2`, the
  tag that shares `latest`'s digest, is never seen. Docker Hub returns every
  tag in one response (4791 for qbittorrent), which is why this never bit a
  Hub image. **Code bug, fixed here.**
- **qbittorrent**: linuxserver changed its tag scheme from `5.1.4-r3-ls453`
  to `5.3.0_v2.1.2-ls484`; the regex matched only the old form. **Config;
  already corrected on the NAS.**
- **gluetun**: Docker Hub `latest` is a rolling main-branch build that
  shares its digest with no version tag; the release line is `v3`.
  **Config; already corrected on the NAS.**
- In all three, the ium log named the real cause ("No tag matching pattern
  … shares a digest with …"), but the web UI's `check_error` event carries a
  hard-coded "no matching tags found" (`ium.py` ≈1598), which sent the
  operator to the wrong place. **Fixed here (§6).**

## Decisions (settled during brainstorming)

| Question | Decision |
|---|---|
| Discovery raises a Docker error | Skip the image this cycle. No pull, no state write, no notification. |
| Container whose tag was re-pointed | Find it anyway, by inspecting ID-reported containers and matching `Config.Image` by name. |
| Where "current version" comes from | The first discovered container's actual image, not saved state, whenever any container exists. |
| Rebuilt-branch skip check | Compare image IDs, never image strings. |
| Blocked downgrade on later cycles | Keeps reappearing in the UI pending list every cycle until applied, dismissed, or the registry moves. **(Brendan's choice.)** |
| Blocked-downgrade notifications | First detection, then at most once per 24 h. (My call; hourly external pings would be spam.) |
| Dismiss | UI action "Ignore until the registry changes" silences both UI and notifications until the resolved digest changes. **(Brendan's choice.)** |
| Resolver failure reasons | `find_matching_tag` returns a reason; the UI shows it verbatim. |
| Tag pagination | Follow `Link` headers, capped at 50 pages. |
| Version | 1.5.0. State file gains two optional fields with `None` defaults, so old state files load unchanged. |
| Resolver diagnosis (Part 7A) | On a persistent digest-mismatch, HEAD the 100 most recently pushed tags to name what the base tag actually is; notify once per distinct finding. |
| Failure-streak escalation (Part 7B) | Notify at 24 h of continuous failure, daily through day 7, weekly after. **(Brendan's schedule.)** |

## Part 1: Container discovery

### 1.1 Discovery failure defers the image

`_get_containers_for_image` (`ium.py` ≈957) currently catches
`DockerAPIError`, logs, and returns `[]`. The caller cannot tell "daemon
unreachable" from "no containers". Change its contract:

- On `DockerAPIError`, **re-raise** after logging. Do not return `[]`.
- In `check_and_update`, wrap the discovery call:

```python
try:
    containers = self._get_containers_for_image(image)
except DockerAPIError as e:
    self.logger.warning(
        f"Could not list containers for {image} ({e}); "
        f"skipping this cycle so the update is retried"
    )
    if progress_callback:
        progress_callback('check_error', {
            'image': image, 'base_tag': base_tag,
            'reason': 'docker_unreachable',
            'error': f"Docker daemon unreachable while listing containers for {image}: {e}",
        })
    continue
```

This `continue` happens **before** any pull, notification, or state write.
`webui.py`'s `api_apply_update` also calls `_get_containers_for_image`;
wrap it the same way and return HTTP 503 with the message.

**Where the call lives (my call):** today discovery runs for every image
before the `saved_state.digest != digest` gate (`ium.py` ≈1605). With the
re-raise, a Docker outage would then emit a WARNING and a `check_error`
per image per cycle even when the registry has not moved. Move the
discovery call **inside** the digest-changed branch, immediately before
the `old_tag` computation (its only consumer, including Part 2 and the
rebuilt branch). The observable sequence for an unchanged image becomes
"no_update" with no Docker call at all; tests for 1.1 must set up a
changed digest to reach the wrapped call.

### 1.2 ID-based fallback for orphaned containers

The Docker list endpoint reports a container's `Image` as its bare ID (either
`sha256:<64 hex>` or a 12–64 character hex string) when the reference the
container was created from no longer resolves to the container's image. The
container's own `Config.Image` string is **not** affected by this; `docker
inspect` still shows `portainer/portainer-ce:2.45.0`.

Note that the obvious alternative, matching the ID against
`list_images(reference=image)`, does **not** work: the orphan has no tags,
so the reference filter never returns it. Inspect is the only reliable path.

In `_get_containers_for_image`, after the existing name-match loop:

```python
ID_LIKE = re.compile(r'^(sha256:)?[0-9a-f]{12,64}$')

for container in api_containers:
    container_image = container.get('Image', '')
    if not ID_LIKE.match(container_image):
        continue                        # already handled by name match
    name = (container.get('Names') or ['/'])[0].lstrip('/')
    info = self.docker.inspect_container(name)    # direct: DockerAPIError propagates (not _get_container_config, which swallows it)
    cfg_image = ((info or {}).get('Config') or {}).get('Image', '')
    if cfg_image and self._image_matches(image, cfg_image):
        self.logger.warning(
            f"Container {name} runs {image} but its image tag no longer "
            f"resolves locally (list reports {container_image[:19]}); "
            f"matched via inspect"
        )
        containers.append({'name': name, 'id': container.get('Id', ''),
                           'state': container.get('State', ''),
                           'image_ref': cfg_image, 'orphaned': True})
```

- Only ID-reported containers are inspected, so the extra cost is one
  inspect per orphan, normally zero.
- An inspect that fails with `DockerAPIError` is treated like 1.1: log and
  re-raise; do not silently drop the container.
- The `orphaned` key is informational for logs and tests; nothing else
  branches on it.

### 1.3 "No containers" is still a valid outcome

After 1.1 and 1.2, an **empty** list from a successful discovery means there
genuinely is no container for this image (ium is being used as a pre-puller).
The existing "image updated only" path is kept for that case. The change is
that the empty list can no longer be produced by an error (1.1) or by a
re-pointed tag (1.2).

## Part 2: Current version comes from the container

In `check_and_update`, the `old_tag` computation (`ium.py` ≈1611) is:

```python
old_tag = saved_state.tag if saved_state else None
if not old_tag and containers:
    old_tag = self._get_container_current_tag(containers[0]['name'], image, regex)
```

Invert the priority:

```python
old_tag = None
if containers:
    old_tag = self._get_container_current_tag(containers[0]['name'], image, regex)
    if not old_tag:
        old_tag = 'unknown'      # container exists but its image has no regex-matching tag
elif saved_state:
    old_tag = saved_state.tag
if not old_tag:
    old_tag = 'unknown'
```

Consequences, all intended:

- A container on an orphaned image (no local tags on its ID) yields
  `'unknown'`, not the saved tag. `'unknown' != matching_tag`, so it takes
  the normal UPDATE AVAILABLE path, the downgrade guard is bypassed (as it
  already is for `'unknown'`), and the container is recreated.
- A container still on the old tag while saved state says the new tag
  (sonarr on Oct 23) yields the **old** tag, so a same-tag re-push routes to
  the normal path, where the downgrade guard runs, rather than to the rebuilt
  branch.
- The rebuilt branch is now reached only when the container really is on
  the candidate tag.
- Multi-container images use the first discovered container, matching the
  existing fallback's behaviour. Per-container versions are out of scope.

`_get_container_current_tag` keeps its matching logic (container image ID
against the local inventory) but needs two changes, both my call:

- **Transient errors defer; they do not become `'unknown'`.** Today the
  function swallows every failure (`_get_container_config` → `None`,
  `list_images` `DockerAPIError` → `None`, bare `except Exception`). With
  Part 2 making it the primary source of truth, a daemon hiccup between the
  successful container list and the inspect/image-list calls would collapse
  to `'unknown'`, switch off the downgrade guard, and recreate the container
  onto whatever the registry offered. Instead: call
  `self.docker.inspect_container` and `self.docker.list_images` directly and
  let `DockerAPIError` propagate; `check_and_update` catches it with the same
  wrapper as 1.1 (`reason: 'docker_unreachable'`, skip the image). `None`
  now means exactly one thing: ium looked and the container's image carries
  no regex-matching tag. Drop the bare `except Exception`.
- **Qualify the image reference for `registry:` overrides.** The function
  calls `list_images(image)` with the unqualified config name, while
  `_pull_image` and `_update_container` qualify it (`ghcr.io/x/y`). Docker's
  `reference` filter matches the full repository name, so override images
  would always miss and read as `'unknown'`. Extract the qualification logic
  in `_update_container` (≈1121-1127) into a helper
  `_qualified_image(image, registry) -> str`, use it in `_pull_image`,
  `_update_container`, `_get_container_current_tag`, and the Part 3
  `list_images(full_image)` call. `_get_container_current_tag` gains a
  `registry: Optional[str] = None` parameter; `check_and_update` passes the
  per-image `registry`. `tests/test_registry_override.py` currently patches
  `_get_container_current_tag` out; add one test that exercises it for real
  with a qualified local tag.

## Part 3: Rebuilt-branch skip check compares image IDs

In `_update_container` (`ium.py` ≈1138), replace the string comparison
`current_image == full_image` with an ID comparison:

```python
target = self.docker.list_images(full_image)      # reference filter: exact repo:tag
target_id = target[0].get('Id', '') if target else ''
current_id = container_info.get('Image', '')
if target_id and current_id == target_id:
    ...existing skip / start-if-stopped logic, unchanged...
```

- `list_images` is called **after** the pull that precedes every
  `_update_containers` call, so the target tag is present locally. If it is
  somehow absent (`target == []`), fall through and recreate; do not skip.
- The existing "on target image but not running → start it" sub-branch keeps
  its semantics, keyed on the ID comparison.
- The log line becomes
  `Container {name} already running {full_image} ({target_id[:19]}), skipping`.

With Part 2 in place the rebuilt branch rarely reaches a container that is
not on the target ID, but the ID check is what makes a same-tag re-push
actually recreate the container when it does. That is the whole point of
the rebuilt branch and it has never worked.

## Part 4: Blocked downgrade stays pending

### 4.1 Behaviour

When the downgrade guard refuses a candidate:

- **State does not advance.** `tag` and `digest` keep their prior values.
- A `pending` record is written (shape in §4.2) with `reason: "downgrade"`.
- The update is reported in `updates_found` with `downgrade: True` and
  `auto_update: False`, exactly as today, so the web UI shows it with an
  Apply button.
- **Every subsequent cycle** that resolves to the same digest re-reports it
  the same way, so it never leaves the UI's pending list. It is logged at
  INFO as `DOWNGRADE PENDING: {image} {old} -> {new} (first seen {ts})`
  rather than the WARNING used on first detection.
- **Notifications:** `send_notifications(..., event='update_found', ...)`
  on first detection, then again only if `last_notified` is ≥ 24 h old.
  Update `last_notified` on each send.
- **History:** `webui.py` `run_check` appends every entry of
  `updates_found` to `update_history` (capped at 500 rows). A pending
  downgrade re-reported hourly would push real history out within weeks.
  Re-reports carry `repeat: True` in `update_info` (absent or `False` on
  first detection); `run_check` skips appending entries with `repeat`
  set. The UI pending list and `last_updates` still receive them. (My call.)
- **Dismiss:** when `pending.dismissed` is true, the cycle still refuses to
  apply and still skips the state advance, but does not add the update to
  `updates_found`, does not log beyond DEBUG, and does not notify.
- **Registry moves on** (resolved digest ≠ `pending.digest`): clear
  `pending` and run the normal path for the new candidate. If the new
  candidate is also a downgrade, a fresh `pending` record is created with a
  fresh `first_seen` and a fresh first-detection notification.
- **Candidate equals the current tag again** (upstream reverted): the
  normal path sees `old_tag == matching_tag`, so `pending` is cleared and
  the rebuilt branch handles any digest drift as usual.
- **Applied from the UI:** `api_apply_update` already writes a fresh
  `ImageState`; with `pending` defaulting to `None` that clears it.
- **Dry run:** nothing is persisted (existing `_save_state` early return).

### 4.2 State

`ImageState` gains one optional field, named and shaped to match what the
1.6.0 cooldown spec reserves (`pending: {"tag","digest","first_seen"}`),
extended with the keys this feature needs:

```python
@dataclass
class ImageState:
    base_tag: str
    tag: str
    digest: str
    last_updated: str
    pending: Optional[Dict[str, Any]] = None
    # {"tag": str, "digest": str, "first_seen": iso, "reason": "downgrade",
    #  "dismissed": bool, "last_notified": iso | None}
```

- `_load_state` constructs `ImageState(**image_data)`; the default makes
  old state files load unchanged. `_save_state` uses `asdict`, so the field
  round-trips with no further change.
- 1.6.0 (cooldown spec) will add `reason: "cooldown"` records to the same field. Code
  written here must branch on `pending['reason']`, never on the mere
  presence of `pending`.

### 4.3 Flow

Inside the `old_tag != matching_tag` branch of `check_and_update`, after
`is_downgrade` is computed:

```python
if is_downgrade:
    pending = (saved_state.pending if saved_state else None) or {}
    same = pending.get('reason') == 'downgrade' and pending.get('digest') == digest
    if not same:
        pending = {'tag': matching_tag, 'digest': digest,
                   'first_seen': now_iso, 'reason': 'downgrade',
                   'dismissed': False, 'last_notified': None}
    if pending['dismissed']:
        self.logger.debug(f"Downgrade {image} {old_tag} -> {matching_tag} dismissed; waiting for registry change")
        self._write_pending(image, saved_state, base_tag, pending)   # persists without advancing tag/digest
        continue
    # report (updates_found + progress_callback) as today
    notify = pending['last_notified'] is None or age(pending['last_notified']) >= 24h
    if notify:
        send_notifications(...)
        pending['last_notified'] = now_iso
    self._write_pending(image, saved_state, base_tag, pending)
    continue        # never reaches the pull / state-advance code below
```

- `_write_pending` builds `ImageState(base_tag=base_tag, tag=<existing or
  old_tag>, digest=<existing or ''>, last_updated=<existing or now>,
  pending=pending)`. When there is no `saved_state` (first-ever run lands on
  a downgrade), `tag` is `old_tag` and `digest` is `''`; a later non-downgrade
  candidate then takes the normal path because `'' != digest`.
- The `continue` replaces today's fall-through into the state-advance block,
  which is the sonarr bug.
- Anywhere the normal path writes a fresh `ImageState` (successful apply,
  non-auto report), `pending` is implicitly cleared because the new object
  does not set it.

### 4.4 Web UI

- `webui.py`: new endpoint `POST /api/updates/dismiss` with JSON
  `{"image": ...}`. Looks up `updater.state[image]`; if it has
  `pending` with `reason == "downgrade"`, sets `dismissed = True`, calls
  `_save_state`, removes the entry from `last_updates`, and emits
  `check_complete` with the trimmed list so open tabs refresh. 404 if there
  is no such pending record. Guard with `require_updater` like its siblings.
- `static/js/app.js` `displayUpdates`: when `update.downgrade` is true,
  render a `badge-downgrade` ("Older than current") next to the tag change
  and, beside the existing Apply button, a second button
  "Ignore until registry changes" that POSTs to the dismiss endpoint and
  removes the item on success. Apply continues to work unchanged.
- `static/css`: one badge style; reuse the existing warning colour tokens.

## Part 5: Tag pagination

`_get_all_tags` (`ium.py` ≈701) follows `Link` headers:

```python
tags, url, pages = [], tags_url, 0
while url and pages < MAX_TAG_PAGES:          # MAX_TAG_PAGES = 50
    response = self._request_with_retry('GET', url, headers=headers)
    response.raise_for_status()
    tags.extend(response.json().get('tags') or [])
    url = _next_link(response.headers.get('Link'), base=f"https://{registry}")
    pages += 1
if url:
    self.logger.warning(f"Tag list for {namespace}/{repo} truncated at {MAX_TAG_PAGES} pages")
return tags
```

- `_next_link` parses `<path-or-url>; rel="next"` (RFC 5988 form, possibly
  several comma-separated links) and resolves a relative path against the
  registry base. ghcr.io sends a relative path such as
  `/v2/homarr-labs/homarr/tags/list?last=feat-no-tasks-modals&n=0`; pass the
  query through untouched, including the odd `n=0`.
- Any `RequestException` mid-way keeps the existing behaviour: log and
  return `[]` (not the partial list), so the caller's "could not get tags"
  path fires and the cycle is skipped rather than resolved against a
  truncated list.
- `_get_all_tags_by_date` is unaffected; it already paginates Hub's API.

## Part 6: Honest resolver failure reasons

`find_matching_tag` currently returns `Optional[Tuple[str, str]]`. Keep the
success shape; on failure, record a reason the caller can surface.

Add a small enum:

```python
class ResolveFailure(Enum):
    REGISTRY_ERROR   = "Transient registry error resolving the base tag; will retry next cycle"
    NO_TAGS          = "Could not list tags from the registry"
    NO_REGEX_MATCH   = "No tags match the regex"
    NO_DIGEST_MATCH  = ("Tags match the regex but none shares the base tag's digest; "
                       "the base tag may be a rolling build (try a release-line base_tag such as v3) "
                       "or a release may be mid-publish")
    NO_FALLBACK_DIGEST = "Base tag does not exist and the newest matching tag could not be read"
    INTERNAL         = "Internal error: regex not found in the compiled-pattern cache; re-save the config or restart ium"
```

`INTERNAL` covers the sixth `return None` site, the `compiled_patterns`
cache miss (`ium.py` ≈819). Every `return None` in `find_matching_tag`
must set a member; a `None` reaching Part 7's bookkeeping is a bug, and
the Part 6 test asserts the attribute is non-`None` after every failure
branch.

Rather than change the return type (many call sites and tests),
`find_matching_tag` sets `self._last_resolve_failure: Optional[ResolveFailure]`
to `None` on entry and to the applicable member before each `return None`.
`check_and_update` reads it to build the `check_error` payload:

```python
progress_callback('check_error', {
    'image': image, 'base_tag': base_tag,
    'reason': failure.name.lower(),
    'error': f"Could not determine version for {image}:{base_tag}: {failure.value}",
})
```

The WARNING log line gains the same suffix. `app.js` already prints
`eventData.error` verbatim, so no front-end change is needed beyond
styling nothing.

## Part 7: Warn about failures that are not transient

Every resolver failure today is handled as "skip this cycle, retry next
hour". That is right for a mid-release race and wrong for qbittorrent (regex
stale for weeks) and gluetun (base tag can never match). Two heuristics:
**A** makes the digest-mismatch failure actionable by naming what the base
tag actually is; **B** escalates any failure that has lasted a day,
whatever its cause.

### 7.1 State

`ImageState` gains a second optional field:

```python
resolve_failure: Optional[Dict[str, Any]] = None
# {"reason": str,            # ResolveFailure.name, updated each failing cycle
#  "since": iso,             # first consecutive failure
#  "cycles": int,            # consecutive failing cycles
#  "last_notified": iso | None,
#  "diag": {                 # Part 7A result, or absent
#      "base_digest": str, "checked_at": iso,
#      "outcome": "regex_stale" | "rolling_base" | "transient",
#      "sharing_tags": [str, ...]   # at most 5
#  }}
```

Images that have never resolved (gluetun) have no state entry. Use the same
helper pattern as Part 4: write an `ImageState(base_tag=base_tag,
tag='unknown', digest='', last_updated=now, resolve_failure=...)`. When such
an image later resolves, `'' != digest` routes it through the normal path
and Part 2 reads the current version from the container.

### 7.2 Streak bookkeeping (B)

In `check_and_update`, in the `if not result:` branch (after Part 6 has set
`self._last_resolve_failure`):

```python
rf = (saved_state.resolve_failure if saved_state else None) or {}
if not rf:
    rf = {'reason': failure.name, 'since': now_iso, 'cycles': 0, 'last_notified': None}
rf['reason'] = failure.name          # reason may change mid-streak; `since` does not
rf['cycles'] += 1
```

On success (`result` is not None), if `saved_state.resolve_failure` is set:
clear it (rewrite the `ImageState` without it, even when the digest is
unchanged and nothing else would be written), log INFO
`RESOLVED AGAIN: {image} after {duration}`, and if `last_notified` was not
`None`, send one `resolve_recovered` notification. Nothing is sent for a
streak that never crossed the notification threshold.

### 7.3 Diagnosis (A)

Runs only when all of these hold:

- `failure is ResolveFailure.NO_DIGEST_MATCH`;
- `rf['cycles'] >= 2` **and** `now - rf['since'] >= 2 h` (a mid-release race
  resolves within minutes; do not diagnose a race). The gate is primarily
  time-based so that a daily cron cadence gets the refined reason in its
  first B notification rather than a day later;
- no `diag` yet, **or** `diag['checked_at']` is ≥ 24 h old, **or**
  `diag['base_digest'] != base_digest` **and** `diag['checked_at']` is ≥ 6 h
  old. The 6 h floor matters for exactly the rolling-build case A targets:
  gluetun's `latest` moves most cycles, and without the floor the sweep
  would run every hour for as long as the misconfiguration lasts.

`find_matching_tag` must expose the base digest for this: store it on
`self._last_base_digest` next to `_last_resolve_failure` (set to `None` on
entry).

Candidate tags, at most 100:

- Docker Hub: `_get_all_tags_by_date` (already exists, newest-pushed last);
  take the last 100.
- Other registries: the last 100 of the paginated `_get_all_tags` list.
  ghcr.io lists in push order; other registries may not. Good enough: the
  goal is "what is latest, probably", and a miss degrades to the
  `rolling_base` message, which still tells the operator to look.

HEAD each candidate with the same `ThreadPoolExecutor(max_workers=10)`
pattern `find_matching_tag` uses, collecting tags whose digest equals the
base digest. Exclude `base_tag` itself. Outcome:

| Finding | `outcome` | Refined failure shown to the operator |
|---|---|---|
| Some sharing tag matches the regex | `transient` | Unchanged `NO_DIGEST_MATCH`; the earlier miss was a per-tag error. No notification from A. |
| Sharing tags exist, none match the regex | `regex_stale` | New member `ResolveFailure.REGEX_STALE`: "latest is currently `{tags}`, which the regex `{regex}` does not match". |
| No tag shares the digest | `rolling_base` | New member `ResolveFailure.ROLLING_BASE`: "no tag names the current `{base_tag}` digest; it is probably a rolling build. Try a release-line base tag (for example `v3`, `lts`, `stable`)". |

Store `diag` on `rf`. **Which reason is surfaced each cycle:** 7.2 writes
the raw `failure.name` into `rf['reason']` every failing cycle; the refined
member is derived, not stored there. On every failing cycle where the raw
failure is `NO_DIGEST_MATCH`, `diag` exists, `diag['base_digest']` equals
the current base digest, and `diag['outcome']` is `regex_stale` or
`rolling_base`, the log line, the `check_error` payload, and B's
notification text use the refined member; otherwise they use the raw one.
So a refined finding stays visible between diagnoses and drops back to the
raw text only once the registry has moved and the next sweep has not yet
run.

**A's notification:** send one `resolve_failure` notification immediately
when `outcome` is `regex_stale` or `rolling_base` **and** either no previous
`diag` existed or `(outcome, sharing_tags)` differs from the previous
`diag`. Set `rf['last_notified']`. This fires within about three hours of a
regex going stale (three cycles, two hours, plus the diagnosis), long before
B's 24-hour threshold, and only once per distinct finding.

Cost: at most four sweeps (one tag listing plus up to 100 HEADs each) per
affected image per day, given the 6 h floor. HEAD manifest requests do not
count toward Docker Hub's pull-rate limit.

### 7.4 Escalation schedule (B)

After bookkeeping and diagnosis, with `age = now - rf['since']` and
`gap = now - rf['last_notified']` (infinite when `None`):

| Condition | Action |
|---|---|
| `age < 24 h` | Nothing (beyond A's immediate notification, if any). |
| `24 h <= age < 7 d` and `gap >= 24 h` | Send `resolve_failure`; set `last_notified`. |
| `age >= 7 d` and `gap >= 7 d` | Send `resolve_failure`; set `last_notified`. |

Brendan's schedule: daily for the first week, weekly after. A's immediate
notification counts as a send for gap purposes, so a `regex_stale` finding
at hour 3 is followed by the first B reminder at about hour 27, not hour 24.

Message content for `resolve_failure`: image, base tag, `since` as an
absolute date plus duration, cycle count, and the refined failure text.
Message for `resolve_recovered`: image, resolved tag, duration of the
outage.

Persist `rf` on the `ImageState` every failing cycle (the `cycles` counter
changes each time). Dry run persists nothing, as everywhere else.

### 7.5 Notification plumbing

`send_notifications` / `_build_payload` in `notify.py` gain
`message: str = ''`. Payload fields for the new events, so webhook
templates see defined values:

| Event | `old_version` | `new_version` | `digest` | `auto_update` |
|---|---|---|---|---|
| `resolve_failure` | current tag (`state.tag`, or `'unknown'`) | `base_tag` | base digest if known, else `''` | the image's config value |
| `resolve_recovered` | current tag before recovery | the tag just resolved | resolved digest | the image's config value |

`send_ntfy` branches on the new event names:

- `resolve_failure`: title `ium: {image} cannot resolve {base_tag}`, body =
  `message`, tag `warning`.
- `resolve_recovered`: title `ium: {image} resolving again`, body =
  `message`, tag `white_check_mark`.
- all other events: unchanged.

The webhook sender already forwards the whole payload; `message` rides
along. The `$message` placeholder joins the documented template variables
(`notify.py` ≈95).

### 7.6 Web UI

The `check_error` payload gains `failing_since` (ISO) and `failing_cycles`
when a streak exists. `app.js` appends `(failing since {date}, {n}
cycles)` to the error line. No new panel; the log line is where the
operator already looks.

### 7.7 Tests (Part 7, new `tests/test_resolve_failure.py`, fake clock)

Registry side via `patch("ium.requests.request")`, as in Part 5.

- First failure creates `resolve_failure` with `cycles == 1`, `since == now`,
  no notification.
- Reason changes mid-streak: `reason` updated, `since` unchanged.
- Success after a streak that never notified: field cleared, no
  notification, INFO logged.
- Success after a streak that did notify: field cleared, one
  `resolve_recovered` sent.
- Success with unchanged digest still clears the field (state written even
  though no update occurred).
- Diagnosis gating: not run at cycle 1; not run at cycle 2 if `since` is
  90 minutes old; run at cycle 2 at 2 h; not re-run within 24 h for the same
  base digest; with a changed base digest, not re-run at 5 h since the last
  sweep, re-run at 6 h.
- Reason surfaced between sweeps: after a `regex_stale` sweep, the next
  failing cycle (no sweep) still shows the refined text; after the base
  digest changes and before the next sweep, the raw text shows.
- `INTERNAL`: a cache miss sets the member and the streak bookkeeping runs
  without raising.
- Diagnosis outcomes: qbittorrent fixture (sharing tag `5.3.0_v2.1.2-ls484`,
  regex matches only `-r\d+-ls\d+`) → `regex_stale`, message names the tag,
  one notification; gluetun fixture (no sharing tag) → `rolling_base`, one
  notification; sharing tag that matches the regex → `transient`, no
  notification.
- Same finding on the next diagnosis → no second A notification; a
  different `sharing_tags` set → one more.
- B schedule with a fake clock: nothing at 23 h; one at 24 h; none at 36 h;
  one at 48 h; … one at day 7; none at day 8; one at day 14. With an A send
  at hour 3, the first B send is at hour 27.
- Hub candidate set is the last 100 of the date-ordered list; non-Hub is the
  last 100 of the paginated list; `base_tag` itself is excluded.
- `notify.py`: `resolve_failure` and `resolve_recovered` ntfy titles and
  bodies; webhook payload contains `message`.
- Legacy state without `resolve_failure` loads; a record round-trips.
- Dry run persists nothing.

## Tests

All unit tests use the existing `tests/fake_docker.py` socket fake for the
Docker side and `unittest.mock.patch("ium.requests.request")` for the
registry side, as `tests/test_retry.py` and `tests/test_tag_selection.py`
already do (there is no `responses`/`requests-mock` dependency; do not add
one). There is no shared `updater` fixture in `conftest.py`; each test
module defines its own (see `tests/test_container_discovery.py:10`) and new
modules should follow that pattern. Each test below **must pass**; a
skipped test indicates a missing fixture, not a passing bar.

**Part 1 (`tests/test_container_discovery.py`, extend):**
- List endpoint raises → `_get_containers_for_image` raises `DockerAPIError`;
  `check_and_update` skips the image: no pull request issued, state unchanged,
  `check_error` emitted with `reason == 'docker_unreachable'`.
- List reports `Image: "3fe380ead685"` for a container whose inspect
  `Config.Image` is `portainer/portainer-ce:2.45.0` → discovered with
  `orphaned == True`; a WARNING naming the container is logged.
- List reports a bare ID for a container whose `Config.Image` is a
  **different** repo → not discovered; exactly one inspect was issued.
- Successful discovery with zero containers still takes the "image updated
  only" path and writes state (regression guard for the pre-puller use).

**Part 2 (`tests/test_tag_selection.py` or new `tests/test_current_version.py`):**
- Saved state tag `2.45.3`, container's image ID has no tags → `old_tag ==
  'unknown'`, normal path, container recreated, state written.
- Container list succeeds but inspect raises → image skipped with
  `reason == 'docker_unreachable'`; no recreate, state unchanged, downgrade
  guard never consulted.
- `registry: ghcr.io` override with bare image name and a local
  `ghcr.io/x/y:1.2.3` tag → `old_tag == '1.2.3'`, not `'unknown'`.
- Saved state tag `ls1`, container on `ls329`, candidate `ls1` with a new
  digest → normal path, downgrade guard fires, container **not** recreated.
- No containers, saved state tag `X` → `old_tag == X` (fallback preserved).

**Part 3 (`tests/test_multi_container_update.py`, extend):**
- Container on image ID A, local `repo:tag` is ID B → recreated.
- Container on ID A, local `repo:tag` is ID A, running → skipped, no create.
- Container on ID A, local `repo:tag` is ID A, exited → started, not
  recreated (existing behaviour, now keyed on ID).
- `list_images(full_image)` returns `[]` → recreated, not skipped.

**Part 4 (new `tests/test_blocked_downgrade.py`):**
- First downgrade: state `tag`/`digest` unchanged, `pending` written with
  `reason == 'downgrade'`, `dismissed == False`, one notification.
- Second cycle, same digest: reported again in `updates_found` with
  `repeat == True`; no notification (under 24 h); `first_seen` unchanged.
- `tests/test_webui.py`: a `repeat` entry is not appended to
  `update_history`; a first-detection entry is.
- Second cycle with `last_notified` 25 h old (fake clock): one notification,
  `last_notified` updated.
- Dismissed: not in `updates_found`, no notification, state still not
  advanced, `pending` persisted with `dismissed == True`.
- Registry moves to a newer non-downgrade digest: `pending` cleared, normal
  update applied.
- Registry moves to a different downgrade digest: new `pending`, new
  `first_seen`, `dismissed` reset to `False`, notification sent.
- Same-tag re-push of the pending candidate (sonarr scenario, end to end):
  container stays on the old tag across both cycles.
- Legacy state file without `pending` loads; state file with `pending`
  round-trips through `_save_state`/`_load_state`.
- Dry run writes nothing to disk.
- `tests/test_webui.py`: dismiss endpoint 200 path mutates state and trims
  `last_updates`; 404 when nothing is pending; apply endpoint clears
  `pending`.

**Part 5 (new `tests/test_tag_pagination.py`, patching
`ium.requests.request` with a side-effect that returns per-URL responses
carrying a `headers` dict):**
- Two pages joined by a relative `Link` header → both pages' tags returned,
  second request URL is the resolved link.
- Absolute `Link` URL → used as given.
- No `Link` → single request (regression guard).
- Page cap reached → WARNING logged, tags from the fetched pages returned.
- Second page raises → `[]` returned, not the partial list.

**Part 6 (`tests/test_tag_selection.py`, extend):**
- Each failure branch sets the matching `ResolveFailure` and the emitted
  `check_error.error` contains its text; the success path leaves it `None`.

**Live verification (must run, must pass; the fake cannot prove Docker's
list-endpoint behaviour):** on a machine with a real daemon, reproduce the
orphan: `docker tag alpine:3.20 t/x:1`, run a container from it, `docker
tag alpine:3.19 t/x:1`, then run ium against a config tracking `t/x` with a
local registry or by calling `_get_containers_for_image('t/x')` directly.
It must return the container with `orphaned == True`. Tear down afterwards.

## Implementation ordering

1. Part 1 (discovery) — everything else assumes containers are found.
2. Part 2 (current version from container).
3. Part 3 (ID skip check).
4. Part 4 (pending downgrade) — depends on 2 for routing.
5. Part 5 and Part 6 are independent of 1–4 and of each other; do them in
   either order, or in parallel.
6. Part 7 depends on Part 6 (it refines `ResolveFailure`) and on the
   state-record pattern from Part 4; do it after both.
7. Bump `__version__` to `1.5.0`; update `CHANGELOG` if present. Retarget
   `2026-10-11-release-cooldown-and-child-reconnect-design.md` to 1.6.0
   (done in this commit).

## Compatibility

- State file: two new optional fields (`pending`, `resolve_failure`) with
  `None` defaults. Old files load; new files written by 1.5.0 load in 1.4.0
  only if that version tolerates unknown keys. It does not
  (`ImageState(**data)` raises `TypeError`, caught per image with a warning
  and the image treated as never seen). A rollback to 1.4.0 therefore
  re-reports current versions once; it does not misapply.
- Config: no changes.
- Web UI: one new endpoint, one new button, one new badge, and a
  "failing since" suffix on resolver error lines.
- Notifications: two new event names (`resolve_failure`, `resolve_recovered`)
  and a free-text `message` field in the payload. Webhook consumers that
  switch on `event` see new values; release notes must say so.
- Behaviour change operators will notice: a refused downgrade now stays in
  the pending list until dealt with. Mention in release notes with the
  dismiss action.

## Out of scope

- Per-container version tracking for images with several containers on
  different tags.
- A periodic self-check that containers are on the image ID that state
  claims (would catch future "state says applied, container disagrees"
  drift without waiting for the registry to move). Worth a later spec.
- Repairing the orphaned tag on disk (re-tagging the `<none>` image).
  Unnecessary once discovery finds the container by inspect.
- Release-age cooldown and dependent-child reconnect (now the 1.6.0 spec).
