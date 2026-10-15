from contextlib import closing
import json
from pathlib import Path
import socketserver
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from channelbox import ChannelBox
from channelbox_input import linux_key
from channelbox_remote import dispatch
from channelbox_tvguide.controller import Overlay, TVGuide
from channelbox_tvguide.model import metadata, read_schedule, segments
from channelbox_tvguide.render import literal, render


class TVGuideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.playlist = self.root/'channels.m3u'
        self.playlist.write_text('#EXTM3U\n'
            '#EXTINF:-1 tvg-chno="101" channelbox-id="player-one" tvg-id="epg.shared" group-title="Local Affiliates",Local News\nhttps://provider.invalid/PRIVATE_TOKEN\n'
            '#EXTINF:-1 tvg-chno="205" channelbox-id="player-two" tvg-id="epg.shared" group-title="News",News backup\nhttps://provider.invalid/PRIVATE_PASSWORD\n'
            '#EXTINF:-1 tvg-chno="999" group-title="Movies",Cinema\nhttps://provider.invalid/PRIVATE_USER\n')
        self.box = ChannelBox(str(self.playlist), self.root/'state.json', favorites_file=self.root/'favorites.json')
        self.box.mpv = Mock()
        self.database = self.root/'epg.sqlite'
        self.now = 1800*1000000+900
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE programmes(channel TEXT,start INTEGER,stop INTEGER,title TEXT,subtitle TEXT)')
            db.executemany('INSERT INTO programmes VALUES (?,?,?,?,?)', [
                ('epg.shared', self.now-900, self.now+900, 'Live show', 'A real subtitle'),
                ('epg.shared', self.now+900, self.now+2700, 'Next show', ''),
                ('epg.shared', self.now+3600, self.now+7200, 'Later show', ''),
                ('player-one', self.now-900, self.now+900, 'Wrong identity', '')])
            db.commit()
        self.overlay = Mock()
        self.guide = TVGuide(self.box, self.database, overlay=self.overlay, now=lambda: self.now)
        self.box.tv_guide = self.guide

    def tearDown(self):
        self.guide.close()
        self.box._cancel_number_timer();self.box._cancel_guide_timer()
        self.temp.cleanup()

    def test_original_epg_id_and_read_only_cache(self):
        self.assertEqual(metadata(self.playlist)['101']['epg_id'], 'epg.shared')
        before = self.database.read_bytes()
        values = read_schedule(self.database, ['epg.shared'], self.now, self.now+7200)
        self.assertEqual(values['epg.shared'][0]['title'], 'Live show')
        self.assertEqual(before, self.database.read_bytes())
        absent = self.root/'absent.sqlite'
        self.assertEqual(read_schedule(absent, ['x'], 1, 2), {})
        self.assertFalse(absent.exists())

    def test_gaps_are_explicit_and_end_is_exclusive(self):
        parts = segments([{'start': 10, 'stop': 20, 'title': 'One'},
                          {'start': 30, 'stop': 40, 'title': 'Two'}], 0, 50)
        self.assertEqual([p['title'] for p in parts], ['No listings','One','No listings','Two','No listings'])
        self.assertEqual(segments([{'start': 1,'stop': 10,'title': 'Ended'}],10,20)[0]['title'], 'No listings')

    def test_browsing_keeps_current_video_and_escape_only_closes_new_mode(self):
        dispatch(self.box, {'action':'key','key':'h'})
        self.assertTrue(self.guide.is_open)
        self.box.on_press('down');self.box.on_press('right')
        self.assertEqual(self.box.current.number, '101')
        self.assertEqual(self.guide.selected.number, '205')
        self.assertEqual(self.guide.view()['programme']['title'], 'Next show')
        self.box.mpv.tune.assert_not_called()
        self.box.on_press('esc')
        self.assertFalse(self.guide.is_open)
        self.assertFalse(self.box.stop_event.is_set())
        self.overlay.close.assert_called()

    def test_enter_tunes_live_and_preserves_previous_channel(self):
        self.box.on_press('h');self.box.on_press('down');self.box.on_press('right');self.box.on_press('enter')
        self.assertFalse(self.guide.is_open)
        self.assertEqual(self.box.current.number, '205')
        self.assertEqual(self.box.channels[self.box.previous_index].number, '101')
        self.box.mpv.tune.assert_called_once()
        self.box.on_press('d')
        self.assertEqual(self.box.current.number, '101')

    def test_favorites_apply_to_highlighted_channel_and_empty_filter_is_safe(self):
        self.box.on_press('h');self.box.on_press('c');self.box.on_press('enter')
        self.assertTrue(self.guide.is_open)
        self.assertEqual(self.guide.view()['rows'], [])
        self.box.mpv.tune.assert_not_called()
        self.box.on_press('c');self.box.on_press('down');self.box.on_press('a')
        self.assertFalse(self.box.is_favorite(self.box.channels[0]))
        self.assertTrue(self.box.is_favorite(self.box.channels[1]))
        self.box.on_press('c')
        self.assertEqual([c.number for c in self.guide.channels], ['205'])

    def test_old_guide_and_browser_tuning_coexist(self):
        self.box.on_press('h');self.box.on_press('b')
        self.assertFalse(self.guide.is_open)
        self.assertTrue(self.box.guide_open)
        self.box.on_press('h')
        self.assertFalse(self.box.guide_open)
        self.assertTrue(self.guide.is_open)
        dispatch(self.box, {'action':'tune','number':'999','identity':''})
        self.assertFalse(self.guide.is_open)
        self.assertEqual(self.box.current.number, '999')

    def test_numeric_selection_and_missing_schedule_do_not_auto_tune(self):
        self.box.on_press('h')
        for key in '999':self.box.on_press(key)
        self.assertEqual(self.guide.selected.number, '999')
        self.assertTrue(self.guide.view()['programme']['missing'])
        self.assertEqual(self.box.current.number, '101')
        self.box.on_press('enter')
        self.assertEqual(self.box.current.number, '999')

    def test_category_navigation_and_window_updates(self):
        self.box.on_press('h');self.box.on_press('left')
        self.assertEqual(self.guide.focus, 'categories')
        self.box.on_press('down');self.box.on_press('down');self.box.on_press('enter')
        self.assertEqual(self.guide.filter, 'local')
        self.assertEqual(len(self.guide.channels), 1)
        self.assertEqual(self.guide.selected.number, '101')
        self.now += 3600
        self.guide.tick()
        self.assertGreaterEqual(self.guide.start, self.now-1800)

    def test_bad_overlay_never_stops_the_video(self):
        self.overlay.draw.side_effect = ConnectionError
        self.box.on_press('h')
        self.assertFalse(self.guide.is_open)
        self.assertFalse(self.box.stop_event.is_set())
        self.box.on_press('up')
        self.assertEqual(self.box.current.number, '205')

    def test_renderer_does_not_expose_streams_or_accept_ass_tags(self):
        self.box.on_press('h')
        text = render(self.guide.view())
        self.assertIn('Live show', text)
        self.assertNotIn('PRIVATE_', text)
        self.assertNotIn('provider.invalid', text)
        self.assertNotIn(r'\bord99', literal(r'{\bord99}bad'))
        self.assertEqual(linux_key('KEY_H',1), 'h')
        self.assertIsNone(linux_key('KEY_H',2))
        self.assertEqual(linux_key('KEY_RIGHT',2), 'right')

    def test_close_remote_closes_tv_mode_without_exiting_player(self):
        self.box.on_press('h')
        result = dispatch(self.box, {'action':'close'})
        self.assertFalse(result['tv_guide'])
        self.assertTrue(result['running'])


class OverlayTests(unittest.TestCase):
    def test_one_persistent_connection_and_exact_margin_restore(self):
        commands, connections = [], []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                connections.append(1)
                for raw in self.rfile:
                    request = json.loads(raw);commands.append(request['command'])
                    data = .03125 if request['command'][0] == 'get_property' else None
                    self.wfile.write(json.dumps({'error':'success','data':data,'request_id':request['request_id']}).encode()+b'\n')
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            path = Path(directory)/'mpv.sock'
            server = socketserver.ThreadingUnixStreamServer(str(path),Handler)
            thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            overlay = Overlay(path)
            try:
                overlay.draw('one');overlay.draw('two');overlay.close()
                self.assertEqual(len(connections),1)
                for prop in Overlay.properties:
                    writes = [c[2] for c in commands if c[:2] == ['set_property',prop]]
                    self.assertEqual(writes[-1],.03125)
                self.assertEqual(commands[-1],['osd-overlay',781,'none',''])
            finally:
                overlay.close();server.shutdown();server.server_close();thread.join(timeout=3)
