# Install, rebuild and restore

Start with the step-by-step [README](../README.md). All commands run as your
regular desktop user except the explicit `sudo apt`/`usermod` commands.
You choose your own username, hostname, Wi-Fi, SSH key and source URLs.
No developer-specific account, IP address or home-network service is required.

## Prerequisites

- Raspberry Pi 5 is the tested initial platform, with Raspberry Pi OS Desktop
  64-bit, HDMI display/audio, network, cooling and suitable power.
- Python 3.10+, current mpv and the OS `python3-evdev` package.
- Your own M3U with authorized, playable HTTP(S) streams.
- Optional XMLTV guide with programme `channel` values matching M3U `tvg-id`.
- Optional FLIRC USB receiver programmed as a keyboard.

Set up the OS with Raspberry Pi Imager and choose your own account. Ethernet
is convenient for first setup; saved Wi-Fi is also supported by the OS.
Optional SSH is for maintenance, not required for normal web control.

## Configuration and data

The portable launchers use `<checkout>/private/` by default:

| File | Purpose |
| --- | --- |
| `source.json` | Your playlist and optional EPG URL |
| `channels.m3u` | Prepared local playback cache |
| `channels.feed-state.json` | Reserved numbers and last applied update |
| `state.json` | Current/previous-channel memory |
| `favorites.json`, `favorites.identities.json` | Shared favorites |
| `epg.sqlite` | Rebuildable programme cache |
| `logo-cache/` | Rebuildable logo cache |

The Unix control socket lives in your user runtime directory and is mode 0600.
Both processes must run as the same user. The launchers derive the account,
runtime path and checkout location; they do not assume UID 1000 or a username.

For a different data directory, pass `--data /path/to/private-data` to **both**
`tools/run.py` commands and give `tools/prepare_playlist.py` matching `--config`
and `--output` paths. The service installer uses this checkout's `private/`.

## M3U download and local-file setup

For hosted sources, edit `private/source.json` rather than entering credentials
in a shell command or browser URL. A Google Drive `/file/d/.../view` link is
converted to its download address by the preparation tool; the file must
already be downloadable without an interactive login. A Drive sign-in page,
sharing error or HTML preview is rejected as a playlist. ChannelBox does not
change sharing permissions or sign into Google. Download a local copy instead
if your source requires an interactive session.

The preparation tool preserves usable numbers and fills missing/invalid
numbers. Inspect `--list-channels` before playback. For a deliberately new
lineup, back up `private/` and run `prepare_playlist.py --replace` to reset
number reservations. That flag does not promise cross-provider state or
favorite migration. Refreshes of the same source preserve stable identities.

## Service installation

`tools/install_services.py` generates `~/.config/systemd/user/channelbox.service`
and `channelbox-guide.service` for the checkout's actual path. It refuses to
overwrite different existing units unless `--force` is supplied; replacements
are backed up under `private/service-backups/`.

`tools/start_session.py` imports the current desktop display/audio environment
into the user service manager and starts both units. Do not launch it from an
SSH-only session. Once installed, SSH can run `systemctl --user restart ...`
while the desktop session is available.

`--autostart` adds `~/.config/autostart/channelbox.desktop`. On graphical login
it imports the real session environment and starts both services. This avoids
assuming `graphical-session.target` is active on every Raspberry Pi OS desktop.
Configure desktop auto-login separately for unattended appliance startup.

Stop the services before manually running a second player:

```sh
systemctl --user stop channelbox-guide.service channelbox.service
```

Remove desktop startup by removing its user autostart file:

```sh
rm ~/.config/autostart/channelbox.desktop
```

This does not delete source, private settings or saved state.

## Rebuild on another SD card

1. Install the OS and dependencies from the README on the replacement card.
2. Clone this repository and restore **your own** private source config.
3. Either restore your `private/` backup, or prepare a new playlist cache.
4. Test manually, then generate units and optional desktop startup again.
5. Use the new Pi's hostname/IP for the browser. Unit paths are regenerated.

Back up the source config, channel cache/number registry, state and favorite
files together. Stream addresses may contain account credentials, so keep that
backup private. EPG and logo caches can be downloaded again. Do not upload a
private backup as a GitHub issue, commit, release asset or support attachment.

## Updating code

Back up `private/` and stop the two services. Run `git pull --ff-only`, then
restart through `python3 tools/start_session.py` from the desktop. Regenerate
service files after moving the checkout or when startup scripts change.
Updating source does not replace your private data. Review intentional
numbering or provider changes before resetting reservations.

## macOS testing

Install mpv with your preferred package manager and use a Python 3.10+ virtual
environment with `requirements-macos.txt`. Prepare your private playlist, then
run `python3 tools/run.py player --input-backend macos` and the guide launcher
with the same interpreter. Grant the Python/Terminal input permissions macOS
requests. Linux user-service installation is not used on macOS.

An optional legacy-sized SSH remote is provided at `tools/remote_app.py`:
configure `CHANNELBOX_SSH_TARGET=user@your-pi` and establish verified SSH key
access yourself before running it. `CHANNELBOX_REMOTE_DIR` defaults to the
`channelbox` folder in that SSH account's home; an optional key path and control
socket can be supplied through `CHANNELBOX_SSH_KEY` and
`CHANNELBOX_CONTROL_SOCKET`. The Pi-hosted web guide does not need this tool.
