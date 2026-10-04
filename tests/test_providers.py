import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.providers import Providers, bounded_request
from app.request_worker import public_download


class Handler(BaseHTTPRequestHandler):
    requests = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        type(self).requests += 1
        self.rfile.read(int(self.headers['Content-Length']))
        if self.path == '/slow':
            time.sleep(1)
        self.send_response(302 if self.path == '/redirect' else 200)
        if self.path == '/redirect':
            self.send_header('Location', '/other')
        self.end_headers()
        try:
            self.wfile.write(b'{"ok":true}')
        except (BrokenPipeError, ConnectionResetError):
            pass


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_stop_event_terminates_process(self):
        import sys
        from app.process import capture
        event = threading.Event()
        timer = threading.Timer(.1, event.set)
        timer.start()
        try:
            start = time.monotonic()
            with self.assertRaises(InterruptedError):
                capture([sys.executable, '-c', 'import time;time.sleep(20)'], 30, stopping=event)
            self.assertLess(time.monotonic() - start, 1)
        finally:
            timer.join()

    def test_local_request_no_external_service(self):
        self.assertEqual(json.loads(bounded_request(self.url + '/ok', {}, timeout=3)), {'ok': True})

    def test_wall_timeout_no_retry(self):
        import subprocess
        before = Handler.requests
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_request(self.url + '/slow', {}, timeout=.4)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(Handler.requests - before, 1)

    def test_redirect_not_followed(self):
        before = Handler.requests
        with self.assertRaises(RuntimeError):
            bounded_request(self.url + '/redirect', {}, timeout=3)
        self.assertEqual(Handler.requests - before, 1)

    def test_private_download_denied(self):
        for url in ['https://127.0.0.1/image', 'http://example.invalid/image', 'https://user:pass@example.invalid/image']:
            with self.assertRaises(ValueError):
                public_download(url)

    def test_download_dns_is_pinned(self):
        import socket
        from app.request_worker import pinned_download
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        with patch('app.request_worker.socket.getaddrinfo', return_value=addresses) as resolver:
            with pinned_download('https://example.invalid/image'):
                self.assertEqual(socket.getaddrinfo('example.invalid', 443), addresses)
                with self.assertRaises(ValueError):
                    socket.getaddrinfo('127.0.0.1', 443)
            self.assertEqual(resolver.call_count, 1)

    def test_separate_llm_and_image_configuration(self):
        config = Config(data=Path('.'), token='test', llm_base='https://chat.invalid/v1', llm_key='chat', image_base='https://image.invalid/v1', image_key='image')
        config.validate()
        providers = Providers(config)
        with patch('app.providers.bounded_request', return_value=b'{}') as call:
            providers.post('/chat/completions', {}, 120)
            self.assertEqual(call.call_args.args[0], 'https://chat.invalid/v1/chat/completions')
            self.assertEqual(call.call_args.args[2]['Authorization'], 'Bearer chat')
            providers.post('/images/generations', {}, 300)
            self.assertEqual(call.call_args.args[0], 'https://image.invalid/v1/images/generations')
            self.assertEqual(call.call_args.args[2]['Authorization'], 'Bearer image')

    def test_base64_contract_and_invalid_data(self):
        providers = Providers(Config(data=Path('.')))
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'image.png'
            raw = b'\x89PNG\r\n\x1a\nfixture'
            with patch.object(providers, 'post', return_value={'data': [{'b64_json': base64.b64encode(raw).decode()}]}):
                providers.image('fixture', file)
                self.assertEqual(file.read_bytes(), raw)
            with patch.object(providers, 'post', return_value={'data': [{'b64_json': 'invalid'}]}):
                with self.assertRaises(ValueError):
                    providers.image('fixture', file)
