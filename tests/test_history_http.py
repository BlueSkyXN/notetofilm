import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from app.config import Config
from app.db import Store, now
from app.model_config import ModelConfig, encode

ROOT = Path(__file__).resolve().parent.parent


class HistoryHttpTests(unittest.TestCase):
    def test_completed_http_detail_and_download_after_key_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            old = Config(data=folder, token='history-http-admin', base='https://old.invalid/v1', key='old-http-model')
            old_text = encode(ModelConfig(old).initial())
            current = Config(data=folder, token='history-http-admin', llm_base='https://chat.invalid/v1', llm_key='new-http-chat',
                             image_base='https://image.invalid/v1', image_key='new-http-image')
            (folder / 'config.toml').write_text(encode(ModelConfig(current).initial()))
            store = Store(folder)
            store.open()
            with store.connect(write=True) as conn:
                conn.execute('INSERT INTO tasks(id,script,status,created_at,model_config,video_path,owner) VALUES(?,?,?,?,?,?,?)',
                             (1, 'completed source', 'done', now(), old_text, 'video.mp4', 'admin'))
            store.close()
            (folder / '1').mkdir()
            (folder / '1/video.mp4').write_bytes(b'offline completed output')
            env = {**os.environ, 'PYTHONPATH': str(ROOT), 'VIDEO_DATA_DIR': str(folder), 'PYTHONDONTWRITEBYTECODE': '1',
                   'ADMIN_TOKEN': 'history-http-admin', 'ACCESS_TOKEN': '', 'OPENAI_BASE': '', 'OPENAI_API_KEY': '',
                   'LLM_BASE': '', 'LLM_API_KEY': 'new-http-chat', 'IMAGE_BASE': '', 'IMAGE_API_KEY': 'new-http-image',
                   'SPACE_HOST': '', 'COOKIE_SECURE': '0', 'FEISHU_WEBHOOK': '', 'ALLOWED_HOSTS': '127.0.0.1,localhost'}
            proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'app.app:app', '--host', '127.0.0.1', '--port', '0', '--no-proxy-headers'],
                                    cwd=ROOT, env=env, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
            try:
                base = None
                for _ in range(20):
                    line = proc.stderr.readline()
                    match = re.search(r'http://127\.0\.0\.1:\d+', line)
                    if match:
                        base = match.group()
                        break
                self.assertIsNotNone(base)
                client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def read(route):
                    req = urllib.request.Request(base + route, headers={'X-Access-Token': 'history-http-admin'})
                    with client.open(req, timeout=5) as response:
                        return response.status, response.read()
                code, raw = read('/api/v1/tasks/1')
                self.assertEqual(code, 200)
                detail = json.loads(raw)
                self.assertFalse(detail['model_available']['image'])
                self.assertEqual(detail['script'], 'completed source')
                self.assertTrue(detail['video'])
                self.assertNotIn(b'old.invalid', raw)
                self.assertEqual(read('/api/v1/tasks/1/download')[1], b'offline completed output')
                self.assertEqual(read(detail['video'])[0], 200)
            finally:
                proc.terminate()
                proc.communicate(timeout=15)


if __name__ == '__main__':
    unittest.main()
