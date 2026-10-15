"""Private M3U downloads; preserve EPG metadata and reserve channel numbers."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from channelbox_feed import web_url

MAX_BYTES = 32 * 1024 * 1024
NUMBER = re.compile(r'[1-9][0-9]{0,4}\Z')


def download_config(path):
    config = json.loads(Path(path).read_text())
    url = web_url(config['playlist_url'], https=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'ChannelBox/2',
                                                  'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=45) as response:
        web_url(response.url, https=True)
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Playlist exceeds download limit')
    return data


def prepare(data, metadata=None):
    # Import lazily: the controller also imports this module during updates.
    from channelbox import ATTR_RE, parse_extinf
    metadata = metadata or {}
    if len(data) > MAX_BYTES:
        raise ValueError('Playlist exceeds download limit')
    text = data.decode('utf-8-sig')
    lines = text.splitlines()
    if not lines or not lines[0].startswith('#EXTM3U'):
        raise ValueError('Expected an M3U playlist, not a web page')
    entries, pending, occurrences = [], None, Counter()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith('#EXTINF:'):
            if pending is not None:
                raise ValueError('Entry missing stream URL')
            attrs, name = parse_extinf(line)
            if not name:
                raise ValueError('Entry missing channel name')
            pending = (index, attrs, name)
        elif line and not line.startswith('#'):
            if pending is None:
                raise ValueError('Unexpected stream or web page content')
            web_url(line)
            row, attrs, name = pending
            supplied = attrs.get('tvg-chno') or attrs.get('channel-number') or attrs.get('ch-number')
            if supplied is None:
                supplied = str(len(entries) + 1)
            number = supplied if NUMBER.fullmatch(supplied) else None
            # EPG IDs can be shared by several feeds. Keep them intact and use
            # separate player identities. Valid source numbers distinguish copies;
            # unnamed-number entries use their URL and duplicate occurrence.
            key = ['cuid', attrs['cuid']] if attrs.get('cuid') else [
                name, attrs.get('group-title', ''), attrs.get('tvg-id', ''),
                number if number else line]
            digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
            occurrences[digest] += 1
            identity = f'm3u-{digest}-{occurrences[digest]}'
            entries.append((row, identity, number))
            pending = None
    if pending is not None or not 1 <= len(entries) <= 99999:
        raise ValueError('Incomplete, empty or oversized playlist')
    if metadata.get('count', 0) and len(entries) < metadata['count'] * 0.5:
        raise ValueError('Playlist lost more than half its entries; review required')
    registry = dict(metadata.get('numbers', {}))
    reserved = {v: k for k, v in registry.items()}
    explicit = [n for _, _, n in entries if n]
    if len(set(explicit)) != len(explicit):
        raise ValueError('Duplicate source channel numbers')
    used = set(explicit) | set(reserved)
    candidate = 1
    repaired = 0
    for row, identity, supplied in entries:
        number = supplied or registry.get(identity)
        if number is None:
            while str(candidate) in used and candidate <= 99999:
                candidate += 1
            if candidate > 99999:
                raise ValueError('No unused channel numbers remain')
            number = str(candidate)
        if not supplied:
            repaired += 1
        if identity in registry and registry[identity] != number:
            raise ValueError('Source attempted to renumber a saved channel')
        if number in reserved and reserved[number] != identity:
            raise ValueError('Source reused a reserved channel number')
        used.add(number)
        registry[identity] = number
        reserved[number] = identity
        # Preserve logos, tvg-id, group, EPG header and VLC directives verbatim.
        line = ATTR_RE.sub(lambda m: '' if m.group(1).lower() in
                           {'tvg-chno', 'channel-number', 'ch-number', 'channelbox-id'}
                           else m.group(0), lines[row])
        position = line.index(',')
        lines[row] = line[:position].rstrip() + f' tvg-chno="{number}" channelbox-id="{identity}"' + line[position:]
    return {'text': '\n'.join(lines) + '\n', 'numbers': registry,
            'count': len(entries), 'repaired': repaired,
            'generated_at': datetime.now(timezone.utc).isoformat()}
