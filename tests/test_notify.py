"""Tests for notify.py: ntfy/webhook senders and the new resolve_* events."""

import json
from unittest.mock import MagicMock, patch

import notify

NTFY = {'url': 'https://ntfy.example/topic'}


def _ntfy(event, **kw):
    args = dict(image='nginx', old_version='1.0', new_version='1.1',
                digest='sha256:abc', auto_update=False, message='')
    args.update(kw)
    with patch('notify.requests.post') as post:
        notify.send_notifications({'ntfy': NTFY}, event=event, **args)
    assert post.call_count == 1
    return post.call_args.kwargs


def _webhook(cfg, event='resolve_failure', **kw):
    args = dict(image='nginx', old_version='1.0', new_version='1.1',
                digest='sha256:abc', auto_update=True, message='boom')
    args.update(kw)
    with patch('notify.requests.request') as req:
        req.return_value = MagicMock()
        notify.send_notifications({'webhook': dict(url='https://h.example', **cfg)},
                                  event=event, **args)
    assert req.call_count == 1
    return req.call_args.kwargs


def test_ntfy_resolve_failure():
    kw = _ntfy('resolve_failure', new_version='latest', message='registry 401')
    assert kw['headers']['Title'] == 'ium: nginx cannot resolve latest'
    assert kw['headers']['Tags'] == 'warning'
    assert kw['data'] == b'registry 401'


def test_ntfy_resolve_recovered():
    kw = _ntfy('resolve_recovered', message='resolved again')
    assert kw['headers']['Title'] == 'ium: nginx resolving again'
    assert kw['headers']['Tags'] == 'white_check_mark'
    assert kw['data'] == b'resolved again'


def test_ntfy_update_found_unchanged():
    kw = _ntfy('update_found')
    assert kw['headers']['Title'] == 'ium: nginx update available'
    assert kw['headers']['Tags'] == 'package'
    assert kw['data'] == '1.0 → 1.1'.encode('utf-8')


def test_ntfy_update_found_auto_update_unchanged():
    kw = _ntfy('update_found', auto_update=True)
    assert kw['data'] == '1.0 → 1.1 (auto-update applied)'.encode('utf-8')


def test_ntfy_image_rebuilt_unchanged():
    kw = _ntfy('image_rebuilt')
    assert kw['headers']['Title'] == 'ium: nginx rebuilt'
    assert kw['headers']['Tags'] == 'package'
    assert kw['data'] == b'1.1 was rebuilt under the same tag (new digest).'


def test_webhook_raw_payload_contains_message():
    kw = _webhook({})
    assert json.loads(kw['data'])['message'] == 'boom'


def test_webhook_body_template_substitutes_message():
    kw = _webhook({'body_template': '$image: $message'})
    assert kw['data'] == b'nginx: boom'


def test_payload_message_defaults_empty():
    p = notify._build_payload('i', 'a', 'b', 'update_found', '', False)
    assert p['message'] == ''
