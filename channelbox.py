#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from channelbox_input import create_listener

LOG = logging.getLogger("channelbox")


@dataclass(frozen=True)
class Channel:
    number: str
    name: str
    url: str
    identity: str = ""
    user_agent: str = ""
    referrer: str = ""


ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)=("[^"]*"|\'[^\']*\'|[^\s]+)')


def load_text(source: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(source)

    if parsed.scheme in {"http", "https"}:
        req = urllib.request.Request(source, headers={"User-Agent": "ChannelBox/1.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        return text, source

    path = Path(source).expanduser().resolve()
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text, str(path.parent) + os.sep


def parse_extinf(line: str) -> tuple[dict[str, str], str]:
    attrs = {}
    for key, value in ATTR_RE.findall(line):
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        attrs[key.lower()] = value

    name = line.split(",", 1)[1].strip() if "," in line else ""
    return attrs, name


def absolute_stream_url(value: str, base: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme:
        return value

    if urllib.parse.urlparse(base).scheme in {"http", "https"}:
        return urllib.parse.urljoin(base, value)

    return str((Path(base) / value).resolve())


def parse_m3u(source: str) -> list[Channel]:
    text, base = load_text(source)
    channels = []
    headers = {}
    attrs = {}
    name = ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.upper().startswith("#EXTINF"):
            attrs, name = parse_extinf(line)
            headers = {}
            continue

        if line.startswith("#EXTVLCOPT:"):
            key, _, value = line[11:].partition("=")
            headers[key] = value
            continue

        if line.startswith("#"):
            continue

        seq = len(channels) + 1
        url = absolute_stream_url(line, base)
        number = (
            attrs.get("tvg-chno")
            or attrs.get("channel-number")
            or attrs.get("ch-number")
            or str(seq)
        )
        channel_name = name or attrs.get("tvg-name") or f"Channel {seq}"

        channels.append(Channel(str(number), channel_name, url, attrs.get("channelbox-id") or attrs.get("tvg-id", ""),
                                headers.get("http-user-agent", ""), headers.get("http-referrer", "")))
        headers = {}
        attrs = {}
        name = ""

    if not channels:
        raise RuntimeError(f"No playable channels found in {source}")

    return channels


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(path)


class MpvController:
    def __init__(self, extra_options=()):
        self.extra_options = list(extra_options)
        self.socket_path = Path(f"/tmp/channelbox-mpv-{os.getuid()}.sock")
        self.process = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running():
            return

        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

        cmd = [
            "mpv",
            "--fullscreen",
            "--no-border",
            "--no-osc",
            "--osd-bar=no",
            "--idle=yes",
            "--keep-open=yes",
            "--force-window=yes",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--terminal=no",
            "--really-quiet",
            "--hwdec=auto",
            f"--input-ipc-server={self.socket_path}",
            *self.extra_options,
        ]

        self.process = subprocess.Popen(cmd)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.running():
                raise RuntimeError("mpv exited before creating its IPC socket")
            if self.socket_path.exists():
                return
            time.sleep(0.1)

        raise TimeoutError("mpv IPC socket was not created")

    def command(self, command, expect_reply=False):
        payload = json.dumps({"command": command}).encode() + b"\n"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(self.socket_path))
            client.sendall(payload)

            if not expect_reply:
                return None

            stream = client.makefile("rb")
            while True:
                line = stream.readline()
                if not line:
                    raise ConnectionError("mpv closed the IPC connection")
                response = json.loads(line.decode())
                if "event" in response:
                    continue
                if response.get("error") not in {None, "success"}:
                    raise RuntimeError(f"mpv command failed: {response.get('error')}")
                return response.get("data")

    def tune(self, channel: Channel, osd_ms: int):
        options = {}
        if channel.user_agent:
            options["user-agent"] = channel.user_agent
        if channel.referrer:
            options["referrer"] = channel.referrer
        command = ["loadfile", channel.url, "replace"]
        if options:
            command += [-1, options]  # mpv 0.38+ per-file options, cleared after this file.
        self.command(command)
        self.command(["show-text", f"CH {channel.number}   {channel.name}", osd_ms])

    def get_volume(self) -> float:
        value = self.command(["get_property", "volume"], expect_reply=True)
        return float(value or 0)

    def volume_up(self, amount: int = 5):
        current = self.get_volume()
        new_volume = min(100, current + amount)
        self.command(["set_property", "volume", new_volume])
        self.command(["show-text", f"Volume: {int(new_volume)}%", 1200])

    def volume_down(self, amount: int = 5):
        current = self.get_volume()
        new_volume = max(0, current - amount)
        self.command(["set_property", "volume", new_volume])
        self.command(["show-text", f"Volume: {int(new_volume)}%", 1200])

    def toggle_mute(self):
        muted = bool(self.command(["get_property", "mute"], expect_reply=True))
        new_value = not muted
        self.command(["set_property", "mute", new_value])
        self.command(["show-text", "Muted" if new_value else "Unmuted", 1200])

    def idle_active(self):
        return bool(self.command(["get_property", "idle-active"], expect_reply=True))

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


class ChannelBox:
    def __init__(self, playlist: str, state_file: Path, *, favorites_file: Path | None = None,
                 input_backend: str = "auto", input_device: str = "flirc",
                 grab_input: bool = False, mpv_options=(), feed_url: str | None = None,
                 remote_socket: str | None = None, m3u_config: str | None = None,
                 tv_guide_epg: str | None = None):
        self.playlist_source = playlist
        self.channels = parse_m3u(playlist)
        self.state_file = state_file
        self.state = read_json(state_file, {})
        self.index = self._starting_index()
        self.previous_index = self._starting_previous_index()
        self.mpv = MpvController(mpv_options)
        self.input_backend = input_backend
        self.input_device = input_device
        self.grab_input = grab_input
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.feed_url = feed_url
        self.m3u_config = m3u_config
        self.pending_m3u = None
        self.remote_socket = remote_socket
        self.pending_feed = None
        self.update_thread = None
        self.last_tune = 0.0
        self.listener = None

        # Simple on-screen channel guide.
        self.guide_open = False
        self.guide_index = self.index
        self.guide_rows = 7
        self.guide_timer = None
        self.guide_timeout = 10.0
        self.guide_mode = "all"  # "all" or "favorites"

        # Favorites are stored separately from the last-channel state.
        self.favorites_file = (favorites_file or Path("~/.channelbox/favorites.json")).expanduser()
        self.favorite_urls = set(read_json(self.favorites_file, []))
        self.favorite_ids_file = self.favorites_file.with_suffix('.identities.json')
        self.favorite_ids = set(read_json(self.favorite_ids_file, []))
        self.favorite_ids.update(c.identity for c in self.channels if c.identity and c.url in self.favorite_urls)
        self.favorite_urls.update(c.url for c in self.channels if c.identity in self.favorite_ids)


        # Direct channel-number entry.
        self.number_buffer = ""
        self.number_timer = None
        self.number_timeout = 1.2
        self.tv_guide = None
        if tv_guide_epg:
            from channelbox_tvguide import TVGuide
            self.tv_guide = TVGuide(self, tv_guide_epg)

    def _starting_index(self):
        saved_id = self.state.get("id")
        if saved_id:
            for i, channel in enumerate(self.channels):
                if channel.identity == saved_id:
                    return i
        saved_url = self.state.get("url")
        if saved_url:
            for i, channel in enumerate(self.channels):
                if channel.url == saved_url:
                    return i
        return 0

    def _starting_previous_index(self):
        saved_id = self.state.get("previous_id")
        if saved_id:
            for i, channel in enumerate(self.channels):
                if channel.identity == saved_id:
                    return i
        saved_previous_url = self.state.get("previous_url")
        if saved_previous_url:
            for i, channel in enumerate(self.channels):
                if channel.url == saved_previous_url:
                    return i
        return None

    @property
    def current(self):
        return self.channels[self.index]

    def save_state(self):
        previous_channel = None
        if self.previous_index is not None and 0 <= self.previous_index < len(self.channels):
            previous_channel = self.channels[self.previous_index]

        write_json(
            self.state_file,
            {
                "id": self.current.identity,
                "previous_id": previous_channel.identity if previous_channel else None,
                "index": self.index,
                "number": self.current.number,
                "name": self.current.name,
                "url": self.current.url,
                "previous_index": self.previous_index,
                "previous_number": previous_channel.number if previous_channel else None,
                "previous_name": previous_channel.name if previous_channel else None,
                "previous_url": previous_channel.url if previous_channel else None,
            },
        )

    def tune_current(self):
        LOG.info("CH %s - %s", self.current.number, self.current.name)
        self.mpv.tune(self.current, 2500)
        self.last_tune = time.monotonic()
        self.save_state()

    def next_channel(self):
        with self.lock:
            self.previous_index = self.index
            self.index = (self.index + 1) % len(self.channels)
            self.tune_current()

    def previous_channel(self):
        with self.lock:
            self.previous_index = self.index
            self.index = (self.index - 1) % len(self.channels)
            self.tune_current()

    def last_channel(self):
        with self.lock:
            if self.previous_index is None:
                LOG.info("No previous channel recorded yet")
                try:
                    self.mpv.command(["show-text", "No previous channel", 1200])
                except Exception:
                    pass
                return

            if not (0 <= self.previous_index < len(self.channels)):
                LOG.info("Previous channel is no longer valid")
                try:
                    self.mpv.command(["show-text", "Previous channel unavailable", 1200])
                except Exception:
                    pass
                return

            self.index, self.previous_index = self.previous_index, self.index
            self.tune_current()

    def reload_playlist(self):
        with self.lock:
            old = self.current
            old_previous = None
            if self.previous_index is not None and 0 <= self.previous_index < len(self.channels):
                old_previous = self.channels[self.previous_index]

            new_channels = parse_m3u(self.playlist_source)

            new_index = None
            for i, channel in enumerate(new_channels):
                if (old.identity and channel.identity == old.identity) or channel.url == old.url:
                    new_index = i
                    break

            if new_index is None:
                for i, channel in enumerate(new_channels):
                    if channel.number == old.number:
                        new_index = i
                        break

            new_previous_index = None
            if old_previous is not None:
                for i, channel in enumerate(new_channels):
                    if (old_previous.identity and channel.identity == old_previous.identity) or channel.url == old_previous.url:
                        new_previous_index = i
                        break

                if new_previous_index is None:
                    for i, channel in enumerate(new_channels):
                        if channel.number == old_previous.number:
                            new_previous_index = i
                            break

            self.favorite_ids.update(c.identity for c in self.channels if c.identity and c.url in self.favorite_urls)
            self.favorite_urls.difference_update(c.url for c in self.channels if c.identity)
            self.favorite_urls.update(c.url for c in new_channels if c.identity in self.favorite_ids)
            self.save_favorites()
            self.channels = new_channels
            self.index = 0 if new_index is None else new_index
            self.previous_index = new_previous_index
            self.save_state()
            LOG.info("Reloaded playlist: %d channels", len(self.channels))

    def save_favorites(self):
        write_json(self.favorite_ids_file, sorted(self.favorite_ids))
        write_json(
            self.favorites_file,
            sorted(self.favorite_urls),
        )

    def is_favorite(self, channel):
        return channel.url in self.favorite_urls or bool(channel.identity and channel.identity in self.favorite_ids)

    def toggle_favorite(self):
        # If the guide is open, toggle the highlighted channel.
        # Otherwise toggle the currently playing channel.
        if self.guide_open and 0 <= self.guide_index < len(self.channels):
            channel = self.channels[self.guide_index]
        else:
            channel = self.current

        if self.is_favorite(channel):
            self.favorite_ids.discard(channel.identity)
            self.favorite_urls.discard(channel.url)
            message = f"Removed from Favorites: {channel.name}"
        else:
            self.favorite_urls.add(channel.url)
            if channel.identity:
                self.favorite_ids.add(channel.identity)
            message = f"Added to Favorites: {channel.name}"

        self.save_favorites()
        LOG.info(message)

        # If we're viewing Favorites and just removed the highlighted item,
        # move the highlight to another valid favorite before redrawing.
        if self.guide_open and self.guide_mode == "favorites":
            favorites = self.favorite_channels()

            if favorites:
                favorite_indexes = [index for index, _ in favorites]
                if self.guide_index not in favorite_indexes:
                    self.guide_index = favorite_indexes[0]

        try:
            if self.guide_open:
                self.render_guide()
            else:
                self.mpv.command(["show-text", message, 1800])
        except Exception:
            pass

    def favorite_channels(self):
        return [
            (index, channel)
            for index, channel in enumerate(self.channels)
            if self.is_favorite(channel)
        ]

    def toggle_guide_mode(self):
        if not self.guide_open:
            return

        if self.guide_mode == "all":
            self.guide_mode = "favorites"

            favorites = self.favorite_channels()
            if favorites:
                # Highlight the current channel if it is a favorite,
                # otherwise start at the first favorite.
                favorite_indexes = [index for index, _ in favorites]
                if self.index in favorite_indexes:
                    self.guide_index = self.index
                else:
                    self.guide_index = favorite_indexes[0]
        else:
            self.guide_mode = "all"
            self.guide_index = self.index

        self.render_guide()

    def _cancel_guide_timer(self):
        if self.guide_timer is not None:
            self.guide_timer.cancel()
            self.guide_timer = None

    def _schedule_guide_timeout(self):
        self._cancel_guide_timer()
        self.guide_timer = threading.Timer(
            self.guide_timeout,
            self.close_guide,
        )
        self.guide_timer.daemon = True
        self.guide_timer.start()

    def guide_playlist_status(self):
        """Describe the loaded lineup, not a pending download or favorites subset."""
        updated = "Awaiting first update" if self.feed_url or self.m3u_config else "Not recorded"
        if urllib.parse.urlparse(self.playlist_source).scheme not in {"http", "https"}:
            metadata = read_json(Path(self.playlist_source).with_suffix('.feed-state.json'), {})
            try:
                stamp = datetime.fromisoformat(metadata['generated_at'].replace('Z', '+00:00'))
                if stamp.tzinfo is not None:
                    updated = stamp.astimezone().strftime('%b %d, %Y  %I:%M %p %Z')
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
                pass
        return [f"Active channels: {len(self.channels):,}", f"Last updated: {updated}"]

    def _show_guide(self, lines):
        # Reserve the channel rows even for short favorites lists, then leave
        # a large gap before the yellow status section lower on the screen.
        lines = lines + [""] * max(0, 3 + self.guide_rows - len(lines))
        body = "\n".join(lines).replace('$', '$$')
        status = "\n".join(self.guide_playlist_status()).replace('$', '$$')
        # Only our style tags use ASS; channel names remain literal text.
        yellow = r'${osd-ass-cc/0}{\1c&H00FFFF&}${osd-ass-cc/1}'
        reset = r'${osd-ass-cc/0}{\r}${osd-ass-cc/1}'
        text = body + "\n" * 12 + yellow + status + reset
        self.mpv.command(["expand-properties", "show-text", text, int(self.guide_timeout * 1000)])
        self._schedule_guide_timeout()

    def render_guide(self):
        if not self.guide_open:
            return

        if self.guide_mode == "favorites":
            entries = self.favorite_channels()
            title = "FAVORITES"
        else:
            entries = list(enumerate(self.channels))
            title = "ALL CHANNELS"

        lines = [
            title,
            "☰: Close   C: All/Favorites   A: Add/Remove Favorite   Enter: Tune",
            "",
        ]

        if not entries:
            lines.append("No favorite channels yet.")
            lines.append("")
            lines.append("Press A while watching a channel to add it.")
            self._show_guide(lines)
            return

        available_indexes = [index for index, _ in entries]

        if self.guide_index not in available_indexes:
            self.guide_index = available_indexes[0]

        selected_position = available_indexes.index(self.guide_index)

        half = self.guide_rows // 2
        start_pos = max(0, selected_position - half)
        end_pos = min(len(entries), start_pos + self.guide_rows)

        if end_pos - start_pos < self.guide_rows:
            start_pos = max(0, end_pos - self.guide_rows)

        visible = entries[start_pos:end_pos]

        for index, channel in visible:
            marker = ">" if index == self.guide_index else " "
            favorite = "*" if self.is_favorite(channel) else " "
            lines.append(
                f"{marker}{favorite} {channel.number:<4} {channel.name}"
            )

        self._show_guide(lines)

    def open_guide(self):
        self.guide_open = True
        self.guide_mode = "all"
        self.guide_index = self.index
        self.render_guide()

    def close_guide(self):
        self._cancel_guide_timer()
        self.guide_open = False
        try:
            self.mpv.command(["show-text", "", 1])
        except Exception:
            pass

    def toggle_guide(self):
        if self.guide_open:
            self.close_guide()
        else:
            self.open_guide()

    def guide_up(self):
        if not self.guide_open:
            return

        if self.guide_mode == "favorites":
            entries = self.favorite_channels()
        else:
            entries = list(enumerate(self.channels))

        if not entries:
            return

        indexes = [index for index, _ in entries]

        if self.guide_index not in indexes:
            self.guide_index = indexes[0]
        else:
            pos = indexes.index(self.guide_index)
            self.guide_index = indexes[(pos - 1) % len(indexes)]

        self.render_guide()

    def guide_down(self):
        if not self.guide_open:
            return

        if self.guide_mode == "favorites":
            entries = self.favorite_channels()
        else:
            entries = list(enumerate(self.channels))

        if not entries:
            return

        indexes = [index for index, _ in entries]

        if self.guide_index not in indexes:
            self.guide_index = indexes[0]
        else:
            pos = indexes.index(self.guide_index)
            self.guide_index = indexes[(pos + 1) % len(indexes)]

        self.render_guide()

    def tune_guide_selection(self):
        if not self.guide_open:
            return

        if self.guide_mode == "favorites" and not self.favorite_channels():
            return

        selected = self.guide_index
        self.close_guide()

        with self.lock:
            if selected != self.index:
                self.previous_index = self.index
            self.index = selected
            self.tune_current()

    def _cancel_number_timer(self):
        if self.number_timer is not None:
            self.number_timer.cancel()
            self.number_timer = None

    def _commit_channel_number(self):
        with self.lock:
            entered = self.number_buffer
            self.number_buffer = ""
            self.number_timer = None

            if not entered:
                return

            # Prefer an exact match against the playlist's displayed channel number.
            for i, channel in enumerate(self.channels):
                if str(channel.number) == entered:
                    if i != self.index:
                        self.previous_index = self.index
                    self.index = i
                    self.tune_current()
                    return

            LOG.info("No channel numbered %s", entered)
            try:
                self.mpv.command(["show-text", f"No channel {entered}", 1200])
            except Exception:
                pass

    def enter_channel_digit(self, digit: str):
        with self.lock:
            # Avoid absurdly long accidental input.
            if len(self.number_buffer) >= max(4, max(len(c.number) for c in self.channels)):
                self.number_buffer = ""

            self.number_buffer += digit

            self._cancel_number_timer()
            self.number_timer = threading.Timer(
                self.number_timeout,
                self._commit_channel_number,
            )
            self.number_timer.daemon = True
            self.number_timer.start()

            LOG.info("Channel entry: %s", self.number_buffer)
            try:
                self.mpv.command(
                    ["show-text", f"CH {self.number_buffer}_", 1200]
                )
            except Exception:
                pass

    def on_press(self, key: str):
        with self.lock:
            self._on_press(key)

    def _on_press(self, key: str):
        # Both platform adapters emit the same symbolic key names.
        LOG.debug("Key received: %s", key)
        if self.tv_guide:
            try:
                if self.tv_guide.handle_key(key):
                    return
            except Exception as error:
                self.tv_guide.close()
                LOG.warning("TV guide closed (%s); playback continues", type(error).__name__)
                return

        if key == "up":
            if self.guide_open:
                self.guide_up()
            else:
                self.next_channel()
            return

        if key == "down":
            if self.guide_open:
                self.guide_down()
            else:
                self.previous_channel()
            return

        if key == "enter":
            if self.guide_open:
                self.tune_guide_selection()
            return

        if key == "f5":
            self.reload_playlist()
            if self.guide_open:
                self.guide_index = min(self.guide_index, len(self.channels) - 1)
                self.render_guide()
            return

        if key == "esc":
            if self.guide_open:
                self.close_guide()
            else:
                self.stop_event.set()
            return

        if len(key) == 1:
            char = key.lower()

            if char.isdigit():
                self.enter_channel_digit(char)
            elif char == "v":
                self.mpv.volume_up()
            elif char == "x":
                self.mpv.volume_down()
            elif char == "m":
                self.mpv.toggle_mute()
            elif char == "p":
                self.mpv.command(["cycle", "pause"])
            elif char == "i":
                self.mpv.command(
                    ["show-text", f"CH {self.current.number}   {self.current.name}", 2500]
                )
            elif char in ("b", "g"):
                self.toggle_guide()
            elif char in ("c", "f"):
                if self.guide_open:
                    self.toggle_guide_mode()
            elif char == "a":
                self.toggle_favorite()
            elif char == "d":
                self.last_channel()

    def _fetch_updates(self):
        from channelbox_feed import download, validate_feed
        while not self.stop_event.is_set():
            try:
                feed = validate_feed(json.loads(download(self.feed_url)))
                with self.lock:
                    self.pending_feed = feed
                delay = 86400
            except Exception as error:
                LOG.warning("Playlist download failed (%s); using local copy", type(error).__name__)
                delay = 3600
            if self.stop_event.wait(delay):
                return

    def _fetch_m3u_updates(self):
        from channelbox_m3u import download_config, prepare
        target = Path(self.playlist_source)
        while not self.stop_event.is_set():
            try:
                raw = download_config(self.m3u_config)
                metadata = read_json(target.with_suffix('.feed-state.json'), {})
                prepared = prepare(raw, metadata)
                with self.lock:
                    self.pending_m3u = prepared
                delay = 86400
            except Exception as error:
                LOG.warning("Private playlist refresh failed (%s); using local copy", type(error).__name__)
                delay = 3600
            if self.stop_event.wait(delay):
                return

    def apply_pending_m3u(self):
        from channelbox_feed import atomic_write
        with self.lock:
            value = self.pending_m3u
            if value is None or self.guide_open or self.number_buffer:
                return False
            # The registry includes retired channels, so check actual M3U entries.
            identities = set(re.findall(r'channelbox-id="([^"]+)"', value['text']))
            keep = [self.current]
            if self.previous_index is not None:
                keep.append(self.channels[self.previous_index])
            if any(c.identity not in identities for c in keep):
                return False
            target = Path(self.playlist_source)
            content = value['text'].encode('utf-8')
            if target.read_bytes() != content:
                atomic_write(target.with_suffix('.m3u.previous'), target.read_bytes())
                self.save_favorites()
                self.save_state()
                atomic_write(target, content)
                self.reload_playlist()
            metadata = {k: v for k, v in value.items() if k != 'text'}
            atomic_write(target.with_suffix('.feed-state.json'), json.dumps(metadata))
            self.pending_m3u = None
            LOG.info("Private playlist refreshed: %d entries", len(self.channels))
            return True

    def apply_pending_feed(self):
        from channelbox_feed import atomic_write, check_transition, render_m3u
        with self.lock:
            feed = self.pending_feed
            if feed is None or self.guide_open or self.number_buffer:
                return False
            target = Path(self.playlist_source)
            metadata_file = target.with_suffix('.feed-state.json')
            metadata = read_json(metadata_file, {})
            if metadata.get('generated_at', '') >= feed['generated_at']:
                self.pending_feed = None
                return False
            seed = {c.identity: c.number for c in self.channels if c.identity}
            registry = check_transition(feed, metadata.get('numbers', seed), metadata.get('count', 0))
            # Do not interrupt viewing or silently change the current/previous channel.
            identities = {c['id'] for c in feed['channels']}
            keep = [self.current]
            if self.previous_index is not None:
                keep.append(self.channels[self.previous_index])
            for c in keep:
                if c.identity and c.identity not in identities:
                    return False
            content = render_m3u(feed['channels'])
            atomic_write(target.with_suffix('.m3u.previous'), target.read_bytes())
            # Persist favorite identities and current identities before replacing the file.
            self.save_favorites()
            self.save_state()
            atomic_write(target, content)
            self.reload_playlist()
            atomic_write(metadata_file, json.dumps({'numbers': registry, 'count': len(feed['channels']),
                                                   'generated_at': feed['generated_at']}))
            self.pending_feed = None
            LOG.info("Installed hosted playlist: %d channels", len(self.channels))
            return True

    def stop(self, *_):
        self.stop_event.set()

    def run(self):
        remote = None
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        LOG.info("Loaded %d channels", len(self.channels))
        LOG.info(
            "UP=next  DOWN=previous  D=last channel  0-9=direct channel  "
            "V=vol+  X=vol-  M=mute  P=play/pause  I=info  "
            "B=guide  C=all/favorites in guide  A=add/remove favorite  "
            "ENTER=select  F5=reload playlist  ESC=close/quit"
        )

        try:
            self.listener = create_listener(
                self.input_backend, self.on_press, self.stop_event,
                selector=self.input_device, grab=self.grab_input,
            )
            self.mpv.start()
            self.tune_current()
            self.listener.start()
            if self.remote_socket:
                from channelbox_remote import RemoteServer
                remote = RemoteServer(self, self.remote_socket)
                remote.start()
            if self.feed_url:
                self.update_thread = threading.Thread(target=self._fetch_updates, daemon=True)
                self.update_thread.start()
            elif self.m3u_config:
                self.update_thread = threading.Thread(target=self._fetch_m3u_updates, daemon=True)
                self.update_thread.start()
            while not self.stop_event.wait(2):
                if self.tv_guide:
                    try:
                        self.tv_guide.tick()
                    except Exception as error:
                        self.tv_guide.close()
                        LOG.warning("TV guide closed (%s); playback continues", type(error).__name__)
                try:
                    self.apply_pending_feed()
                    self.apply_pending_m3u()
                except Exception as error:
                    self.pending_feed = None
                    self.pending_m3u = None
                    LOG.warning("Hosted playlist rejected (%s); keeping local lineup", type(error).__name__)
                if not self.listener.is_alive():
                    raise RuntimeError("Input listener stopped unexpectedly")
                if not self.mpv.running():
                    self.mpv.start()
                    self.tune_current()
                    continue

                if time.monotonic() - self.last_tune < 6:
                    continue

                try:
                    if self.mpv.idle_active():
                        self.tune_current()
                except Exception:
                    LOG.exception("mpv health check failed")
        finally:
            self.stop_event.set()
            if remote:
                remote.close()
            self._cancel_number_timer()
            self._cancel_guide_timer()
            try:
                if self.listener:
                    self.listener.stop()
                    if self.listener.ident is not None:
                        self.listener.join(timeout=2)
            finally:
                if self.tv_guide:
                    self.tv_guide.close()
                self.mpv.stop()


def main():
    parser = argparse.ArgumentParser(description="ChannelBox television controller for macOS and Linux")
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--feed-url", help="HTTPS curated JSON feed; downloads on start and daily with local fallback")
    parser.add_argument("--m3u-config", help="Private JSON file with HTTPS playlist_url; refresh on start and daily")
    parser.add_argument("--remote-socket", help="Optional private Unix socket for SSH remote control")
    parser.add_argument("--tv-guide-epg", help="Enable the separate H-key TV guide with this local EPG SQLite cache")
    parser.add_argument(
        "--state-file",
        default="~/.channelbox/state.json",
    )
    parser.add_argument("--favorites-file", default="~/.channelbox/favorites.json")
    parser.add_argument("--input-backend", choices=["auto", "macos", "linux"], default="auto")
    parser.add_argument("--input-device", default="flirc", help="Linux device name substring or event/by-id path; auto reads all keyboards")
    parser.add_argument("--grab-input", action="store_true", help="Linux: exclusively capture the selected remote (cannot use auto)")
    parser.add_argument("--mpv-option", action="append", default=[], help="Repeat as --mpv-option=--option=value")
    parser.add_argument("--list-channels", action="store_true", help="List number/name only without playback or input dependencies")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.list_channels:
        for channel in parse_m3u(args.playlist):
            print(f"{channel.number}\t{channel.name}")
        return

    if args.feed_url and args.m3u_config:
        parser.error("Choose either --feed-url or --m3u-config")
    if args.m3u_config and urllib.parse.urlparse(args.playlist).scheme in {"http", "https"}:
        parser.error("--m3u-config requires a writable local --playlist cache")
    if args.feed_url:
        from channelbox_feed import web_url
        web_url(args.feed_url, https=True)
        if urllib.parse.urlparse(args.playlist).scheme in {"http", "https"}:
            parser.error("--feed-url requires a writable local --playlist")

    ChannelBox(
        args.playlist,
        Path(args.state_file).expanduser(),
        favorites_file=Path(args.favorites_file).expanduser(),
        input_backend=args.input_backend,
        input_device=args.input_device,
        grab_input=args.grab_input,
        mpv_options=args.mpv_option,
        feed_url=args.feed_url,
        remote_socket=args.remote_socket,
        m3u_config=args.m3u_config,
        tv_guide_epg=args.tv_guide_epg,
    ).run()


if __name__ == "__main__":
    main()
