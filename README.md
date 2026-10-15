# ChannelBox

A Raspberry Pi TV player with a familiar remote and a browser-based channel guide.
Bring your own **M3U playlist** and optional **XMLTV EPG**. Select a channel in
your phone/computer browser and watch it on the TV connected to the Pi.

## What it does

- Full-screen mpv playback, channel up/down, numeric tuning and previous channel.
- FLIRC/keyboard controls, volume, mute, pause and last-channel memory.
- Searchable web guide with categories, logos and shared favorites.
- Current programme titles, local airing times, progress and next-programme details.
- Large lineups: bounded server pages and a small window of visible browser rows.
- Local playlist fallback, daily playlist refresh and six-hour EPG refresh.
- Private local settings and caches; stream/provider URLs are not sent to the browser.

**No channels, provider accounts, stream addresses or EPG data are included.**
EPGenius is optional: any compatible M3U and XMLTV source can be used. ChannelBox
does not require Threadfin, Home Assistant, a Mac, or a hosted playlist repository.

## Install on a Raspberry Pi

Use a Pi 5 with current **Raspberry Pi OS Desktop, 64-bit**, a suitable power
supply/cooling, HDMI display/audio and network access. A FLIRC receiver is
optional when controlling the player through the web guide.

```sh
sudo apt update
sudo apt install git python3 mpv python3-evdev evtest
git clone https://github.com/cycle252/channelbox.git ~/channelbox
cd ~/channelbox
sudo usermod -aG input "$USER"
```

Log out and back in (or reboot) to apply input-group access. Continue in a
terminal **on the Pi desktop** as the same regular user. Python 3.10+ is required;
the tested playback build is mpv 0.40 with FFmpeg 7.1. Use current OS packages.
The player/web guide/EPG need no pip packages on Raspberry Pi OS.

### 1. Add your own sources

```sh
mkdir -m 700 -p private
cp examples/source.example.json private/source.json
chmod 600 private/source.json
nano private/source.json
```

Replace the example values with your own HTTPS playlist and EPG URLs. Use an
empty string for `epg_url` if you do not have a guide. Keep this file private.
The EPG can be plain XML or gzip-compressed XMLTV. Streams inside the M3U can
be HTTP or HTTPS; the remote playlist and EPG downloads require HTTPS.

```sh
python3 tools/prepare_playlist.py
python3 channelbox.py --playlist private/channels.m3u --list-channels
```

For a **local M3U file**, set `playlist_url` to `""` in the private config and run
`python3 tools/prepare_playlist.py --local /path/to/your-channels.m3u` instead.
The EPG URL can still be used with a local playlist. The list command prints
only channel names/numbers. It does not verify stream availability.

### 2. Start playback and the guide

In the first desktop terminal:

```sh
python3 tools/run.py player
```

In a second desktop terminal:

```sh
cd ~/channelbox
python3 tools/run.py guide --lan
hostname -I
```

Open **`http://YOUR_PI_IP:8846/`** on a device on the same trusted network.
The Pi's IP comes from `hostname -I`; it is not supplied by this repository.
Without `--lan`, the guide is accessible only at `http://127.0.0.1:8846/` on the Pi.
The first EPG download may take a minute; playback and channel selection work
while it loads. A channel without matching schedule data shows "No program
information" and remains selectable.

### 3. Optional services and desktop startup

First confirm picture, audio, tuning, favorites and your remote work. Stop both
manual processes with Control-C, then run from the desktop terminal:

```sh
python3 tools/install_services.py
python3 tools/start_session.py
```

After those services pass the same checks, enable startup **at graphical login**:

```sh
python3 tools/install_services.py --autostart
```

Enable desktop auto-login using Raspberry Pi Configuration if this will be a TV
appliance. These are user services and need the desktop/display/audio session.
An SSH-only boot or Raspberry Pi OS Lite needs separate display/session setup.
The installer does not start anything or change OS auto-login settings itself.

```sh
systemctl --user restart channelbox.service
systemctl --user restart channelbox-guide.service
journalctl --user -u channelbox.service -n 50
journalctl --user -u channelbox-guide.service -n 50
```

## More information

- [Complete install, rebuild and backup instructions](docs/INSTALL.md)
- [Remote button assignments](docs/REMOTE.md)
- [Playlist and EPG formats, numbering and refresh behavior](docs/SOURCES.md)
- [Troubleshooting and known limits](docs/TROUBLESHOOTING.md)
- [Optional Home Assistant public-source scanner](hosting/channelbox_scanner/README.md)
- [Development and tests](docs/DEVELOPMENT.md)

The web guide is a **LAN TV controller**: devices that can open its page can
control the TV. It has no user login in this version and is not intended to be
published directly to the internet. The page does not play video in the browser.

## License

ChannelBox code is available under the [MIT license](LICENSE). Your provider's
streams, logos and programme data are separate material and are not distributed
with this project.
