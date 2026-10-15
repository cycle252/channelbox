"""Bounded XMLTV import and read-only schedule lookup for the Pi web guide."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from channelbox_feed import web_url

LOG = logging.getLogger('channelbox.epg')
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
REFRESH_SECONDS = 6 * 3600
DATE = re.compile(r'^(\d{8}(?:\d{2}){0,3})(?:\s*([+-])(\d{2})(\d{2})|\s*Z)?$')


def xmltv_time(value):
    match = DATE.fullmatch(value.strip())
    if not match:
        raise ValueError('Invalid XMLTV date')
    digits, sign, hours, minutes = match.groups()
    zone = timezone.utc
    if sign:
        if int(hours) > 23 or int(minutes) > 59:
            raise ValueError('Invalid XMLTV offset')
        offset = timedelta(hours=int(hours), minutes=int(minutes))
        zone = timezone(offset if sign == '+' else -offset)
    padded = digits.ljust(14, '0')
    return int(datetime(int(padded[:4]), int(padded[4:6]), int(padded[6:8]),
                        int(padded[8:10]), int(padded[10:12]), int(padded[12:14]),
                        tzinfo=zone).timestamp())


class LimitedXML:
    def __init__(self, source, limit=MAX_EXPANDED):
        self.source, self.limit, self.total, self.tail = source, limit, 0, b''
        self.deadline = time.monotonic() + 180

    def read(self, size=-1):
        if time.monotonic() > self.deadline:
            raise ValueError('EPG processing time limit exceeded')
        data = self.source.read(min(size if size >= 0 else 65536, 65536))
        self.total += len(data)
        if self.total > self.limit:
            raise ValueError('Expanded EPG exceeds size limit')
        scan = self.tail + data
        if b'\x00' in scan:
            raise ValueError('Unsupported XML encoding')
        if b'<!ENTITY' in scan.upper():
            raise ValueError('XML entities are not supported')
        self.tail = scan[-16:]
        return data


class EpgStore:
    def __init__(self, database, config=None):
        self.database = Path(database)
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config = Path(config) if config else None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.loading = False
        self.error = False
        self._signature = None
        self._metadata = {}

    def connect(self):
        return sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True, timeout=2)

    def metadata(self):
        with self.lock:
            try:
                signature = self.database.stat().st_mtime_ns
                if signature != self._signature:
                    with closing(self.connect()) as db:
                        self._metadata = json.loads(db.execute('SELECT value FROM metadata WHERE key="import"').fetchone()[0])
                    self._signature = signature
            except (OSError, sqlite3.Error, ValueError, TypeError):
                self._metadata = {}
            return dict(self._metadata, loading=self.loading, refresh_failed=self.error)

    def import_file(self, source, channel_ids, *, now=None, source_hash=''):
        now = int(time.time() if now is None else now)
        channel_ids = set(channel_ids) - {''}
        if not channel_ids:
            raise ValueError('Playlist has no EPG identifiers')
        fd, temporary = tempfile.mkstemp(prefix='.epg-import-', suffix='.sqlite', dir=self.database.parent)
        os.close(fd)
        scanned = retained = invalid = 0
        mapped = set()
        lower, upper = now - 6 * 3600, now + 72 * 3600
        started = time.monotonic()
        try:
            with open(source, 'rb') as probe:
                compressed = probe.read(2) == b'\x1f\x8b'
            opener = gzip.open if compressed else open
            with closing(sqlite3.connect(temporary)) as db, opener(source, 'rb') as raw:
                db.execute('CREATE TABLE programmes (channel TEXT, start INTEGER, stop INTEGER, title TEXT, subtitle TEXT)')
                db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
                context = ET.iterparse(LimitedXML(raw), events=('start', 'end'))
                _, root = next(context)
                if root.tag != 'tv':
                    raise ValueError('Expected XMLTV document')
                batch = []
                for event, element in context:
                    if event != 'end':
                        continue
                    if element.tag == 'programme':
                        scanned += 1
                        identity = element.get('channel', '')
                        if identity in channel_ids:
                            try:
                                start = xmltv_time(element.get('start', ''))
                                stop = xmltv_time(element.get('stop', ''))
                                title = (element.findtext('title') or '').strip()[:512]
                                if stop <= start or not title:
                                    raise ValueError('Invalid programme')
                                if stop > lower and start < upper:
                                    batch.append((identity, start, stop, title,
                                                  (element.findtext('sub-title') or '').strip()[:256]))
                                    mapped.add(identity)
                                    retained += 1
                            except (ValueError, OverflowError):
                                invalid += 1
                        if len(batch) >= 2000:
                            db.executemany('INSERT INTO programmes VALUES (?,?,?,?,?)', batch)
                            batch.clear()
                        root.clear()
                    elif element.tag == 'channel':
                        root.clear()
                if batch:
                    db.executemany('INSERT INTO programmes VALUES (?,?,?,?,?)', batch)
                if not retained:
                    raise ValueError('No matching programmes in the current guide window')
                db.execute('CREATE INDEX programme_times ON programmes(channel, start, stop)')
                value = {'updated_at': datetime.now(timezone.utc).isoformat(),
                         'source_hash': source_hash, 'programmes': retained,
                         'channels': len(mapped), 'scanned': scanned, 'invalid': invalid,
                         'window_start': lower, 'window_stop': upper}
                db.execute('INSERT INTO metadata VALUES (?,?)', ('import', json.dumps(value)))
                db.commit()
            os.replace(temporary, self.database)
            with self.lock:
                self._signature = None
                self.error = False
            LOG.info('EPG imported: %d programmes for %d channel IDs in %.1fs', retained, len(mapped), time.monotonic() - started)
            return value
        finally:
            Path(temporary).unlink(missing_ok=True)

    def lookup(self, channel_ids, *, now=None):
        ids = list(set(channel_ids) - {''})[:100]
        if not ids or not self.database.exists():
            return {}
        now = int(time.time() if now is None else now)
        placeholders = ','.join('?' for _ in ids)
        result = {}
        try:
            with closing(self.connect()) as db:
                # End is exclusive: a programme disappears exactly when it ends.
                current = db.execute(f'SELECT channel,start,stop,title,subtitle FROM programmes WHERE channel IN ({placeholders}) AND start<=? AND stop>? ORDER BY start DESC', [*ids, now, now])
                for identity, start, stop, title, subtitle in current:
                    result.setdefault(identity, {}).setdefault('now', {'title': title, 'subtitle': subtitle, 'start': start, 'stop': stop})
                upcoming = db.execute(f'SELECT channel,start,stop,title,subtitle FROM programmes WHERE channel IN ({placeholders}) AND start>? AND start<? ORDER BY start', [*ids, now, now + 24 * 3600])
                for identity, start, stop, title, subtitle in upcoming:
                    result.setdefault(identity, {}).setdefault('next', {'title': title, 'subtitle': subtitle, 'start': start, 'stop': stop})
        except sqlite3.Error:
            return {}
        return result

    def refresh_once(self, channel_ids, *, force=False):
        if not self.config:
            return
        config = json.loads(self.config.read_text())
        url = web_url(config['epg_url'], https=True)
        source_hash = hashlib.sha256(url.encode()).hexdigest()
        metadata = self.metadata()
        stamp = datetime.fromisoformat(metadata['updated_at']).timestamp() if metadata.get('updated_at') else 0
        if (not force and metadata.get('source_hash') == source_hash and 0 <= time.time() - stamp < REFRESH_SECONDS
                and metadata.get('window_stop', 0) > time.time() + REFRESH_SECONDS):
            return
        with self.lock:
            self.loading = True
        fd, temporary = tempfile.mkstemp(prefix='.epg-download-', dir=self.database.parent)
        try:
            started, total = time.monotonic(), 0
            with os.fdopen(fd, 'wb') as output:
                request = urllib.request.Request(url, headers={'User-Agent': 'ChannelBox/2'})
                with urllib.request.urlopen(request, timeout=30) as response:
                    web_url(response.url, https=True)
                    while not self.stop_event.is_set():
                        block = response.read(256 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > MAX_DOWNLOAD or time.monotonic() - started > 180:
                            raise ValueError('EPG download exceeds limits')
                        output.write(block)
            if self.stop_event.is_set():
                return
            self.import_file(temporary, channel_ids(), source_hash=source_hash)
        finally:
            Path(temporary).unlink(missing_ok=True)
            with self.lock:
                self.loading = False

    def start(self, channel_ids):
        def worker():
            while not self.stop_event.is_set():
                try:
                    self.refresh_once(channel_ids)
                    metadata = self.metadata()
                    stamp = datetime.fromisoformat(metadata['updated_at']).timestamp() if metadata.get('updated_at') else 0
                    delay = max(30, min(REFRESH_SECONDS, REFRESH_SECONDS - (time.time() - stamp)))
                except Exception as error:
                    with self.lock:
                        self.error = True
                    LOG.warning('EPG refresh failed (%s); keeping cached listings', type(error).__name__)
                    delay = 3600
                if self.stop_event.wait(delay):
                    return
        self.thread = threading.Thread(target=worker, name='epg-refresh', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
