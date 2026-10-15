"""Validated, bounded hosted playlist format shared by scanner and player."""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 4 * 1024 * 1024


def web_url(value, *, https=False):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid URL')
    p = urlsplit(value)
    if p.scheme not in ({'https'} if https else {'http', 'https'}) or not p.hostname or p.username or p.password:
        raise ValueError('Expected a public web URL without embedded credentials')
    return value


def download(url):
    web_url(url, https=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'ChannelBox/2'})
    with urllib.request.urlopen(req, timeout=30) as response:
        web_url(response.url, https=True)
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Playlist download exceeds size limit')
    return raw


def text_field(value, maximum=512):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid playlist text')
    return value


def validate_feed(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('Unsupported playlist format')
    when = datetime.fromisoformat(value['generated_at'].replace('Z', '+00:00'))
    if when.tzinfo is None or when.timestamp() > datetime.now(timezone.utc).timestamp() + 86400:
        raise ValueError('Invalid playlist timestamp')
    channels = value.get('channels')
    if not isinstance(channels, list) or not 1 <= len(channels) <= 9999:
        raise ValueError('Empty or oversized playlist')
    ids, numbers = set(), set()
    for c in channels:
        identity = text_field(c['id'])
        number = c['number']
        if not identity or identity in ids or not isinstance(number, str) or not re.fullmatch(r'[1-9][0-9]{0,3}', number) or number in numbers:
            raise ValueError('Duplicate or invalid channel identity/number')
        ids.add(identity)
        numbers.add(number)
        if not text_field(c['name']):
            raise ValueError('Empty channel name')
        web_url(c['url'])
        for key in ('group', 'user_agent', 'referrer'):
            text_field(c.get(key, ''), 2048)
    return value


def render_m3u(channels):
    def attr(value):
        return text_field(value, 2048).replace('"', "'")
    lines = ['#EXTM3U']
    for c in channels:
        lines.append(f'#EXTINF:-1 tvg-id="{attr(c["id"])}" tvg-chno="{c["number"]}" group-title="{attr(c.get("group", ""))}",{c["name"]}')
        for key, directive in [('user_agent', 'http-user-agent'), ('referrer', 'http-referrer')]:
            if c.get(key):
                lines.append(f'#EXTVLCOPT:{directive}={c[key]}')
        lines.append(c['url'])
    return '\n'.join(lines) + '\n'


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode('utf-8')
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_transition(feed, registry, previous_count=0):
    """Never reuse numbers or accept a catastrophic drop automatically."""
    validate_feed(feed)
    result = dict(registry)
    reverse = {n: i for i, n in result.items()}
    for c in feed['channels']:
        if c['id'] in result and result[c['id']] != c['number']:
            raise ValueError('Hosted list attempted to renumber an existing channel')
        if c['number'] in reverse and reverse[c['number']] != c['id']:
            raise ValueError('Hosted list reused a reserved channel number')
        result[c['id']] = c['number']
        reverse[c['number']] = c['id']
    if previous_count and len(feed['channels']) < previous_count * 0.5:
        raise ValueError('Playlist lost more than half its entries; review required')
    return result
