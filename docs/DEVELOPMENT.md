# Development

The player is `channelbox.py`; platform input is `channelbox_input.py`.
`channelbox_remote.py` exposes bounded commands on a private Unix socket.
`channelbox_web.py` serves `web/`, uses that socket for controls, and returns
channel metadata/programmes without stream URLs. `channelbox_epg.py` imports
bounded XMLTV into a last-good SQLite cache. `channelbox_m3u.py` prepares and
validates private M3U downloads while preserving original EPG metadata.

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 channelbox.py --playlist examples/channels.m3u.example --list-channels
node --check web/app.js
git diff --check
```

Listing/tests do not need mpv, evdev, pynput, a Pi, provider credentials, or
internet access. Socket tests need permission to bind local HTTP/Unix sockets.
Linux/Python 3.10+ is the primary target; macOS can run the headless checks.
Node is needed only for the optional JavaScript syntax check, not deployment.

The programme tests cover time zones/DST, exact end boundaries, stale/missing
data, duplicate station entries, failed/oversized imports and cache retention.
The control tests cover favorites, previous-channel behavior, real local socket
tuning, channel-identity checks and web Host/Origin/token checks.

Portable setup tests validate bootstrap imports, private file permissions,
runtime paths and generation of fresh user units/autostart configuration.
Manual playback, HDMI sound, remote programming and boot acceptance remain
hardware checks and must be reported separately.

## Privacy and contributions

Do not commit `private/`, real M3Us, guide downloads/databases, state, favorites,
access tokens, provider URLs, SSH keys, VPN profiles or machine-specific notes.
Use example.invalid and synthetic fixtures in tests/docs. The published source
starts with a clean history; deployment credentials and earlier local history
are not part of the repository. Add only deliberate public source files.

Read `AGENTS.md` and the MIT license before changing the project. The optional
scanner under `hosting/` is independent from private M3U/EPG playback.
