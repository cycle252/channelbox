# Playlist and EPG sources

`examples/source.example.json` contains only non-working example URLs. Copy it
to ignored `private/source.json` and supply your own values. Set unused fields
to `""`. EPGenius-generated M3U/XMLTV sources work, but EPGenius is not required.

## Channel identity and numbers

M3U entries use `tvg-chno`, `channel-number` or `ch-number` when present. Without
one, initial playlist position is used. Preparation fills unusable numbers
with unused values. Up/down follows file order. Existing valid numbers and
category names are retained; there is no automatic News/Kids renumbering.
The private M3U path supports numbers up to 99999 and five-digit tuning.

The preparation layer preserves `tvg-id`, groups, logos, source metadata and
supported VLC header directives. A separate hashed `channelbox-id` identifies
playback/state entries; EPGenius `cuid` is used when available. Some generic
M3Us without stable IDs may require review after provider URL/name changes.

Playlist downloads run on player startup and daily; failed downloads retry
after one hour while keeping the cached lineup. HTML, malformed entries,
duplicate numbers, number reuse and loss of more than half the list are
rejected. Applying an update waits until numeric input/the guide is closed,
and defers removal of the current/previous channel. A changed update saves a
previous cache. An application refresh does not establish stream availability.

## Programme matching

XMLTV `programme channel="..."` must match the M3U's original `tvg-id` exactly.
Several channel entries may share an EPG ID. Player identities, stream URLs,
channel numbers and fuzzy display-name guesses are not used as substitutes.
Missing IDs, expired listings and schedule gaps show "No program information".
This is distinct from whether the stream itself plays.

The guide downloads plain XML or gzip XMLTV over HTTPS, streams it into a
replacement SQLite database and keeps the previous cache if import fails.
Limits are 64 MiB download and 512 MiB expanded XML. Internal XML entities and
unsupported encodings are rejected. Retention is six hours back/72 hours ahead.

EPG refresh is every six hours while the guide runs, with hourly retries on
failure. Restarts use a fresh cache and wait only the remaining time to its
next refresh. Visible browser rows refresh every 30 seconds. XMLTV offsets are
normalized to UTC and shown in the browser's local time zone. Programme end
times are exclusive, so an ended show is not returned as currently airing.

## Optional curated JSON feed

`channelbox_feed.py` also supports the scanner's validated hosted JSON format.
The player's raw CLI accepts `--feed-url` with a writable local `--playlist`.
Do not combine it with `--m3u-config`. This separate public-source feed format
retains its existing 1–9999 numbering limit. The basic installation does not
need a GitHub feed, GitHub token or Home Assistant scanner.
