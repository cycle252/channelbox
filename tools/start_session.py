#!/usr/bin/env python3
"""Start installed services from the logged-in graphical desktop session."""
import os
import subprocess


def main():
    if not (os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')):
        print('Run this from a terminal on the Pi desktop, not an SSH-only session.')
        return 1
    names = [k for k in ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if os.environ.get(k)]
    subprocess.run(['systemctl', '--user', 'import-environment', *names], check=True)
    subprocess.run(['systemctl', '--user', 'start', 'channelbox.service', 'channelbox-guide.service'], check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
