import copy
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock

from channelbox import ChannelBox, MpvController, parse_m3u
from channelbox_feed import check_transition, render_m3u, validate_feed

spec = importlib.util.spec_from_file_location('scanner', Path(__file__).parents[1] / 'hosting/channelbox_scanner/scanner.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def channel(i, url=None):
    return dict(id=f'channel{i}', number=str(i), name=f'Channel {i}', url=url or f'https://example.invalid/{i}')


def feed(channels=None):
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc).isoformat(), channels=channels or [channel(101), channel(201), channel(301)])


class FeedTests(unittest.TestCase):
    def test_duplicates_empty_and_unsafe_urls_rejected(self):
        for change in [lambda f: f.update(channels=[]),
                       lambda f: f['channels'].append(f['channels'][0]),
                       lambda f: f['channels'][0].update(url='file:///etc/passwd'),
                       lambda f: f['channels'][0].update(user_agent='a\r\nb'),
                       lambda f: f['channels'][0].update(number='10000')]:
            value = feed()
            change(value)
            with self.assertRaises(ValueError):
                validate_feed(value)

    def test_number_registry_survives_removal_and_rejects_reuse(self):
        registry = {'channel101': '101', 'gone': '500'}
        new = check_transition(feed(), registry)
        self.assertEqual(new['gone'], '500')
        f = feed()
        f['channels'][0]['number'] = '500'
        with self.assertRaises(ValueError):
            check_transition(f, registry)
        f['channels'][0]['number'] = '501'
        with self.assertRaises(ValueError):
            check_transition(f, registry)

    def test_mass_drop_rejected_after_initial_curated_import(self):
        with self.assertRaises(ValueError):
            check_transition(feed(), {}, previous_count=10)
        self.assertEqual(len(check_transition(feed(), {}, previous_count=0)), 3)

    def test_headers_and_ids_round_trip_to_player(self):
        c = dict(channel(101), user_agent='Test Agent', referrer='https://example.invalid/')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'test.m3u'
            p.write_text(render_m3u([c, channel(201)]))
            channels = parse_m3u(str(p))
        self.assertEqual(channels[0].identity, 'channel101')
        player = MpvController()
        player.command = Mock()
        player.tune(channels[0], 2000)
        self.assertEqual(player.command.call_args_list[0].args[0][-1], {'user-agent': 'Test Agent', 'referrer': 'https://example.invalid/'})
        self.assertEqual(channels[1].user_agent, '')


class ScannerTests(unittest.TestCase):
    def state(self):
        f = feed()
        return {'numbers': {c['id']: c['number'] for c in f['channels']},
                'records': {c['id']: {'channel': c, 'failures': 0, 'active': True} for c in f['channels']}}

    def test_only_decoded_video_passes(self):
        for code, progress, expected in [(0, 'frame=90\nprogress=end\n', True), (0, 'frame=0\n', False), (1, 'frame=90\n', False)]:
            with patch.object(scanner.subprocess, 'run', return_value=Mock(returncode=code, stdout=progress)):
                self.assertEqual(scanner.probe(channel(101)), expected)

    def test_headers_parsed_from_upstream(self):
        result = scanner.parse_source(render_m3u([dict(channel(101), user_agent='Agent', referrer='https://example.invalid/')]))
        self.assertEqual(result['channel101']['user_agent'], 'Agent')
        self.assertEqual(result['channel101']['referrer'], 'https://example.invalid/')

    def test_probe_accepts_hls_url_shapes_but_keeps_transport_and_decode_checks(self):
        candidate = dict(channel(187), user_agent='Test Agent', referrer='https://example.invalid/')
        with patch.object(scanner.subprocess, 'run', return_value=Mock(returncode=0, stdout='frame=90\n')) as run:
            self.assertTrue(scanner.probe(candidate))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-extension_picky') + 1], '0')
        self.assertLess(command.index('-extension_picky'), command.index('-i'))
        self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'http,https,tcp,tls,crypto')
        self.assertNotIn('-allowed_extensions', command)
        self.assertNotIn('-allowed_segment_extensions', command)
        self.assertEqual(command[command.index('-user_agent') + 1], 'Test Agent')
        self.assertEqual(command[command.index('-referer') + 1], candidate['referrer'])
        with patch.object(scanner.subprocess, 'run', side_effect=scanner.subprocess.TimeoutExpired(command, 20)):
            self.assertFalse(scanner.probe(candidate))

    def test_compatibility_upgrade_preserves_numbers_schedule_and_failed_records(self):
        for passes in (True, False):
            with self.subTest(passes=passes):
                state = self.state()
                state.update(last_scan='2026-10-14T09:00:00+00:00', last_full='2026-10-13T09:00:00+00:00', last_scan_day='2026-10-14')
                grit = dict(channel(187), id='GritXtra.us@SD')
                state['numbers'][grit['id']] = '187'
                state['records'][grit['id']] = dict(active=False, failures=1)
                original = copy.deepcopy(state)
                with patch.object(scanner, 'download', return_value=render_m3u([grit]).encode()), patch.object(scanner, 'probe', return_value=passes):
                    updated, value = scanner.refresh_compatibility_additions(state)
                self.assertEqual(state, original)
                self.assertEqual(updated['numbers'], state['numbers'])
                for key in ('last_scan', 'last_full', 'last_scan_day'):
                    self.assertEqual(updated[key], state[key])
                for identity in ('channel101', 'channel201', 'channel301'):
                    self.assertEqual(updated['records'][identity], state['records'][identity])
                self.assertEqual(updated['compatibility_revision'], scanner.COMPATIBILITY_REVISION)
                if passes:
                    self.assertEqual(next(c['number'] for c in value['channels'] if c['id'] == grit['id']), '187')
                    self.assertEqual(updated['records'][grit['id']]['failures'], 0)
                else:
                    self.assertNotIn(grit['id'], {c['id'] for c in value['channels']})
                    self.assertEqual(updated['records'][grit['id']], state['records'][grit['id']])

    def test_daily_scan_retries_inactive_grit_without_admitting_failed_video(self):
        state = self.state()
        grit = dict(channel(187), id='GritXtra.us@SD')
        state['numbers'][grit['id']] = '187'
        source = render_m3u(feed()['channels'] + [grit]).encode()
        for passes in (False, True):
            with self.subTest(passes=passes), patch.object(scanner, 'download', return_value=source), patch.object(scanner, 'local_sources', return_value={}), patch.object(scanner, 'probe', side_effect=lambda c: passes if c['id'] == grit['id'] else True) as probe:
                updated, value = scanner.scan(state, False, 2)
            self.assertIn(grit['id'], {call.args[0]['id'] for call in probe.call_args_list})
            self.assertEqual(grit['id'] in {c['id'] for c in value['channels']}, passes)
            self.assertEqual(updated['numbers'][grit['id']], '187')

    def test_three_failed_cycles_remove_then_recover_same_number(self):
        state = self.state()
        source = {c['id']: c for c in feed()['channels']}
        for day in range(3):
            state, value = scanner.evolve(state, source, {'channel101': False, 'channel201': True, 'channel301': True}, False, f'2026-10-{10+day}T10:00:00+00:00')
            self.assertEqual(len(value['channels']), 3 if day < 2 else 2)
        source['channel101']['url'] = 'https://example.invalid/changed'
        state, value = scanner.evolve(state, source, {'channel101': True, 'channel201': True, 'channel301': True}, True, '2026-10-13T10:00:00+00:00')
        self.assertEqual(value['channels'][0]['number'], '101')
        self.assertTrue(value['channels'][0]['url'].endswith('changed'))

    def test_widespread_failure_cannot_erase_lineup_or_advance_failures(self):
        state = self.state()
        original = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            scanner.evolve(state, {}, {i: False for i in state['records']}, True, '2026-10-13T10:00:00+00:00')
        self.assertEqual(state, original)

    def test_new_channel_gets_number_above_reserved_range(self):
        state = self.state()
        state['numbers']['gone'] = '2164'
        source = {'new': dict(channel(50), id='new')}
        _, value = scanner.evolve(state, source, {'new': True}, True, '2026-10-13T10:00:00+00:00')
        self.assertEqual(value['channels'][-1]['number'], '2165')

    def test_weekly_full_scan_catches_up_after_restart(self):
        state = {'last_scan_day': '2026-10-12', 'last_full': '2026-10-01T10:00:00+00:00'}
        self.assertTrue(scanner.due(state, datetime(2026, 10, 13, 12, tzinfo=timezone.utc), 4))
        state['last_full'] = '2026-10-06T22:00:00+00:00'
        self.assertTrue(scanner.due(state, datetime(2026, 10, 13, 4, tzinfo=timezone.utc), 4))
        state['last_scan_day'] = '2026-10-13'
        self.assertIsNone(scanner.due(state, datetime(2026, 10, 13, 12, tzinfo=timezone.utc), 4))

    def test_manual_full_scan_runs_once_and_publication_retry_does_not_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = dict(self.state(), local_revision=scanner.LOCAL_REVISION,
                         compatibility_revision=scanner.COMPATIBILITY_REVISION)
            (root / 'scanner-state.json').write_text(json.dumps(state))
            (root / 'options.json').write_text(json.dumps({'full_scan_request': 1}))
            with patch('sys.argv', ['scanner', '--data', directory]), \
                 patch.object(scanner, 'due', return_value=None), \
                 patch.object(scanner, 'scan', return_value=(state, feed())) as scan, \
                 patch.object(scanner, 'publish_github', side_effect=[RuntimeError('offline'), True]) as publish, \
                 patch.object(scanner.time, 'sleep', side_effect=KeyboardInterrupt):
                for _ in range(2):
                    with self.assertRaises(KeyboardInterrupt):
                        scanner.main()
                scan.assert_called_once()
                self.assertTrue(scan.call_args.args[1])
                self.assertEqual(publish.call_count, 2)
            saved = json.loads((root / 'scanner-state.json').read_text())
            self.assertEqual(saved['completed_full_scan_request'], 1)
            self.assertFalse(saved['publish_pending'])
            self.assertEqual(saved['numbers'], state['numbers'])

    def test_failed_manual_full_scan_keeps_request_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = dict(self.state(), local_revision=scanner.LOCAL_REVISION,
                         compatibility_revision=scanner.COMPATIBILITY_REVISION)
            (root / 'scanner-state.json').write_text(json.dumps(state))
            (root / 'options.json').write_text(json.dumps({'full_scan_request': 1}))
            with patch('sys.argv', ['scanner', '--data', directory]), \
                 patch.object(scanner, 'due', return_value=None), \
                 patch.object(scanner, 'scan', side_effect=RuntimeError('failed')), \
                 patch.object(scanner, 'publish_github') as publish, \
                 patch.object(scanner.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    scanner.main()
                publish.assert_not_called()
            self.assertEqual(json.loads((root / 'scanner-state.json').read_text()), state)

    def test_local_order_preserves_numbers_and_survives_later_scans(self):
        state = self.state()
        state['numbers'].update({'gone': '2164', 'KVTNDT251.us@HD': '1546'})
        source = {i: dict(channel(1), id=i) for i in scanner.LOCAL_ORDER}
        for full in (False, True):
            state, value = scanner.evolve(state, source, {i: True for i in source}, full,
                                           '2026-10-13T10:00:00+00:00')
            self.assertEqual([c['id'] for c in value['channels'][:len(scanner.LOCAL_ORDER)]], list(scanner.LOCAL_ORDER))
            self.assertEqual(value['channels'][3]['number'], '1546')
            self.assertEqual(value['channels'][len(scanner.LOCAL_ORDER)]['number'], '101')
            self.assertEqual(state['numbers']['gone'], '2164')

    def test_local_page_parser_uses_live_attribute_not_vod(self):
        page = b'<video src="https://example.invalid/clip.m3u8" data-elvs-stream-url="https://example.invalid/live.m3u8?a=1&amp;b=2">'
        with patch.object(scanner, 'download', return_value=page):
            sources = scanner.local_sources()
        self.assertEqual(len(sources), 3)
        self.assertTrue(all(c['url'].endswith('live.m3u8?a=1&b=2') for c in sources.values()))
        with patch.object(scanner, 'download', return_value=b'<video src="https://example.invalid/clip.m3u8">'):
            self.assertEqual(scanner.local_sources(), {})

    def test_fox13_resolver_rejects_clips_and_ambiguous_streams(self):
        def page(url, **changes):
            item = dict({'@type': 'VideoObject', 'name': 'FOX13 Memphis News',
                         'description': 'Watch FOX13 News LIVE Any Time for Free.',
                         'contentUrl': url}, **changes)
            return '<script type="application/ld+json">' + json.dumps(item) + '</script>'
        live = 'https://example.invalid/linear/news/playlist.m3u8'
        self.assertEqual(scanner.fox13_live_url(page(live)), live)
        for text in (page(live, name='News clip'), page('https://example.invalid/vod/clip.m3u8'),
                     page(live) + page(live + '?different=1')):
            with self.assertRaises(ValueError):
                scanner.fox13_live_url(text)

    def test_station_watchlist_matches_exact_affiliate_and_retries_daily(self):
        state = self.state()
        wmc = dict(channel(55), id='NBC.us@WMCTV')
        other = dict(channel(56), id='NBC.us@OtherCity')
        source = render_m3u(feed()['channels'] + [wmc, other]).encode()
        with patch.object(scanner, 'download', return_value=b'No embedded live player'):
            local = scanner.local_sources({c['id']: c for c in [wmc, other]})
        self.assertEqual(set(local), {wmc['id']})
        with patch.object(scanner, 'download', return_value=source), patch.object(scanner, 'local_sources', return_value=local), patch.object(scanner, 'probe', return_value=True) as probe:
            updated, value = scanner.scan(state, False, 2)
        checked = {call.args[0]['id'] for call in probe.call_args_list}
        self.assertIn(wmc['id'], checked)
        self.assertNotIn(other['id'], checked)
        self.assertTrue(updated['records'][wmc['id']]['active'])
        self.assertNotIn('Fox.us@WJKT', {c['id'] for c in value['channels']})

    def test_local_enrichment_does_not_advance_national_scan_or_failures(self):
        state = self.state()
        state.update(last_scan='2026-10-13T10:00:00+00:00', last_full='2026-10-12T10:00:00+00:00', last_scan_day='2026-10-13')
        candidate = dict(channel(1), id='ChannelBox.THV11News')
        with patch.object(scanner, 'local_sources', return_value={candidate['id']: candidate}), patch.object(scanner, 'probe', return_value=False):
            updated, value = scanner.refresh_local_additions(state)
        for key in ('records', 'last_scan', 'last_full', 'last_scan_day'):
            self.assertEqual(updated[key], state[key])
        self.assertEqual(len(value['channels']), 3)

    def test_daily_scan_includes_locals_and_uses_last_good_url_if_page_fails(self):
        state = self.state()
        local = dict(channel(2165), id='ChannelBox.THV11News')
        state['numbers'][local['id']] = local['number']
        state['records'][local['id']] = dict(channel=local, active=True, failures=0)
        source = render_m3u(feed()['channels']).encode()
        with patch.object(scanner, 'download', return_value=source), patch.object(scanner, 'local_sources', return_value={}), patch.object(scanner, 'probe', return_value=True) as probe:
            updated, value = scanner.scan(state, False, 2)
        self.assertIn(local, [call.args[0] for call in probe.call_args_list])
        self.assertEqual(value['channels'][0]['id'], local['id'])
        self.assertEqual(updated['records'][local['id']]['failures'], 0)

    def test_no_token_means_no_publication(self):
        with patch.object(scanner.urllib.request, 'urlopen') as request:
            self.assertFalse(scanner.publish_github(feed(), {'github_repository': 'example-owner/example-feed'}))
            request.assert_not_called()


class PlayerUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.playlist = self.root / 'channels.m3u'
        self.playlist.write_text(render_m3u(feed()['channels']))
        self.box = self.new_box()
        self.box.mpv = Mock()

    def new_box(self):
        return ChannelBox(str(self.playlist), self.root / 'state.json', favorites_file=self.root / 'favorites.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_keeps_favorites_current_previous_without_retuning(self):
        self.box.toggle_favorite()
        self.box.next_channel()
        before = self.playlist.read_text()
        changed = feed([channel(i, f'https://example.invalid/new{i}') for i in (301, 101, 201)])
        self.box.mpv.reset_mock()
        self.box.pending_feed = changed
        self.assertTrue(self.box.apply_pending_feed())
        self.assertEqual(self.box.current.number, '201')
        self.assertEqual(self.box.channels[self.box.previous_index].number, '101')
        self.box.mpv.tune.assert_not_called()
        restored = self.new_box()
        self.assertEqual(restored.current.number, '201')
        self.assertTrue(restored.is_favorite(next(c for c in restored.channels if c.number == "101")))
        self.assertEqual(self.playlist.with_suffix('.m3u.previous').read_text(), before)

    def test_missing_current_defers_without_changing_file(self):
        before = self.playlist.read_bytes()
        self.box.pending_feed = feed([channel(201), channel(301)])
        self.assertFalse(self.box.apply_pending_feed())
        self.assertEqual(self.playlist.read_bytes(), before)
        self.assertIsNotNone(self.box.pending_feed)

    def test_removed_favorite_reappears_at_new_url_after_restart(self):
        self.box.index = 2
        self.box.toggle_favorite()
        self.box.index = 0
        self.box.pending_feed = feed([channel(101), channel(201)])
        self.assertTrue(self.box.apply_pending_feed())
        box = self.new_box()
        box.mpv = Mock()
        box.pending_feed = feed([channel(101), channel(201), channel(301, 'https://example.invalid/recovered')])
        self.assertTrue(box.apply_pending_feed())
        self.assertTrue(box.is_favorite(box.channels[2]))

    def test_invalid_update_keeps_playlist_and_favorites(self):
        before = self.playlist.read_bytes()
        self.box.pending_feed = feed([dict(channel(101), number='201')])
        with self.assertRaises(ValueError):
            self.box.apply_pending_feed()
        self.assertEqual(self.playlist.read_bytes(), before)

    def test_guide_and_number_entry_defer_update(self):
        self.box.pending_feed = feed()
        self.box.guide_open = True
        self.assertFalse(self.box.apply_pending_feed())
        self.box.guide_open = False
        self.box.number_buffer = '10'
        self.assertFalse(self.box.apply_pending_feed())


if __name__ == '__main__':
    unittest.main()
