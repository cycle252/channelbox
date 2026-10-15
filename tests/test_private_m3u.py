import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from channelbox import ChannelBox, parse_m3u
from channelbox_m3u import prepare, download_config, MAX_BYTES


def source(rows):
    return ('#EXTM3U url-tvg="https://example.invalid/epg.xml"\n' + ''.join(
        f'#EXTINF:-1 tvg-id="shared.epg" tvg-chno="{number}" group-title="TV",{name}\n'
        f'#EXTVLCOPT:http-user-agent=Example\nhttps://example.invalid/{name}\n'
        for number, name in rows)).encode()


class PrivateM3UTests(unittest.TestCase):
    def test_large_playlist_retains_five_digit_numbers(self):
        value = prepare(source([(str(n), f'Channel{n}') for n in range(1, 10002)]))
        self.assertEqual(value['count'], 10001)
        self.assertEqual(value['repaired'], 0)
        self.assertIn('tvg-chno="10001"', value['text'])

    def test_missing_number_uses_existing_parser_order_and_cuid_survives_url_change(self):
        raw = b'#EXTM3U\n#EXTINF:-1 cuid="one",First\nhttps://example.invalid/old\n#EXTINF:-1 cuid="two" tvg-chno="null",Second\nhttps://example.invalid/2\n'
        value = prepare(raw)
        self.assertEqual(value['repaired'], 1)
        self.assertEqual(set(value['numbers'].values()), {'1', '2'})
        updated = prepare(raw.replace(b'/old', b'/new'), value)
        self.assertEqual(value['numbers'], updated['numbers'])

    def test_preserves_source_numbers_epg_and_separate_playback_ids(self):
        value = prepare(source([('2', 'A'), ('null', 'B'), ('4', 'C')]))
        self.assertEqual(value['repaired'], 1)
        self.assertIn('url-tvg="https://example.invalid/epg.xml"', value['text'])
        self.assertEqual(value['text'].count('tvg-id="shared.epg"'), 3)
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'channels.m3u'; p.write_text(value['text'])
            channels = parse_m3u(str(p))
        self.assertEqual([c.number for c in channels], ['2', '1', '4'])
        self.assertEqual(len({c.identity for c in channels}), 3)
        self.assertEqual(channels[0].user_agent, 'Example')
        again = prepare(source([('4', 'C'), ('null', 'B'), ('2', 'A')]), value)
        self.assertEqual(again['numbers'], value['numbers'])

    def test_rejects_html_truncated_unsafe_duplicate_and_reused_numbers(self):
        for raw in [b'<html>login</html>', b'#EXTM3U\n#EXTINF:-1,Incomplete\n',
                    source([('1', 'A'), ('1', 'B')]),
                    b'#EXTM3U\n#EXTINF:-1,X\nfile:///etc/passwd\n']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                prepare(raw)
        old = prepare(source([('1', 'A'), ('2', 'B'), ('3', 'C')]))
        with self.assertRaises(ValueError):
            prepare(source([('1', 'A')]), old)
        with self.assertRaises(ValueError):
            prepare(source([('1', 'Replacement'), ('2', 'B'), ('3', 'C')]), old)

    def test_refresh_defers_guide_and_missing_current_and_keeps_favorites(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); p = root / 'channels.m3u'
            first = prepare(source([('1', 'A'), ('2', 'B'), ('3', 'C')]))
            p.write_text(first['text'])
            box = ChannelBox(str(p), root / 'state.json', favorites_file=root / 'fav.json')
            box.mpv = Mock()
            box.previous_index = 1
            box.favorite_urls.add(box.current.url)
            update = prepare(source([('1', 'A'), ('2', 'B'), ('3', 'C'), ('4', 'D')]), first)
            box.pending_m3u = update; box.guide_open = True
            self.assertFalse(box.apply_pending_m3u())
            self.assertEqual(p.read_text(), first['text'])
            box.guide_open = False
            self.assertTrue(box.apply_pending_m3u())
            self.assertEqual(box.current.name, 'A')
            self.assertEqual(box.channels[box.previous_index].name, 'B')
            self.assertTrue(box.is_favorite(box.current))
            box.mpv.tune.assert_not_called()
            self.assertEqual(p.with_suffix('.m3u.previous').read_text(), first['text'])
            box.pending_m3u = prepare(source([('2', 'B'), ('3', 'C'), ('4', 'D')]), update)
            self.assertFalse(box.apply_pending_m3u())
            self.assertEqual(len(box.channels), 4)

    def test_download_is_bounded_and_failure_keeps_cached_file(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); cfg = root / 'config.json'
            cfg.write_text(json.dumps({'playlist_url': 'https://example.invalid/playlist'}))
            response = Mock(url='https://example.invalid/playlist')
            response.read.return_value = b'x' * (MAX_BYTES + 1)
            context = Mock(); context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            with patch('channelbox_m3u.urllib.request.urlopen', return_value=context):
                with self.assertRaises(ValueError):
                    download_config(cfg)
            response.read.assert_called_once_with(MAX_BYTES + 1)
            p = root / 'channels.m3u'; p.write_bytes(source([('1', 'A')]))
            box = ChannelBox(str(p), root / 'state.json', favorites_file=root / 'fav.json', m3u_config=str(cfg))
            box.stop_event = Mock(); box.stop_event.is_set.return_value = False
            box.stop_event.wait.return_value = True
            before = p.read_bytes()
            with patch('channelbox_m3u.download_config', side_effect=TimeoutError):
                box._fetch_m3u_updates()
            self.assertEqual(p.read_bytes(), before)
            self.assertIsNone(box.pending_m3u)
            box.stop_event.wait.assert_called_once_with(3600)
