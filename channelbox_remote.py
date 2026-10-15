"""Private, bounded command socket. Stream addresses are never returned."""
import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import sys
import threading

KEYS = set('0123456789abcdim pvx'.replace(' ', '')) | {'up', 'down', 'enter'}
LIMIT = 4096


def dispatch(box, request):
    if not isinstance(request, dict):
        raise ValueError('Invalid request')
    action = request.get('action')
    with box.lock:
        if action == 'key':
            if request.get('key') not in KEYS:
                raise ValueError('Unsupported key')
            box.on_press(request['key'])
        elif action == 'close':
            if box.guide_open:
                box.close_guide()
        elif action == 'tune':
            number = request.get('number')
            if not isinstance(number, str) or not number.isascii() or not number.isdigit() or not 1 <= len(number) <= 5:
                raise ValueError('Invalid channel number')
            selected = next((c for c in box.channels if c.number == number), None)
            if selected is None:
                raise ValueError('Channel not found')
            if request.get('identity') and request['identity'] != selected.identity:
                raise ValueError('Channel list changed; refresh before tuning')
            box._cancel_number_timer()
            if box.guide_open:
                box.close_guide()
            box.number_buffer = number
            box._commit_channel_number()
        elif action == 'favorite':
            selected = next((c for c in box.channels if c.number == request.get('number')), None)
            if selected is None or not isinstance(request.get('enabled'), bool):
                raise ValueError('Invalid favorite channel')
            if request.get('identity') and request['identity'] != selected.identity:
                raise ValueError('Channel list changed; refresh before saving')
            if request['enabled']:
                box.favorite_urls.add(selected.url)
                if selected.identity:
                    box.favorite_ids.add(selected.identity)
            else:
                # The existing player treats duplicate stream URLs as one favorite.
                box.favorite_urls.discard(selected.url)
                for channel in box.channels:
                    if channel.url == selected.url:
                        box.favorite_ids.discard(channel.identity)
            box.save_favorites()
            if box.guide_open:
                box.render_guide()
        elif action != 'status':
            raise ValueError('Unsupported action')
        return {'ok': True, 'number': box.current.number, 'name': box.current.name,
                'count': len(box.channels), 'guide': box.guide_open,
                'digits': box.number_buffer}


class RemoteServer:
    def __init__(self, box, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            # Never steal a live listener from another player.
            with socket.socket(socket.AF_UNIX) as client:
                try:
                    client.connect(str(self.path))
                except ConnectionRefusedError:
                    self.path.unlink()
                else:
                    raise RuntimeError('Remote socket already in use')

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                try:
                    raw = self.rfile.readline(LIMIT + 1)
                    if not raw:
                        return
                    if len(raw) > LIMIT:
                        raise ValueError('Request too large')
                    result = dispatch(box, json.loads(raw))
                except Exception:
                    result = {'ok': False, 'error': 'Command rejected or player unavailable'}
                self.wfile.write(json.dumps(result).encode() + b'\n')

        self.server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        self.server.daemon_threads = True
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.readline(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError()
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(args.send)
            client.sendall(raw.rstrip(b'\n') + b'\n')
            response = client.makefile('rb').readline(LIMIT + 1)
        value = json.loads(response)
        print(json.dumps(value))
    except Exception:
        print(json.dumps({'ok': False, 'error': 'Pi player is not available'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
