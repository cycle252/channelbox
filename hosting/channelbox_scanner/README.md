# Optional Home Assistant public-source scanner

This is separate from the player/web guide and is **not required** for the
normal M3U/XMLTV installation. It checks the public IPTV-org US source plus
configured local-station additions with ffprobe, then publishes a validated
curated JSON feed. It does not scan a private paid-provider playlist.

For Home Assistant OS, copy this folder to `/addons/channelbox_scanner/`, reload
local apps/add-ons in the Supervisor store, then install **ChannelBox Scanner**.
Supply your own GitHub output repository and a token limited to Contents write
access to that repository. Enter the token only in the app's private options;
do not add it to this repository's config.yaml or commit any runtime data.

Configure:

- `timezone`: your IANA zone, for example UTC or America/Chicago.
- `scan_hour`: local hour of the daily run, 0–23.
- `workers`: concurrent checks, 1–8; default 4.
- `github_repository`: your own owner/repository for the generated public feed.
- `github_token`: your own narrowly scoped token, stored only in HA options.
- `full_scan_request`: increment this integer to request another full scan.

The repository intentionally seeds `numbers.json` with an empty registry.
Fresh installations allocate their own stable numbers; copy an existing
registry privately if intentionally restoring one. Runtime state, options,
probe logs and downloaded playlists are not included here.

Daily scans check the active set, compatibility additions and new entries;
weekly full scans retry the complete source set. A full scan request is consumed
only after successful scanning. Failed publication retries without repeating
the entire scan. Existing state protects number reservations.

The Dockerfile uses the Home Assistant base image plus Python, ffmpeg and
tzdata. The supported image architectures and options schema are in config.yaml.
Installation needs your HA account's normal local-app capabilities; this source
publication does not install anything onto an HA server.

To use the output, run the player's raw CLI with a writable local playlist and
`--feed-url https://YOUR_OWN_PUBLIC_JSON_FEED`. That JSON mode is separate from
the private M3U source config and supports 1–9999 channel numbers. Publish only
sources you intend to make public; private provider feeds do not belong here.
