import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


class HttpFramingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1', 'VIDEO_DATA_DIR': cls.temp.name,
               'ADMIN_TOKEN': 'framing-test-admin', 'ACCESS_TOKEN': '', 'OPENAI_BASE': '', 'OPENAI_API_KEY': '',
               'LLM_BASE': '', 'LLM_API_KEY': '', 'IMAGE_BASE': '', 'IMAGE_API_KEY': '', 'SPACE_HOST': '',
               'COOKIE_SECURE': '0', 'FEISHU_WEBHOOK': '', 'ALLOWED_HOSTS': '127.0.0.1,localhost', 'REQUEST_BODY_TIMEOUT': '1'}
        cls.proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'app.app:app', '--host', '127.0.0.1', '--port', '0',
                                     '--http', 'h11', '--no-proxy-headers'], cwd=ROOT, env=env, stderr=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, text=True)
        cls.base = None
        for _ in range(20):
            line = cls.proc.stderr.readline()
            match = re.search(r'http://127\.0\.0\.1:\d+', line)
            if match:
                cls.base = match.group()
                break
        if cls.base is None:
            cls.proc.terminate()
            cls.proc.communicate(timeout=10)
            raise RuntimeError('framing test server did not start')
        cls.port = urlsplit(cls.base).port

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.communicate(timeout=15)
        cls.temp.cleanup()

    def raw(self, headers, body=b''):
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
            client.settimeout(3)
            request = (b'POST /api/v1/auth/login HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                       b'Connection: close\r\n' + headers + b'\r\n')
            client.sendall(request)
            try:
                client.sendall(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            result = bytearray()
            while True:
                try:
                    chunk = client.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                result.extend(chunk)
        return bytes(result)

    def test_h11_chunked_without_content_length_is_bounded(self):
        body = b'{"token":"wrong"}' + b' ' * 330000
        wire = hex(len(body))[2:].encode() + b'\r\n' + body + b'\r\n0\r\n\r\n'
        raw = self.raw(b'Transfer-Encoding: chunked\r\n', wire)
        self.assertTrue(raw.startswith(b'HTTP/1.1 413 '), raw[:100])
        self.assertIn(b'payload_too_large', raw)
        self.assertIn(b'x-request-id:', raw.lower())

    def test_h11_conflicting_framing_is_rejected(self):
        raw = self.raw(b'Content-Length: 2\r\nTransfer-Encoding: chunked\r\n', b'2\r\n{}\r\n0\r\n\r\n')
        self.assertTrue(raw.startswith(b'HTTP/1.1 400 '), raw[:100])

    def test_declared_large_body_rejected_without_upload(self):
        raw = self.raw(b'Content-Length: 330000\r\n')
        self.assertTrue(raw.startswith(b'HTTP/1.1 413 '), raw[:100])

    def test_wire_read_timeout(self):
        raw = self.raw(b'Content-Length: 20\r\n', b'{')
        self.assertTrue(raw.startswith(b'HTTP/1.1 408 '), raw[:100])

    def test_small_legal_chunked_request_reaches_validation(self):
        body = b'{"token":"wrong"}'
        wire = hex(len(body))[2:].encode() + b'\r\n' + body + b'\r\n0\r\n\r\n'
        raw = self.raw(b'Transfer-Encoding: chunked\r\n', wire)
        self.assertTrue(raw.startswith(b'HTTP/1.1 401 '), raw[:100])


if __name__ == '__main__':
    unittest.main()
