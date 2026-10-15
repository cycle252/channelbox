# Troubleshooting and current limits

## No picture or audio

Run from the actual desktop session first; SSH alone does not supply the
display/audio environment. Test one known-good stream. Check `mpv --version`
and use a current Raspberry Pi OS image/package set. The launchers include
the FFmpeg HLS `extension_picky=0` compatibility option used in the tested build.
Older FFmpeg versions may not recognize that option.

Select the intended HDMI audio output in the OS. Add an explicit option if
needed: `python3 tools/run.py player --mpv-option=--audio-device=YOUR_DEVICE`.
Inspect device names with `mpv --audio-device=help`. Software mute/volume and
TV/capture-application audio routing are independent. Avoid sharing diagnostic
output that includes a provider stream address.

## Remote does not respond

Confirm input-group membership with `id`, then log out/reboot after changing
groups. Run `sudo evtest`, identify the receiver and verify its programmed keys.
Stop evtest before starting the player. Use a stable by-id event path or name
substring with `--input-device`; event numbers can change after reconnects.
Without a matching receiver the app waits for hotplug, and web control still
works. Direct receiver placement, IR interference and weak power can matter.

## Browser cannot connect

Start the guide with `--lan`, use the Pi's current IP and port 8846, and keep the
devices on the same trusted network without client isolation. Host checks are
intentional: add `--allowed-host your-custom-name` if using another DNS name.
Reload the page after a guide service restart, which rotates its page token.

The guide and player must use the same account/data/runtime socket. The guide
can browse a playlist while the player is stopped, but tuning then fails.
Check both user service logs. Do not run two players as the same user: mpv uses
a per-user socket.

## Missing programmes

Wait for the first feed import. Check `epg_url`, exact M3U `tvg-id` matches, the
Pi clock/NTP, and whether the feed has current entries rather than expired
data. Browser times follow the viewer's local timezone. A missing listing
does not establish that the stream is broken. The last good cache remains
available if the next feed download fails; the footer indicates cached data.

## Number changes or rejected playlist updates

Source preparation deliberately rejects duplicate numbers, reuse of reserved
numbers, renumbering and catastrophic list shrinkage. Review the source and
keep a private backup before an intentional provider/lineup replacement with
`prepare_playlist.py --replace`. Updates removing a currently viewed/previous
channel are deferred to avoid silently changing viewing.

## Pi 3 performance

Pi 5 is the tested baseline for the full desktop/web-guide installation. A Pi 3
may need genuinely lower-resolution/frame-rate streams and 720p HDMI output.
Setting HDMI to 720p does not reduce the decode cost of a 1080p60 source.
Network, codec, stream bitrate, software decoding, cooling and power affect
results. A desktop-free DRM/KMS deployment needs separate display/audio/input
configuration and is not installed by the desktop service helper.

## Limits

No browser video, recording, catch-up, public login/account system, automatic
category-number assignment, or TV-displayed web timeline is implemented.
The web UI is a local-network controller with current/next previews. Stream
availability and EPG correctness depend on your own source. Existing retry
logic does not detect every stalled stream. Use current OS packages, test your
own representative channels, and retain a private restore backup.
