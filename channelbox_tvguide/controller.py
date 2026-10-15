"""Optional TV guide mode sharing one player, its saved state and its EPG cache."""
import json
import socket
import time
from pathlib import Path

from .model import FILTERS, HALF_HOUR, ROWS, WINDOW, matches, metadata, read_schedule, segments
from .render import WIDTH, HEIGHT, PREVIEW, render


class Overlay:
    """mpv owns overlays per IPC connection, so this connection stays open."""
    properties = ('video-margin-ratio-left', 'video-margin-ratio-top',
                  'video-margin-ratio-right', 'video-margin-ratio-bottom')

    def __init__(self, path):
        self.path = str(path)
        self.client = self.stream = None
        self.saved = None
        self.serial = 0

    def disconnect(self):
        if self.stream:
            self.stream.close()
        if self.client:
            self.client.close()
        self.client = self.stream = None

    def command(self, command):
        if self.client is None:
            self.client = socket.socket(socket.AF_UNIX)
            self.client.settimeout(2)
            self.client.connect(self.path)
            self.stream = self.client.makefile('rb')
        self.serial += 1
        try:
            self.client.sendall(json.dumps({'command': command, 'request_id': self.serial}).encode()+b'\n')
            for _ in range(100):
                raw = self.stream.readline(65537)
                if not raw or len(raw) > 65536:
                    raise ConnectionError('Invalid overlay response')
                value = json.loads(raw)
                if value.get('request_id') != self.serial:
                    continue
                if value.get('error') != 'success':
                    raise RuntimeError('Overlay command failed')
                return value.get('data')
            raise ConnectionError('Missing overlay response')
        except Exception:
            self.disconnect()
            raise

    def draw(self, text):
        if self.client is None:
            if self.saved is None:
                self.saved = {p: self.command(['get_property', p]) for p in self.properties}
            x, y, w, h = PREVIEW
            for prop, value in zip(self.properties, (x/WIDTH, y/HEIGHT,
                                                     (WIDTH-x-w)/WIDTH, (HEIGHT-y-h)/HEIGHT)):
                self.command(['set_property', prop, value])
        self.command(['osd-overlay', 781, 'ass-events', text, WIDTH, HEIGHT, 20])

    def close(self):
        try:
            if self.saved is not None:
                for prop, value in self.saved.items():
                    self.command(['set_property', prop, value])
                self.command(['osd-overlay', 781, 'none', ''])
        except Exception:
            pass
        finally:
            self.disconnect()
            self.saved = None


class TVGuide:
    def __init__(self, box, database, *, overlay=None, now=time.time):
        self.box, self.database, self.now = box, Path(database), now
        self.overlay = overlay or Overlay(box.mpv.socket_path)
        self.is_open = False
        self.filter = 'all'
        self.focus = 'grid'
        self.category = self.position = 0
        self.start = self.anchor = 0
        self.info, self.channels, self.counts = {}, [], {}
        self.choices = {}
        self.metadata_signature = None
        self.signature = self.schedule_key = None
        self.schedule = {}
        self.digits = self.notice = ''
        self.digit_time = 0

    def refresh_channels(self):
        source = Path(self.box.playlist_source)
        try:
            stat = source.stat()
            source_signature = (stat.st_mtime_ns, stat.st_size)
            signature = (*source_signature, id(self.box.channels),
                         frozenset(self.box.favorite_urls), frozenset(self.box.favorite_ids))
        except OSError:
            source_signature = None
            signature = (id(self.box.channels), frozenset(self.box.favorite_urls), frozenset(self.box.favorite_ids))
        if signature == self.signature and self.channels is self.choices.get(self.filter):
            return
        selected = self.selected
        if signature != self.signature:
            if source_signature != self.metadata_signature:
                self.info = metadata(source)
                self.metadata_signature = source_signature
            self.choices = {kind: [] for kind, _ in FILTERS}
            for channel in self.box.channels:
                info, favorite = self.info.get(channel.number, {}), self.box.is_favorite(channel)
                for kind, _ in FILTERS:
                    if matches(kind, channel, info, favorite):
                        self.choices[kind].append(channel)
            self.counts = {kind: len(channels) for kind, channels in self.choices.items()}
        self.channels = self.choices[self.filter]
        number = selected.number if selected else self.box.current.number
        self.position = next((i for i, c in enumerate(self.channels) if c.number == number),
                             min(self.position, max(0, len(self.channels)-1)))
        self.signature = signature
        self.schedule_key = None

    @property
    def selected(self):
        return self.channels[self.position] if self.channels and self.position < len(self.channels) else None

    def set_filter(self, kind):
        self.filter = kind
        self.refresh_channels()
        self.category = next(i for i, item in enumerate(FILTERS) if item[0] == kind)
        self.focus = 'grid'
        self.digits = self.notice = ''

    def view(self):
        self.refresh_channels()
        now = self.now()
        current_start = int(now // HALF_HOUR) * HALF_HOUR
        if self.start < current_start:
            self.start = current_start
            self.anchor = max(now, self.anchor)
        offset = min(max(0, self.position-ROWS//2), max(0, len(self.channels)-ROWS))
        channels = self.channels[offset:offset+ROWS]
        ids = [self.info.get(c.number, {}).get('epg_id', '') for c in channels]
        try:
            stat = self.database.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        key = (tuple(ids), self.start, stamp)
        if key != self.schedule_key:
            self.schedule = read_schedule(self.database, ids, self.start, self.start+WINDOW)
            self.schedule_key = key
        rows, selected_programme = [], None
        for channel in channels:
            epg_id = self.info.get(channel.number, {}).get('epg_id', '')
            items = segments(self.schedule.get(epg_id, []), self.start, self.start+WINDOW)
            rows.append({'channel': channel, 'programmes': items, 'favorite': self.box.is_favorite(channel)})
            if channel == self.selected:
                selected_programme = next((p for p in items if p['start'] <= self.anchor < p['stop']), items[0])
        return {'now': now, 'start': self.start, 'rows': rows, 'selected': self.selected,
                'programme': selected_programme, 'group': self.info.get(self.selected.number, {}).get('group', '') if self.selected else '',
                'current': self.box.current, 'filter': self.filter, 'focus': self.focus,
                'category': self.category, 'counts': self.counts, 'notice': self.notice,
                'position': self.position+1 if self.channels else 0, 'total': len(self.channels)}

    def draw(self):
        self.overlay.draw(render(self.view()))

    def open(self):
        self.box._cancel_number_timer()
        self.box.number_buffer = ''
        self.box.close_guide()
        self.is_open = True
        self.start = int(self.now() // HALF_HOUR) * HALF_HOUR
        self.anchor = self.now()
        self.focus = 'grid'
        self.channels = []
        self.position = 0
        self.digits = self.notice = ''
        self.draw()

    def close(self):
        self.is_open = False
        self.digits = self.notice = ''
        self.overlay.close()

    def tick(self):
        with self.box.lock:
            if self.is_open:
                if self.digits and self.now()-self.digit_time >= 2:
                    self.digits = self.notice = ''
                self.draw()

    def handle_key(self, key):
        if key == 'h':
            self.close() if self.is_open else self.open()
            return True
        if not self.is_open:
            return False
        if key == 'esc':
            self.close()
            return True
        if key in ('b', 'g'):
            self.close()
            return False
        self.refresh_channels()
        if key == 'c':
            self.set_filter('all' if self.filter == 'favorites' else 'favorites')
        elif self.focus == 'categories' and key in ('up', 'down', 'right', 'enter', 'left'):
            if key in ('up', 'down'):
                self.category = (self.category + (1 if key == 'down' else -1)) % len(FILTERS)
            elif key in ('right', 'enter'):
                self.set_filter(FILTERS[self.category][0])
            else:
                self.focus = 'grid'
        elif key in ('up', 'down'):
            if self.channels:
                self.position = (self.position+(1 if key == 'down' else -1)) % len(self.channels)
            self.digits = self.notice = ''
        elif key in ('left', 'right'):
            view = self.view()
            item = view['programme']
            row = next((r for r in view['rows'] if r['channel'] == self.selected), None)
            if row:
                index = row['programmes'].index(item)
                if key == 'left' and index > 0:
                    self.anchor = row['programmes'][index-1]['stop']-.01
                elif key == 'right' and index < len(row['programmes'])-1:
                    self.anchor = row['programmes'][index+1]['start']+.01
                elif key == 'left' and self.start <= int(self.now()//HALF_HOUR)*HALF_HOUR:
                    self.focus = 'categories'
                else:
                    delta = HALF_HOUR if key == 'right' else -HALF_HOUR
                    self.start = max(int(self.now()//HALF_HOUR)*HALF_HOUR,
                                     min(self.start+delta, int(self.now()//HALF_HOUR)*HALF_HOUR+24*3600))
                    self.anchor = self.start+WINDOW-1 if key == 'right' else self.start+1
            elif key == 'left':
                self.focus = 'categories'
        elif key == 'a' and self.selected:
            from channelbox_remote import dispatch
            selected = self.selected
            dispatch(self.box, {'action': 'favorite', 'number': selected.number,
                               'identity': selected.identity, 'enabled': not self.box.is_favorite(selected)})
            self.refresh_channels()
        elif len(key) == 1 and key.isascii() and key.isdigit():
            self.digits = (self.digits+key)[-5:] if self.now()-self.digit_time < 2 else key
            self.digit_time = self.now()
            selected = next((c for c in self.box.channels if c.number == self.digits), None)
            if selected:
                if selected not in self.channels:
                    self.set_filter('all')
                    # set_filter clears digits; preserve the direct-entry buffer.
                    self.digits = selected.number
                self.position = self.channels.index(selected)
            self.notice = 'CHANNEL '+self.digits+'  ·  OK to watch live'
        elif key == 'enter' and self.selected:
            if self.digits and self.selected.number != self.digits:
                self.notice = 'CHANNEL '+self.digits+'  ·  Not found'
                self.draw()
                return True
            selected = self.selected
            self.close()
            index = next(i for i, channel in enumerate(self.box.channels) if channel == selected)
            if index != self.box.index:
                self.box.previous_index = self.box.index
            self.box.index = index
            self.box.tune_current()
            return True
        else:
            return False
        self.draw()
        return True
