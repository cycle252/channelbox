#!/usr/bin/env python3
"""Pi-hosted channel browser. Credentials and stream URLs never leave the Pi."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from channelbox import parse_extinf, read_json
from channelbox_feed import atomic_write

ASSETS = Path(__file__).resolve().parent / 'web'
IMAGE_LIMIT = 1024 * 1024


def public_image_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or
            parsed.username or parsed.password or any(ord(c) < 32 for c in url)):
        raise ValueError('Invalid image URL')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Image must be on the public internet')
    return url


class ImageRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('Unsupported image')


class Catalog:
    def __init__(self, playlist, favorites, cache, epg=None):
        self.playlist = Path(playlist)
        self.favorites = Path(favorites)
        self.epg = epg
        self.cache = Path(cache)
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.signature = None
        self.entries = []
        self.by_number = {}
        self.logos = {}
        self.groups = []
        self.revision = ''
        self.image_slots = threading.BoundedSemaphore(4)
        self.failed_images = {}

    def refresh(self):
        with self.lock:
            stat = self.playlist.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self.signature:
                return
            raw = self.playlist.read_bytes()
            entries, logos, groups = [], {}, {}
            attrs = None
            for line in raw.decode('utf-8-sig').splitlines():
                line = line.strip()
                if line.startswith('#EXTINF:'):
                    attrs, name = parse_extinf(line)
                elif line and not line.startswith('#') and attrs is not None:
                    number = attrs.get('tvg-chno') or attrs.get('channel-number') or attrs.get('ch-number') or str(len(entries) + 1)
                    group = attrs.get('group-title') or 'Other channels'
                    identity = attrs.get('channelbox-id') or attrs.get('tvg-id') or ''
                    logo = attrs.get('tvg-logo', '')
                    logo_key = hashlib.sha256(logo.encode()).hexdigest() if logo else ''
                    if logo_key:
                        logos[logo_key] = logo
                    entries.append({'number': number, 'name': name, 'group': group,
                                    'identity': identity, 'logo': '/api/logo/' + logo_key if logo else '',
                                    '_epg_id': attrs.get('tvg-id', '').strip(),
                                    '_url': line, '_search': (number + ' ' + name + ' ' + group).casefold()})
                    groups[group] = groups.get(group, 0) + 1
                    attrs = None
            if not entries or len({c['number'] for c in entries}) != len(entries):
                raise ValueError('Invalid channel catalog')
            self.entries = entries
            self.by_number = {c['number']: c for c in entries}
            self.groups = [{'name': g, 'count': n} for g, n in groups.items()]
            self.logos = logos
            self.signature = signature
            self.revision = hashlib.sha256(raw).hexdigest()[:16]

    def favorites_set(self):
        urls = set(read_json(self.favorites, []))
        identities = set(read_json(self.favorites.with_suffix('.identities.json'), []))
        return {c['number'] for c in self.entries if c['_url'] in urls or (c['identity'] and c['identity'] in identities)}

    def epg_ids(self):
        self.refresh()
        with self.lock:
            return {c['_epg_id'] for c in self.entries if c['_epg_id']}

    def info(self):
        self.refresh()
        with self.lock:
            metadata = read_json(self.playlist.with_suffix('.feed-state.json'), {})
            epg = self.epg.metadata() if self.epg else {}
            return {'total': len(self.entries), 'groups': self.groups, 'revision': self.revision,
                    'favorites': sorted(self.favorites_set()), 'updated_at': metadata.get('generated_at'),
                    'epg_available': bool(epg.get('updated_at')),
                    'epg': {k: epg[k] for k in ('updated_at', 'channels', 'programmes', 'loading', 'refresh_failed') if k in epg}}

    def query(self, params):
        self.refresh()
        q = params.get('q', [''])[0][:160].casefold().strip()
        group = params.get('group', [''])[0]
        favorites_only = params.get('favorites', ['0'])[0] == '1'
        exact_number = params.get('number', [''])[0]
        offset = max(0, min(99999, int(params.get('offset', ['0'])[0])))
        limit = max(1, min(100, int(params.get('limit', ['80'])[0])))
        with self.lock:
            favorites = self.favorites_set()
            matches = [c for c in self.entries if (not group or c['group'] == group) and
                       (not exact_number or c['number'] == exact_number) and
                       (not q or all(term in c['_search'] for term in q.split())) and
                       (not favorites_only or c['number'] in favorites)]
            selected = matches[offset:offset + limit]
            schedules = self.epg.lookup([c['_epg_id'] for c in selected]) if self.epg else {}
            items = [{k: v for k, v in c.items() if not k.startswith('_')} |
                     {'favorite': c['number'] in favorites,
                      'now': schedules.get(c['_epg_id'], {}).get('now'),
                      'next': schedules.get(c['_epg_id'], {}).get('next')}
                     for c in selected]
            return {'items': items, 'count': len(matches), 'offset': offset,
                    'has_more': offset + len(items) < len(matches), 'revision': self.revision}

    def image(self, key):
        self.refresh()
        with self.lock:
            url = self.logos.get(key)
            if not url or self.failed_images.get(key, 0) > time.monotonic():
                raise ValueError('Image unavailable')
        path = self.cache / (key + '.image')
        if path.exists():
            data = path.read_bytes()
            return data, image_type(data)
        if not self.image_slots.acquire(timeout=0.15):
            raise ValueError('Image loader busy')
        try:
            public_image_url(url)
            request = urllib.request.Request(url, headers={'User-Agent': 'ChannelBox/2'})
            with urllib.request.build_opener(ImageRedirect()).open(request, timeout=5) as response:
                data = response.read(IMAGE_LIMIT + 1)
            if len(data) > IMAGE_LIMIT:
                raise ValueError('Image too large')
            mime = image_type(data)
            atomic_write(path, data)
            return data, mime
        except Exception:
            with self.lock:
                self.failed_images[key] = time.monotonic() + 3600
            raise
        finally:
            self.image_slots.release()


class GuideServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, catalog, control_socket, allowed_hosts=()):
        self.catalog = catalog
        self.control_socket = str(control_socket)
        self.token = secrets.token_urlsafe(32)
        self.allowed_hosts = {address[0], '127.0.0.1', 'localhost', *allowed_hosts}
        self.command_lock = threading.Lock()
        super().__init__(address, Handler)

    def control(self, request):
        action = request.get('action')
        if action not in ('status', 'tune', 'favorite', 'key', 'close'):
            raise ValueError('Unsupported command')
        if action in ('tune', 'favorite'):
            self.catalog.refresh()
            with self.catalog.lock:
                channel = self.catalog.by_number.get(request.get('number'))
                if not channel or request.get('identity') != channel['identity']:
                    raise ValueError('Channel list changed; reload the guide')
        payload = json.dumps(request).encode() + b'\n'
        if len(payload) > 4096:
            raise ValueError('Command too large')
        with self.command_lock, socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(self.control_socket)
            client.sendall(payload)
            raw = client.makefile('rb').readline(4097)
        if len(raw) > 4096:
            raise ValueError('Invalid player response')
        value = json.loads(raw)
        if not value.get('ok'):
            raise ValueError('Player rejected command; refresh the guide')
        return value


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def authorized_host(self):
        host = self.headers.get('Host', '')
        try:
            parsed = urllib.parse.urlsplit('http://' + host)
            return parsed.hostname in self.server.allowed_hosts and parsed.port == self.server.server_port and not parsed.username
        except ValueError:
            return False

    def reply(self, status, value, mime='application/json', cache=False):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'private, max-age=86400' if cache else 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def api_authorized(self):
        return secrets.compare_digest(self.headers.get('X-Guide-Token', ''), self.server.token)

    def do_GET(self):
        if not self.authorized_host():
            return self.reply(403, {'error': 'Unknown host'})
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path == '/':
                page = (ASSETS / 'index.html').read_text().replace('__GUIDE_TOKEN__', self.server.token)
                return self.reply(200, page.encode(), 'text/html; charset=utf-8')
            if path in ('/app.js', '/styles.css'):
                mime = 'application/javascript' if path.endswith('.js') else 'text/css'
                return self.reply(200, (ASSETS / path[1:]).read_bytes(), mime)
            if path.startswith('/api/logo/'):
                key = path.rsplit('/', 1)[1]
                if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                    return self.reply(404, {'error': 'Image not found'})
                try:
                    data, mime = self.server.catalog.image(key)
                    return self.reply(200, data, mime, cache=True)
                except Exception:
                    return self.reply(404, {'error': 'Image unavailable'})
            if not self.api_authorized():
                return self.reply(403, {'error': 'Reload the guide to reconnect'})
            if path == '/api/meta':
                return self.reply(200, self.server.catalog.info())
            if path == '/api/channels':
                return self.reply(200, self.server.catalog.query(urllib.parse.parse_qs(parsed.query)))
            if path == '/api/status':
                return self.reply(200, self.server.control({'action': 'status'}))
            return self.reply(404, {'error': 'Not found'})
        except ValueError:
            return self.reply(400, {'error': 'Invalid request; reload the guide'})
        except Exception:
            return self.reply(503, {'error': 'Player unavailable. Check that ChannelBox is running.'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        expected = 'http://' + self.headers.get('Host', '')
        if not self.authorized_host() or not self.api_authorized() or origin not in (None, expected):
            return self.reply(403, {'error': 'Command denied'})
        if self.path != '/api/control':
            return self.reply(404, {'error': 'Not found'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 4096:
                raise ValueError()
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError()
            return self.reply(200, self.server.control(request))
        except ValueError:
            return self.reply(400, {'error': 'Channel or command changed. Reload the guide and try again.'})
        except Exception:
            return self.reply(503, {'error': 'Cannot reach the TV player. Try again in a moment.'})


def main():
    parser = argparse.ArgumentParser(description='ChannelBox web guide for the Pi')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8846)
    parser.add_argument('--allowed-host', action='append', default=[])
    parser.add_argument('--playlist', required=True)
    parser.add_argument('--favorites', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument('--epg-config', help='Private JSON containing the saved epg_url')
    parser.add_argument('--epg-database', help='Local SQLite programme cache')
    parser.add_argument('--control-socket', default=str(Path(os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/channelbox-{os.getuid()}') / 'channelbox-control.sock'))
    args = parser.parse_args()
    epg = None
    if args.epg_config:
        from channelbox_epg import EpgStore
        epg = EpgStore(args.epg_database or str(Path(args.cache).parent / 'epg.sqlite'), args.epg_config)
    catalog = Catalog(args.playlist, args.favorites, args.cache, epg)
    catalog.refresh()
    server = GuideServer((args.host, args.port), catalog, args.control_socket, args.allowed_host)
    if epg:
        epg.start(catalog.epg_ids)
    print(f'ChannelBox Guide listening on port {args.port}; {len(catalog.entries)} channels', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if epg:
            epg.close()


if __name__ == '__main__':
    main()
