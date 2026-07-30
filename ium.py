#!/usr/bin/env python3
"""
Docker Image Auto-Update with Specific Tag Tracking

This script monitors Docker images for updates by comparing a base tag
(e.g., 'latest', 'stable', or a version like '14') with version-specific
tags that match user-defined regex patterns.
"""

__version__ = "1.3.0"

import json
import re
import secrets
import sys
import time
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from dataclasses import dataclass, asdict
from contextlib import contextmanager
from enum import Enum
import argparse
import os
import platform
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
REQUEST_TIMEOUT = 30
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
                    "keep_versions": {"type": "integer", "minimum": 1}
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


@dataclass
class ImageState:
    """State information for a tracked image."""
    base_tag: str
    tag: str
    digest: str
    last_updated: str


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
        self.config = self._load_config()
        self.state = self._load_state()
        
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
        kwargs.setdefault('timeout', REQUEST_TIMEOUT)
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
            response = self._request_with_retry('GET', tags_url, headers=headers)
            response.raise_for_status()
            return response.json().get('tags') or []
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
            return None
        if base_status is DigestStatus.NOT_FOUND:
            self.logger.warning(f"Tag '{base_tag}' not found in registry for {image}")

        # Get all available tags
        all_tags = self._get_all_tags(registry, namespace, repo, token)
        if not all_tags:
            self.logger.error(f"Could not get tags for {image}")
            return None

        # Get cached compiled pattern
        pattern = self.compiled_patterns.get(regex_pattern)
        if not pattern:
            self.logger.error(f"Pattern not found in cache: '{regex_pattern}'")
            return None

        # Find tags matching the pattern
        matching_tags = [tag for tag in all_tags if pattern.match(tag)]
        self.logger.debug(f"Found {len(matching_tags)} tags matching pattern")

        if not matching_tags:
            self.logger.warning(f"No tags matching pattern '{regex_pattern}'")
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
        return None
        
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
        pull_image = image
        if registry and registry != DEFAULT_REGISTRY:
            first = image.split('/')[0]
            if '.' not in first and first != 'localhost' and ':' not in first:
                pull_image = f"{registry}/{image}"

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

    def _get_containers_for_image(self, image: str) -> List[Dict[str, str]]:
        """Get all containers (running or stopped) using a specific image.

        Returns:
            List of dicts with keys: name, id, state, image_ref
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

            if not containers and all_images:
                normalized = self._normalize_image_ref(image)
                self.logger.debug(
                    f"No containers matched '{image}' (normalized: '{normalized}'). "
                    f"All container images: {all_images}"
                )

            return containers

        except DockerAPIError as e:
            self.logger.error(f"Failed to list containers: {e}")
            return []

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

    def _get_container_current_tag(self, container_name: str, image: str, regex: str) -> Optional[str]:
        """Get the current version tag of a running container by checking image inventory."""
        try:
            container_info = self._get_container_config(container_name)
            if not container_info:
                self.logger.debug(f"Container {container_name} not found or no config")
                return None

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

            # Query Docker API for images matching this name
            try:
                images = self.docker.list_images(image)
            except DockerAPIError:
                self.logger.debug(f"Failed to get image tags for {image}")
                return None

            # Normalize the container's image ID for comparison
            # image_id from inspect is full sha256:..., API returns Id as sha256:...
            normalized_id = image_id if image_id.startswith('sha256:') else f"sha256:{image_id}"

            # Find tags that match both the image ID and regex pattern
            for img in images:
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
        except Exception as e:
            self.logger.debug(f"Could not get current tag for {container_name}: {e}")
            return None

    def _update_container(self, container_name: str, image: str, tag: str,
                          registry: Optional[str] = None) -> bool:
        """
        Update a running container with a new image.

        Args:
            container_name: Name of the container to update
            image: Image name
            tag: Tag to use
            registry: Optional registry override (e.g. 'ghcr.io')

        Returns:
            True if successful, False otherwise
        """
        # Apply registry prefix the same way _pull_image does, so the image
        # reference matches what Docker stored when it pulled the image.
        pull_image = image
        if registry and registry != DEFAULT_REGISTRY:
            first = image.split('/')[0]
            if '.' not in first and first != 'localhost' and ':' not in first:
                pull_image = f"{registry}/{image}"
        full_image = f"{pull_image}:{tag}"

        if self.dry_run:
            self.logger.info(f"[DRY RUN] Would update container {container_name} with image {full_image}")
            return True

        # Get current container configuration
        container_info = self._get_container_config(container_name)
        if not container_info:
            return False

        # Skip if already running the target image (e.g. retry after partial failure)
        current_image = container_info.get('Config', {}).get('Image', '')
        if current_image == full_image:
            self.logger.info(f"Container {container_name} already running {full_image}, skipping")
            return True

        try:
            # Build container create config preserving all settings
            create_config, extra_networks = self._build_create_config(
                container_name, full_image, container_info
            )

            # Stop the container
            self.logger.info(f"Stopping container {container_name}...")
            self.docker.stop_container(container_name)

            # Rename old container as backup
            backup_name = f"{container_name}_backup_{int(time.time())}"
            self.logger.info(f"Renaming old container to {backup_name}")
            self.docker.rename_container(container_name, backup_name)

            # Create and start new container
            self.logger.info(f"Creating new container {container_name}...")
            try:
                container_id = self.docker.create_container(container_name, create_config)
                self.docker.start_container(container_id)

                # Connect to additional networks
                for network in extra_networks:
                    self.docker.connect_network(network, container_id)

            except DockerAPIError as e:
                # Rollback on failure
                self.logger.error(f"Failed to create new container: {e.message}")
                self.logger.info("Rolling back...")
                try:
                    self.docker.rename_container(backup_name, container_name)
                    self.docker.start_container(container_name)
                except DockerAPIError as rb_err:
                    # Rename failed — the new container likely took the name already
                    # (e.g. it was created and started before an extra-network connect
                    # failed).  Remove the stranded backup instead of leaving it.
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
                           registry: Optional[str] = None) -> Dict[str, bool]:
        """Update multiple containers to a new image tag.

        Args:
            container_names: List of container names to update
            image: Base image name
            tag: Target tag to update to
            registry: Optional registry override (e.g. 'ghcr.io')

        Returns:
            Dict mapping container_name -> success boolean
        """
        results = {}
        for container_name in container_names:
            self.logger.info(f"Updating container {container_name} to {image}:{tag}")
            success = self._update_container(container_name, image, tag, registry)
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

            if not result:
                self.logger.warning(f"Could not determine version for {image}:{base_tag}")
                if progress_callback:
                    progress_callback('check_error', {
                        'image': image,
                        'base_tag': base_tag,
                        'error': f"Could not determine version for {image}:{base_tag} - no matching tags found"
                    })
                continue

            matching_tag, digest = result
            self.logger.info(f"Base tag '{base_tag}' corresponds to: {matching_tag}")
            self.logger.debug(f"Digest: {digest}")

            # Check if this is different from our saved state
            saved_state = self.state.get(image)

            # Discover all containers using this image
            containers = self._get_containers_for_image(image)

            if not saved_state or saved_state.digest != digest:
                # Determine current version (from saved state or first container)
                old_tag = saved_state.tag if saved_state else None
                if not old_tag and containers:
                    old_tag = self._get_container_current_tag(containers[0]['name'], image, regex)
                if not old_tag:
                    old_tag = 'unknown'

                # Only report update if tags are actually different
                if old_tag != matching_tag:
                    # Downgrade guard: a candidate older than the current
                    # version is reported but never auto-applied.  Last line
                    # of defense against bad candidates (2026-05-24 forgejo
                    # incident); a genuine upstream rollback can still be
                    # applied manually.
                    is_downgrade = (
                        old_tag not in (None, 'unknown')
                        and _natural_sort_key(matching_tag) < _natural_sort_key(old_tag)
                    )
                    if is_downgrade:
                        self.logger.warning(
                            f"DOWNGRADE DETECTED: {image} candidate {matching_tag} "
                            f"is older than current {old_tag}; reporting but not "
                            f"auto-applying"
                        )
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
                                update_results = self._update_containers(container_names, image, matching_tag, registry)

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
                                update_results = self._update_containers(container_names, image, matching_tag, registry)
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