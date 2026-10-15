#!/usr/bin/env python3
"""Generate user services for this checkout, without starting or enabling them."""
import argparse
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def quoted(value):
    if any(c in str(value) for c in '\n\r\0'):
        raise ValueError('Unsupported installation path')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install(root=ROOT, home=None, *, autostart=False, force=False):
    root = Path(root).resolve()
    home = Path(home or Path.home())
    units = home / '.config/systemd/user'
    launcher = quoted(root / 'tools/run.py')
    working_directory = str(root).replace('%', '%%')
    common = 'Restart=on-failure\nRestartSec=5\nUMask=0077\nNoNewPrivileges=yes\n'
    files = {
        units / 'channelbox.service': '[Unit]\nDescription=ChannelBox TV player\n\n[Service]\nType=simple\nWorkingDirectory=' + working_directory + '\nExecStart=/usr/bin/python3 ' + launcher + ' player\n' + common,
        units / 'channelbox-guide.service': '[Unit]\nDescription=ChannelBox web guide\nAfter=channelbox.service\n\n[Service]\nType=simple\nWorkingDirectory=' + working_directory + '\nExecStart=/usr/bin/python3 ' + launcher + ' guide --lan\n' + common,
    }
    if autostart:
        # XDG .desktop Exec has its own percent/backslash escaping rules.
        path = str(root / 'tools/start_session.py')
        desktop_path = '"' + path.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
        files[home / '.config/autostart/channelbox.desktop'] = '[Desktop Entry]\nType=Application\nName=ChannelBox\nExec=/usr/bin/python3 ' + desktop_path + '\nTerminal=false\n'
    conflicts = [p for p, text in files.items() if p.exists() and p.read_text() != text]
    if conflicts and not force:
        raise FileExistsError('Service files already exist; review them before using --force')
    if conflicts:
        backup = root / 'private/service-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup.mkdir(mode=0o700, parents=True)
        for path in conflicts:
            shutil.copy2(path, backup / path.name)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
    return list(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--autostart', action='store_true', help='Start both services on the next graphical desktop login')
    parser.add_argument('--force', action='store_true', help='Back up and replace different existing unit files')
    args = parser.parse_args()
    try:
        files = install(autostart=args.autostart, force=args.force)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    except Exception as error:
        print(f'Service installation failed ({type(error).__name__}); existing conflicting files were not replaced without --force.')
        return 1
    print(f'Installed {len(files)} configuration files. Services were not started.')
    print('After manual playback checks, run: python3 tools/start_session.py')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
