import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from channelbox import ChannelBox
from channelbox_remote import RemoteServer, dispatch
from channelbox_web import Catalog, GuideServer, image_type, public_image_url


def playlist():
    return ('#EXTM3U\n'
            '#EXTINF:-1 tvg-chno="1620" channelbox-id="kait" tvg-id="kait.epg" group-title="Local" tvg-logo="https://example.invalid/logo.png",KAIT 8\n'
            'https://provider.invalid/live/PRIVATE_USER/PRIVATE_PASSWORD/123.ts\n'
            '#EXTINF:-1 tvg-chno="19151" channelbox-id="news" group-title="News",News & More\n'
            'https://provider.invalid/other?token=PRIVATE_TOKEN\n'
            '#EXTINF:-1 tvg-chno="3" channelbox-id="copy" group-title="Backups",KAIT backup\n'
            'https://provider.invalid/live/PRIVATE_USER/PRIVATE_PASSWORD/123.ts\n')


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'channels.m3u'
        self.path.write_text(playlist())
        self.favorites = self.root / 'favorites.json'
        self.catalog = Catalog(self.path, self.favorites, self.root / 'images')

    def tearDown(self):
        self.temp.cleanup()

    def test_search_categories_paging_and_private_data_is_not_exposed(self):
        meta = self.catalog.info()
        self.assertEqual(meta['total'], 3)
        self.assertEqual([g['name'] for g in meta['groups']], ['Local', 'News', 'Backups'])
        page = self.catalog.query({'q': ['kait'], 'limit': ['1']})
        self.assertEqual(page['count'], 2)
        self.assertTrue(page['has_more'])
        self.assertEqual(page['items'][0]['number'], '1620')
        self.assertEqual(self.catalog.query({'group': ['News']})['items'][0]['number'], '19151')
        self.assertEqual(self.catalog.query({'number': ['3']})['items'][0]['name'], 'KAIT backup')
        second = self.catalog.query({'q': ['kait'], 'limit': ['1'], 'offset': ['1']})
        self.assertFalse(second['has_more'])
        self.assertEqual(second['items'][0]['number'], '3')
        serialized = json.dumps([meta, page, second])
        for secret in ['PRIVATE_', 'provider.invalid', 'example.invalid', 'https://']:
            self.assertNotIn(secret, serialized)

    def test_live_favorites_and_atomic_playlist_changes_are_reflected(self):
        self.catalog.refresh()
        self.favorites.write_text(json.dumps([self.catalog.entries[0]['_url']]))
        self.assertEqual(self.catalog.info()['favorites'], ['1620', '3'])
        result = self.catalog.query({'favorites': ['1']})
        self.assertEqual(result['count'], 2)
        revision = self.catalog.revision
        self.path.write_text(playlist().replace('KAIT 8', 'KAIT Updated'))
        self.assertEqual(self.catalog.query({'number': ['1620']})['items'][0]['name'], 'KAIT Updated')
        self.assertNotEqual(self.catalog.revision, revision)

    def test_bad_logo_sources_and_active_content_are_rejected(self):
        for url in ['file:///etc/passwd', 'http://user:password@example.invalid/image']:
            with self.assertRaises(ValueError):
                public_image_url(url)
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 80))]
        with patch('channelbox_web.socket.getaddrinfo', return_value=address):
            with self.assertRaises(ValueError):
                public_image_url('http://router.invalid/logo')
        with self.assertRaises(ValueError):
            image_type(b'<svg><script>alert(1)</script></svg>')
        self.assertEqual(image_type(b'\x89PNG\r\n\x1a\nrest'), 'image/png')

    def test_favorites_do_not_retune_and_stale_channel_identity_is_rejected(self):
        box = ChannelBox(str(self.path), self.root / 'state.json', favorites_file=self.favorites)
        box.mpv = Mock()
        dispatch(box, {'action': 'favorite', 'number': '19151', 'identity': 'news', 'enabled': True})
        self.assertEqual(box.current.number, '1620')
        self.assertTrue(box.is_favorite(box.channels[1]))
        box.mpv.tune.assert_not_called()
        with self.assertRaises(ValueError):
            dispatch(box, {'action': 'tune', 'number': '1620', 'identity': 'retired-channel'})
        dispatch(box, {'action': 'favorite', 'number': '1620', 'identity': 'kait', 'enabled': True})
        dispatch(box, {'action': 'favorite', 'number': '3', 'identity': 'copy', 'enabled': False})
        self.assertFalse(box.is_favorite(box.channels[0]))
        self.assertFalse(box.is_favorite(box.channels[2]))

    def test_http_access_control_and_real_unix_tuning(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            box = ChannelBox(str(self.path), self.root / 'state.json', favorites_file=self.favorites)
            box.mpv = Mock()
            remote = RemoteServer(box, Path(directory) / 'control.sock')
            remote.start()
            web = GuideServer(('127.0.0.1', 0), self.catalog, remote.path)
            worker = threading.Thread(target=web.serve_forever, daemon=True)
            worker.start()
            host = f'127.0.0.1:{web.server_port}'
            def request(method, path, payload=None, **headers):
                client = http.client.HTTPConnection('127.0.0.1', web.server_port, timeout=4)
                client.request(method, path, json.dumps(payload) if payload else None,
                               {'Host': host, **headers})
                response = client.getresponse()
                value = response.read()
                status = response.status
                client.close()
                return status, value
            try:
                status, page = request('GET', '/')
                self.assertEqual(status, 200)
                self.assertIn(web.token.encode(), page)
                self.assertEqual(request('GET', '/api/meta')[0], 403)
                auth = {'X-Guide-Token': web.token}
                self.assertEqual(request('GET', '/api/meta', **auth)[0], 200)
                self.assertEqual(request('GET', '/api/meta', Host='evil.invalid', **auth)[0], 403)
                self.assertEqual(request('POST', '/api/control', {'action': 'status'}, Origin='https://evil.invalid', **auth)[0], 403)
                self.assertEqual(request('POST', '/api/control', {'action': 'shell'}, **auth)[0], 400)
                self.assertEqual(request('POST', '/api/control', {'action': 'tune', 'number': '19151', 'identity': 'wrong'}, **auth)[0], 400)
                status, raw = request('POST', '/api/control', {'action': 'tune', 'number': '19151', 'identity': 'news'}, **auth)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)['number'], '19151')
                self.assertEqual(box.current.number, '19151')
                self.assertNotIn('PRIVATE_', raw.decode())
                self.assertEqual(request('POST', '/api/control', {'action': 'exit'})[0], 403)
                self.assertFalse(box.stop_event.is_set())
                status, raw = request('POST', '/api/control', {'action': 'exit'}, **auth)
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(raw)['running'])
                self.assertTrue(box.stop_event.is_set())
            finally:
                web.shutdown(); web.server_close(); worker.join(timeout=3)
                remote.close()
                box._cancel_number_timer(); box._cancel_guide_timer()

    def test_guide_remains_available_when_player_has_exited(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            web = GuideServer(('127.0.0.1', 0), self.catalog, Path(directory) / 'absent.sock')
            try:
                self.assertEqual(web.control({'action': 'status'}), {'ok': True, 'running': False})
                self.assertEqual(web.catalog.query({})['count'], 3)
                with self.assertRaises(FileNotFoundError):
                    web.control({'action': 'exit'})
            finally:
                web.server_close()
