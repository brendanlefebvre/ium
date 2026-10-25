#!/usr/bin/env python3
"""
Docker Image Auto-Update with Specific Tag Tracking

This script monitors Docker images for updates by comparing a base tag
(e.g., 'latest', 'stable', or a version like '14') with version-specific
tags that match user-defined regex patterns.
"""

__version__ = "1.5.0"

import copy
import json
import re
import secrets
import sys
import time
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from dataclasses import dataclass, asdict, replace
from contextlib import contextmanager
from enum import Enum
import argparse
import os
import platform
from urllib.parse import urljoin, urlparse
import requests
import jsonschema

from pattern_utils import detect_tag_patterns, detect_base_tags
from docker_api import DockerClient, DockerAPIError
from notify import send_notifications

# Platform-specific imports and constant
IS_WINDOWS = platform.system() == 'Windows'
if not IS_WINDOWS:
    import fcntl
else:
    import msvcrt

# Apply TZ from environment (default UTC) before any logging is configured
os.environ.setdefault('TZ', 'UTC')
if not IS_WINDOWS:
    time.tzset()


# Constants
DEFAULT_REGISTRY = "registry-1.docker.io"
DEFAULT_AUTH_URL = "https://auth.docker.io/token"
DEFAULT_NAMESPACE = "library"
DEFAULT_BASE_TAG = "latest"

# Container-update timeouts, overridable per image in config.json.
# DEFAULT_STOP_TIMEOUT is Docker's SIGTERM grace period; DEFAULT_REQUEST_TIMEOUT
# is the socket read budget for lifecycle calls.  60 s rather than the 30 s
# http.client default: stopping a large container on NAS-grade storage overran
# the old 40 s stop budget and left the container down (2026-07-29 incident).
DEFAULT_STOP_TIMEOUT = 10
DEFAULT_REQUEST_TIMEOUT = 60

# Registry (HTTPS) request timeout — unrelated to the Docker socket budget above.
REGISTRY_REQUEST_TIMEOUT = 30

# Upper bound on Link-header pages followed when listing a repository's tags.
MAX_TAG_PAGES = 50

# A pending (refused) downgrade is notified on first detection, then at most
# once per this interval; it is re-reported to the UI every cycle regardless.
DOWNGRADE_RENOTIFY_INTERVAL = timedelta(hours=24)

# Resolver failure escalation (spec §7.4): silent for the first day of a
# streak, then at most daily through day 7, then at most weekly.
RESOLVE_FAILURE_FIRST_NOTIFY = timedelta(hours=24)
RESOLVE_FAILURE_DAILY = timedelta(hours=24)
RESOLVE_FAILURE_WEEKLY_AFTER = timedelta(days=7)
RESOLVE_FAILURE_WEEKLY = timedelta(days=7)
# Digest-mismatch diagnosis gate and sweep size (spec §7.3).
DIAG_MIN_STREAK = timedelta(hours=2)    # shorter streaks are release races
DIAG_REFRESH = timedelta(hours=24)      # re-diagnose an unchanged base daily
DIAG_MOVED_FLOOR = timedelta(hours=6)   # min gap when the base digest moved
DIAG_MAX_CANDIDATES = 100
DIAG_MAX_SHARING_TAGS = 5

# Container states that mean "not running", so an update may safely continue
# after a stop request failed.  'paused' is deliberately excluded: a paused
# container still holds its processes.
STOPPED_STATUSES = frozenset({'exited', 'created', 'dead'})

# Docker's container list reports a bare image ID (instead of a name) when the
# container's original image reference no longer resolves to its image.
ID_LIKE = re.compile(r'^(sha256:)?[0-9a-f]{12,64}$')
MANIFEST_ACCEPT_HEADER = (
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json"
)


class DigestStatus(Enum):
    """Outcome of a manifest digest HEAD request."""
    OK = "ok"
    NOT_FOUND = "not_found"   # registry said 404: the tag does not exist
    ERROR = "error"           # transient/auth/protocol failure: result unknown


class ResolveFailure(Enum):
    """Why ``find_matching_tag`` could not resolve a version (spec §6).

    ``REGEX_STALE`` and ``ROLLING_BASE`` are refined diagnoses of
    ``NO_DIGEST_MATCH`` (spec §7.3); their values are ``str.format``
    templates.  The other values contain no braces.
    """
    REGISTRY_ERROR = "Transient registry error resolving the base tag; will retry next cycle"
    NO_TAGS = "Could not list tags from the registry"
    NO_REGEX_MATCH = "No tags match the regex"
    NO_DIGEST_MATCH = ("Tags match the regex but none shares the base tag's digest; "
                       "the base tag may be a rolling build (try a release-line base_tag such as v3) "
                       "or a release may be mid-publish")
    NO_FALLBACK_DIGEST = "Base tag does not exist and the newest matching tag could not be read"
    INTERNAL = "Internal error: regex not found in the compiled-pattern cache; re-save the config or restart ium"
    REGEX_STALE = "{base_tag} is currently `{tags}`, which the regex `{regex}` does not match"
    ROLLING_BASE = ("no tag names the current `{base_tag}` digest; it is probably a rolling build. "
                    "Try a release-line base tag (for example `v3`, `lts`, `stable`)")


# diag['outcome'] -> the refined failure it surfaces (spec §7.3).
_DIAG_REFINEMENTS = {
    'regex_stale': ResolveFailure.REGEX_STALE,
    'rolling_base': ResolveFailure.ROLLING_BASE,
}


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp from state; ``None`` if missing or malformed."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _format_duration(delta: timedelta) -> str:
    """Coarse human duration: ``'3d 4h'``, ``'5h 12m'``, ``'7m'``."""
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _natural_sort_key(tag: str) -> tuple:
    """Sort key ordering digit runs numerically and text runs lexically.

    "v9.8.0-ls399" -> ((1,'v'), (0,9), (1,'.'), (0,8), (1,'.'), (0,0),
                       (1,'-ls'), (0,399))

    The (0, int) / (1, str) tagging keeps any two keys mutually comparable
    (Python 3 raises TypeError on bare int < str), with numbers ordering
    before text when shapes differ.  Plain lexicographic sorting ranked
    "9.0.3" above "15.0.2" and downgraded a production forgejo install.
    """
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r'(\d+)', tag)
        if part
    )

# Configuration schema
CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image": {"type": "string"},
                    "regex": {"type": "string"},
                    "base_tag": {"type": "string"},
                    "auto_update": {"type": "boolean"},
                    "registry": {"type": "string"},
                    "cleanup_old_images": {"type": "boolean"},
                    "keep_versions": {"type": "integer", "minimum": 1},
                    "stop_timeout": {"type": "integer", "minimum": 1},
                    "request_timeout": {"type": "integer", "minimum": 1}
                },
                "required": ["image", "regex"]
            }
        },
        "notifications": {
            "type": "object",
            "properties": {
                "ntfy": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["min", "low", "default", "high", "urgent"]
                        },
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"}
                        }
                    },
                    "required": ["url"]
                },
                "webhook": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "method": {"type": "string"},
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"}
                        },
                        "body_template": {"type": "string"}
                    },
                    "required": ["url"]
                }
            }
        }
    },
    "required": ["images"]
}


def _validate_regex(pattern: str, timeout: float = 2.0) -> re.Pattern:
    """Compile a regex pattern and test it against a short string to detect ReDoS.

    Raises ValueError on invalid pattern or catastrophic backtracking.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}")

    # Test-match against a string that can trigger catastrophic backtracking
    test_string = "a" * 100
    import threading
    result = [None]
    error = [None]

    def _run():
        try:
            compiled.match(test_string)
            result[0] = True
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        raise ValueError(
            f"Regex pattern '{pattern}' is too expensive (possible ReDoS). "
            f"Simplify the pattern to avoid catastrophic backtracking."
        )
    if error[0]:
        raise ValueError(f"Regex pattern '{pattern}' failed test: {error[0]}")

    return compiled


class AuthManager:
    """Manages web UI credentials with auto-generated secure defaults.

    Priority order:
    1. WEBUI_USER / WEBUI_PASSWORD environment variables
    2. Credentials stored in <state_dir>/.auth.json
    3. Auto-generated credentials (first run only)
    """

    AUTH_FILE = ".auth.json"

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.user: str = ""
        self.password: str = ""
        self._load()

    def _load(self) -> None:
        env_user = os.environ.get('WEBUI_USER', '').strip()
        env_password = os.environ.get('WEBUI_PASSWORD', '').strip()
        if env_user and env_password:
            self.user = env_user
            self.password = env_password
            logging.getLogger(__name__).info(
                "Web UI authentication: using credentials from environment variables"
            )
            return

        auth_file = self.state_dir / self.AUTH_FILE
        if auth_file.exists():
            try:
                with open(auth_file, 'r') as f:
                    data = json.load(f)
                self.user = data['username']
                self.password = data['password']
                logging.getLogger(__name__).info(
                    "Web UI authentication: loaded stored credentials"
                )
                return
            except (json.JSONDecodeError, KeyError, IOError) as e:
                logging.getLogger(__name__).warning(
                    f"Could not load auth file, regenerating: {e}"
                )

        self.user = "admin"
        self.password = secrets.token_urlsafe(16)
        self._store(auth_file, first_run=True)

    def _store(self, auth_file: Path, first_run: bool = False) -> None:
        """Atomically write credentials with owner-only permissions."""
        log = logging.getLogger(__name__)
        try:
            auth_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"version": 1, "username": self.user, "password": self.password}
            tmp = auth_file.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            tmp.rename(auth_file)

            if first_run:
                sep = "=" * 48
                log.info(
                    "\n%s\n IUM Web UI - Auto-Generated Credentials\n%s\n"
                    " Username: %s\n Password: %s\n\n"
                    " Stored in: %s\n"
                    " To override, set WEBUI_USER and WEBUI_PASSWORD env vars.\n%s",
                    sep, sep, self.user, self.password, auth_file, sep
                )
        except (IOError, OSError) as e:
            log.error("Could not persist auth credentials: %s", e)
            if first_run:
                log.warning(
                    "IUM Web UI credentials (not persisted):\n"
                    "  Username: %s\n  Password: %s\n"
                    "  Set WEBUI_USER and WEBUI_PASSWORD to avoid regeneration on restart.",
                    self.user, self.password
                )


def _next_link(link_header: Optional[str], base: str) -> Optional[str]:
    """Return the ``rel="next"`` URL from an RFC 5988 Link header, or None.

    Relative targets (ghcr.io sends ``/v2/...?last=x&n=0``) are resolved
    against ``base``; the query string is passed through untouched.

    The caller sends the registry bearer token to the returned URL, so a
    target on a different scheme or host than ``base`` is rejected: None is
    returned, pagination stops, and the tags fetched so far are still used.
    """
    if not link_header:
        return None
    for part in link_header.split(','):
        target, _, params = part.partition(';')
        target = target.strip()
        if not (target.startswith('<') and target.endswith('>')):
            continue
        if re.search(r'rel\s*=\s*"?next"?(?:[\s;]|$)', params, re.IGNORECASE):
            resolved = urljoin(base, target[1:-1])
            base_parts, next_parts = urlparse(base), urlparse(resolved)
            if (base_parts.scheme, base_parts.netloc) != (next_parts.scheme, next_parts.netloc):
                return None
            return resolved
    return None


@dataclass
class ImageState:
    """State information for a tracked image."""
    base_tag: str
    tag: str
    digest: str
    last_updated: str
    pending: Optional[Dict[str, Any]] = None
    resolve_failure: Optional[Dict[str, Any]] = None


class DockerImageUpdater:
    def __init__(self, config_file: str, state_file: str = "image_update_state.json",
                 dry_run: bool = False, log_level: str = "INFO"):
        """
        Initialize the Docker Image Updater.
        
        Args:
            config_file: Path to JSON configuration file
            state_file: Path to store state between runs
            dry_run: If True, only log what would be done without making changes
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        """
        self.config_file = Path(config_file)
        self.state_file = Path(state_file)
        self.dry_run = dry_run
        
        # Setup logging
        self.logger = self._setup_logging(log_level)
        
        # Docker Engine API client
        self.docker = DockerClient()

        # Load configuration and state
        self.compiled_patterns = {}  # Cache for compiled regex patterns
        # Per-registry cache of discovered (realm, service) — None means no auth required.
        self._auth_endpoints: Dict[str, Optional[Tuple[str, str]]] = {}
        # Set by find_matching_tag: why the last call returned None (None on
        # success) and the base tag's digest once its HEAD resolved.
        self._last_resolve_failure: Optional[ResolveFailure] = None
        self._last_base_digest: Optional[str] = None
        self.config = self._load_config()
        self.state = self._load_state()
        
    def _now(self) -> datetime:
        """Current time; a seam so tests can inject a fake clock."""
        return datetime.now()

    def _downgrade_pending(self, saved_state: Optional['ImageState'],
                           digest: str) -> Optional[Dict[str, Any]]:
        """Return the saved downgrade record if it is for this digest, else None.

        Branches on ``reason`` rather than mere presence: a pending record
        with another reason (e.g. a future ``'cooldown'``) is not ours.
        """
        pending = saved_state.pending if saved_state else None
        if (pending and pending.get('reason') == 'downgrade'
                and pending.get('digest') == digest):
            return pending
        return None

    def _downgrade_notify_due(self, last_notified: Optional[str],
                              now: datetime) -> bool:
        """True on first detection or once ``last_notified`` is >= 24 h old."""
        if not last_notified:
            return True
        try:
            return now - datetime.fromisoformat(last_notified) >= DOWNGRADE_RENOTIFY_INTERVAL
        except (TypeError, ValueError):
            # Unparseable timestamp: notify and overwrite it with a good one.
            return True

    def _write_pending(self, image: str, saved_state: Optional['ImageState'],
                       base_tag: str, pending: Dict[str, Any], old_tag: str) -> None:
        """Record ``pending`` without advancing the saved tag/digest.

        With no saved state (first-ever run lands on a downgrade) the
        placeholder is ``tag=old_tag, digest=''``, so a later non-downgrade
        candidate takes the normal path because ``'' != digest``.  Only
        updates ``self.state``; the end-of-cycle ``_save_state`` persists it
        (and skips the write in dry run).
        """
        if saved_state:
            tag, digest, last_updated = saved_state.tag, saved_state.digest, saved_state.last_updated
        else:
            tag, digest, last_updated = old_tag, '', self._now().isoformat()
        self.state[image] = ImageState(
            base_tag=base_tag, tag=tag, digest=digest,
            last_updated=last_updated, pending=pending,
        )

    def _write_resolve_failure(self, image: str, saved_state: Optional['ImageState'],
                               base_tag: str, rf: Dict[str, Any]) -> None:
        """Record the failure streak ``rf`` without touching anything else.

        Preserves the saved tag/digest/last_updated/pending.  An image that
        has never resolved gets the placeholder ``tag='unknown', digest=''``
        (spec §7.1), so its first success takes the normal path because
        ``'' != digest``.  Only updates ``self.state``; the end-of-cycle
        ``_save_state`` persists it (and skips the write in dry run).
        """
        if saved_state:
            self.state[image] = replace(saved_state, resolve_failure=rf)
        else:
            self.state[image] = ImageState(
                base_tag=base_tag, tag='unknown', digest='',
                last_updated=self._now().isoformat(), resolve_failure=rf,
            )

    def _clear_resolve_failure(self, image: str, saved_state: 'ImageState',
                               matching_tag: str, digest: str,
                               image_config: Dict[str, Any]) -> None:
        """End a failure streak: drop the record, log, notify if one was sent.

        Runs on every successful resolve that finds a record, before any of
        the success path's ``continue``s, and regardless of ``auto_update``
        or whether the digest changed (spec §7.2).
        """
        rf = saved_state.resolve_failure or {}
        self.state[image] = replace(saved_state, resolve_failure=None)
        since = _parse_iso(rf.get('since'))
        duration = _format_duration(self._now() - since) if since else 'an unknown time'
        self.logger.info(f"RESOLVED AGAIN: {image} after {duration}")
        if rf.get('last_notified') is not None:
            send_notifications(
                self.config.get('notifications'),
                image=image, old_version=saved_state.tag, new_version=matching_tag,
                event='resolve_recovered', digest=digest,
                auto_update=image_config.get('auto_update', False),
                message=(f"{image} resolves again: {matching_tag} "
                         f"(after failing for {duration})."),
            )

    def _effective_failure(self, failure: ResolveFailure, rf: Dict[str, Any],
                           base_digest: Optional[str]) -> ResolveFailure:
        """The failure surfaced to the operator this cycle.

        A ``regex_stale`` / ``rolling_base`` finding in ``rf['diag']`` refines
        ``NO_DIGEST_MATCH`` while it describes the current base digest
        (spec §7.3, "Which reason is surfaced each cycle"); otherwise the raw
        failure is surfaced.
        """
        diag = rf.get('diag')
        if (failure is not ResolveFailure.NO_DIGEST_MATCH or not diag
                or base_digest is None or diag.get('base_digest') != base_digest):
            return failure
        return _DIAG_REFINEMENTS.get(diag.get('outcome'), failure)

    def _failure_text(self, failure: ResolveFailure, rf: Dict[str, Any],
                      base_tag: str, regex: str) -> str:
        """Operator-facing text for ``failure``; formats the refined templates."""
        if failure is ResolveFailure.REGEX_STALE:
            tags = (rf.get('diag') or {}).get('sharing_tags') or []
            return failure.value.format(tags=', '.join(tags), regex=regex,
                                        base_tag=base_tag)
        if failure is ResolveFailure.ROLLING_BASE:
            return failure.value.format(base_tag=base_tag)
        return failure.value

    def _should_diagnose(self, rf: Dict[str, Any], base_digest: Optional[str]) -> bool:
        """The §7.3 gate: a persistent streak with no fresh diagnosis.

        Not a race (>= 2 cycles and >= 2 h), and either never diagnosed, last
        diagnosed >= 24 h ago, or the base digest moved and the last sweep is
        >= 6 h old (the floor that stops a rolling base re-sweeping hourly).
        The caller checks that the raw failure is ``NO_DIGEST_MATCH``.
        """
        if base_digest is None:
            return False
        now = self._now()
        since = _parse_iso(rf.get('since'))
        if since is None or rf.get('cycles', 0) < 2 or now - since < DIAG_MIN_STREAK:
            return False
        diag = rf.get('diag')
        if not diag:
            return True
        checked_at = _parse_iso(diag.get('checked_at'))
        if checked_at is None:
            return True  # corrupt record: re-diagnose and overwrite it
        age = now - checked_at
        if age >= DIAG_REFRESH:
            return True
        return diag.get('base_digest') != base_digest and age >= DIAG_MOVED_FLOOR

    def _diagnose_base_tag(self, image: str, base_tag: str, regex: str,
                           registry: Optional[str], base_digest: str) -> Dict[str, Any]:
        """Name what ``base_tag`` really is: which tags share its digest (§7.3).

        HEADs up to the last ``DIAG_MAX_CANDIDATES`` tags (Docker Hub in
        push order, others in list order), excluding ``base_tag``.  Outcome:
        a sharing tag matches the regex -> ``transient``; sharing tags exist
        but none matches -> ``regex_stale``; none shares the digest ->
        ``rolling_base``, unless the evidence is incomplete (no tags listed,
        or a HEAD failed) -> ``transient``, so a flaky registry never
        produces a false "rolling build" notification.
        """
        reg, namespace, repo = self._parse_image_reference(image)
        if registry:
            reg = registry
        token = self._get_docker_token(reg, namespace, repo)
        # _get_all_tags_by_date delegates to _get_all_tags off Docker Hub.
        candidates = [t for t in self._get_all_tags_by_date(reg, namespace, repo)
                      [-DIAG_MAX_CANDIDATES:] if t != base_tag]

        def fetch_digest(tag: str) -> Tuple[str, Optional[str], DigestStatus]:
            digest, status = self._get_manifest_digest_head(reg, namespace, repo, tag, token)
            return (tag, digest, status)

        sharing: List[str] = []
        head_errors = 0
        if candidates:
            with ThreadPoolExecutor(max_workers=min(10, len(candidates))) as executor:
                futures = [executor.submit(fetch_digest, tag) for tag in candidates]
                for future in as_completed(futures):
                    tag, digest, status = future.result()
                    if status is DigestStatus.OK and digest == base_digest:
                        sharing.append(tag)
                    elif status is DigestStatus.ERROR:
                        head_errors += 1

        pattern = self.compiled_patterns.get(regex)
        if sharing:
            matched = pattern is None or any(pattern.match(t) for t in sharing)
            outcome = 'transient' if matched else 'regex_stale'
        elif not candidates or head_errors:
            outcome = 'transient'
        else:
            outcome = 'rolling_base'
        return {
            'base_digest': base_digest,
            'checked_at': self._now().isoformat(),
            'outcome': outcome,
            'sharing_tags': sorted(sharing)[:DIAG_MAX_SHARING_TAGS],
        }

    def _maybe_diagnose(self, image: str, base_tag: str, regex: str,
                        registry: Optional[str], failure: ResolveFailure,
                        rf: Dict[str, Any], base_digest: Optional[str],
                        image_config: Dict[str, Any]) -> None:
        """Run the §7.3 diagnosis sweep when gated in; send A's notification.

        Runs after streak bookkeeping and before the §7.4 escalation; stores
        ``rf['diag']`` and, on a new ``regex_stale`` / ``rolling_base``
        finding, sends ``resolve_failure`` at once and sets
        ``rf['last_notified']`` (so it counts toward B's gap).
        """
        if failure is not ResolveFailure.NO_DIGEST_MATCH or base_digest is None:
            return
        if not self._should_diagnose(rf, base_digest):
            return
        previous = rf.get('diag')
        diag = self._diagnose_base_tag(image, base_tag, regex, registry, base_digest)
        rf['diag'] = diag
        self.logger.info(
            f"Diagnosed {image}:{base_tag}: {diag['outcome']}"
            + (f" (shares digest with {', '.join(diag['sharing_tags'])})"
               if diag['sharing_tags'] else "")
        )
        if diag['outcome'] not in ('regex_stale', 'rolling_base'):
            return
        if previous and (previous.get('outcome'), previous.get('sharing_tags')) == \
                (diag['outcome'], diag['sharing_tags']):
            return
        shown = self._effective_failure(failure, rf, base_digest)
        self._send_resolve_failure(image, base_tag, rf,
                                   self._failure_text(shown, rf, base_tag, regex),
                                   base_digest, image_config)

    def _escalate_resolve_failure(self, image: str, base_tag: str, rf: Dict[str, Any],
                                  failure_text: str, image_config: Dict[str, Any]) -> None:
        """Send ``resolve_failure`` per the §7.4 schedule; sets ``last_notified``.

        Nothing before 24 h of continuous failure, then at most daily until
        day 7, then at most weekly.
        """
        now = self._now()
        since = _parse_iso(rf.get('since'))
        if since is None:
            # Corrupt record: restart the streak clock rather than crash.
            rf['since'] = now.isoformat()
            return
        age = now - since
        last_notified = _parse_iso(rf.get('last_notified'))
        gap = (now - last_notified) if last_notified else None  # None: infinite

        if age < RESOLVE_FAILURE_FIRST_NOTIFY:
            return
        # Exactly day 7 is still in the daily band, so the week's seventh
        # reminder lands at 7 d and the first weekly one at 14 d (§7.7).
        interval = (RESOLVE_FAILURE_DAILY if age <= RESOLVE_FAILURE_WEEKLY_AFTER
                    else RESOLVE_FAILURE_WEEKLY)
        if gap is not None and gap < interval:
            return
        self._send_resolve_failure(image, base_tag, rf, failure_text,
                                   self._last_base_digest, image_config)

    def _send_resolve_failure(self, image: str, base_tag: str, rf: Dict[str, Any],
                              failure_text: str, base_digest: Optional[str],
                              image_config: Dict[str, Any]) -> None:
        """Send one ``resolve_failure`` (§7.4 content, §7.5 payload); set ``last_notified``.

        Shared by A's immediate send and B's schedule, so either counts
        toward the other's gap.
        """
        now = self._now()
        since = _parse_iso(rf.get('since')) or now
        saved_state = self.state.get(image)
        current_tag = saved_state.tag if saved_state else 'unknown'
        sep = '' if failure_text.endswith('.') else '.'
        message = (
            f"{image} cannot resolve {base_tag}: {failure_text}{sep} "
            f"Failing since {since.strftime('%Y-%m-%d %H:%M')} "
            f"({_format_duration(now - since)}, {rf.get('cycles')} cycles)."
        )
        send_notifications(
            self.config.get('notifications'),
            image=image, old_version=current_tag, new_version=base_tag,
            event='resolve_failure', digest=base_digest or '',
            auto_update=image_config.get('auto_update', False),
            message=message,
        )
        rf['last_notified'] = now.isoformat()

    def _setup_logging(self, level: str) -> logging.Logger:
        """Setup logging configuration."""
        logger = logging.getLogger('ium')
        logger.setLevel(getattr(logging, level.upper()))
        logger.propagate = False

        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - [%(name)s] - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S %Z'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return logger
        
    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP request with retry on transient failures (connection errors, 5xx)."""
        max_retries = 3
        backoff = 2
        kwargs.setdefault('timeout', REGISTRY_REQUEST_TIMEOUT)
        last_exception: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                response = requests.request(method, url, **kwargs)
                if response.status_code >= 500 and attempt < max_retries:
                    self.logger.warning(
                        f"Registry returned {response.status_code} for {url}, "
                        f"retrying in {backoff}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                return response
            except requests.ConnectionError as e:
                last_exception = e
                if attempt < max_retries:
                    self.logger.warning(
                        f"Connection error for {url}: {e}, "
                        f"retrying in {backoff}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    raise
        raise last_exception  # unreachable, satisfies type checker

    def _load_config(self) -> Dict[str, Any]:
        """Load and validate configuration from JSON file."""
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
                
            # Validate against schema
            jsonschema.validate(config, CONFIG_SCHEMA)
            
            # Validate and cache regex patterns
            for image_config in config.get('images', []):
                regex_pattern = image_config['regex']
                self.compiled_patterns[regex_pattern] = _validate_regex(regex_pattern)
                    
            return config
            
        except FileNotFoundError:
            self.logger.error(f"Config file {self.config_file} not found")
            raise
        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing config file: {e}")
            raise
        except jsonschema.ValidationError as e:
            self.logger.error(f"Configuration validation failed: {e}")
            raise
            
    @contextmanager
    def _file_lock(self, file_path: Path):
        """Context manager for file locking."""
        lock_file = file_path.with_suffix('.lock')
        fp = open(lock_file, 'w')
        try:
            if IS_WINDOWS:
                # Windows
                while True:
                    try:
                        msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except IOError:
                        time.sleep(0.1)
            else:
                # Unix-like systems
                fcntl.flock(fp, fcntl.LOCK_EX)
            yield
        finally:
            if IS_WINDOWS:
                try:
                    msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            else:
                fcntl.flock(fp, fcntl.LOCK_UN)
            fp.close()
            try:
                lock_file.unlink()
            except (OSError, FileNotFoundError):
                pass
                
    def _load_state(self) -> Dict[str, ImageState]:
        """Load previous state from file with validation."""
        try:
            if not self.state_file.exists():
                return {}
                
            with self._file_lock(self.state_file):
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    
            # Convert to ImageState objects
            state = {}
            for image, image_data in data.items():
                try:
                    state[image] = ImageState(**image_data)
                except (TypeError, KeyError) as e:
                    self.logger.warning(f"Invalid state data for {image}: {e}")
                    
            return state
            
        except json.JSONDecodeError as e:
            self.logger.warning(f"Error parsing state file, starting fresh: {e}")
            return {}
        except Exception as e:
            self.logger.warning(f"Error loading state: {e}")
            return {}
            
    def _save_state(self):
        """Save current state to file with locking."""
        if self.dry_run:
            self.logger.info("[DRY RUN] Would save state to file")
            return
            
        try:
            # Convert ImageState objects to dicts
            state_dict = {
                image: asdict(state) 
                for image, state in self.state.items()
            }
            
            with self._file_lock(self.state_file):
                # Write to temp file first
                temp_file = self.state_file.with_suffix('.tmp')
                with open(temp_file, 'w') as f:
                    json.dump(state_dict, f, indent=2)
                    
                # Atomic rename
                temp_file.replace(self.state_file)
                
        except Exception as e:
            self.logger.error(f"Error saving state: {e}")
            raise
            
    def _parse_image_reference(self, image: str) -> Tuple[str, str, str]:
        """
        Parse image reference into registry, namespace, and repository.

        Args:
            image: Image reference (e.g., 'ubuntu', 'linuxserver/calibre', 'gcr.io/project/image')

        Returns:
            Tuple of (registry, namespace, repository)
        """
        # Handle explicit protocol prefixes first
        if image.startswith(('http://', 'https://')):
            parts = image.split('/', 1)
            registry = parts[0]
            remaining = parts[1] if len(parts) > 1 else ''
        else:
            # Split once to check first component
            parts = image.split('/', 1)
            first_part = parts[0]

            # Registry indicators: contains '.', is localhost, or has port ':'
            if '.' in first_part or first_part == 'localhost' or ':' in first_part:
                # First part is a custom registry
                registry = first_part
                remaining = parts[1] if len(parts) > 1 else ''
            else:
                # No custom registry detected, use default
                registry = DEFAULT_REGISTRY
                remaining = image

        # Parse namespace and repository from remaining path
        if '/' in remaining:
            namespace, repo = remaining.split('/', 1)
        else:
            namespace = DEFAULT_NAMESPACE
            repo = remaining

        return registry, namespace, repo
        
    def _discover_auth_endpoint(self, registry: str, namespace: str, repo: str
                                ) -> Optional[Tuple[str, str]]:
        """Probe a registry and return (realm, service) from its WWW-Authenticate.

        Returns None if the registry serves anonymously (200 on probe) or if
        discovery fails. Result is cached per registry hostname.
        """
        if registry in self._auth_endpoints:
            return self._auth_endpoints[registry]

        probe_url = f"https://{registry}/v2/{namespace}/{repo}/manifests/latest"
        try:
            response = self._request_with_retry('HEAD', probe_url)
        except requests.RequestException as e:
            self.logger.error(f"Auth discovery failed for {registry}: {e}")
            return None

        if response.status_code == 200:
            self._auth_endpoints[registry] = None
            return None

        if response.status_code != 401:
            self.logger.error(
                f"Unexpected status {response.status_code} probing {probe_url}"
            )
            return None

        challenge = response.headers.get("WWW-Authenticate", "")
        realm_match = re.search(r'realm="([^"]+)"', challenge)
        service_match = re.search(r'service="([^"]+)"', challenge)
        if not realm_match:
            self.logger.error(
                f"Registry {registry} returned 401 without a Bearer realm"
            )
            return None

        endpoint = (realm_match.group(1),
                    service_match.group(1) if service_match else "")
        self._auth_endpoints[registry] = endpoint
        return endpoint

    def _get_docker_token(self, registry: str, namespace: str, repo: str) -> Optional[str]:
        """
        Get authentication token for a Docker Registry v2 endpoint.

        Discovers the auth endpoint via the WWW-Authenticate challenge
        rather than hardcoding per-host URLs.

        Args:
            registry: Registry hostname
            namespace: Image namespace
            repo: Repository name

        Returns:
            Authentication token or None
        """
        endpoint = self._discover_auth_endpoint(registry, namespace, repo)
        if endpoint is None:
            return None

        realm, service = endpoint
        scope = f"repository:{namespace}/{repo}:pull"
        auth_url = (f"{realm}?service={service}&scope={scope}"
                    if service else f"{realm}?scope={scope}")

        try:
            response = self._request_with_retry('GET', auth_url)
            response.raise_for_status()
            return response.json().get('token')
        except requests.RequestException as e:
            self.logger.error(f"Error getting token for {namespace}/{repo}: {e}")
            return None
            
    def _get_manifest_digest(self, registry: str, namespace: str, repo: str, 
                           tag: str, token: Optional[str], platform: Optional[str] = None) -> Optional[str]:
        """
        Get manifest digest for a specific image:tag.
        
        Args:
            registry: Registry hostname
            namespace: Image namespace  
            repo: Repository name
            tag: Tag name
            token: Authentication token
            platform: Platform (e.g., 'linux/amd64')
            
        Returns:
            Manifest digest or None
        """
        manifest_url = f"https://{registry}/v2/{namespace}/{repo}/manifests/{tag}"
        
        headers = {
            'Accept': MANIFEST_ACCEPT_HEADER
        }
        if token:
            headers['Authorization'] = f'Bearer {token}'
            
        try:
            response = self._request_with_retry('GET', manifest_url, headers=headers)
            response.raise_for_status()
            
            content_type = response.headers.get('Content-Type', '')
            
            # Handle manifest lists for multi-arch support
            if 'manifest.list' in content_type or 'image.index' in content_type:
                manifest_list = response.json()
                manifests = manifest_list.get('manifests') or []

                # If platform specified, find matching manifest
                if platform:
                    for manifest in manifests:
                        plat = manifest.get('platform', {})
                        plat_str = f"{plat.get('os', '')}/{plat.get('architecture', '')}"
                        if plat_str == platform:
                            return manifest.get('digest')

                # Return first manifest if no platform specified
                if manifests:
                    return manifests[0].get('digest')
                    
            # Single manifest
            return response.headers.get('Docker-Content-Digest')
            
        except requests.RequestException as e:
            self.logger.error(f"Error getting manifest for {namespace}/{repo}:{tag}: {e}")
            return None

    def _get_manifest_digest_head(self, registry: str, namespace: str, repo: str,
                                   tag: str, token: Optional[str]
                                   ) -> Tuple[Optional[str], DigestStatus]:
        """
        Get manifest digest using HEAD request (faster, no body transfer).

        Returns the Docker-Content-Digest header which is the digest of the
        manifest list for multi-arch images, or the manifest itself for
        single-arch.  This is more correct for comparison than parsing
        manifest list JSON.

        Args:
            registry: Registry hostname
            namespace: Image namespace
            repo: Repository name
            tag: Tag name
            token: Authentication token

        Returns:
            (digest, DigestStatus.OK) on success.
            (None, DigestStatus.NOT_FOUND) when the registry returns 404.
            (None, DigestStatus.ERROR) on any other failure — 5xx, timeout,
            connection error, auth failure, or a 200 response missing the
            Docker-Content-Digest header.  Callers must not treat ERROR as
            "tag does not exist".
        """
        manifest_url = f"https://{registry}/v2/{namespace}/{repo}/manifests/{tag}"

        headers = {
            'Accept': MANIFEST_ACCEPT_HEADER
        }
        if token:
            headers['Authorization'] = f'Bearer {token}'

        try:
            response = self._request_with_retry('HEAD', manifest_url, headers=headers)
            response.raise_for_status()
            digest = response.headers.get('Docker-Content-Digest')
            if not digest:
                self.logger.debug(
                    f"No Docker-Content-Digest header for {namespace}/{repo}:{tag}"
                )
                return None, DigestStatus.ERROR
            return digest, DigestStatus.OK
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self.logger.debug(f"Tag not found: {namespace}/{repo}:{tag}")
                return None, DigestStatus.NOT_FOUND
            self.logger.debug(
                f"HTTP error getting manifest digest for {namespace}/{repo}:{tag}: {e}"
            )
            return None, DigestStatus.ERROR
        except requests.RequestException as e:
            self.logger.debug(
                f"Error getting manifest digest for {namespace}/{repo}:{tag}: {e}"
            )
            return None, DigestStatus.ERROR

    def _get_all_tags(self, registry: str, namespace: str, repo: str, token: Optional[str]) -> List[str]:
        """
        Get all available tags for an image.

        Args:
            registry: Registry hostname
            namespace: Image namespace
            repo: Repository name
            token: Authentication token

        Returns:
            List of available tags
        """
        tags_url = f"https://{registry}/v2/{namespace}/{repo}/tags/list"

        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'

        try:
            tags: List[str] = []
            url: Optional[str] = tags_url
            pages = 0
            while url and pages < MAX_TAG_PAGES:
                response = self._request_with_retry('GET', url, headers=headers)
                response.raise_for_status()
                tags.extend(response.json().get('tags') or [])
                url = _next_link(response.headers.get('Link'), base=f"https://{registry}")
                pages += 1
            if url:
                self.logger.warning(
                    f"Tag list for {namespace}/{repo} truncated at {MAX_TAG_PAGES} pages"
                )
            return tags
        except requests.RequestException as e:
            self.logger.error(f"Error getting tags for {namespace}/{repo}: {e}")
            return []

    def _get_all_tags_by_date(self, registry: str, namespace: str, repo: str) -> List[str]:
        """
        Get all tags ordered by last_updated (oldest first) via Docker Hub API.

        Falls back to _get_all_tags() for non-Docker Hub registries.

        Args:
            registry: Registry hostname
            namespace: Image namespace
            repo: Repository name

        Returns:
            List of tags ordered oldest-first (last element = most recent)
        """
        if registry != DEFAULT_REGISTRY:
            token = self._get_docker_token(registry, namespace, repo)
            return self._get_all_tags(registry, namespace, repo, token)

        tag_dates = []  # list of (name, tag_last_pushed_iso)
        # Docker Hub: ordering=last_updated gives newest first
        url = f"https://hub.docker.com/v2/repositories/{namespace}/{repo}/tags?page_size=100&ordering=last_updated"
        max_tags = 500
        while url and len(tag_dates) < max_tags:
            try:
                response = self._request_with_retry('GET', url)
                response.raise_for_status()
                data = response.json()
                for result in data.get('results') or []:
                    name = result.get('name')
                    if name:
                        tag_dates.append((name, result.get('tag_last_pushed', '')))
                url = data.get('next')
            except requests.RequestException as e:
                self.logger.error(f"Error getting tags from Hub API for {namespace}/{repo}: {e}")
                if not tag_dates:
                    token = self._get_docker_token(registry, namespace, repo)
                    return self._get_all_tags(registry, namespace, repo, token)
                break
        # Sort by push date ascending — last element = most recently pushed
        tag_dates.sort(key=lambda x: x[1])
        return [name for name, _ in tag_dates]
            
    def find_matching_tag(self, image: str, base_tag: str, regex_pattern: str,
                         registry_override: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """
        Find a tag matching the regex pattern that has the same digest as the base tag.

        Uses HEAD requests for faster digest fetching and parallel requests
        for checking multiple tags concurrently.

        Args:
            image: Image name
            base_tag: Base tag to track (e.g., 'latest', 'stable', '14')
            regex_pattern: Regex pattern to match tags
            registry_override: Override registry from config

        Returns:
            Tuple of (matching_tag, digest), or None when the result is
            uncertain (transient registry errors, no digest match) — the
            caller treats None as "skip this cycle and retry next run".
            The newest-matching-tag fallback fires only when the base tag
            genuinely does not exist (HTTP 404).
        """
        # Every `return None` below records why (spec §6); success leaves None.
        self._last_resolve_failure = None
        self._last_base_digest = None

        # Parse image reference
        registry, namespace, repo = self._parse_image_reference(image)
        if registry_override:
            registry = registry_override

        # Get authentication token
        token = self._get_docker_token(registry, namespace, repo)

        # Get digest for base tag using HEAD request
        base_digest, base_status = self._get_manifest_digest_head(
            registry, namespace, repo, base_tag, token
        )
        if base_status is DigestStatus.ERROR:
            # Transient/auth/protocol failure: we cannot know what the base
            # tag points at, so guessing is unsafe (2026-05-24 forgejo
            # incident).  Skip; the next cycle retries.
            self.logger.warning(
                f"Transient registry error resolving {image}:{base_tag}; "
                f"skipping check this cycle"
            )
            self._last_resolve_failure = ResolveFailure.REGISTRY_ERROR
            return None
        if base_status is DigestStatus.OK:
            self._last_base_digest = base_digest
        if base_status is DigestStatus.NOT_FOUND:
            self.logger.warning(f"Tag '{base_tag}' not found in registry for {image}")

        # Get all available tags
        all_tags = self._get_all_tags(registry, namespace, repo, token)
        if not all_tags:
            self.logger.error(f"Could not get tags for {image}")
            self._last_resolve_failure = ResolveFailure.NO_TAGS
            return None

        # Get cached compiled pattern
        pattern = self.compiled_patterns.get(regex_pattern)
        if not pattern:
            self.logger.error(f"Pattern not found in cache: '{regex_pattern}'")
            self._last_resolve_failure = ResolveFailure.INTERNAL
            return None

        # Find tags matching the pattern
        matching_tags = [tag for tag in all_tags if pattern.match(tag)]
        self.logger.debug(f"Found {len(matching_tags)} tags matching pattern")

        if not matching_tags:
            self.logger.warning(f"No tags matching pattern '{regex_pattern}'")
            self._last_resolve_failure = ResolveFailure.NO_REGEX_MATCH
            return None

        # Newest first.  Natural sort: digit runs compare numerically, so
        # 15.0.2 ranks above 9.0.3 (lexicographic sorting got this wrong).
        matching_tags.sort(key=_natural_sort_key, reverse=True)

        if base_status is DigestStatus.OK:
            # Find the version tag sharing the base tag's digest.
            def fetch_digest(tag: str) -> Tuple[str, Optional[str], DigestStatus]:
                digest, status = self._get_manifest_digest_head(
                    registry, namespace, repo, tag, token
                )
                return (tag, digest, status)

            # Use ThreadPoolExecutor for parallel fetching (limit concurrency
            # to be nice to registries)
            max_workers = min(10, len(matching_tags))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(fetch_digest, tag): tag for tag in matching_tags}

                for future in as_completed(futures):
                    tag, digest, status = future.result()
                    if status is DigestStatus.OK and digest == base_digest:
                        # Found a match - cancel remaining futures and return
                        for f in futures:
                            f.cancel()
                        self.logger.debug(f"Found matching tag {tag} with digest {digest[:16]}...")
                        return (tag, base_digest)

            # The base tag resolved but nothing matched: either a mid-release
            # race (registry repointed the base tag before pushing the new
            # version tag) or transient per-tag failures hid the match.
            # Both heal by themselves — never guess here.
            self.logger.warning(
                f"No tag matching pattern '{regex_pattern}' shares a digest "
                f"with {image}:{base_tag} (mid-release race or transient "
                f"registry errors); skipping check this cycle"
            )
            self._last_resolve_failure = ResolveFailure.NO_DIGEST_MATCH
            return None

        # Base tag genuinely does not exist (true 404, e.g. a repo that
        # publishes only version tags): fall back to the newest matching tag.
        latest_tag = matching_tags[0]
        latest_digest, latest_status = self._get_manifest_digest_head(
            registry, namespace, repo, latest_tag, token
        )
        if latest_status is DigestStatus.OK:
            self.logger.info(
                f"Using latest matching tag '{latest_tag}' for {image}"
                f" (base tag '{base_tag}' does not exist in the registry)"
            )
            return (latest_tag, latest_digest)

        self.logger.error(f"Could not get digest for latest matching tag {image}:{latest_tag}")
        self._last_resolve_failure = ResolveFailure.NO_FALLBACK_DIGEST
        return None
        
    def _qualified_image(self, image: str, registry: Optional[str]) -> str:
        """Prefix *image* with *registry* when the Docker daemon needs it.

        Returns *image* unchanged when no registry is given, the registry is
        Docker Hub, or the image's first path component already names a host
        (contains '.' or ':', or is 'localhost').
        """
        if not registry or registry == DEFAULT_REGISTRY:
            return image
        first = image.split('/')[0]
        if '.' in first or ':' in first or first == 'localhost':
            return image
        return f"{registry}/{image}"

    def _pull_image(self, image: str, tag: str,
                    registry: Optional[str] = None) -> bool:
        """
        Pull a Docker image.

        Args:
            image: Image name
            tag: Tag to pull
            registry: Optional registry override (e.g. 'ghcr.io').  When set
                      and not already embedded in *image*, it is prepended so
                      the Docker daemon pulls from the correct registry rather
                      than defaulting to Docker Hub.

        Returns:
            True if successful, False otherwise
        """
        # Qualify image name with registry when the daemon needs it
        pull_image = self._qualified_image(image, registry)

        full_image = f"{pull_image}:{tag}"

        if self.dry_run:
            self.logger.info(f"[DRY RUN] Would pull {full_image}")
            return True

        self.logger.info(f"Pulling {full_image}...")

        try:
            self.docker.pull_image(pull_image, tag)
            self.logger.info(f"Successfully pulled {full_image}")
            return True
        except DockerAPIError as e:
            self.logger.error(f"Error pulling {full_image}: {e.message}")
            return False
            
    def _get_container_config(self, container_name: str) -> Optional[Dict[str, Any]]:
        """Get full container configuration."""
        try:
            return self.docker.inspect_container(container_name)
        except DockerAPIError as e:
            self.logger.error(
                f"Error inspecting container '{container_name}': {e.message}. "
                f"Check that the container_name in config matches a running container."
            )
            return None

    def _container_exists(self, container_name: str) -> Optional[bool]:
        """Whether a container exists, as far as the daemon will say.

        Returns True/False when the daemon answers, and None when it could not
        be reached — an unreachable daemon must not be mistaken for a 404, since
        callers use this to decide whether a half-applied operation landed.
        """
        try:
            self.docker.inspect_container(container_name)
            return True
        except DockerAPIError as e:
            if e.status == 404:
                return False
            self.logger.warning(
                f"Could not determine whether '{container_name}' exists: {e.message}"
            )
            return None

    def _get_containers_for_image(self, image: str) -> List[Dict[str, str]]:
        """Get all containers (running or stopped) using a specific image.

        Returns:
            List of dicts with keys: name, id, state, image_ref. An empty list
            means discovery succeeded and no container uses the image.

        Raises:
            DockerAPIError: the container list could not be fetched; callers
                must not treat this as "no containers".
        """
        try:
            api_containers = self.docker.list_containers(all=True)

            containers = []
            all_images = []
            for container in api_containers:
                container_image = container.get('Image', '')
                all_images.append(container_image)

                if self._image_matches(image, container_image):
                    # API returns Names as list with "/" prefix, e.g. ["/sonarr"]
                    names = container.get('Names', [])
                    name = names[0].lstrip('/') if names else ''
                    containers.append({
                        'name': name,
                        'id': container.get('Id', ''),
                        'state': container.get('State', ''),
                        'image_ref': container_image
                    })

            # Orphaned containers: the list reports a bare image ID, but
            # inspect still shows the original Config.Image string.
            for container in api_containers:
                container_image = container.get('Image', '')
                if not ID_LIKE.match(container_image):
                    continue  # already handled by name match
                names = container.get('Names') or ['/']
                name = names[0].lstrip('/')
                # Direct call (not _get_container_config, which swallows
                # errors): DockerAPIError must propagate.
                info = self.docker.inspect_container(name)
                cfg_image = ((info or {}).get('Config') or {}).get('Image', '')
                if cfg_image and self._image_matches(image, cfg_image):
                    self.logger.warning(
                        f"Container {name} runs {image} but its image tag no longer "
                        f"resolves locally (list reports {container_image[:19]}); "
                        f"matched via inspect"
                    )
                    containers.append({
                        'name': name,
                        'id': container.get('Id', ''),
                        'state': container.get('State', ''),
                        'image_ref': cfg_image,
                        'orphaned': True,
                    })

            if not containers and all_images:
                normalized = self._normalize_image_ref(image)
                self.logger.debug(
                    f"No containers matched '{image}' (normalized: '{normalized}'). "
                    f"All container images: {all_images}"
                )

            return containers

        except DockerAPIError as e:
            self.logger.error(f"Failed to list containers: {e}")
            raise

    @staticmethod
    def _normalize_image_ref(img: str) -> str:
        """Normalize a Docker image reference for comparison.

        Strips tags, digest qualifiers, and registry prefixes to yield just
        the repository path (e.g. ``portainer/portainer-ce``).  Single-name
        images get an implicit ``library/`` prefix so that ``nginx`` and
        ``library/nginx`` compare equal.
        """
        # Strip digest qualifier (@sha256:...)
        at_pos = img.find('@')
        if at_pos != -1:
            img = img[:at_pos]

        # Strip tag — but only when the colon is in the *tag* position
        # (after the last slash), not in a registry:port position.
        last_slash = img.rfind('/')
        last_colon = img.rfind(':')
        if last_colon > last_slash:
            img = img[:last_colon]

        # Strip registry prefix.  The first path component is a registry if
        # it contains a dot, a colon (port), or is literally "localhost".
        if '/' in img:
            path_parts = img.split('/')
            first = path_parts[0]
            if '.' in first or ':' in first or first == 'localhost':
                img = '/'.join(path_parts[1:])

        # Implicit library namespace: postgres → library/postgres
        if '/' not in img:
            return f"library/{img}"

        return img

    def _image_matches(self, config_image: str, container_image: str) -> bool:
        """Check if a container image matches the configured image.

        Handles:
        - Tag variations: nginx matches nginx:alpine
        - Registry prefixes: linuxserver/sonarr matches lscr.io/linuxserver/sonarr:latest
        - Implicit library namespace: postgres matches library/postgres
        - Digest qualifiers: image:tag@sha256:... matches image
        - Registry ports: localhost:5000/img matches img
        """
        normalized_config = self._normalize_image_ref(config_image)
        normalized_container = self._normalize_image_ref(container_image)

        # Also check without library/ prefix so that "library/nginx" matches "nginx"
        def strip_library(s: str) -> str:
            return s[len('library/'):] if s.startswith('library/') else s

        return (normalized_config == normalized_container or
                strip_library(normalized_config) == strip_library(normalized_container))

    def _get_container_current_tag(self, container_name: str, image: str, regex: str,
                                   registry: Optional[str] = None) -> Optional[str]:
        """Return the version tag the container runs, by matching its image ID against the
        local image inventory.

        Returns None only when the lookup succeeded but the image carries no tag matching
        the regex. Raises DockerAPIError if the inspect or image listing fails, so callers
        can tell "unknown" apart from "daemon unreachable".
        """
        container_info = self.docker.inspect_container(container_name)

        # Get the image ID (sha256) from the container
        image_id = container_info.get('Image', '')
        if not image_id:
            self.logger.debug(f"No image ID found for container {container_name}")
            return None

        # Get cached compiled pattern
        pattern = self.compiled_patterns.get(regex)
        if not pattern:
            self.logger.debug(f"Pattern not found in cache: '{regex}'")
            return None

        # Local tags carry the registry host for non-Docker-Hub images
        images = self.docker.list_images(self._qualified_image(image, registry))

        # image_id from inspect is full sha256:..., API returns Id as sha256:...
        normalized_id = image_id if image_id.startswith('sha256:') else f"sha256:{image_id}"

        # Find tags that match both the image ID and regex pattern
        for img in images or []:
            if img.get('Id', '') != normalized_id:
                continue
            for repo_tag in img.get('RepoTags') or []:
                # RepoTags are "image:tag" format
                if ':' in repo_tag:
                    tag = repo_tag.rsplit(':', 1)[1]
                else:
                    tag = repo_tag
                if pattern.match(tag):
                    self.logger.debug(f"Found matching tag for {container_name}: {tag}")
                    return tag

        self.logger.debug(f"No matching tag found in image inventory for {container_name}")
        return None

    def _update_container(self, container_name: str, image: str, tag: str,
                          registry: Optional[str] = None,
                          stop_timeout: int = DEFAULT_STOP_TIMEOUT,
                          request_timeout: int = DEFAULT_REQUEST_TIMEOUT) -> bool:
        """
        Update a running container with a new image.

        Args:
            container_name: Name of the container to update
            image: Image name
            tag: Tag to use
            registry: Optional registry override (e.g. 'ghcr.io')
            stop_timeout: SIGTERM grace period passed to Docker when stopping
            request_timeout: Socket read budget for the Docker lifecycle calls

        Returns:
            True if successful, False otherwise
        """
        # Apply registry prefix the same way _pull_image does, so the image
        # reference matches what Docker stored when it pulled the image.
        pull_image = self._qualified_image(image, registry)
        full_image = f"{pull_image}:{tag}"

        if self.dry_run:
            self.logger.info(f"[DRY RUN] Would update container {container_name} with image {full_image}")
            return True

        # Get current container configuration
        container_info = self._get_container_config(container_name)
        if not container_info:
            return False

        # Skip if already on the target image (e.g. retry after partial failure).
        # Compare image IDs, not reference strings: after a same-tag re-push
        # Config.Image still equals full_image while the container sits on the
        # old image, and a string match would skip the update forever.
        try:
            target = self.docker.list_images(full_image)  # reference filter: exact repo:tag
        except DockerAPIError as e:
            self.logger.error(
                f"Could not look up local image {full_image} for "
                f"{container_name}: {e.message}"
            )
            return False
        target_id = target[0].get('Id', '') if target else ''
        current_id = container_info.get('Image', '')
        if target_id and current_id == target_id:
            status = (container_info.get('State') or {}).get('Status', '')
            if status == 'running':
                self.logger.info(
                    f"Container {container_name} already running {full_image} "
                    f"({target_id[:19]}), skipping"
                )
                return True

            # On the target image but not running: a previous update was
            # interrupted after create_container (e.g. its response timed out).
            # Reporting success here would leave the container down forever,
            # since every later cycle takes this same short-circuit.  Starting
            # is idempotent — Docker answers 304 if it is already up.
            self.logger.warning(
                f"Container {container_name} is on {full_image} but status is "
                f"'{status or 'unknown'}' — starting it (interrupted update)"
            )
            try:
                self.docker.start_container(container_name, timeout=request_timeout)
            except DockerAPIError as e:
                self.logger.error(
                    f"Could not start {container_name}: {e.message}"
                )
                return False
            return True

        try:
            # Build container create config preserving all settings
            create_config, extra_networks = self._build_create_config(
                container_name, full_image, container_info
            )

            # Stop the container
            self.logger.info(f"Stopping container {container_name}...")
            try:
                self.docker.stop_container(container_name, timeout=stop_timeout,
                                          request_timeout=request_timeout)
            except DockerAPIError as stop_err:
                # A failed stop request does not prove the stop did not happen.
                # The daemon can finish stopping a heavy container after our
                # socket budget expires, which is exactly what stranded a
                # container on 2026-07-29.  Inspect before giving up.
                post_stop = self._get_container_config(container_name)
                status = ((post_stop or {}).get('State') or {}).get('Status', '')
                if status in STOPPED_STATUSES:
                    self.logger.warning(
                        f"Stop request for {container_name} failed "
                        f"({stop_err.message}) but the container is '{status}' "
                        f"— continuing with the update"
                    )
                else:
                    detail = f"still '{status}'" if status else "state unknown"
                    self.logger.error(
                        f"Could not stop {container_name} ({stop_err.message}); "
                        f"{detail} — aborting update to avoid renaming a live "
                        f"container"
                    )
                    return False

            # Rename old container as backup
            backup_name = f"{container_name}_backup_{int(time.time())}"
            self.logger.info(f"Renaming old container to {backup_name}")
            try:
                self.docker.rename_container(container_name, backup_name,
                                            timeout=request_timeout)
            except DockerAPIError as rename_err:
                # The most dangerous call in the update to lose the answer to:
                # the container is already stopped, and its identity is now
                # ambiguous.  Creating a replacement blindly could collide with
                # a container still holding the name, while aborting blindly
                # could abandon it under the backup name forever.  Ask both.
                renamed = self._container_exists(backup_name)
                original = self._container_exists(container_name)

                if renamed is True and original is False:
                    self.logger.warning(
                        f"Rename request failed ({rename_err.message}) but "
                        f"{container_name} is now {backup_name} — continuing "
                        f"with the update"
                    )
                elif original is True:
                    self.logger.error(
                        f"Could not rename {container_name} "
                        f"({rename_err.message}); restarting it and aborting "
                        f"the update"
                    )
                    try:
                        self.docker.start_container(container_name,
                                                   timeout=request_timeout)
                    except DockerAPIError as restart_err:
                        self.logger.error(
                            f"Could not restart {container_name} after the "
                            f"failed rename: {restart_err.message} — start it "
                            f"manually"
                        )
                    return False
                else:
                    self.logger.error(
                        f"Could not rename {container_name} "
                        f"({rename_err.message}) and cannot determine whether "
                        f"the rename was applied — aborting without creating a "
                        f"replacement; check for {backup_name} manually"
                    )
                    return False

            # Create and start new container
            self.logger.info(f"Creating new container {container_name}...")
            # Track how far the replacement got: the backup may only be
            # discarded once the replacement has actually started.
            new_id: Optional[str] = None
            started = False
            try:
                new_id = self.docker.create_container(
                    container_name, create_config, timeout=request_timeout)
                self.docker.start_container(new_id, timeout=request_timeout)
                started = True

                # Connect to additional networks
                for network in extra_networks:
                    self.docker.connect_network(network, new_id,
                                                timeout=request_timeout)

            except DockerAPIError as e:
                # Rollback on failure
                stage = ('connect networks for' if started
                         else ('start' if new_id else 'create'))
                self.logger.error(f"Failed to {stage} new container: {e.message}")
                self.logger.info("Rolling back...")
                if new_id and not started:
                    # The replacement exists under the service name but never
                    # ran.  Remove it so the backup can take the name back.
                    try:
                        self.docker.remove_container(new_id, force=True, timeout=120)
                    except (DockerAPIError, OSError, TimeoutError) as rm_err:
                        self.logger.error(
                            f"Could not remove the failed replacement {new_id[:12]} "
                            f"({rm_err}); {backup_name} still holds the previous "
                            f"container — restore it manually"
                        )
                        return False
                try:
                    self.docker.rename_container(backup_name, container_name,
                                                timeout=request_timeout)
                    self.docker.start_container(container_name,
                                               timeout=request_timeout)
                except DockerAPIError as rb_err:
                    if started:
                        # The replacement started and holds the name (an
                        # extra-network connect failed after start), so the
                        # rename back conflicts.  The replacement is running;
                        # remove the stranded backup instead of leaving it.
                        self.logger.warning(
                            f"Rollback rename failed ({rb_err.message}); "
                            f"removing stranded backup {backup_name}"
                        )
                        try:
                            self.docker.remove_container(backup_name, force=True, timeout=120)
                        except (DockerAPIError, OSError, TimeoutError) as rm_err:
                            self.logger.warning(
                                f"Could not remove backup container {backup_name}: {rm_err} "
                                f"— remove it manually"
                            )
                    else:
                        # The replacement never ran: the backup is the only
                        # working copy and must never be removed here.
                        self.logger.error(
                            f"Rollback failed ({rb_err.message}); {backup_name} still holds "
                            f"the previous container — restore it manually"
                        )
                return False

            # Success - remove old container (best-effort; new container is already running)
            self.logger.info(f"Removing old container {backup_name}")
            try:
                self.docker.remove_container(backup_name, force=True, timeout=120)
            except (TimeoutError, OSError, DockerAPIError) as e:
                self.logger.warning(f"Could not remove backup container {backup_name}: {e} — remove it manually")

            self.logger.info(f"Successfully updated container {container_name}")
            return True

        except DockerAPIError as e:
            self.logger.error(f"Error updating container: {e}")
            return False

    def _update_containers(self, container_names: List[str], image: str, tag: str,
                           registry: Optional[str] = None,
                           stop_timeout: int = DEFAULT_STOP_TIMEOUT,
                           request_timeout: int = DEFAULT_REQUEST_TIMEOUT) -> Dict[str, bool]:
        """Update multiple containers to a new image tag.

        Args:
            container_names: List of container names to update
            image: Base image name
            tag: Target tag to update to
            registry: Optional registry override (e.g. 'ghcr.io')
            stop_timeout: SIGTERM grace period passed to Docker when stopping
            request_timeout: Socket read budget for the Docker lifecycle calls

        Returns:
            Dict mapping container_name -> success boolean
        """
        results = {}
        for container_name in container_names:
            self.logger.info(f"Updating container {container_name} to {image}:{tag}")
            success = self._update_container(
                container_name, image, tag, registry,
                stop_timeout=stop_timeout, request_timeout=request_timeout,
            )
            results[container_name] = success

        # Log summary
        success_count = sum(1 for v in results.values() if v)
        total_count = len(results)
        if success_count == total_count:
            self.logger.info(f"Container update summary: {success_count}/{total_count} succeeded (all)")
        else:
            self.logger.warning(f"Container update summary: {success_count}/{total_count} succeeded")

        return results

    def _build_create_config(self, container_name: str, image: str,
                             container_info: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        """Build Docker API container create config preserving container settings.

        Returns:
            Tuple of (create_config dict, list of additional network names to connect after creation)
        """
        config = container_info['Config']
        host_config = container_info['HostConfig']

        # Determine network mode constraints
        network_mode = host_config.get('NetworkMode', 'default')
        is_host_network = network_mode == 'host'
        is_container_network = network_mode.startswith('container:')
        shares_network_namespace = is_host_network or is_container_network

        # Build the create config
        create_config: Dict[str, Any] = {
            'Image': image,
        }

        # Hostname (not allowed with host or container: network modes)
        if not shares_network_namespace:
            if config.get('Hostname') and config['Hostname'] != container_info['Id'][:12]:
                create_config['Hostname'] = config['Hostname']

        # User
        if config.get('User'):
            create_config['User'] = config['User']

        # Working directory
        if config.get('WorkingDir'):
            create_config['WorkingDir'] = config['WorkingDir']

        # Environment variables (filtered)
        env = []
        for env_var in config.get('Env') or []:
            if not any(env_var.startswith(prefix) for prefix in ('PATH=', 'HOSTNAME=')):
                env.append(env_var)
        if env:
            create_config['Env'] = env

        # Labels (preserve compose labels for stack membership)
        labels = {}
        for key, value in (config.get('Labels') or {}).items():
            if key.startswith('com.docker.compose.'):
                labels[key] = value
            elif not key.startswith('com.docker.'):
                labels[key] = value
        if labels:
            create_config['Labels'] = labels

        # Command
        if config.get('Cmd'):
            create_config['Cmd'] = config['Cmd']

        # ExposedPorts — pass through from original config
        if not shares_network_namespace and config.get('ExposedPorts'):
            create_config['ExposedPorts'] = config['ExposedPorts']

        # Health check
        if config.get('Healthcheck'):
            healthcheck = config['Healthcheck']
            # Only preserve if it has an actual test command (not NONE)
            if healthcheck.get('Test') and healthcheck['Test'] != ['NONE']:
                create_config['Healthcheck'] = healthcheck

        # ── HostConfig ────────────────────────────────────────────
        hc: Dict[str, Any] = {}

        # Restart policy
        restart_policy = host_config.get('RestartPolicy', {})
        if restart_policy.get('Name'):
            hc['RestartPolicy'] = restart_policy

        # Port bindings (not applicable with host or container: network modes)
        if not shares_network_namespace:
            if host_config.get('PortBindings'):
                hc['PortBindings'] = host_config['PortBindings']

        # Volume bindings (from Mounts → Binds format)
        binds = []
        for mount in container_info.get('Mounts') or []:
            if mount['Type'] == 'bind':
                source = mount['Source']
            elif mount['Type'] == 'volume':
                source = mount['Name']
            else:
                continue

            bind_str = f"{source}:{mount['Destination']}"
            if mount.get('Mode'):
                bind_str += f":{mount['Mode']}"
            binds.append(bind_str)
        if binds:
            hc['Binds'] = binds

        # Network mode
        if network_mode and network_mode != 'default':
            hc['NetworkMode'] = network_mode

        # Privileged
        if host_config.get('Privileged'):
            hc['Privileged'] = True

        # Capabilities
        if host_config.get('CapAdd'):
            hc['CapAdd'] = host_config['CapAdd']
        if host_config.get('CapDrop'):
            hc['CapDrop'] = host_config['CapDrop']

        # Devices
        if host_config.get('Devices'):
            hc['Devices'] = host_config['Devices']

        # Memory limits
        if host_config.get('Memory'):
            hc['Memory'] = host_config['Memory']

        # CPU limits
        if host_config.get('CpuShares'):
            hc['CpuShares'] = host_config['CpuShares']
        if host_config.get('CpuQuota'):
            hc['CpuQuota'] = host_config['CpuQuota']

        # Security options
        if host_config.get('SecurityOpt'):
            hc['SecurityOpt'] = host_config['SecurityOpt']

        # Runtime
        if host_config.get('Runtime'):
            hc['Runtime'] = host_config['Runtime']

        # Logging configuration (preserve non-default drivers)
        log_config = host_config.get('LogConfig', {})
        if log_config.get('Type') and log_config['Type'] != 'json-file':
            hc['LogConfig'] = log_config

        if hc:
            create_config['HostConfig'] = hc

        # ── Additional networks ───────────────────────────────────
        extra_networks: List[str] = []
        if not is_container_network:
            # Docker stores the default bridge network as 'bridge' in NetworkSettings
            # but HostConfig.NetworkMode reports it as 'default'.  Treat the two as
            # equivalent so we don't try to connect the new container to bridge a
            # second time (it is already connected automatically when NetworkMode is
            # 'default'), which would produce an "endpoint already exists" error.
            primary_networks = {network_mode}
            if network_mode == 'default':
                primary_networks.add('bridge')
            elif network_mode == 'bridge':
                primary_networks.add('default')
            for network in ((container_info.get('NetworkSettings') or {}).get('Networks') or {}).keys():
                if network not in primary_networks:
                    extra_networks.append(network)

        return create_config, extra_networks
        
    def _cleanup_old_images(self, image: str, keep_versions: int = 3) -> None:
        """Remove old images, keeping the specified number of most recent versions.

        Counts distinct image IDs, not tag rows — so a current image carrying
        multiple local tags (e.g. :version + :latest after a base_tag pull)
        consumes only ONE keep slot, not one per tag.
        """
        try:
            api_images = self.docker.list_images(image)

            if not api_images:
                return

            # Group tag refs per distinct image ID (skip <none> tags).
            distinct = []
            for img in api_images:
                tags = [t for t in (img.get('RepoTags') or [])
                        if t and not t.endswith(':<none>') and t != '<none>:<none>']
                if not tags:
                    continue
                distinct.append({
                    'id': img.get('Id', ''),
                    'tags': tags,
                    'created': img.get('Created', 0),
                })

            # Sort distinct images by creation time descending (newest first)
            distinct.sort(key=lambda x: x['created'], reverse=True)

            # Keep the first N distinct images, mark the rest for removal
            images_to_remove = distinct[keep_versions:]

            if not images_to_remove:
                self.logger.debug(f"No old images to clean up for {image} (keeping {keep_versions})")
                return

            if self.dry_run:
                for img in images_to_remove:
                    short_id = img['id'][7:19] if img['id'].startswith('sha256:') else img['id'][:12]
                    self.logger.info(
                        f"[DRY RUN] Would remove old image {short_id} "
                        f"(tags: {', '.join(img['tags'])})"
                    )
                return

            # Drop every tag of each old image so the image is actually freed,
            # not just untagged on one ref.
            for img in images_to_remove:
                for tag_ref in img['tags']:
                    try:
                        if self.docker.remove_image(tag_ref):
                            self.logger.info(f"Removed old image {tag_ref}")
                        else:
                            self.logger.debug(f"Could not remove {tag_ref} (may be in use)")
                    except (DockerAPIError, OSError) as e:
                        self.logger.warning(f"Could not remove {tag_ref}: {e}")

        except DockerAPIError as e:
            self.logger.warning(f"Error during image cleanup: {e}")
            
    def check_and_update(self, progress_callback=None) -> List[Dict[str, Any]]:
        """Check for updates and apply them if configured.

        Args:
            progress_callback: Optional function(event_type, data) called for progress updates
        """
        if self.dry_run:
            self.logger.info("=== DRY RUN MODE ===")

        updates_found = []
        total_images = len(self.config.get('images', []))

        for idx, image_config in enumerate(self.config.get('images', []), 1):
            image = image_config['image']
            regex = image_config['regex']
            base_tag = image_config.get('base_tag', DEFAULT_BASE_TAG)
            auto_update = image_config.get('auto_update', False)
            registry = image_config.get('registry')
            cleanup = image_config.get('cleanup_old_images', False)
            keep_versions = image_config.get('keep_versions', 3)
            stop_timeout = image_config.get('stop_timeout', DEFAULT_STOP_TIMEOUT)
            request_timeout = image_config.get('request_timeout', DEFAULT_REQUEST_TIMEOUT)

            self.logger.info(f"Checking {image}:{base_tag}...")

            # Emit progress: starting check for this image
            if progress_callback:
                progress_callback('checking_image', {
                    'image': image,
                    'base_tag': base_tag,
                    'progress': idx,
                    'total': total_images
                })
            
            # Find matching tag for current base tag
            result = self.find_matching_tag(image, base_tag, regex, registry)
            saved_state = self.state.get(image)

            if not result:
                # Defensive: every failing branch records a reason, so None
                # here would be a bug; still say something rather than crash.
                failure = self._last_resolve_failure or ResolveFailure.INTERNAL
                base_digest = self._last_base_digest

                # Streak bookkeeping (spec §7.2).  Deep copy (``diag`` is
                # nested) so the saved record is only replaced via
                # _write_resolve_failure, never mutated in place.
                rf = copy.deepcopy((saved_state.resolve_failure if saved_state else None) or {})
                if not rf:
                    rf = {'reason': failure.name, 'since': self._now().isoformat(),
                          'cycles': 0, 'last_notified': None}
                rf['reason'] = failure.name  # may change mid-streak; `since` does not
                rf['cycles'] = rf.get('cycles', 0) + 1

                # Diagnosis (spec §7.3): at most one sweep, gated, and only
                # for NO_DIGEST_MATCH; may send A's notification.
                self._maybe_diagnose(image, base_tag, regex, registry, failure,
                                     rf, base_digest, image_config)
                shown = self._effective_failure(failure, rf, base_digest)
                shown_text = self._failure_text(shown, rf, base_tag, regex)

                message = f"Could not determine version for {image}:{base_tag}: {shown_text}"
                self.logger.warning(message)
                if progress_callback:
                    payload = {
                        'image': image,
                        'base_tag': base_tag,
                        'reason': shown.name.lower(),
                        'error': message,
                    }
                    if rf['cycles'] >= 2:
                        payload['failing_since'] = rf['since']
                        payload['failing_cycles'] = rf['cycles']
                    progress_callback('check_error', payload)

                self._escalate_resolve_failure(image, base_tag, rf, shown_text, image_config)
                # Persist every failing cycle (the counter changes).
                self._write_resolve_failure(image, saved_state, base_tag, rf)
                continue

            matching_tag, digest = result
            self.logger.info(f"Base tag '{base_tag}' corresponds to: {matching_tag}")
            self.logger.debug(f"Digest: {digest}")

            # A streak ends on any successful resolve, before any of the
            # `continue`s below and regardless of auto_update or digest.
            if saved_state and saved_state.resolve_failure:
                self._clear_resolve_failure(image, saved_state, matching_tag,
                                            digest, image_config)
                saved_state = self.state[image]

            # Check if this is different from our saved state
            if not saved_state or saved_state.digest != digest:
                # Discover all containers using this image.  Done only when
                # the registry has moved, so an unchanged image makes no
                # Docker call.  A list or current-tag lookup failure defers
                # the image to the next cycle before any pull, notification,
                # or state write.
                try:
                    containers = self._get_containers_for_image(image)

                    # Current version comes from the first container's actual
                    # image; saved state is used only when no container runs
                    # this image.  None from the lookup means the container's
                    # image carries no regex-matching tag (e.g. orphaned).
                    old_tag = None
                    if containers:
                        old_tag = self._get_container_current_tag(
                            containers[0]['name'], image, regex, registry)
                        if not old_tag:
                            old_tag = 'unknown'
                    elif saved_state:
                        old_tag = saved_state.tag
                    if not old_tag:
                        old_tag = 'unknown'
                except DockerAPIError as e:
                    self.logger.warning(
                        f"Could not list or inspect containers for {image} ({e}); "
                        f"skipping this cycle so the update is retried"
                    )
                    if progress_callback:
                        progress_callback('check_error', {
                            'image': image, 'base_tag': base_tag,
                            'reason': 'docker_unreachable',
                            'error': f"Docker daemon unreachable while listing or inspecting containers for {image}: {e}",
                        })
                    continue

                # Only report update if tags are actually different
                if old_tag != matching_tag:
                    # Downgrade guard: a candidate older than the current
                    # version is reported but never auto-applied.  Last line
                    # of defense against bad candidates (2026-05-24 forgejo
                    # incident); a genuine upstream rollback can still be
                    # applied manually.
                    # When the container yields no regex-matching tag
                    # ('unknown', e.g. created from :latest or orphaned), guard
                    # against the saved tag instead; old_tag stays 'unknown'
                    # for routing and reporting.
                    guard_tag = old_tag if old_tag != 'unknown' else (
                        saved_state.tag if saved_state else None)
                    is_downgrade = (
                        guard_tag not in (None, 'unknown')
                        and _natural_sort_key(matching_tag) < _natural_sort_key(guard_tag)
                    )
                    if is_downgrade:
                        # A refused downgrade never advances state: it is
                        # held as a `pending` record and re-reported every
                        # cycle until applied, dismissed, or the registry
                        # moves on.  Falling through to the state-advance
                        # block below is what handed a refused downgrade to
                        # the rebuilt path on the next same-tag re-push.
                        now = self._now()
                        now_iso = now.isoformat()
                        existing = self._downgrade_pending(saved_state, digest)
                        repeat = existing is not None
                        if existing is not None:
                            pending = dict(existing)
                        else:
                            pending = {
                                'tag': matching_tag, 'digest': digest,
                                'first_seen': now_iso, 'reason': 'downgrade',
                                'dismissed': False, 'last_notified': None,
                            }

                        if pending.get('dismissed'):
                            self.logger.debug(
                                f"Downgrade {image} {old_tag} -> {matching_tag} "
                                f"dismissed; waiting for registry change"
                            )
                            self._write_pending(image, saved_state, base_tag, pending, old_tag)
                            continue

                        if repeat:
                            self.logger.info(
                                f"DOWNGRADE PENDING: {image} {old_tag} -> {matching_tag} "
                                f"(first seen {pending.get('first_seen')})"
                            )
                        else:
                            self.logger.warning(
                                f"DOWNGRADE DETECTED: {image} candidate {matching_tag} "
                                f"is older than current {old_tag}; reporting but not "
                                f"auto-applying"
                            )

                        update_info = {
                            'image': image,
                            'base_tag': base_tag,
                            'old_tag': old_tag,
                            'new_tag': matching_tag,
                            'digest': digest,
                            'auto_update': False,
                            'downgrade': True,
                            'repeat': repeat,
                        }
                        updates_found.append(update_info)
                        if progress_callback:
                            progress_callback('update_found', update_info)

                        if self._downgrade_notify_due(pending.get('last_notified'), now):
                            send_notifications(
                                self.config.get('notifications'),
                                image=image, old_version=old_tag, new_version=matching_tag,
                                event='update_found', digest=digest,
                                auto_update=False
                            )
                            pending['last_notified'] = now_iso

                        self._write_pending(image, saved_state, base_tag, pending, old_tag)
                        continue  # never reaches the pull / state-advance code below

                    effective_auto_update = auto_update and not is_downgrade

                    self.logger.info(f"UPDATE AVAILABLE: {old_tag} -> {matching_tag}")

                    update_info = {
                        'image': image,
                        'base_tag': base_tag,
                        'old_tag': old_tag,
                        'new_tag': matching_tag,
                        'digest': digest,
                        'auto_update': effective_auto_update,
                        'downgrade': is_downgrade
                    }
                    updates_found.append(update_info)

                    # Emit progress: update found
                    if progress_callback:
                        progress_callback('update_found', update_info)

                    send_notifications(
                        self.config.get('notifications'),
                        image=image, old_version=old_tag, new_version=matching_tag,
                        event='update_found', digest=digest,
                        auto_update=effective_auto_update
                    )

                    update_ok = True
                    if effective_auto_update:
                        # Pull the new images
                        if self._pull_image(image, base_tag, registry):
                            self._pull_image(image, matching_tag, registry)

                            if containers:
                                # Update all discovered containers
                                container_names = [c['name'] for c in containers]
                                self.logger.info(f"Found {len(containers)} container(s) using {image}: {', '.join(container_names)}")
                                update_results = self._update_containers(
                                    container_names, image, matching_tag, registry,
                                    stop_timeout=stop_timeout,
                                    request_timeout=request_timeout,
                                )

                                # Only mark success if ALL containers updated;
                                # partial failure leaves state unchanged so the
                                # update is retried next cycle (already-updated
                                # containers are skipped automatically)
                                update_ok = all(update_results.values()) if update_results else True
                            else:
                                # No containers - just image update
                                self.logger.info(f"No containers found for {image}, image updated only")
                                update_ok = True

                            # Only cleanup old images after a successful update,
                            # otherwise we may remove tags still in use
                            if update_ok and cleanup:
                                self._cleanup_old_images(image, keep_versions)
                        else:
                            update_ok = False

                    # Update state: always for non-auto (to prevent
                    # re-reporting), but only on success for auto_update
                    # so the update is retried next cycle
                    if not auto_update or update_ok:
                        self.state[image] = ImageState(
                            base_tag=base_tag,
                            tag=matching_tag,
                            digest=digest,
                            last_updated=datetime.now().isoformat()
                        )
                else:
                    # Digest changed but tag is the same — image was
                    # rebuilt under the same tag.  Treat as an update.
                    self.logger.info(f"IMAGE REBUILT: {matching_tag} (new digest)")

                    update_info = {
                        'image': image,
                        'base_tag': base_tag,
                        'old_tag': matching_tag,
                        'new_tag': matching_tag,
                        'digest': digest,
                        'auto_update': auto_update
                    }
                    updates_found.append(update_info)

                    # Emit progress: image rebuilt
                    if progress_callback:
                        progress_callback('image_rebuilt', {
                            'image': image,
                            'tag': matching_tag
                        })

                    send_notifications(
                        self.config.get('notifications'),
                        image=image, old_version=matching_tag, new_version=matching_tag,
                        event='image_rebuilt', digest=digest, auto_update=auto_update
                    )

                    update_ok = True
                    if auto_update:
                        # Pull the fresh image
                        if self._pull_image(image, base_tag, registry):
                            self._pull_image(image, matching_tag, registry)

                            if containers:
                                container_names = [c['name'] for c in containers]
                                self.logger.info(f"Found {len(containers)} container(s) using {image}: {', '.join(container_names)}")
                                update_results = self._update_containers(
                                    container_names, image, matching_tag, registry,
                                    stop_timeout=stop_timeout,
                                    request_timeout=request_timeout,
                                )
                                update_ok = any(update_results.values()) if update_results else True
                            else:
                                self.logger.info(f"No containers found for {image}, image updated only")
                                update_ok = True

                            if update_ok and cleanup:
                                self._cleanup_old_images(image, keep_versions)
                        else:
                            update_ok = False

                    # Update state
                    if not auto_update or update_ok:
                        self.state[image] = ImageState(
                            base_tag=base_tag,
                            tag=matching_tag,
                            digest=digest,
                            last_updated=datetime.now().isoformat()
                        )
            else:
                self.logger.info("No update available")
                # Upstream reverted the base tag to the saved digest: the
                # downgrade code never runs on this path, so a refused
                # downgrade record would otherwise persist forever.
                if saved_state.pending and saved_state.pending.get('reason') == 'downgrade':
                    self.logger.info(
                        f"Clearing pending downgrade for {image} "
                        f"({saved_state.pending.get('tag')}): registry is back on {matching_tag}"
                    )
                    self.state[image] = replace(saved_state, pending=None)
                # Emit progress: no update
                if progress_callback:
                    progress_callback('no_update', {
                        'image': image,
                        'base_tag': base_tag
                    })
                    
        # Save state
        self._save_state()
        
        # Summary
        if updates_found:
            self.logger.info("=== Update Summary ===")
            for update in updates_found:
                self.logger.info(
                    f"{update['image']}: {update['old_tag']} -> {update['new_tag']}"
                )
        else:
            self.logger.info("No updates found")
            
        return updates_found


def main():
    parser = argparse.ArgumentParser(
        description='Image auto-updater with tag tracking'
    )
    parser.add_argument(
        'config',
        nargs='?',
        default=os.environ.get('CONFIG_FILE', 'config.json'),
        help='Path to configuration JSON file (env: CONFIG_FILE, default: config.json)'
    )
    parser.add_argument(
        '--state',
        default=os.environ.get('STATE_FILE', 'image_update_state.json'),
        help='Path to state file (env: STATE_FILE, default: image_update_state.json)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=os.environ.get('DRY_RUN', '').lower() == 'true',
        help='Show what would be done without making any changes (env: DRY_RUN)'
    )
    parser.add_argument(
        '--daemon',
        action='store_true',
        default=os.environ.get('DAEMON', '').lower() == 'true',
        help='Run continuously, checking at intervals (env: DAEMON)'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=int(os.environ.get('CHECK_INTERVAL', '3600')),
        help='Check interval in seconds when running as daemon (env: CHECK_INTERVAL, default: 3600)'
    )
    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default=os.environ.get('LOG_LEVEL', 'INFO'),
        help='Logging level (env: LOG_LEVEL, default: INFO)'
    )

    args = parser.parse_args()
    
    try:
        updater = DockerImageUpdater(
            args.config,
            args.state,
            args.dry_run,
            args.log_level
        )
        
        if args.daemon:
            updater.logger.info(f"Running in daemon mode, checking every {args.interval} seconds")
            while True:
                try:
                    updater.check_and_update()
                    updater.logger.info(f"Sleeping for {args.interval} seconds...")
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    updater.logger.info("Exiting...")
                    break
                except Exception as e:
                    updater.logger.error(f"Error during update check: {e}")
                    time.sleep(args.interval)
        else:
            updater.check_and_update()
            
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()