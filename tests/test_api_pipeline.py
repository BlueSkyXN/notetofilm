import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class ApiPipelineTests(unittest.TestCase):
    def test_entire_pipeline_without_page_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, 'VIDEO_DATA_DIR': folder, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join([str(ROOT), str(ROOT / 'tests')])}
            proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'api_fixture:app', '--host', '127.0.0.1', '--port', '0', '--no-proxy-headers'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                base = None
                for _ in range(20):
                    line = proc.stderr.readline()
                    match = re.search(r'http://127\.0\.0\.1:\d+', line)
                    if match:
                        base = match.group()
                        break
                    if not line and proc.poll() is not None:
                        break
                self.assertIsNotNone(base)
                guest = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                other = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

                def call(path, body=None, method=None, client=guest):
                    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                                 headers={'Content-Type': 'application/json'}, method=method)
                    try:
                        with client.open(req, timeout=10) as response:
                            data = response.read()
                            result = json.loads(data) if 'application/json' in response.headers.get('Content-Type', '') else data
                            return response.status, response.headers, result
                    except urllib.error.HTTPError as exc:
                        return exc.code, exc.headers, json.loads(exc.read())

                self.assertEqual(call('/api/v1/session')[0], 200)
                code, headers, accepted = call('/api/v1/tasks', {'script': 'Offline API contract test.'})
                self.assertEqual(code, 202)
                status_url = accepted['status_url']
                self.assertEqual(status_url, headers['Location'])

                def wait_for(status):
                    for _ in range(200):
                        code, _, detail = call(status_url)
                        self.assertEqual(code, 200)
                        if detail['status'] == status:
                            return detail
                        if detail['status'] == 'failed':
                            self.fail(detail['error'])
                        time.sleep(.05)
                    self.fail('task did not reach ' + status)

                plan = wait_for('planned')
                self.assertEqual(call(status_url + '/plan', {'scenes': ['Edited API narration.'], 'revision': plan['plan_revision']}, 'PUT')[0], 200)
                plan = call(status_url)[2]
                self.assertEqual(call(status_url + '/generate', {'confirmed': True, 'revision': plan['plan_revision']})[0], 202)
                detail = wait_for('review')
                scene = detail['scenes'][0]
                regen_url = status_url + '/scenes/0/regen-image'
                self.assertEqual(call(regen_url, {'confirmed': True})[0], 422)
                regen = {'confirmed': True, 'expected_revision': scene['img_revision'], 'idempotency_key': 'http-pipeline-image-intent'}
                code, _, original = call(regen_url, regen)
                self.assertEqual(code, 202)
                self.assertTrue(original['operation_id'])
                for _ in range(200):
                    detail = call(status_url)[2]
                    if not detail['busy']:
                        break
                    time.sleep(.05)
                self.assertEqual(detail['status'], 'review')
                self.assertEqual(detail['image_requests'], 2)
                code, _, replay = call(regen_url, regen)
                self.assertEqual(code, 202)
                self.assertTrue(replay['replayed'])
                self.assertEqual(replay['operation_id'], original['operation_id'])
                self.assertEqual(call(status_url)[2]['image_requests'], 2)
                self.assertEqual(call(regen_url, {**regen, 'expected_revision': regen['expected_revision'] + 1})[0], 409)
                self.assertEqual(call('/api/v1/session', client=other)[0], 200)
                self.assertEqual(call(regen_url, regen, client=other)[0], 404)
                scene = detail['scenes'][0]
                self.assertNotIn('image_path', scene)
                self.assertNotIn('video_path', detail)
                for kind in ('image', 'audio'):
                    self.assertTrue(scene[kind].startswith('/api/v1/'))
                    self.assertEqual(call(scene[kind])[0], 200)
                    self.assertEqual(call(scene[kind], client=other)[0], 404)
                for kind in ('img', 'aud'):
                    self.assertEqual(call(status_url + '/scenes/0/gate', {'gate': kind, 'passed': True, 'revision': scene[kind + '_revision']}, 'PUT')[0], 200)
                self.assertEqual(call(status_url + '/render', {})[0], 202)
                detail = wait_for('done')
                code, headers, video = call(detail['video'])
                self.assertEqual(code, 200)
                self.assertIsNone(headers.get('Content-Disposition'))
                self.assertIn(b'ftyp', video[:32])
                code, headers, attachment = call(status_url + '/download')
                self.assertEqual(code, 200)
                self.assertEqual(attachment, video)
                self.assertEqual(headers['Content-Type'], 'video/mp4')
                self.assertIn('attachment;', headers['Content-Disposition'])
                self.assertEqual(call(status_url + '/download', client=other)[0], 404)
                self.assertEqual(call('/api/v1/tasks')[2]['tasks'][0]['preview'], 'Edited API narration.')
                self.assertEqual(call(detail['video'], client=other)[0], 404)
                self.assertEqual(call('/api/v1/tasks', client=other)[2]['tasks'], [])
                self.assertEqual(call('/api/v1/admin/config')[0], 401)
                self.assertEqual(call('/api/v1/auth/login', {'token': 'offline-api-admin'})[0], 200)
                code, _, info = call('/api/v1/admin/config')
                self.assertEqual(code, 200)
                changed = info['text'].replace('origin-deepseek-v4-1-flash', 'new-default')
                self.assertEqual(call('/api/v1/admin/config', {'text': changed, 'revision': info['revision']}, 'PUT')[0], 200)
                self.assertEqual(call(status_url)[2]['config_revision'], detail['config_revision'])
                self.assertEqual(call('/api/v1/admin/config/export')[0], 200)
                self.assertEqual(call('/api/v1/auth/logout', {})[0], 200)
                self.assertEqual(call('/api/v1/admin/config')[0], 401)
                self.assertEqual(call(status_url + '/archive', {})[0], 200)
            finally:
                proc.terminate()
                proc.communicate(timeout=15)


if __name__ == '__main__':
    unittest.main()
