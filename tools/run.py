#!/usr/bin/env python3
"""Portable player/guide launcher; all account-specific data stays in private/."""
import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime_socket():
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/channelbox-{os.getuid()}')
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_uid != os.getuid():
        raise ValueError('Runtime directory must belong to the current account')
    if not os.environ.get('XDG_RUNTIME_DIR'):
        runtime.chmod(0o700)
    return str(runtime / 'channelbox-control.sock')


def allowed_hosts(extra):
    hostname = socket.gethostname()
    hosts = {hostname, hostname + '.local', *extra}
    if sys.platform.startswith('linux'):
        result = subprocess.run(['hostname', '-I'], text=True, capture_output=True, timeout=5)
        hosts.update(result.stdout.split())
    return sorted(hosts)


def command(args):
    data = args.data.resolve()
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    data.chmod(0o700)
    config = data / 'source.json'
    options = json.loads(config.read_text()) if config.exists() else {}
    if not (data / 'channels.m3u').exists():
        raise ValueError('Prepare your playlist first: python3 tools/prepare_playlist.py')
    if args.mode == 'player':
        cmd = [sys.executable, str(ROOT / 'channelbox.py'), '--playlist', str(data / 'channels.m3u'),
               '--state-file', str(data / 'state.json'), '--favorites-file', str(data / 'favorites.json'),
               '--remote-socket', runtime_socket(), '--input-backend', args.input_backend,
               '--input-device', args.input_device]
        if sys.platform.startswith('linux') and args.input_device != 'auto' and not args.no_grab:
            cmd.append('--grab-input')
        if options.get('playlist_url'):
            cmd += ['--m3u-config', str(config)]
        cmd += ['--mpv-option=--audio-device=auto', '--mpv-option=--demuxer-lavf-o=extension_picky=0']
        cmd += ['--mpv-option=' + option for option in args.mpv_option]
        return cmd
    cmd = [sys.executable, str(ROOT / 'channelbox_web.py'), '--host', '0.0.0.0' if args.lan else '127.0.0.1',
           '--port', str(args.port), '--playlist', str(data / 'channels.m3u'),
           '--favorites', str(data / 'favorites.json'), '--cache', str(data / 'logo-cache'),
           '--control-socket', runtime_socket()]
    for host in allowed_hosts(args.allowed_host):
        cmd += ['--allowed-host', host]
    if options.get('epg_url'):
        cmd += ['--epg-config', str(config), '--epg-database', str(data / 'epg.sqlite')]
    return cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['player', 'guide'])
    parser.add_argument('--data', type=Path, default=ROOT / 'private')
    parser.add_argument('--lan', action='store_true', help='Allow browsers on the trusted local network')
    parser.add_argument('--allowed-host', action='append', default=[])
    parser.add_argument('--port', type=int, default=8846)
    parser.add_argument('--input-backend', choices=['auto', 'linux', 'macos'], default='auto')
    parser.add_argument('--input-device', default='flirc')
    parser.add_argument('--no-grab', action='store_true')
    parser.add_argument('--mpv-option', action='append', default=[])
    args = parser.parse_args()
    try:
        cmd = command(args)
    except Exception as error:
        print(f'Cannot start ChannelBox ({type(error).__name__}). Check private/source.json and prepare the playlist.', file=sys.stderr)
        return 1
    os.execv(sys.executable, cmd)


if __name__ == '__main__':
    raise SystemExit(main())
