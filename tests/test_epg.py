import gzip
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from channelbox_epg import EpgStore, LimitedXML, xmltv_time
from channelbox_web import Catalog

NOW = xmltv_time('20261015142500 +0000')


def xml():
    return b'''<?xml version="1.0"?><tv>
      <channel id="guide.news"><display-name>News</display-name></channel>
      <programme channel="guide.news" start="20261015090000 -0500" stop="20261015100000 -0500"><title>Morning &amp; More</title></programme>
      <programme channel="guide.news" start="20261015100000 -0500" stop="20261015110000 -0500"><title>Next programme</title></programme>
      <programme channel="unmatched.id" start="20261015140000 +0000" stop="20261015150000 +0000"><title>Wrong station</title></programme>
    </tv>'''


class EpgTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'guide.xml.gz'
        self.source.write_bytes(gzip.compress(xml()))
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'epg_url': 'https://example.invalid/private-guide.gz'}))
        self.epg = EpgStore(self.root / 'epg.sqlite', self.config)

    def tearDown(self):
        self.epg.close()
        self.temp.cleanup()

    def test_offsets_dst_and_programme_boundaries(self):
        self.assertEqual(xmltv_time('20261015090000 -0500'), xmltv_time('20261015140000 +0000'))
        self.assertEqual(xmltv_time('20261015193000 +0530'), xmltv_time('20261015140000Z'))
        self.assertEqual(xmltv_time('20261101013000 -0600') - xmltv_time('20261101013000 -0500'), 3600)
        with self.assertRaises(ValueError):
            xmltv_time('20261315140000 +0000')
        self.epg.import_file(self.source, {'guide.news'}, now=NOW)
        value = self.epg.lookup({'guide.news'}, now=NOW)['guide.news']
        self.assertEqual(value['now']['title'], 'Morning & More')
        self.assertEqual(value['next']['title'], 'Next programme')
        boundary = xmltv_time('20261015150000 +0000')
        self.assertEqual(self.epg.lookup({'guide.news'}, now=boundary)['guide.news']['now']['title'], 'Next programme')
        self.assertNotIn('now', self.epg.lookup({'guide.news'}, now=boundary + 3600).get('guide.news', {}))

    def test_gaps_and_missing_ids_do_not_invent_current_programmes(self):
        self.epg.import_file(self.source, {'guide.news'}, now=NOW)
        value = self.epg.lookup({'guide.news', 'no-id'}, now=xmltv_time('20261015133000 +0000'))
        self.assertNotIn('now', value['guide.news'])
        self.assertEqual(value['guide.news']['next']['title'], 'Morning & More')
        self.assertNotIn('no-id', value)

    def test_bad_import_never_replaces_last_good_database(self):
        self.epg.import_file(self.source, {'guide.news'}, now=NOW)
        before = self.epg.database.read_bytes()
        for contents in [b'<html>login</html>', xml().decode().encode('utf-16'), xml()[:-6], b'<!DOCTYPE tv [<!ENTITY secret "unsafe">]><tv/>', xml()]:
            with self.subTest(contents=contents[:30]):
                self.source.write_bytes(contents)
                with self.assertRaises(Exception):
                    self.epg.import_file(self.source, {'does-not-match'} if contents == xml() else {'guide.news'}, now=NOW)
                self.assertEqual(self.epg.database.read_bytes(), before)
        with self.assertRaises(ValueError):
            LimitedXML(io.BytesIO(b'x' * 40), limit=20).read(40)

    def test_matching_uses_original_epg_id_and_handles_duplicate_station_entries(self):
        self.epg.import_file(self.source, {'guide.news'}, now=NOW)
        playlist = self.root / 'channels.m3u'
        playlist.write_text('#EXTM3U\n#EXTINF:-1 tvg-chno="1" channelbox-id="player-one" tvg-id="guide.news",News\nhttps://example.invalid/SECRET/1\n#EXTINF:-1 tvg-chno="2" channelbox-id="player-two" tvg-id="guide.news",News backup\nhttps://example.invalid/SECRET/2\n#EXTINF:-1 tvg-chno="3" channelbox-id="player-three",Unknown\nhttps://example.invalid/SECRET/3\n')
        catalog = Catalog(playlist, self.root / 'fav.json', self.root / 'images', self.epg)
        with patch('channelbox_epg.time.time', return_value=NOW):
            items = catalog.query({})['items']
        self.assertEqual(items[0]['now'], items[1]['now'])
        self.assertEqual(items[0]['now']['title'], 'Morning & More')
        self.assertIsNone(items[2]['now'])
        self.assertNotIn('SECRET', json.dumps(items))
        self.assertNotIn('guide.news', json.dumps(items))

    def test_download_failure_and_oversize_keep_cached_schedules(self):
        self.epg.import_file(self.source, {'guide.news'}, now=NOW)
        before = self.epg.database.read_bytes()
        with patch('channelbox_epg.urllib.request.urlopen', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.epg.refresh_once(lambda: {'guide.news'})
        self.assertFalse(self.epg.loading)
        self.assertEqual(self.epg.database.read_bytes(), before)
        response = Mock(url='https://example.invalid/private-guide.gz')
        response.read.side_effect = [b'x' * 65, b'']
        context = Mock(); context.__enter__ = Mock(return_value=response); context.__exit__ = Mock(return_value=False)
        with patch('channelbox_epg.MAX_DOWNLOAD', 64), patch('channelbox_epg.urllib.request.urlopen', return_value=context):
            with self.assertRaises(ValueError):
                self.epg.refresh_once(lambda: {'guide.news'})
        self.assertEqual(self.epg.database.read_bytes(), before)

    def test_fresh_cache_avoids_repeated_full_downloads(self):
        url = json.loads(self.config.read_text())['epg_url']
        source_hash = hashlib.sha256(url.encode()).hexdigest()
        with patch('channelbox_epg.time.time', return_value=NOW):
            self.epg.import_file(self.source, {'guide.news'}, now=NOW, source_hash=source_hash)
            metadata = self.epg.metadata()
            # The source fixture has a short horizon; retain the import's 72h window.
            with patch('channelbox_epg.datetime') as date, patch('channelbox_epg.urllib.request.urlopen') as download:
                date.fromisoformat.return_value.timestamp.return_value = NOW - 60
                self.epg.refresh_once(lambda: {'guide.news'})
                download.assert_not_called()
                download.side_effect = TimeoutError
                with self.assertRaises(TimeoutError):
                    self.epg.refresh_once(lambda: {'guide.news'}, force=True)
                download.assert_called_once()
        self.assertEqual(metadata['channels'], 1)

    def test_restart_waits_only_until_cached_listings_are_six_hours_old(self):
        stamp = datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()
        with patch.object(self.epg, 'refresh_once'), patch.object(self.epg, 'metadata', return_value={'updated_at': stamp}), patch('channelbox_epg.time.time', return_value=NOW), patch.object(self.epg.stop_event, 'wait', return_value=True) as wait:
            self.epg.start(lambda: {'guide.news'})
            self.epg.thread.join(timeout=2)
            self.assertFalse(self.epg.thread.is_alive())
            wait.assert_called_once_with(5 * 3600)
