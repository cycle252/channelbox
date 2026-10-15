import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from channelbox import parse_m3u
from tools.prepare_playlist import prepare_cache, normalize_drive
from tools.install_services import install
from tools.run import command, runtime_socket


class SetupTests(unittest.TestCase):
    def test_fresh_local_install_has_private_cache_and_matching_programme_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input.m3u'
            source.write_text('#EXTM3U\n#EXTINF:-1 tvg-chno="101" tvg-id="news.epg" group-title="News",Example\nhttps://example.invalid/stream\n')
            config = root / 'source.json';config.write_text('{"playlist_url":"","epg_url":""}')
            target = root / 'private/channels.m3u'
            result = prepare_cache(config, target, local=source)
            self.assertEqual(result['count'], 1)
            self.assertEqual(parse_m3u(str(target))[0].number, '101')
            self.assertIn('tvg-id="news.epg"', target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_failed_download_keeps_previous_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);cfg = root / 'source.json';output = root / 'private/channels.m3u'
            cfg.write_text('{"playlist_url":"https://example.invalid/list"}')
            output.parent.mkdir();output.write_text('last good')
            with patch('tools.prepare_playlist.download_config', side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    prepare_cache(cfg, output)
            self.assertEqual(output.read_text(), 'last good')

    def test_drive_preview_conversion_preserves_resource_key(self):
        value = normalize_drive('https://drive.google.com/file/d/EXAMPLE_FILE_ID/view?resourcekey=EXAMPLE_KEY&usp=sharing')
        self.assertIn('export=download', value)
        self.assertIn('id=EXAMPLE_FILE_ID', value)
        self.assertIn('resourcekey=EXAMPLE_KEY', value)
        self.assertNotIn('/view', value)

    def test_generated_services_follow_checkout_and_account_without_enabling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'My ChannelBox';root.mkdir()
            home = Path(directory) / 'different-user';home.mkdir()
            files = install(root, home)
            self.assertEqual(len(files), 2)
            unit = (home / '.config/systemd/user/channelbox.service').read_text()
            self.assertIn(str(root / 'tools/run.py'), unit)
            self.assertIn('WorkingDirectory=' + str(root) + '\n', unit)
            self.assertNotIn('WorkingDirectory="', unit)
            self.assertNotIn('/home/' + 'fixed-user', unit)
            self.assertFalse((home / '.config/autostart/channelbox.desktop').exists())
            install(root, home, autostart=True)
            desktop = home / '.config/autostart/channelbox.desktop'
            self.assertIn(str(root / 'tools/start_session.py'), desktop.read_text())
            files[0].write_text('existing unrelated service')
            with self.assertRaises(FileExistsError):
                install(root, home)
            self.assertEqual(files[0].read_text(), 'existing unrelated service')
            install(root, home, force=True)
            self.assertTrue(list((root / 'private/service-backups').glob('*/channelbox.service')))

    def test_launchers_share_runtime_socket_and_only_enable_configured_sources(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            data = Path(directory) / 'data';data.mkdir();(data / 'channels.m3u').write_text('#EXTM3U')
            cfg = data / 'source.json';cfg.write_text('{"playlist_url":"","epg_url":""}')
            args = argparse.Namespace(mode='player', data=data, input_backend='auto', input_device='flirc', no_grab=False, mpv_option=[], lan=False, port=8846, allowed_host=[])
            with patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory}):
                player = command(args);args.mode = 'guide'
                with patch('tools.run.allowed_hosts', return_value=['example-pi.local']):
                    guide = command(args)
                self.assertEqual(player[player.index('--remote-socket') + 1], guide[guide.index('--control-socket') + 1])
                self.assertNotIn('--m3u-config', player)
                self.assertNotIn('--epg-config', guide)
                cfg.write_text('{"playlist_url":"https://example.invalid/list","epg_url":"https://example.invalid/epg.gz"}')
                args.mode = 'player';self.assertIn('--m3u-config', command(args))
                args.mode = 'guide'
                with patch('tools.run.allowed_hosts', return_value=[]):
                    self.assertIn('--epg-config', command(args))
