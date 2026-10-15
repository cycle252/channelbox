import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from channelbox import ChannelBox, MpvController, parse_m3u
from channelbox_input import LinuxListener, linux_key, macos_key
from tools.recover_playlist import recover


class QuietTimer:
    def __init__(self, *args, **kwargs):
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


class FakePlayer:
    def __init__(self):
        self.tuned = []
        self.commands = []

    def tune(self, channel, duration):
        self.tuned.append(channel.number)

    def command(self, command, **kwargs):
        self.commands.append(command)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.playlist = self.root / "channels.m3u"
        self.write_playlist()
        self.timer_patch = patch("channelbox.threading.Timer", QuietTimer)
        self.timer_patch.start()
        self.box = self.new_box()

    def tearDown(self):
        self.timer_patch.stop()
        self.temp.cleanup()

    def write_playlist(self, prefix=""):
        self.playlist.write_text(prefix + '#EXTM3U\n'
            '#EXTINF:-1 tvg-chno="101" group-title="News",News\nhttps://example.invalid/news\n'
            '#EXTINF:-1 channel-number="201",Kids\nhttps://example.invalid/kids\n'
            '#EXTINF:-1 ch-number="111",Movie\nhttps://example.invalid/movie\n')

    def new_box(self):
        box = ChannelBox(str(self.playlist), self.root / "state.json",
                         favorites_file=self.root / "favorites.json")
        box.mpv = FakePlayer()
        return box

    def test_number_metadata_and_relative_stream_paths(self):
        channels = parse_m3u(str(self.playlist))
        self.assertEqual([c.number for c in channels], ["101", "201", "111"])
        self.assertEqual([c.name for c in channels], ["News", "Kids", "Movie"])
        self.playlist.write_text('#EXTM3U\n#EXTINF:-1,Local clip\nclips/test.mp4\n')
        channel = parse_m3u(str(self.playlist))[0]
        self.assertEqual(channel.number, "1")
        self.assertEqual(channel.url, str((self.root / "clips/test.mp4").resolve()))

    def test_empty_playlist_is_rejected(self):
        self.playlist.write_text("#EXTM3U\n")
        with self.assertRaises(RuntimeError):
            parse_m3u(str(self.playlist))

    @unittest.skipUnless(hasattr(time, 'tzset'), 'Requires local timezone control')
    def test_guide_shows_loaded_update_time_and_total_even_in_empty_favorites(self):
        metadata = self.playlist.with_suffix('.feed-state.json')
        metadata.write_text(json.dumps({'generated_at': '2026-10-13T22:25:24+00:00'}))
        self.box.pending_feed = {'generated_at': '2026-10-14T22:25:24+00:00'}
        try:
            with patch.dict(os.environ, {'TZ': 'America/Chicago'}):
                time.tzset()
                self.box.open_guide()
                self.box.toggle_guide_mode()
                shown = self.box.mpv.commands[-1][-2]
                self.assertIn('Active channels: 3', shown)
                self.assertIn('Last updated: Oct 13, 2026  05:25 PM CDT', shown)
                self.assertIn('No favorite channels yet.', shown)
                self.assertNotIn('Oct 14', shown)
        finally:
            time.tzset()

    def test_guide_without_valid_timestamp_does_not_invent_an_update(self):
        self.assertIn('Last updated: Not recorded', self.box.guide_playlist_status())
        self.box.feed_url = 'https://example.invalid/channels.json'
        for metadata in [{}, [], {'generated_at': None}, {'generated_at': 'bad'},
                         {'generated_at': '2026-10-13T22:25:24'}]:
            with self.subTest(metadata=metadata):
                self.playlist.with_suffix('.feed-state.json').write_text(json.dumps(metadata))
                self.assertIn('Last updated: Awaiting first update', self.box.guide_playlist_status())

    def test_wrap_and_last_channel_swap_survive_restart(self):
        self.box.previous_channel()
        self.assertEqual(self.box.current.number, "111")
        self.box.next_channel()
        self.assertEqual(self.box.current.number, "101")
        self.box.last_channel()
        self.assertEqual(self.box.current.number, "111")
        restored = self.new_box()
        self.assertEqual(restored.current.number, "111")
        restored.last_channel()
        self.assertEqual(restored.current.number, "101")

    def test_numeric_selection_uses_exact_number_not_index(self):
        for digit in "201":
            self.box.on_press(digit)
        self.box._commit_channel_number()
        self.assertEqual(self.box.current.number, "201")
        self.assertEqual(self.box.previous_index, 0)
        for digit in "999":
            self.box.on_press(digit)
        self.box._commit_channel_number()
        self.assertEqual(self.box.current.number, "201")
        self.assertIn(["show-text", "No channel 999", 1200], self.box.mpv.commands)

    def test_repeated_numeric_digits_are_retained(self):
        for digit in "111":
            self.box.on_press(digit)
        self.box._commit_channel_number()
        self.assertEqual(self.box.current.number, "111")

    def test_favorites_filter_tuning_and_persistence(self):
        self.box.toggle_favorite()
        restored = self.new_box()
        self.assertTrue(restored.is_favorite(restored.current))
        self.box.open_guide()
        self.box.guide_down()
        self.box.toggle_favorite()
        self.box.toggle_guide_mode()
        self.box.guide_down()
        self.box.tune_guide_selection()
        self.assertEqual(self.box.current.number, "201")
        self.assertFalse(self.box.guide_open)
        self.assertEqual(len(json.loads((self.root / "favorites.json").read_text())), 2)

    def test_empty_favorites_enter_does_not_tune(self):
        self.box.open_guide()
        self.box.toggle_guide_mode()
        self.box.tune_guide_selection()
        self.assertEqual(self.box.mpv.tuned, [])
        self.assertTrue(self.box.guide_open)

    def test_reload_retains_current_and_previous_by_url(self):
        self.box.next_channel()
        self.write_playlist('#EXTINF:-1 tvg-chno="50",New\nhttps://example.invalid/new\n')
        self.box.reload_playlist()
        self.assertEqual(self.box.current.number, "201")
        self.assertEqual(self.box.index, 2)
        self.box.last_channel()
        self.assertEqual(self.box.current.number, "101")

    def test_missing_previous_url_is_safe(self):
        (self.root / "state.json").write_text(json.dumps({
            "url": "https://example.invalid/kids", "previous_url": "https://example.invalid/removed"}))
        restored = self.new_box()
        self.assertEqual(restored.current.number, "201")
        restored.last_channel()
        self.assertEqual(restored.current.number, "201")

    def test_escape_closes_guide_before_quitting(self):
        self.box.on_press("g")
        self.box.on_press("esc")
        self.assertFalse(self.box.guide_open)
        self.assertFalse(self.box.stop_event.is_set())
        self.box.on_press("esc")
        self.assertTrue(self.box.stop_event.is_set())

    def test_lettered_remote_guide_does_not_change_channel_or_volume(self):
        self.box.mpv.volume_down = Mock()
        self.box.next_channel()
        self.box.on_press(linux_key("KEY_A", 1))
        self.assertTrue(self.box.is_favorite(self.box.current))
        self.box.on_press(linux_key("KEY_B", 1))
        self.assertTrue(self.box.guide_open)
        self.assertEqual(self.box.current.number, "201")
        self.box.on_press(linux_key("KEY_C", 1))
        self.assertEqual(self.box.guide_mode, "favorites")
        self.box.mpv.volume_down.assert_not_called()
        self.assertIn("☰: Close   C: All/Favorites", self.box.mpv.commands[-1][-2])
        self.box.on_press(linux_key("KEY_B", 1))
        self.assertFalse(self.box.guide_open)
        self.box.on_press(linux_key("KEY_D", 1))
        self.assertEqual(self.box.current.number, "101")
        self.box.on_press(linux_key("KEY_VOLUMEDOWN", 1))
        self.box.on_press(linux_key("KEY_X", 1))
        self.assertEqual(self.box.mpv.volume_down.call_count, 2)


class PlayerTests(unittest.TestCase):
    def test_volume_is_clamped(self):
        player = MpvController()
        player.command = Mock(side_effect=[98, None, None, 2, None, None])
        player.volume_up()
        player.volume_down()
        self.assertIn(unittest.mock.call(["set_property", "volume", 100]), player.command.call_args_list)
        self.assertIn(unittest.mock.call(["set_property", "volume", 0]), player.command.call_args_list)

    def test_ipc_eof_and_error_are_reported(self):
        for payload, exception in [(b"", ConnectionError),
                                   (b'{"error":"property unavailable"}\n', RuntimeError)]:
            with self.subTest(payload=payload), patch("channelbox.socket.socket") as sock:
                sock.return_value.__enter__.return_value.makefile.return_value = io.BytesIO(payload)
                with self.assertRaises(exception):
                    MpvController().command(["get_property", "volume"], expect_reply=True)

    def test_ipc_skips_events_and_reads_data(self):
        with patch("channelbox.socket.socket") as sock:
            sock.return_value.__enter__.return_value.makefile.return_value = io.BytesIO(
                b'{"event":"start-file"}\n{"error":"success","data":50}\n')
            self.assertEqual(MpvController().command(["get_property", "volume"], expect_reply=True), 50)


class InputTests(unittest.TestCase):
    def test_linux_releases_and_held_toggles_do_not_act(self):
        for name in ["KEY_1", "KEY_A", "KEY_B", "KEY_C", "KEY_D", "KEY_M", "KEY_G", "KEY_ENTER"]:
            self.assertIsNone(linux_key(name, 0))
            self.assertIsNone(linux_key(name, 2))
            self.assertIsNotNone(linux_key(name, 1))
        self.assertEqual(linux_key("KEY_CHANNELUP", 2), "up")
        self.assertEqual(linux_key("KEY_VOLUMEDOWN", 2), "x")

    def test_mac_mapping_uses_same_actions(self):
        keyboard = SimpleNamespace(Key=SimpleNamespace(**{n: object() for n in ["up", "down", "enter", "esc", "f5"]}))
        self.assertEqual(macos_key(keyboard.Key.up, keyboard), "up")
        self.assertEqual(macos_key(SimpleNamespace(char="G"), keyboard), "g")

    def test_linux_dispatch_and_repeat_throttle(self):
        listener = object.__new__(LinuxListener)
        listener.evdev = SimpleNamespace(ecodes=SimpleNamespace(EV_KEY=1, KEY={1: "KEY_1", 2: "KEY_UP"}))
        listener.callback = Mock()
        listener.last_repeat = {}
        for value in [1, 2, 0, 1]:
            listener.handle_event(SimpleNamespace(type=1, code=1, value=value))
        self.assertEqual(listener.callback.call_count, 2)
        with patch("channelbox_input.time.monotonic", side_effect=[1, 1.1, 1.3]):
            for value in [1, 2, 2]:
                listener.handle_event(SimpleNamespace(type=1, code=2, value=value))
        self.assertEqual(listener.callback.call_count, 4)

    def test_auto_selector_cannot_grab_all_keyboards(self):
        with self.assertRaises(ValueError):
            LinuxListener(Mock(), threading.Event(), selector="auto", grab=True)

    def test_receiver_discovery_grabs_only_match_and_closes_on_disconnect(self):
        receiver = Mock(name="receiver")
        receiver.name = "flirc keyboard"
        receiver.capabilities.return_value = {1: [103]}
        other = Mock(name="maintenance keyboard")
        other.name = "USB Keyboard"
        other.capabilities.return_value = {1: [103]}
        evdev = SimpleNamespace(
            ecodes=SimpleNamespace(EV_KEY=1, KEY_UP=103),
            list_devices=Mock(side_effect=[["/dev/input/event1", "/dev/input/event2"], []]),
            InputDevice=Mock(side_effect=[receiver, other]),
        )
        with patch.dict("sys.modules", {"evdev": evdev}):
            listener = LinuxListener(Mock(), threading.Event(), selector="flirc", grab=True)
            listener.rescan()
            receiver.grab.assert_called_once()
            other.grab.assert_not_called()
            other.close.assert_called_once()
            listener.rescan()
            receiver.close.assert_called_once()
            self.assertEqual(listener.devices, {})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backup = self.root / "backup"
        (self.backup / "data").mkdir(parents=True)
        (self.backup / "data/threadfin.m3u").write_text(
            '#EXTM3U\n#EXTINF:-1 channelID="x-ID.0" tvg-chno="101" group-title="News",News\nhttp://proxy.invalid:34400/stream/0\n')
        (self.backup / "xepg.json").write_text(json.dumps({"x-ID.0": {"url": "https://upstream.invalid/live"}}))
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "favorites.json").write_text('["http://proxy.invalid:34400/stream/0"]')
        (self.state / "state.json").write_text('{"url":"http://proxy.invalid:34400/stream/0","previous_url":"https://removed.invalid/live"}')
        self.output = self.root / "private"

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_id_recovery_keeps_metadata_and_remaps_state(self):
        summary = recover(self.backup, self.output, self.state)
        playlist = (self.output / "channels.m3u").read_text()
        self.assertIn('tvg-chno="101" group-title="News",News', playlist)
        self.assertIn("https://upstream.invalid/live", playlist)
        self.assertNotIn("http://proxy.invalid", playlist)
        self.assertEqual(summary["favorites_remapped"], 1)
        self.assertEqual(summary["state_urls_unmatched"], 1)
        self.assertEqual(json.loads((self.output / "state.json").read_text())["url"], "https://upstream.invalid/live")
        self.assertEqual((self.output / "original-state/state.json").read_bytes(), (self.state / "state.json").read_bytes())
        with self.assertRaises(FileExistsError):
            recover(self.backup, self.output, self.state)

    def test_missing_mapping_does_not_write_partial_playlist(self):
        (self.backup / "xepg.json").write_text("{}")
        with self.assertRaises(ValueError):
            recover(self.backup, self.output)
        self.assertFalse(self.output.exists())

    def test_upstream_proxy_address_is_rejected(self):
        (self.backup / "xepg.json").write_text(json.dumps({"x-ID.0": {"url": "http://proxy.invalid:34400/stream/0"}}))
        with self.assertRaises(ValueError):
            recover(self.backup, self.output)

    def test_omitted_current_channel_restored_by_one_exact_name(self):
        (self.state / "state.json").write_text(json.dumps({
            "url": "http://proxy.invalid:34400/stream/old", "name": "Remembered", "number": "939",
            "previous_url": "http://proxy.invalid:34400/stream/0"}))
        source = self.root / "upstream.m3u"
        source.write_text('#EXTM3U\n#EXTINF:-1 tvg-chno="" group-title="Comedy",Remembered\nhttps://upstream.invalid/remembered\n')
        summary = recover(self.backup, self.output, self.state, source)
        self.assertEqual(summary["channels"], 2)
        self.assertEqual(summary["state_urls_remapped"], 2)
        state = json.loads((self.output / "state.json").read_text())
        self.assertEqual(state["url"], "https://upstream.invalid/remembered")
        channels = parse_m3u(str(self.output / "channels.m3u"))
        self.assertEqual(channels[-1].number, "939")

    def test_ambiguous_remembered_channel_does_not_write_output(self):
        (self.state / "state.json").write_text(json.dumps({
            "url": "http://proxy.invalid:34400/stream/old", "name": "Remembered", "number": "939"}))
        source = self.root / "upstream.m3u"
        source.write_text('#EXTINF:-1,Remembered\nhttps://upstream.invalid/one\n#EXTINF:-1,Remembered\nhttps://upstream.invalid/two\n')
        with self.assertRaises(ValueError):
            recover(self.backup, self.output, self.state, source)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
