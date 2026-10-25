"""
Notification senders for ium: ntfy.sh and generic outgoing webhook.

Failures are always logged as warnings and never re-raised so that a broken
notification channel cannot interrupt the update cycle.
"""

import json
import logging
import string
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10  # seconds

_NTFY_PRIORITIES = {'min', 'low', 'default', 'high', 'urgent'}


def _build_payload(image: str, old_version: str, new_version: str,
                   event: str, digest: str, auto_update: bool,
                   message: str = '') -> Dict[str, Any]:
    """Return the standard dict passed to every sender."""
    return {
        'event': event,
        'image': image,
        'old_version': old_version,
        'new_version': new_version,
        'digest': digest,
        'auto_update': auto_update,
        'message': message,
    }


def send_ntfy(cfg: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    """POST a notification to an ntfy topic URL.

    Config keys:
        url      (required) Full ntfy topic URL, e.g. https://ntfy.sh/my-topic
        priority (optional) min / low / default / high / urgent  (default: default)
        headers  (optional) Extra HTTP headers dict (e.g. {"Authorization": "Bearer token"})
    """
    url = (cfg.get('url') or '').strip()
    if not url:
        logger.warning("ntfy: no URL configured, skipping")
        return False

    image = payload['image']
    event = payload['event']
    old_v = payload['old_version']
    new_v = payload['new_version']

    tags = 'package'
    if event == 'resolve_failure':
        title = f"ium: {image} cannot resolve {new_v}"
        message = payload.get('message', '')
        tags = 'warning'
    elif event == 'resolve_recovered':
        title = f"ium: {image} resolving again"
        message = payload.get('message', '')
        tags = 'white_check_mark'
    elif event == 'image_rebuilt':
        title = f"ium: {image} rebuilt"
        message = f"{new_v} was rebuilt under the same tag (new digest)."
    else:
        title = f"ium: {image} update available"
        message = f"{old_v} \u2192 {new_v}"
        if payload.get('auto_update'):
            message += " (auto-update applied)"

    priority = cfg.get('priority', 'default')
    if priority not in _NTFY_PRIORITIES:
        priority = 'default'

    headers: Dict[str, str] = {
        'Title': title,
        'Priority': priority,
        'Tags': tags,
        'Content-Type': 'text/plain',
    }
    for k, v in (cfg.get('headers') or {}).items():
        headers[str(k)] = str(v)

    try:
        response = requests.post(url, data=message.encode('utf-8'),
                                 headers=headers, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info("ntfy: notification sent for %s", image)
        return True
    except requests.RequestException as e:
        logger.warning("ntfy: failed to send notification: %s", e)
        return False


def send_webhook(cfg: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    """POST (or PUT) a notification payload to a webhook URL.

    Config keys:
        url           (required) Webhook URL
        method        (optional) HTTP method — POST (default) or PUT
        headers       (optional) Dict of extra request headers
        body_template (optional) Python string.Template body.
                                 Available variables: $image, $old_version,
                                 $new_version, $event, $digest, $auto_update,
                                 $message.
                                 If omitted, the raw payload JSON is sent.
    """
    url = (cfg.get('url') or '').strip()
    if not url:
        logger.warning("webhook: no URL configured, skipping")
        return False

    method = (cfg.get('method') or 'POST').upper()
    extra_headers: Dict[str, str] = {str(k): str(v) for k, v in (cfg.get('headers') or {}).items()}
    body_template: Optional[str] = cfg.get('body_template')

    headers: Dict[str, str] = {'Content-Type': 'application/json'}
    headers.update(extra_headers)

    if body_template:
        try:
            body_str = string.Template(body_template).safe_substitute(
                image=payload['image'],
                old_version=payload['old_version'],
                new_version=payload['new_version'],
                event=payload['event'],
                digest=payload.get('digest', ''),
                auto_update=str(payload.get('auto_update', False)).lower(),
                message=payload.get('message', ''),
            )
        except (KeyError, ValueError) as e:
            logger.warning("webhook: body_template substitution failed: %s — sending raw payload", e)
            body_str = json.dumps(payload)
        data = body_str.encode('utf-8')
    else:
        data = json.dumps(payload).encode('utf-8')

    try:
        response = requests.request(method, url, data=data,
                                    headers=headers, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        logger.info("webhook: notification sent for %s", payload['image'])
        return True
    except requests.RequestException as e:
        logger.warning("webhook: failed to send notification: %s", e)
        return False


def send_notifications(notif_cfg: Optional[Dict[str, Any]],
                       image: str, old_version: str, new_version: str,
                       event: str, digest: str = '', auto_update: bool = False,
                       message: str = '') -> None:
    """Dispatch notifications to all configured channels.

    Safe to call unconditionally — exits immediately when notif_cfg is None
    or empty.  All sender errors are caught and logged, never re-raised.
    """
    if not notif_cfg:
        return

    payload = _build_payload(image, old_version, new_version, event, digest, auto_update,
                             message)

    ntfy_cfg = notif_cfg.get('ntfy')
    if ntfy_cfg and ntfy_cfg.get('url'):
        try:
            send_ntfy(ntfy_cfg, payload)
        except Exception as e:
            logger.warning("ntfy: unexpected error: %s", e)

    webhook_cfg = notif_cfg.get('webhook')
    if webhook_cfg and webhook_cfg.get('url'):
        try:
            send_webhook(webhook_cfg, payload)
        except Exception as e:
            logger.warning("webhook: unexpected error: %s", e)
