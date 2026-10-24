# Orphaned-Image Discovery, Blocked-Downgrade Visibility, Resolver-Failure Heuristics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ium 1.5.0: containers whose image tag was re-pointed are still found and updated, a refused downgrade can never be applied by the rebuilt path and stays visible until dealt with, ghcr tag lists paginate, and resolver failures carry an honest reason that escalates to a notification when they persist.

**Architecture:** All logic lives in the single-file updater `ium.py` (class `DockerImageUpdater`, dataclass `ImageState`) with Docker socket access through `docker_api.DockerClient` and notifications through `notify.send_notifications`. The web UI (`webui.py` + `static/js/app.js`) consumes the updater's `progress_callback` events and the `updates_found` list. Two new optional `ImageState` fields (`pending`, `resolve_failure`) carry per-image memory across cycles. No new config keys.

**Tech Stack:** Python 3.8+ (type hints, dataclasses), `requests`, Flask-SocketIO, vanilla JS. Tests: pytest in `.venv` (`.venv/bin/python -m pytest`), Docker fake in `tests/fake_docker.py`, registry mocking via `unittest.mock.patch("ium.requests.request")`. Baseline: 460 tests pass.

**Spec:** `docs/superpowers/specs/2026-10-24-orphaned-image-and-blocked-downgrade-design.md` — read it first; every task below argues from it by section number.

## Global Constraints

- Work on branch `release/v1.5.0` cut from `main`. Do not merge, tag, or publish; Brendan does that. Version becomes `1.5.0` in the last task only.
- No new dependencies. No `responses`/`requests-mock`; patch `ium.requests.request`.
- All Docker socket failures must surface as `DockerAPIError` (CLAUDE.md). Never catch bare `Exception` in new code.
- `socketio.emit()` from background threads includes `namespace='/'`.
- Dry run (`self.dry_run`) persists nothing: `_save_state` already returns early; never bypass it.
- `ImageState` new fields default to `None`; old state files must load unchanged.
- Every test listed **must pass**. A skipped test means a missing prerequisite, not a lowered bar.
- Run the full suite (`.venv/bin/python -m pytest -q`) before every commit; it must stay green.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

Inputs the spec implies but whose tests live only here; each is pinned to a task below.

1. A container list entry whose `Image` is a 64-hex ID **with** `sha256:` prefix and one **without** must both be treated as ID-like (Task 3).
2. A `Link` header carrying two links (`rel="prev"` and `rel="next"`) must pick `next` (Task 10).
3. `pending` with an unknown `reason` (written by a future ium) must be left alone, not treated as a downgrade record (Task 8).
4. `resolve_failure` state must survive a cycle where the image has `auto_update: false` and resolves fine: the clear must not depend on the auto-update path (Task 13).
5. `_qualified_image` with a registry override on an image that already carries a registry host must not double-prefix (Task 1).

---

### Task 1: `_qualified_image` helper

**Files:**
- Modify: `ium.py` — add a method near `_pull_image` (≈890); replace the inline qualification blocks in `_pull_image` (≈905-910) and `_update_container` (≈1121-1127).
- Test: `tests/test_registry_override.py`

**Interfaces:**
- Produces: `DockerImageUpdater._qualified_image(self, image: str, registry: Optional[str]) -> str`. Returns `image` unchanged when `registry` is falsy, equals `DEFAULT_REGISTRY`, or `image`'s first path component already contains `.` or `:` or is `localhost`; otherwise `f"{registry}/{image}"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_registry_override.py`:

```python
class TestQualifiedImage:
    def test_ghcr_override_prefixes(self, updater):
        assert updater._qualified_image("x/y", "ghcr.io") == "ghcr.io/x/y"
    def test_docker_hub_untouched(self, updater):
        assert updater._qualified_image("x/y", "registry-1.docker.io") == "x/y"
    def test_none_untouched(self, updater):
        assert updater._qualified_image("x/y", None) == "x/y"
    def test_already_qualified_not_doubled(self, updater):   # Review Focus 5
        assert updater._qualified_image("ghcr.io/x/y", "ghcr.io") == "ghcr.io/x/y"
    def test_localhost_port_not_doubled(self, updater):
        assert updater._qualified_image("localhost:5000/x", "ghcr.io") == "localhost:5000/x"
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_registry_override.py -q` — expect FAIL, `AttributeError: _qualified_image`.
- [ ] **Step 3: Implement** the method; make `_pull_image` and `_update_container` call it instead of their inline copies. Behaviour of both must be unchanged.
- [ ] **Step 4: Run** the full suite — expect all green.
- [ ] **Step 5: Commit** `refactor: extract _qualified_image for registry-prefixed references`.

---

### Task 2: Discovery failure defers the image (spec §1.1, §1.3)

**Files:**
- Modify: `ium.py` `_get_containers_for_image` (≈957-995, the `except DockerAPIError` at the end), `check_and_update` (discovery call ≈1605, moved inside the digest-changed branch ≈1607), `webui.py` `api_apply_update` (≈470).
- Test: `tests/test_container_discovery.py`, `tests/test_webui.py`

**Interfaces:**
- Produces: `_get_containers_for_image` **raises** `DockerAPIError` instead of returning `[]` on a list failure. `check_and_update` emits `check_error` with `reason: 'docker_unreachable'` and `continue`s before any pull/notify/state write. Discovery now happens only when `not saved_state or saved_state.digest != digest`.

- [ ] **Step 1: Write the failing tests** in `tests/test_container_discovery.py` (new class `TestDiscoveryFailureDefers`; fixture config has `auto_update: True`, state pre-seeded with a different digest so the branch is reached; patch `find_matching_tag` to return `('4.0.1.100-ls1', 'sha256:new')`, patch `updater.docker.list_containers` with `side_effect=DockerAPIError(0, "boom")`, patch `_pull_image`):

```python
def test_list_error_raises(self, updater):            # unit
    with patch.object(updater.docker, 'list_containers', side_effect=DockerAPIError(0, "boom")):
        with pytest.raises(DockerAPIError):
            updater._get_containers_for_image("linuxserver/sonarr")

def test_list_error_skips_image(self, updater):       # integration
    events = []
    ... updater.check_and_update(progress_callback=lambda e, d: events.append((e, d)))
    assert mock_pull.call_count == 0
    assert updater.state == {}  or state unchanged from seed
    assert any(e == 'check_error' and d['reason'] == 'docker_unreachable' for e, d in events)

def test_unchanged_digest_makes_no_docker_call(self, updater):
    # state digest == resolved digest; list_containers must not be called at all
```

Also in `tests/test_webui.py`: `api_apply_update` returns 503 when `_get_containers_for_image` raises `DockerAPIError`.

`DockerAPIError(status: int, message: str)` is the constructor; status 0 means transport failure.

- [ ] **Step 2: Run** the three new tests — expect FAIL (today `[]` is returned and the pull happens).
- [ ] **Step 3: Implement**: re-raise in `_get_containers_for_image` after the existing `self.logger.error`; move the discovery call to immediately before the `old_tag` computation inside the branch; wrap with the spec §1.1 snippet; wrap in `api_apply_update` returning `jsonify({'error': ...}), 503`.
- [ ] **Step 4: Run** the full suite. Existing tests that patch `_get_containers_for_image` with `return_value` are unaffected; any test asserting a discovery call for an unchanged digest must be updated to the new behaviour (there should be none; if one exists, the assertion was pinning the bug).
- [ ] **Step 5: Commit** `fix: defer the image when container discovery fails instead of recording success`.

---

### Task 3: ID-based discovery fallback for orphaned containers (spec §1.2)

**Files:**
- Modify: `ium.py` `_get_containers_for_image`; add module-level `ID_LIKE = re.compile(r'^(sha256:)?[0-9a-f]{12,64}$')`.
- Test: `tests/test_container_discovery.py`

**Interfaces:**
- Produces: discovery entries may carry `'orphaned': True`. Inspect is via `self.docker.inspect_container(name)` directly (not `_get_container_config`, which swallows errors), so `DockerAPIError` propagates.

- [ ] **Step 1: Write the failing tests** (new class `TestOrphanedContainerFallback`; patch `updater.docker.list_containers` and `updater.docker.inspect_container`):

```python
def test_orphan_found_via_inspect(self, updater, caplog):
    list → [{'Id': 'c1', 'Names': ['/portainer'], 'Image': '3fe380ead685', 'State': 'running'}]
    inspect('portainer') → {'Config': {'Image': 'portainer/portainer-ce:2.45.0'}, 'Image': 'sha256:3fe380…'}
    result = updater._get_containers_for_image('portainer/portainer-ce')
    assert result == [{'name': 'portainer', 'id': 'c1', 'state': 'running',
                       'image_ref': 'portainer/portainer-ce:2.45.0', 'orphaned': True}]
    assert 'matched via inspect' in caplog.text  (WARNING level)

def test_sha256_prefixed_id_also_treated_as_id(self, updater):   # Review Focus 1
    'Image': 'sha256:' + 'a'*64 → inspect is called

def test_orphan_of_other_repo_not_matched(self, updater):
    inspect Config.Image 'linuxserver/sonarr:1' → result == []; inspect called exactly once

def test_named_containers_not_inspected(self, updater):
    'Image': 'portainer/portainer-ce:2.45.0' → found by name; inspect_container.call_count == 0

def test_inspect_error_propagates(self, updater):
    inspect side_effect DockerAPIError → pytest.raises(DockerAPIError)
```

- [ ] **Step 2: Run** — expect FAIL (orphan returns `[]`).
- [ ] **Step 3: Implement** per the spec §1.2 snippet, after the name-match loop. Keep the existing debug line for the no-match case.
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `fix: discover containers whose image tag no longer resolves, via inspect`.

---

### Task 4: `_get_container_current_tag` raises on transport errors and qualifies the reference (spec §2, second half)

**Files:**
- Modify: `ium.py` `_get_container_current_tag` (≈1051-1100).
- Test: `tests/test_registry_override.py`, `tests/test_container_discovery.py`

**Interfaces:**
- Produces: `_get_container_current_tag(self, container_name: str, image: str, regex: str, registry: Optional[str] = None) -> Optional[str]`. Returns `None` only when the container's image ID carries no regex-matching tag. Raises `DockerAPIError` on inspect or image-list failure. Uses `self.docker.inspect_container` and `self.docker.list_images(self._qualified_image(image, registry))` directly. No bare `except Exception`.

- [ ] **Step 1: Write the failing tests**:

```python
# tests/test_container_discovery.py, class TestCurrentTag
def test_inspect_error_raises(self, updater): inspect side_effect DockerAPIError → raises
def test_list_images_error_raises(self, updater): list_images side_effect DockerAPIError → raises
def test_no_matching_tag_returns_none(self, updater): image ID present, RepoTags ['x/y:latest'] only → None
def test_matching_tag_returned(self, updater): RepoTags ['linuxserver/sonarr:4.0.1.100-ls1'] → '4.0.1.100-ls1'

# tests/test_registry_override.py
def test_current_tag_uses_qualified_reference(self, updater):
    list_images is called with 'ghcr.io/x/y' when registry='ghcr.io'; RepoTags ['ghcr.io/x/y:1.2.3'] → '1.2.3'
```

- [ ] **Step 2: Run** — expect FAIL (errors are swallowed today; `list_images` is called with the bare name).
- [ ] **Step 3: Implement.** Remove the `try/except Exception` and the `_get_container_config` indirection; add the `registry` parameter.
- [ ] **Step 4: Run** the full suite. Tests that patch `_get_container_current_tag` keep working.
- [ ] **Step 5: Commit** `fix: current-tag lookup propagates Docker errors and honours registry overrides`.

---

### Task 5: Current version comes from the container (spec §2, first half)

**Files:**
- Modify: `ium.py` `check_and_update` `old_tag` block (≈1611-1616).
- Test: new `tests/test_current_version.py`

**Interfaces:**
- Consumes: Task 4's signature; Task 2's wrapper (a `DockerAPIError` from the lookup is caught by the **same** try/except as discovery and yields `reason: 'docker_unreachable'`).
- Produces: `old_tag` priority = container's actual tag → `'unknown'` if containers exist but none has a matching tag → `saved_state.tag` when there are no containers → `'unknown'`.

- [ ] **Step 1: Write the failing tests** (fixture: `auto_update: True`; patch `find_matching_tag`, `_get_containers_for_image`, `_get_container_current_tag`, `_pull_image`, `_update_containers`; read `updates[0]['old_tag']`):

```python
def test_container_tag_beats_saved_state(updater):        # saved 'ls1', container 'ls329' → old_tag 'ls329'
def test_orphan_container_is_unknown(updater):            # saved '2.45.3', lookup None → 'unknown'; downgrade False; _update_containers called
def test_no_containers_falls_back_to_state(updater):      # containers [] → saved tag
def test_sonarr_repush_hits_downgrade_guard(updater):     # saved 'ls1', container 'ls329', candidate 'ls1' new digest → updates[0]['downgrade'] is True; _update_containers NOT called
def test_lookup_error_defers(updater):                    # lookup raises DockerAPIError → check_error docker_unreachable, no pull
```

- [ ] **Step 2: Run** — expect FAIL on the first, second and fourth.
- [ ] **Step 3: Implement** the spec §2 block; pass `registry` through to the lookup.
- [ ] **Step 4: Run** the full suite. `tests/test_multi_container_update.py` patches the lookup to return the old tag already, so it stays green.
- [ ] **Step 5: Commit** `fix: read the current version from the container, not saved state`.

---

### Task 6: Rebuilt-path skip check compares image IDs (spec §3)

**Files:**
- Modify: `ium.py` `_update_container` skip block (≈1138-1162).
- Test: `tests/test_multi_container_update.py` (new class `TestSkipCheckById`) using `tests/fake_docker.py` so the real `DockerClient` request path runs. Read `tests/test_interrupted_update_recovery.py` for how the fake handler is wired for inspect/start/create.

**Interfaces:**
- Consumes: Task 1 `_qualified_image`.
- Produces: skip iff `list_images(full_image)[0]['Id'] == container_info['Image']`; empty `list_images` result → recreate.

- [ ] **Step 1: Write the failing tests**:

```python
def test_same_id_running_skips(): container Image 'sha256:A', images/json → [{'Id': 'sha256:A'}] → returns True, no POST /containers/create
def test_same_id_exited_starts():  status 'exited' → POST /containers/{name}/start issued, no create
def test_different_id_recreates(): container 'sha256:A', local tag 'sha256:B' → create issued (string Config.Image equal to full_image!)
def test_missing_local_tag_recreates(): images/json → [] → create issued
```

The third test is the regression for the Aug 27 portainer event: `Config.Image` equals the target string yet the container must be recreated.

- [ ] **Step 2: Run** — expect FAIL on the third.
- [ ] **Step 3: Implement** per spec §3; log `already running {full_image} ({target_id[:19]}), skipping`.
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `fix: skip an update only when the container is on the target image ID`.

---

### Task 7: `ImageState` fields and a clock hook

**Files:**
- Modify: `ium.py` `ImageState` (≈281), `DockerImageUpdater.__init__`; add `_now(self) -> datetime`.
- Test: `tests/test_state.py`, `tests/conftest.py` `sample_state`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class ImageState:
      base_tag: str; tag: str; digest: str; last_updated: str
      pending: Optional[Dict[str, Any]] = None
      resolve_failure: Optional[Dict[str, Any]] = None
  ```
  and `DockerImageUpdater._now(self) -> datetime` returning `datetime.now()`; all **new** code in later tasks calls `self._now()` so tests can `patch.object(updater, '_now', return_value=...)`. Existing `datetime.now()` call sites are left alone.

- [ ] **Step 1: Write the failing tests**: in `tests/test_state.py`, update `test_asdict` to expect `"pending": None, "resolve_failure": None`; add `test_legacy_dict_loads` (`ImageState(**{four keys})` works) and `test_pending_round_trips` (dict with a `pending` record survives `asdict` → JSON → `ImageState(**json.loads(...))`).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `feat: optional pending/resolve_failure state fields and a clock hook`.

---

### Task 8: Blocked downgrade stays pending (spec §4.1–4.3)

**Files:**
- Modify: `ium.py` `check_and_update` inside the `old_tag != matching_tag` branch after `is_downgrade` (≈1623); add `_write_pending(self, image, saved_state, base_tag, pending) -> None` and `_downgrade_pending(self, saved_state, digest) -> Optional[dict]`.
- Test: new `tests/test_blocked_downgrade.py`

**Interfaces:**
- Consumes: Task 7 fields and `_now()`; Task 5 `old_tag`.
- Produces: `update_info` gains `'repeat': bool`. `pending` record shape exactly as spec §4.2. `send_notifications` gating per §4.1 (first detection, then ≥ 24 h).

- [ ] **Step 1: Write the failing tests** (fixture `auto_update: True`; patch `find_matching_tag`, `_get_containers_for_image` → one container, `_get_container_current_tag` → `'4.0.20.3014-ls329'`, `_pull_image`, `_update_containers`, `ium.send_notifications`, `_now`):

```python
def test_first_downgrade_writes_pending_not_tag(): state.tag unchanged, state.pending == {'tag': 'ls1', 'digest': 'sha256:d1', 'first_seen': NOW, 'reason': 'downgrade', 'dismissed': False, 'last_notified': NOW}; notify called once; updates[0]['repeat'] is False; _update_containers not called
def test_second_cycle_repeats_without_notify(): same digest, +1h → in updates with repeat True; notify not called; first_seen unchanged
def test_renotifies_after_24h(): +25h → notify once, last_notified updated
def test_dismissed_is_silent(): pending.dismissed True → updates == []; notify not called; pending persisted; tag unchanged
def test_registry_moves_on_clears_pending(): candidate newer digest, tag 'ls330' → _update_containers called; state.pending is None
def test_different_downgrade_digest_resets(): candidate 'ls2' digest d2 → new first_seen, dismissed False, notify once
def test_unknown_reason_pending_left_alone():   # Review Focus 3
    saved pending {'reason': 'cooldown', ...} and a normal upgrade → applied; pending cleared by the fresh ImageState (not by downgrade code)
def test_first_run_downgrade_writes_placeholder(): no saved state → state.tag == 'ls329', digest '' , pending set
def test_dry_run_persists_nothing(): updater.dry_run True → state file content unchanged on disk
def test_state_round_trip(): save then reload → equal
```

- [ ] **Step 2: Run** — expect FAIL (today state advances to `ls1`).
- [ ] **Step 3: Implement** per spec §4.3: compute `pending`, branch on `dismissed`, gate the notification on `last_notified`, call `_write_pending`, `continue`. Add `'repeat': not newly_created` to `update_info`. `_write_pending` builds the `ImageState` per spec (existing tag/digest/last_updated, or `old_tag`/`''`/now).
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `fix: a refused downgrade stays pending instead of advancing state`.

---

### Task 9: Dismiss endpoint, history filter, UI (spec §4.4)

**Files:**
- Modify: `webui.py` (`run_check` history append ≈225; new route `/api/updates/dismiss`), `static/js/app.js` `displayUpdates` (≈263) and a new `dismissUpdate(btn)`, `static/css/style.css` (add `.badge-downgrade` next to ≈498).
- Test: `tests/test_webui.py`

**Interfaces:**
- Consumes: Task 8 `repeat` flag and `pending` record.
- Produces: `POST /api/updates/dismiss` JSON `{"image"}` → 200 `{"success": true}` after setting `pending['dismissed'] = True`, `_save_state()`, removing the image from `last_updates`, emitting `check_complete` with the trimmed list (`namespace='/'`); 404 `{"error": ...}` when no downgrade `pending` exists. Decorated with `@require_updater` and the same CSRF header check the apply route uses.

- [ ] **Step 1: Write the failing tests** in `tests/test_webui.py` (follow the existing `app_client` fixture and the apply-update tests' patching style):

```python
def test_dismiss_marks_pending_and_trims(app_client): seed updater.state[...] with a downgrade pending; seed webui.last_updates → 200; state pending dismissed; last_updates empty; _save_state called
def test_dismiss_404_without_pending(app_client)
def test_repeat_update_not_appended_to_history(app_client): patch check_and_update → [{..., 'repeat': True}] → history length unchanged
def test_first_detection_appended_to_history(app_client): 'repeat': False → appended
```

- [ ] **Step 2: Run** — expect FAIL (404 route; history grows).
- [ ] **Step 3: Implement** the route and the `if update.get('repeat'): continue` in `run_check`. In `app.js`: when `update.downgrade`, render `<span class="badge badge-downgrade">Older than current</span>` and a `btn btn-dismiss` "Ignore until registry changes" next to Apply; `dismissUpdate` POSTs with the same headers as `applyUpdate`, removes the item on success, toasts on failure. CSS: `.badge-downgrade { color: var(--warning); border-color: var(--warning); background: var(--warning-soft); }` (check the variable names exist in `style.css`; use the closest existing warning tokens).
- [ ] **Step 4: Run** the full suite — green. Open the UI once locally (gunicorn per project memory) with a fake pending entry injected via a test or a dry-run config, and confirm the badge and button render. State what you saw.
- [ ] **Step 5: Commit** `feat: dismiss a pending downgrade from the web UI; keep repeats out of history`.

---

### Task 10: Tag pagination (spec §5)

**Files:**
- Modify: `ium.py` `_get_all_tags` (≈701-727); add module-level `MAX_TAG_PAGES = 50` and `_next_link(link_header: Optional[str], base: str) -> Optional[str]`.
- Test: new `tests/test_tag_pagination.py`

**Interfaces:**
- Produces: `_next_link` parses RFC 5988 `Link` values (comma-separated, each `<url>; rel="x"`), returns the `rel="next"` URL resolved against `base` (`urllib.parse.urljoin`), or `None`.

- [ ] **Step 1: Write the failing tests** (patch `ium.requests.request` with a `side_effect` function keyed on URL; responses built like `_mock_response` in `tests/test_tag_selection.py`, with `headers={'Link': ...}`):

```python
def test_next_link_relative(): _next_link('</v2/a/b/tags/list?last=x&n=0>; rel="next"', 'https://ghcr.io') == 'https://ghcr.io/v2/a/b/tags/list?last=x&n=0'
def test_next_link_absolute()
def test_next_link_picks_next_among_several():   # Review Focus 2  '<u1>; rel="prev", <u2>; rel="next"' → u2
def test_next_link_none_without_next()
def test_two_pages_joined(updater): tags == page1 + page2; second request URL is the resolved link
def test_single_page_single_request(updater)
def test_page_cap_warns(updater, caplog): 51 pages offered → 50 requests, WARNING 'truncated at 50 pages'
def test_mid_way_error_returns_empty(updater): page 2 raises RequestException → []
```

- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** per spec §5.
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `fix: follow Link headers when listing registry tags (ghcr returns 100 per page)`.

---

### Task 11: Honest resolver failure reasons (spec §6)

**Files:**
- Modify: `ium.py` — add `class ResolveFailure(Enum)` next to `DigestStatus` (≈78); set `self._last_resolve_failure` and `self._last_base_digest` in `find_matching_tag`; use them in `check_and_update`'s `if not result:` branch (≈1588).
- Test: `tests/test_tag_selection.py`

**Interfaces:**
- Produces: `ResolveFailure` members `REGISTRY_ERROR, NO_TAGS, NO_REGEX_MATCH, NO_DIGEST_MATCH, NO_FALLBACK_DIGEST, INTERNAL, REGEX_STALE, ROLLING_BASE` with the `.value` texts from spec §6 and §7.3 (add the last two now so Task 14 does not touch the enum). `find_matching_tag` sets `self._last_resolve_failure: Optional[ResolveFailure] = None` and `self._last_base_digest: Optional[str] = None` on entry; every `return None` sets a member; success leaves it `None`. `check_error` payload: `{'image', 'base_tag', 'reason': failure.name.lower(), 'error': f"Could not determine version for {image}:{base_tag}: {failure.value}"}`.

- [ ] **Step 1: Write the failing tests** (class `TestResolveFailureReason`, one test per branch, each asserting `updater._last_resolve_failure is ResolveFailure.X` and, through `check_and_update` with a `progress_callback`, that `check_error['error']` contains `X.value`; plus `test_success_leaves_none` and `test_internal_on_pattern_cache_miss` which deletes the entry from `updater.compiled_patterns`).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.** The WARNING log line gets the same suffix.
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `feat: resolver failures carry a specific reason to the log and UI`.

---

### Task 12: Notification plumbing for new events (spec §7.5)

**Files:**
- Modify: `notify.py` `_build_payload`, `send_notifications` (add `message: str = ''`), `send_ntfy` branches, `send_webhook` template vars + docstring.
- Test: find the existing notify tests (`grep -l "send_ntfy\|send_webhook" tests/*.py`); extend that file.

**Interfaces:**
- Produces: `send_notifications(notif_cfg, image, old_version, new_version, event, digest='', auto_update=False, message='')`. Payload gains `'message'`. ntfy: `resolve_failure` → title `ium: {image} cannot resolve {new_version}`, body `message`, `Tags: warning`; `resolve_recovered` → title `ium: {image} resolving again`, body `message`, `Tags: white_check_mark`. Webhook template gains `$message`.

- [ ] **Step 1: Write the failing tests**: ntfy title/body/tags for both events (patch `notify.requests.post`, inspect `headers['Title']`, `headers['Tags']`, `data`); webhook raw payload contains `message`; `body_template` with `$message` substitutes; existing events unchanged.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `feat: resolve_failure/resolve_recovered notification events with a message body`.

---

### Task 13: Failure-streak bookkeeping and escalation schedule (spec §7.1, §7.2, §7.4, §7.6)

**Files:**
- Modify: `ium.py` `check_and_update` — the `if not result:` branch (after Task 11) and the success path immediately after `matching_tag, digest = result`; add `_write_resolve_failure(self, image, saved_state, base_tag, rf) -> None`, `_clear_resolve_failure(self, image, saved_state, matching_tag, digest, image_config) -> None`, `_escalate_resolve_failure(self, image, base_tag, rf, failure_text, image_config) -> None`.
- Test: new `tests/test_resolve_failure.py`

**Interfaces:**
- Consumes: Task 7 fields/`_now`; Task 11 `_last_resolve_failure`/`_last_base_digest`; Task 12 events.
- Produces: `rf` record per spec §7.1 (without `diag`; Task 14 adds it). Schedule per §7.4 with `age`/`gap`. `check_error` payload gains `failing_since` and `failing_cycles` from cycle 2. Success with a prior `resolve_failure` clears it even when the digest is unchanged, logs `RESOLVED AGAIN: {image} after {duration}`, sends `resolve_recovered` iff `last_notified` was set. Notification payload fields per the §7.5 table.

- [ ] **Step 1: Write the failing tests** (patch `find_matching_tag` to return `None` and set `updater._last_resolve_failure = ResolveFailure.NO_REGEX_MATCH` via a `side_effect`; patch `ium.send_notifications`; drive `_now`):

```python
def test_first_failure_creates_record(): cycles 1, since NOW, last_notified None, no notify, state file written
def test_reason_updates_since_stays()
def test_no_notify_before_24h(): at 23h → none
def test_daily_then_weekly(): sends at 24h, not 36h, 48h, …, day 7, not day 8, day 14   (loop with a table of (hours, expected_count))
def test_recovered_after_notify(): success → field None, resolve_recovered sent once with new_version == resolved tag
def test_recovered_without_notify_is_silent()
def test_success_unchanged_digest_still_clears():     # Review Focus 4; also auto_update False
def test_failing_since_in_check_error(): cycle 2 payload has failing_since, failing_cycles == 2; cycle 1 has neither
def test_never_resolved_image_gets_placeholder_state(): no saved state → tag 'unknown', digest ''
def test_dry_run_persists_nothing()
```

- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.** Keep the `rf` write **before** the `continue`; keep the clear **before** the Part 1.1/Part 4 `continue`s on the success path (reviewer note in spec). Preserve `tag`/`digest`/`last_updated`/`pending` on failing-cycle writes.
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `feat: track resolver failure streaks and notify daily, then weekly`.

---

### Task 14: Digest-mismatch diagnosis (spec §7.3)

**Files:**
- Modify: `ium.py` — add `_diagnose_base_tag(self, image, base_tag, regex, registry, base_digest) -> Dict[str, Any]` (returns the `diag` dict) and `_should_diagnose(self, rf, base_digest) -> bool`; call from the failure branch after bookkeeping and before escalation; add `_effective_failure(self, failure, rf, base_digest) -> ResolveFailure` used by the log line, `check_error`, and escalation text.
- Test: `tests/test_resolve_failure.py` (extend)

**Interfaces:**
- Consumes: `_get_all_tags_by_date` (Hub, newest last, take `[-100:]`), `_get_all_tags` (others, `[-100:]`), `_get_manifest_digest_head`, `_get_docker_token`, `ThreadPoolExecutor(max_workers=min(10, n))`.
- Produces: `diag = {'base_digest', 'checked_at', 'outcome': 'regex_stale'|'rolling_base'|'transient', 'sharing_tags': sorted(list)[:5]}` stored on `rf`. Gate: `rf['cycles'] >= 2 and age >= 2h and (no diag or checked_at ≥ 24h old or (base_digest changed and checked_at ≥ 6h old))`. A-notification: `resolve_failure` sent immediately iff outcome ∈ {regex_stale, rolling_base} and `(outcome, sharing_tags)` differs from the previous diag (or none existed); sets `last_notified`. `_effective_failure` returns `REGEX_STALE`/`ROLLING_BASE` when `diag` exists, `diag['base_digest'] == base_digest`, and the raw failure is `NO_DIGEST_MATCH`; else the raw failure. `REGEX_STALE.value` is formatted with the sharing tags and regex; `ROLLING_BASE.value` with `base_tag`.

- [ ] **Step 1: Write the failing tests** (patch `_get_all_tags_by_date`, `_get_manifest_digest_head` with a dict of tag→digest, `_get_docker_token`):

```python
def test_gate_cycle1_no(); def test_gate_cycle2_90min_no(); def test_gate_cycle2_2h_yes()
def test_gate_same_digest_within_24h_no(); def test_gate_digest_changed_5h_no(); def test_gate_digest_changed_6h_yes()
def test_qbittorrent_regex_stale(): sharing '5.3.0_v2.1.2-ls484', regex old form → outcome regex_stale; one notify; message names the tag and the regex
def test_gluetun_rolling_base(): no sharing tag → rolling_base; one notify; message mentions 'rolling'
def test_transient_no_notify(): sharing tag matches regex → 'transient', no notify
def test_same_finding_no_second_notify(); def test_new_sharing_set_notifies_again()
def test_refined_reason_persists_between_sweeps(): next failing cycle without sweep → check_error['error'] contains REGEX_STALE text
def test_raw_reason_after_digest_moves_before_sweep()
def test_hub_candidates_last_100_of_date_order(); def test_non_hub_candidates_last_100_of_list(); def test_base_tag_excluded()
def test_a_send_delays_first_b_send(): A at hour 3 → no B at 24h, B at 27h
```

- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.** Reuse the HEAD-fan-out shape from `find_matching_tag` (do not refactor it; a second small loop is fine).
- [ ] **Step 4: Run** the full suite — green.
- [ ] **Step 5: Commit** `feat: diagnose persistent digest mismatches by naming what the base tag really is`.

---

### Task 15: Failing-since suffix in the UI (spec §7.6)

**Files:**
- Modify: `static/js/app.js` `handleCheckProgress` `check_error` case (≈214).

- [ ] **Step 1:** Append `` (failing since ${new Date(eventData.failing_since).toLocaleDateString()}, ${eventData.failing_cycles} cycles)`` when `eventData.failing_since` is present. No automated UI test exists; verify by loading the UI with a config whose image cannot resolve (e.g. regex `^nomatch$`) in dry-run and running two checks. State what you saw.
- [ ] **Step 2: Commit** `feat: show how long a resolver failure has persisted in the check log`.

---

### Task 16: Version, changelog, live verification, release prep

**Files:**
- Modify: `ium.py` `__version__` → `"1.5.0"`, `CHANGELOG.md` (new `## [1.5.0] - 2026-10-24` section with Fixed/Added/Changed per the spec's Compatibility section, including the new notification events, the `pending` behaviour change, and the dismiss action).

- [ ] **Step 1: Live verification (must run, must pass).** On this machine's Docker daemon, run the orphan repro from spec "Tests → Live verification":
  ```bash
  docker pull -q alpine:3.20 && docker pull -q alpine:3.19
  docker tag alpine:3.20 iumtest/foo:1
  docker run -d --name iumtest-c iumtest/foo:1 sleep 600
  docker tag alpine:3.19 iumtest/foo:1
  ```
  Then from the repo, with a throwaway config tracking `iumtest/foo`, call `_get_containers_for_image('iumtest/foo')` against the real socket and assert it returns `iumtest-c` with `orphaned == True`. Write the one-off as `scripts/live_orphan_check.py` or run it inline; paste the output into the commit message body. Tear down: `docker rm -f iumtest-c; docker rmi iumtest/foo:1`.
- [ ] **Step 2:** Bump version, write the changelog, run the full suite.
- [ ] **Step 3: Commit** `chore: bump version to 1.5.0`.
- [ ] **Step 4:** Push `release/v1.5.0` and open a PR to `main` titled `Release v1.5.0: orphaned-image discovery, blocked-downgrade visibility, resolver-failure heuristics`, body summarising the spec's Background and Decisions table, ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Do not merge.

---

## Self-review notes

- Spec coverage: §1.1/§1.3 → T2; §1.2 → T3; §2 → T4, T5; §3 → T6; §4.1–4.3 → T8; §4.4 → T9; §5 → T10; §6 → T11; §7.1 → T7, T13; §7.2/§7.4/§7.6 → T13, T15; §7.3 → T14; §7.5 → T12; Compatibility/ordering/version → T16. Cooldown-spec retarget already committed.
- Interfaces: `_qualified_image` (T1) used by T4, T6; `_now` (T7) by T8, T13, T14; `ResolveFailure` incl. `REGEX_STALE`/`ROLLING_BASE` defined once in T11; `repeat` flag T8 → T9; `message` kwarg T12 → T13, T14.
- Ordering constraints: T1 before T4/T6; T2 before T3/T5; T7 before T8/T13; T11 before T13/T14; T12 before T13. T10 is independent and may run at any point.
