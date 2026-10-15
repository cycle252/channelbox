# ChannelBox TV Guide (preview)

This is a separate **TV interface** alongside the existing browser guide.
`channelbox_tvguide/` contains its model, read-only schedule reader, renderer
and remote navigation. The existing `channelbox_web.py` and `web/` product
remain unchanged. Both interfaces control the same ChannelBox player,
channel numbers, favorites and channel memory.

## Use it

- **H** opens/closes TV Guide. Map a spare FLIRC button to the keyboard key H.
- The compact browser remote has a **TV guide** button and accepts H while
  its page is focused. It also sends left/right arrows for this interface.
- Up/down browses channels without retuning the live preview.
- Left/right moves between programmes; left at the first time block opens
  the library. Up/down selects a library view; Enter/right applies it.
- **C** switches All/Favorites; **A** stars the highlighted channel.
- Digits select a channel in the grid; **Enter/OK** tunes it live and restores
  full-screen video. Future programme selection still tunes the channel live;
  this version does not provide catch-up playback or scheduled tuning.
- **H or Escape** returns to the current video without exiting the player.
- **B** still opens the original text overlay. Existing web-guide controls
  and the Exit player buttons remain available.

The player is normally full-screen. Opening TV Guide boxes the same live
video into the top-right pane: it does not start a second decoder/stream.
Browsing leaves the live preview on the current channel until Enter/OK.
The TV layout uses a 1280x720 design canvas that scales with the video window,
six channel rows and a two-hour timeline. Future browsing is capped at 24h.
Network TV means the playlist's affiliate/network groups, not a geographic
search for channels near the viewer.

The TV interface shares the browser guide's charcoal background and lime-green
accent (`#c7f37a`). Green marks the focused programme, active library, channel
details and current-time line; muted gray text separates secondary information.

## Enable on another installation

With the portable source and an already prepared private playlist:

```sh
python3 tools/run.py player --tv-guide
python3 tools/run.py guide --lan
```

The first command enables the native interface and reads `private/epg.sqlite`.
The guide service continues to refresh that shared cache on its existing
six-hour schedule. The TV interface only reads it, including while browsing;
it does not download the feed again or modify the database. Missing listings
are shown explicitly and the channel remains selectable.

For a direct controller launch, add `--tv-guide-epg /path/to/epg.sqlite`.
For automatic startup, append `--tv-guide` to the portable player's generated
ExecStart command, preserving its actual checkout path, then reload the user
service manager and restart the player. Remove that flag to disable TV Guide.
No GUI toolkit, browser kiosk or additional Pi packages are required.

Full programme descriptions are displayed when the cache contains a
`description` column. The current shared importer stores titles/subtitles;
the first version shows those, airtimes, duration and the channel category.
Channel logos and a dedicated remote configuration screen are later work.

## Verification

96 automated checks passed, including live Unix IPC connection lifetime,
exact restoration of video margins, no stream addresses in rendered data,
literal ASS text, original EPG IDs, gaps, category/favorite navigation,
old-guide coexistence, channel memory and safe fallback if the overlay fails.
The TV layout was captured from the actual Pi 5/mpv output with live KAIT video.
Live navigation did not retune playback; Enter returned to full-screen on the
selected channel and all four previous video margins were restored exactly.
B still opened the original overlay afterward. The compact browser remote
button opened the new interface in a live test. Its cached filter switches
took 2–3 ms, video playback time advanced while the guide was open, and
only one mpv process was running. The existing guide service retained the
same PID throughout deployment. Handheld FLIRC H programming
and viewing-distance acceptance require the user to try the controls.

mpv overlay and video-margin APIs: https://mpv.io/manual/stable/
