#!/usr/bin/env python3
"""
Web UI for Image Update Manager
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import threading
import traceback
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Dict, List, Any, Optional

import jsonschema
from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO, emit

from docker_api import DockerAPIError
from ium import DockerImageUpdater, CONFIG_SCHEMA, __version__, _validate_regex, AuthManager, ImageState
from notify import send_ntfy, send_webhook, _build_payload
from pattern_utils import detect_tag_patterns, detect_base_tags

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(32).hex()
socketio = SocketIO(app)

# Global variables
updater: Optional[DockerImageUpdater] = None
daemon_thread: Optional[threading.Thread] = None
daemon_stop_event = threading.Event()
last_check_time: Optional[datetime] = None
last_updates: List[Dict[str, Any]] = []
update_history: List[Dict[str, Any]] = []
is_checking = False
daemon_running = False
daemon_interval = 3600

# History file path (in config directory for persistence)
HISTORY_FILE = Path(os.environ.get('CONFIG_FILE', '/config/config.json')).parent / 'history.json'
MAX_HISTORY_ENTRIES = 500  # Limit history size

# Daemon state file (in state directory for persistence across restarts)
DAEMON_STATE_FILE = Path(os.environ.get('STATE_FILE', '/state/image_update_state.json')).parent / 'daemon_state.json'

# Apply TZ from environment (default UTC) before any logging is configured
os.environ.setdefault('TZ', 'UTC')
if hasattr(time, 'tzset'):
    time.tzset()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S %Z'
)
logger = logging.getLogger(__name__)

# Auth setup: auto-generates secure credentials on first run if env vars are not set
_state_dir = Path(os.environ.get('STATE_FILE', '/state/image_update_state.json')).parent
_auth_manager = AuthManager(_state_dir)
AUTH_USER = _auth_manager.user
AUTH_PASSWORD = _auth_manager.password
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)


def _check_credentials(username: str, password: str) -> bool:
    """Verify credentials using constant-time comparison."""
    return (hmac.compare_digest(username, AUTH_USER) and
            hmac.compare_digest(password, AUTH_PASSWORD))


@app.before_request
def require_auth():
    """Enforce basic auth on all requests when enabled."""
    if not AUTH_ENABLED:
        return None

    if request.path == '/health':
        return None

    auth = request.authorization
    if auth and _check_credentials(auth.username, auth.password):
        return None

    return Response(
        'Authentication required', 401,
        {'WWW-Authenticate': 'Basic realm="ium"'}
    )


@app.before_request
def require_csrf():
    """Reject state-changing requests missing the X-Requested-With header.

    Browsers block cross-origin custom headers by default, so requiring
    this header on POST/PUT/DELETE prevents cross-site request forgery
    without tokens or extra dependencies.  GET and SocketIO transport
    requests are exempt.
    """
    if request.method in ('POST', 'PUT', 'DELETE'):
        if request.headers.get('X-Requested-With') != 'XMLHttpRequest':
            return jsonify({'error': 'CSRF check failed'}), 403


def load_history():
    """Load update history from file."""
    global update_history
    try:
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, 'r') as f:
                loaded = json.load(f)
                # Ensure we have a list, not None or other type
                update_history = loaded if isinstance(loaded, list) else []
                logger.info(f"Loaded {len(update_history)} history entries from {HISTORY_FILE}")
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Could not load history file: {e}")
        update_history = []


def save_history():
    """Save update history to file."""
    try:
        # Trim to max entries before saving
        trimmed = update_history[-MAX_HISTORY_ENTRIES:]
        with open(HISTORY_FILE, 'w') as f:
            json.dump(trimmed, f, indent=2)
    except IOError as e:
        logger.error(f"Could not save history file: {e}")


def save_daemon_state():
    """Persist daemon enabled/interval to disk."""
    try:
        with open(DAEMON_STATE_FILE, 'w') as f:
            json.dump({'enabled': daemon_running, 'interval': daemon_interval}, f)
    except IOError as e:
        logger.error(f"Could not save daemon state: {e}")


def restore_daemon_state():
    """Start daemon if it was running before container restart."""
    global daemon_running, daemon_thread, daemon_interval

    try:
        if not DAEMON_STATE_FILE.exists():
            return  # OOBE: no state file means daemon stays off

        with open(DAEMON_STATE_FILE, 'r') as f:
            state = json.load(f)

        if not state.get('enabled', False):
            return

        if not updater:
            logger.warning("Cannot restore daemon: updater not loaded")
            return

        daemon_interval = state.get('interval', 3600)
        daemon_running = True
        daemon_stop_event.clear()
        daemon_thread = threading.Thread(target=daemon_worker, args=(daemon_interval,))
        daemon_thread.start()
        logger.info(f"Restored daemon (interval={daemon_interval}s) from previous state")
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Could not restore daemon state: {e}")


def load_updater():
    """Load or reload the updater instance."""
    global updater
    config_file = os.environ.get('CONFIG_FILE', '/config/config.json')
    state_file = os.environ.get('STATE_FILE', '/state/image_update_state.json')
    dry_run = os.environ.get('DRY_RUN', 'false').lower() == 'true'
    log_level = os.environ.get('LOG_LEVEL', 'INFO')
    
    try:
        updater = DockerImageUpdater(config_file, state_file, dry_run, log_level)
        return True
    except Exception as e:
        logger.error(f"Failed to load updater: {e}")
        return False


def require_updater(f):
    """Decorator to check if updater is loaded before executing route."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not updater:
            return jsonify({'error': 'Updater not loaded'}), 503
        return f(*args, **kwargs)
    return decorated


def run_check():
    """Run a single check cycle."""
    global is_checking, last_check_time, last_updates

    if is_checking:
        return

    is_checking = True
    socketio.emit('status_update', {'checking': True}, namespace='/')

    def progress_callback(event_type, data):
        """Emit progress updates to connected clients."""
        socketio.emit('check_progress', {
            'event': event_type,
            'data': data
        }, namespace='/')

    try:
        if updater:
            updates = updater.check_and_update(progress_callback=progress_callback)
            last_updates = updates
            last_check_time = datetime.now()

            # Add to history
            if updates:
                for update in updates:
                    # A pending downgrade is re-reported every cycle; record
                    # only its first detection so it cannot crowd out history.
                    if update.get('repeat'):
                        continue
                    update_history.append({
                        'timestamp': last_check_time.isoformat(),
                        'image': update['image'],
                        'old_tag': update['old_tag'],
                        'new_tag': update['new_tag'],
                        'applied': not updater.dry_run and update.get('auto_update', False)
                    })
                save_history()  # Persist to disk

            socketio.emit('check_complete', {
                'updates': updates,
                'timestamp': last_check_time.isoformat()
            }, namespace='/')
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Check failed: {e}\n{tb}")
        socketio.emit('check_error', {'error': str(e), 'traceback': tb}, namespace='/')
    finally:
        is_checking = False
        socketio.emit('status_update', {'checking': False}, namespace='/')


def daemon_worker(interval):
    """Background worker for daemon mode."""
    global daemon_running

    logger.info(f"Daemon started (interval={interval}s)")
    while daemon_running:
        try:
            run_check()
        except Exception as e:
            logger.error(f"Daemon check cycle failed unexpectedly: {e}\n{traceback.format_exc()}")
        # Wait with efficient interruption support
        if daemon_stop_event.wait(timeout=interval):
            break
    logger.info("Daemon stopped")


@app.route('/')
def index():
    """Main web interface."""
    return render_template('index.html')


@app.route('/health')
def health():
    """Unauthenticated liveness probe for container health checks."""
    return '', 200


@app.route('/api/status')
def api_status():
    """Get current status."""
    return jsonify({
        'updater_loaded': updater is not None,
        'dry_run': updater.dry_run if updater else True,
        'is_checking': is_checking,
        'daemon_running': daemon_running,
        'daemon_interval': daemon_interval,
        'last_check': last_check_time.isoformat() if last_check_time else None,
        'config_file': updater.config_file.as_posix() if updater else None,
        'state_file': updater.state_file.as_posix() if updater else None
    })


@app.route('/api/version')
def api_version():
    """Get application version."""
    return jsonify({'version': __version__})


@app.route('/api/config')
@require_updater
def api_config():
    """Get current configuration."""
    return jsonify(updater.config)


@app.route('/api/config', methods=['POST'])
@require_updater
def api_update_config():
    """Update configuration."""
    try:
        new_config = request.json
        if not new_config:
            return jsonify({'error': 'No configuration provided'}), 400

        # Validate regex patterns before saving (ReDoS protection)
        for img in new_config.get('images', []):
            if 'regex' in img:
                try:
                    _validate_regex(img['regex'])
                except ValueError as e:
                    return jsonify({'error': str(e)}), 400

        # Validate against schema before saving
        jsonschema.validate(new_config, CONFIG_SCHEMA)

        with open(updater.config_file, 'w') as f:
            json.dump(new_config, f, indent=2)

        # Reload updater
        if load_updater():
            return jsonify({'status': 'success'})
        else:
            return jsonify({'error': 'Failed to reload updater'}), 500
    except jsonschema.ValidationError as e:
        return jsonify({'error': f'Invalid configuration: {e.message}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/detect-patterns', methods=['POST'])
@require_updater
def api_detect_patterns():
    """Fetch tags from registry and detect regex patterns."""
    try:
        data = request.json or {}
        image = data.get('image', '').strip()
        if not image:
            return jsonify({'error': 'Image name is required'}), 400

        registry_override = data.get('registry', '').strip() or None

        registry, namespace, repo = updater._parse_image_reference(image)
        if registry_override:
            registry = registry_override

        tags = updater._get_all_tags_by_date(registry, namespace, repo)

        if not tags:
            return jsonify({'error': f'No tags found for {image}. Check the image name and registry.'}), 404

        patterns = detect_tag_patterns(tags)
        base_tags = detect_base_tags(tags, patterns)
        return jsonify({'patterns': patterns, 'base_tags': base_tags, 'total_tags': len(tags)})

    except Exception as e:
        logger.error(f"Pattern detection failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/notifications/test', methods=['POST'])
@require_updater
def api_notifications_test():
    """Fire a test notification for a specific notifier type."""
    data = request.json or {}
    notif_type = data.get('type')
    if notif_type not in ('ntfy', 'webhook'):
        return jsonify({'error': 'type must be "ntfy" or "webhook"'}), 400

    notif_cfg = updater.config.get('notifications') or {}
    channel_cfg = notif_cfg.get(notif_type)
    if not channel_cfg or not channel_cfg.get('url'):
        return jsonify({'error': f'{notif_type} is not configured — save a URL first'}), 400

    test_payload = _build_payload(
        image='example/image',
        old_version='v1.0.0',
        new_version='v1.1.0',
        event='update_found',
        digest='sha256:abc123',
        auto_update=False
    )

    ok = (send_ntfy(channel_cfg, test_payload) if notif_type == 'ntfy'
          else send_webhook(channel_cfg, test_payload))

    if ok:
        return jsonify({'status': 'sent'})
    return jsonify({'error': f'Failed to send {notif_type} test — check logs for details'}), 500


@app.route('/api/state')
@require_updater
def api_state():
    """Get current state."""
    state_dict = {image: asdict(state) for image, state in updater.state.items()}
    return jsonify(state_dict)


@app.route('/api/check', methods=['POST'])
def api_check():
    """Trigger a manual check."""
    if is_checking:
        return jsonify({'error': 'Check already in progress'}), 409
        
    if not updater:
        if not load_updater():
            return jsonify({'error': 'Failed to load updater'}), 503
            
    # Run check in background
    threading.Thread(target=run_check).start()
    
    return jsonify({'status': 'started'})


@app.route('/api/updates')
def api_updates():
    """Get last update results."""
    return jsonify({
        'last_check': last_check_time.isoformat() if last_check_time else None,
        'updates': last_updates
    })


@app.route('/api/history')
def api_history():
    """Get update history."""
    limit = request.args.get('limit', 50, type=int)
    return jsonify(update_history[-limit:])


@app.route('/api/apply-update', methods=['POST'])
@require_updater
def api_apply_update():
    """Manually apply a specific pending update."""
    data = request.json or {}
    image = data.get('image', '').strip()
    new_tag = data.get('new_tag', '').strip()

    if not image or not new_tag:
        return jsonify({'error': 'image and new_tag are required'}), 400

    # Find the pending update in last_updates
    update_info = next(
        (u for u in last_updates if u['image'] == image and u['new_tag'] == new_tag),
        None
    )
    if not update_info:
        return jsonify({'error': f'No pending update found for {image}:{new_tag}'}), 404

    # Get image config for registry and cleanup settings
    image_config = next(
        (c for c in updater.config.get('images', []) if c['image'] == image),
        {}
    )
    base_tag = update_info.get('base_tag', 'latest')
    registry = image_config.get('registry') or update_info.get('registry')

    try:
        # Pull base tag and new tag
        if not updater._pull_image(image, base_tag, registry):
            return jsonify({'error': f'Failed to pull {image}:{base_tag}'}), 500
        if not updater._pull_image(image, new_tag, registry):
            return jsonify({'error': f'Failed to pull {image}:{new_tag}'}), 500

        # Discover and update containers
        try:
            containers = updater._get_containers_for_image(image)
        except DockerAPIError as e:
            logger.warning(f"apply-update: could not list containers for {image}: {e}")
            return jsonify({
                'error': f"Docker daemon unreachable while listing containers for {image}: {e}"
            }), 503
        if containers:
            container_names = [c['name'] for c in containers]
            results = updater._update_containers(container_names, image, new_tag, registry)
            if not any(results.values()):
                return jsonify({'error': 'All container updates failed'}), 500

        # Update state
        updater.state[image] = ImageState(
            base_tag=base_tag,
            tag=new_tag,
            digest=update_info.get('digest', ''),
            last_updated=datetime.now().isoformat()
        )
        if not updater.dry_run:
            updater._save_state()

        # Record in history
        update_history.append({
            'timestamp': datetime.now().isoformat(),
            'image': image,
            'old_tag': update_info.get('old_tag', 'unknown'),
            'new_tag': new_tag,
            'applied': True
        })
        save_history()

        logger.info(f"Manually applied update: {image} \u2192 {new_tag}")
        return jsonify({'success': True, 'image': image, 'new_tag': new_tag})

    except Exception as e:
        logger.error(f"apply-update failed for {image}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/updates/dismiss', methods=['POST'])
@require_updater
def api_dismiss_update():
    """Ignore a pending downgrade until the registry offers something else."""
    global last_updates

    # A running cycle rewrites state at its end; refuse rather than race it.
    if is_checking:
        return jsonify({'error': 'Check in progress; try again shortly'}), 409

    data = request.json or {}
    image = (data.get('image') or '').strip()

    image_state = updater.state.get(image)
    pending = getattr(image_state, 'pending', None)
    if not pending or pending.get('reason') != 'downgrade':
        return jsonify({'error': f'No pending downgrade found for {image}'}), 404

    pending['dismissed'] = True
    updater._save_state()

    last_updates = [u for u in last_updates if u.get('image') != image]
    socketio.emit('check_complete', {
        'updates': last_updates,
        'timestamp': last_check_time.isoformat() if last_check_time else None
    }, namespace='/')

    logger.info(f"Dismissed pending downgrade for {image} -> {pending.get('tag')}")
    return jsonify({'success': True})


@app.route('/api/daemon', methods=['POST'])
def api_daemon():
    """Start/stop daemon mode."""
    global daemon_running, daemon_thread, daemon_interval

    data = request.json or {}
    action = data.get('action')

    if action == 'start':
        if daemon_running:
            return jsonify({'error': 'Daemon already running'}), 409

        if not updater:
            if not load_updater():
                return jsonify({'error': 'Failed to load updater'}), 503

        # Get interval from request or use default
        daemon_interval = data.get('interval', 3600)
        if not isinstance(daemon_interval, (int, float)) or daemon_interval < 60:
            return jsonify({'error': 'Interval must be at least 60 seconds'}), 400
        daemon_interval = int(daemon_interval)

        daemon_running = True
        daemon_stop_event.clear()
        daemon_thread = threading.Thread(target=daemon_worker, args=(daemon_interval,))
        daemon_thread.start()
        save_daemon_state()

        return jsonify({'status': 'started', 'interval': daemon_interval})

    elif action == 'stop':
        if not daemon_running:
            return jsonify({'error': 'Daemon not running'}), 409

        daemon_running = False
        daemon_stop_event.set()
        if daemon_thread:
            daemon_thread.join(timeout=5)
        save_daemon_state()

        return jsonify({'status': 'stopped'})
        
    else:
        return jsonify({'error': 'Invalid action'}), 400


@socketio.on('connect')
def handle_connect():
    """Handle client connection, rejecting unauthenticated Socket.IO when auth is enabled."""
    if AUTH_ENABLED:
        auth = request.authorization
        if not auth or not _check_credentials(auth.username, auth.password):
            return False  # Reject connection

    emit('connected', {'status': 'Connected to Docker Updater'})
    
    # Send current status
    emit('status_update', {
        'checking': is_checking,
        'daemon_running': daemon_running,
        'last_check': last_check_time.isoformat() if last_check_time else None
    })


# Load updater and history on startup (runs when gunicorn imports this module)
load_updater()
load_history()
restore_daemon_state()

if __name__ == '__main__':
    # For local development only
    socketio.run(app, host='0.0.0.0', port=5050)