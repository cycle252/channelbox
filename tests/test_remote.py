import json
import socket
import tempfile
import unittest
import http.client
import threading
from pathlib import Path
from unittest.mock import Mock, patch
from channelbox import ChannelBox, Channel
from channelbox_remote import RemoteServer, dispatch
from tools import remote_app


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.box = ChannelBox(str(Path(__file__).parents[1] / 'examples/channels.m3u.example'),
                              root / 'state.json', favorites_file=root / 'favorites.json')
        self.box.mpv = Mock()

    def tearDown(self):
        self.box._cancel_number_timer()
        self.box._cancel_guide_timer()
        self.tmp.cleanup()

    def test_guide_navigation_and_close_do_not_quit(self):
        dispatch(self.box, {'action': 'key', 'key': 'b'})
        self.assertTrue(self.box.guide_open)
        dispatch(self.box, {'action': 'key', 'key': 'down'})
        dispatch(self.box, {'action': 'key', 'key': 'enter'})
        self.assertEqual(self.box.current.number, '201')
        dispatch(self.box, {'action': 'close'})
        self.assertFalse(self.box.stop_event.is_set())

    def test_tune_preserves_previous_channel_and_rejects_unknown(self):
        dispatch(self.box, {'action': 'tune', 'number': '201'})
        self.assertEqual(self.box.current.number, '201')
        dispatch(self.box, {'action': 'key', 'key': 'd'})
        self.assertEqual(self.box.current.number, '101')
        for req in [{'action': 'tune', 'number': '9999'}, {'action': 'key', 'key': 'esc'},
                    {'action': 'shell', 'command': 'anything'}, []]:
            with self.assertRaises(ValueError):
                dispatch(self.box, req)

    def test_socket_is_private_and_returns_no_stream_addresses(self):
        # Short Unix path also works within macOS's socket length limit.
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            path = Path(directory) / 'control.sock'
            server = RemoteServer(self.box, path)
            server.start()
            try:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(str(path))
                    client.sendall(b'{"action":"status"}\n')
                    result = json.loads(client.makefile().readline())
                self.assertEqual(result['number'], '101')
                self.assertNotIn('url', result)
                self.assertNotIn('http', json.dumps(result))
                with self.assertRaises(RuntimeError):
                    RemoteServer(self.box, path)
            finally:
                server.close()

    def test_five_digit_tuning_through_direct_and_individual_keys(self):
        self.box.channels.append(Channel('19151', 'Large lineup', 'https://example.invalid/test'))
        dispatch(self.box, {'action': 'tune', 'number': '19151'})
        self.assertEqual(self.box.current.number, '19151')
        dispatch(self.box, {'action': 'key', 'key': 'd'})
        with patch('channelbox.threading.Timer'):
            for digit in '19151':
                dispatch(self.box, {'action': 'key', 'key': digit})
            self.assertEqual(self.box.number_buffer, '19151')
            self.box._commit_channel_number()
        self.assertEqual(self.box.current.number, '19151')
        with self.assertRaises(ValueError):
            dispatch(self.box, {'action': 'tune', 'number': '119151'})
            self.assertFalse(path.exists())

    def test_http_requires_local_host_origin_and_control_token(self):
        server = remote_app.ThreadingHTTPServer(('127.0.0.1', 0), remote_app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(remote_app, 'send', return_value={'ok': True}) as send:
                for host, origin, token, expected in [
                    ('evil.example', None, remote_app.TOKEN, 403),
                    ('127.0.0.1:8845', 'https://evil.example', remote_app.TOKEN, 403),
                    ('127.0.0.1:8845', None, 'wrong', 403),
                    ('127.0.0.1:8845', 'http://127.0.0.1:8845', remote_app.TOKEN, 200),
                ]:
                    headers = {'Host': host, 'X-Remote-Token': token}
                    if origin:
                        headers['Origin'] = origin
                    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    client.request('POST', '/control', '{"action":"status"}', headers)
                    response = client.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                    client.close()
                send.assert_called_once_with({'action': 'status'})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
