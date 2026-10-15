#!/usr/bin/env python3
"""Local Mac remote; commands reach your Pi over authenticated SSH."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import secrets
import subprocess
import threading
import webbrowser
import argparse
import os
import shlex

ROOT = Path(__file__).resolve().parents[1]
PORT = 8845
TOKEN = secrets.token_urlsafe(32)
SSH_LOCK = threading.Lock()


def send(request):
    target = os.environ.get('CHANNELBOX_SSH_TARGET')
    if not target:
        raise RuntimeError('Set CHANNELBOX_SSH_TARGET to your Pi user@hostname')
    directory = os.environ.get('CHANNELBOX_REMOTE_DIR', 'channelbox')
    remote_socket = os.environ.get('CHANNELBOX_CONTROL_SOCKET')
    socket_arg = shlex.quote(remote_socket) if remote_socket else '"${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/channelbox-control.sock"'
    remote = 'cd ' + shlex.quote(directory) + ' && python3 channelbox_remote.py --send ' + socket_arg
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3',
               '-o', 'StrictHostKeyChecking=yes']
    key = os.environ.get('CHANNELBOX_SSH_KEY')
    if key:
        command += ['-i', key, '-o', 'IdentitiesOnly=yes']
    command += [target, remote]
    with SSH_LOCK:
        result = subprocess.run(command, input=json.dumps(request) + '\n', text=True,
                                capture_output=True, timeout=8)
    if result.returncode:
        raise RuntimeError('Pi is unreachable or the test player is stopped')
    return json.loads(result.stdout)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data, content_type='application/json'):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{PORT}'

    def do_GET(self):
        if not self.valid_host() or self.path != '/':
            return self.reply(404, {'ok': False})
        page = (ROOT / 'remote/index.html').read_text().replace('__TOKEN__', TOKEN)
        self.reply(200, page.encode(), 'text/html; charset=utf-8')

    def do_POST(self):
        if (not self.valid_host() or self.path != '/control'
                or self.headers.get('Origin') not in (None, f'http://127.0.0.1:{PORT}')
                or not secrets.compare_digest(self.headers.get('X-Remote-Token', ''), TOKEN)):
            return self.reply(403, {'ok': False, 'error': 'Access denied'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 4096:
                raise ValueError()
            request = json.loads(self.rfile.read(length))
            self.reply(200, send(request))
        except Exception:
            self.reply(503, {'ok': False, 'error': 'Cannot reach the Pi. Check its power and connection.'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError:
        print(f'Port {PORT} is already in use. The remote may already be running.')
        if not args.no_browser:
            webbrowser.open(f'http://127.0.0.1:{PORT}')
        return
    if not args.no_browser:
        webbrowser.open(f'http://127.0.0.1:{PORT}')
    print(f'ChannelBox Remote: http://127.0.0.1:{PORT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
