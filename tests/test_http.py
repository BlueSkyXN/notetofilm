import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
               'VIDEO_DATA_DIR': cls.temp.name, 'ADMIN_TOKEN': '', 'ACCESS_TOKEN': 'http-test-only',
               'OPENAI_BASE': '', 'OPENAI_API_KEY': '', 'FEISHU_WEBHOOK': '',
               'LLM_BASE': '', 'LLM_API_KEY': '', 'IMAGE_BASE': '', 'IMAGE_API_KEY': '',
               'SPACE_HOST': 'test-owner-notetofilm.hf.space', 'COOKIE_SECURE': '1',
               'ALLOWED_HOSTS': '127.0.0.1,localhost,::1'}
        cls.proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'app.app:app', '--host', '127.0.0.1', '--port', '0', '--no-proxy-headers'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import re
        cls.base = None
        for _ in range(15):
            line = cls.proc.stderr.readline()
            match = re.search(r'http://127\.0\.0\.1:(\d+)', line)
            if match:
                cls.base = match.group(0)
                break
            if not line and cls.proc.poll() is not None:
                break
        if not cls.base:
            cls.proc.terminate()
            cls.proc.communicate(timeout=10)
            raise RuntimeError('test server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.communicate(timeout=15)
        cls.temp.cleanup()

    def call(self, path, body=None, headers=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={'Content-Type': 'application/json', **(headers or {})}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_auth_login_cookie_and_no_query_token(self):
        self.assertEqual(self.call('/api/v1/admin/config')[0], 401)
        self.assertEqual(self.call('/api/v1/admin/config?token=http-test-only')[0], 401)
        self.assertEqual(self.call('/api/v1/auth/login', {'token': 'wrong'})[0], 401)
        status, headers, data = self.call('/api/v1/auth/login', {'token': 'http-test-only'})
        self.assertEqual(status, 200)
        cookie = next(c for c in headers.get_all('Set-Cookie') if c.startswith('ntf_admin='))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=strict', cookie)
        self.assertNotIn('http-test-only', cookie)
        self.assertEqual(self.call('/api/v1/tasks', headers={'Cookie': cookie.split(';')[0]})[0], 200)
        self.assertEqual(self.call('/api/v1/tasks', headers={'X-Access-Token': 'http-test-only'})[0], 200)

    def test_hf_landing_is_only_embeddable_page(self):
        code, headers, body = self.call('/space')
        self.assertEqual(code, 200)
        self.assertNotIn('X-Frame-Options', headers)
        self.assertIn('frame-ancestors https://huggingface.co', headers['Content-Security-Policy'])
        self.assertIn(b'href="/"', body)
        self.assertNotIn(b'http-test-only', body)
        _, headers, _ = self.call('/login')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        _, headers, _ = self.call('/api/v1/auth/login', {'token': 'http-test-only'})
        self.assertIn('Secure', headers['Set-Cookie'])
        self.assertEqual(self.call('/healthz', headers={'Host': 'test-owner-notetofilm.hf.space'})[0], 200)

    def test_cross_origin_write_rejected(self):
        code, _, _ = self.call('/api/v1/tasks', {'script': 'test'}, {'X-Access-Token': 'http-test-only', 'Origin': 'https://other.invalid'})
        self.assertEqual(code, 403)

    def test_disallowed_host_rejected(self):
        self.assertEqual(self.call('/healthz', headers={'Host': 'evil.invalid'})[0], 400)

    def test_login_logout_and_security_headers(self):
        _, headers, _ = self.call('/api/v1/auth/login', {'token': 'http-test-only'})
        cookie = next(c for c in headers.get_all('Set-Cookie') if c.startswith('ntf_admin=')).split(';')[0]
        status, security, _ = self.call('/api/v1/tasks', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", security['Content-Security-Policy'])
        status, headers, _ = self.call('/api/v1/auth/logout', {}, {'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])

    def visitor(self):
        code, headers, _ = self.call('/api/v1/session')
        self.assertEqual(code, 200)
        cookie = next(c for c in headers.get_all('Set-Cookie') if c.startswith('ntf_visitor='))
        return {'Cookie': cookie.split(';')[0]}

    def raw(self, path, body, headers, method='POST'):
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_visitors_cannot_read_or_mutate_other_tasks(self):
        alice, bob = self.visitor(), self.visitor()
        status, _, body = self.call('/api/v1/tasks', {'script': 'private visitor script'}, alice)
        self.assertEqual(status, 202)
        tid = json.loads(body)['task_id']
        for _ in range(30):
            task = json.loads(self.call(f'/api/v1/tasks/{tid}', headers=alice)[2])
            if task['status'] == 'planned':
                break
            time.sleep(.02)
        self.assertEqual(task['status'], 'planned')
        self.assertEqual(self.call(f'/api/v1/tasks/{tid}', headers=bob)[0], 404)
        self.assertEqual(self.call(f'/tasks/{tid}/review', headers=bob)[0], 200)
        self.assertEqual(self.call(f'/api/v1/tasks/{tid}/files/anything.png', headers=bob)[0], 404)
        paths = ['generate', 'plan', 'archive', 'retry', 'render', 'scenes/0/gate', 'scenes/0/regen-image', 'scenes/0/regen-audio']
        bodies = [{'confirmed': True, 'revision': task['plan_revision']}, {'scenes': ['x'], 'revision': task['plan_revision']}, {}, {}, {},
                  {'gate': 'img', 'passed': True, 'revision': 1}, {'confirmed': True, 'expected_revision': 0, 'idempotency_key': 'owner-test-request-key'}, {}]
        for path, body in zip(paths, bodies):
            method = 'PUT' if path in ('plan', 'scenes/0/gate') else 'POST'
            self.assertEqual(self.call(f'/api/v1/tasks/{tid}/{path}', body, bob, method)[0], 404, path)
        self.assertEqual(json.loads(self.call('/api/v1/tasks', headers=bob)[2])['tasks'], [])
        self.assertEqual(self.call(f'/api/v1/tasks/{tid}', headers={'X-Access-Token': 'http-test-only'})[0], 200)
        self.assertNotIn('model_config', task)
        self.assertNotIn('owner', task)

    def test_existing_files_are_owner_scoped(self):
        import sqlite3
        alice, bob = self.visitor(), self.visitor()
        _, _, body = self.call('/api/v1/tasks', {'script': 'file ownership'}, alice)
        tid = json.loads(body)['task_id']
        for _ in range(30):
            if json.loads(self.call(f'/api/v1/tasks/{tid}', headers=alice)[2])['status'] == 'planned':
                break
            time.sleep(.02)
        root = Path(self.temp.name) / str(tid)
        root.mkdir()
        (root / 'fixture.png').write_bytes(b'private fixture')
        with sqlite3.connect(Path(self.temp.name) / 'tasks.db') as conn:
            conn.execute("UPDATE scenes SET image_path='fixture.png' WHERE task_id=?", (tid,))
        self.assertEqual(self.call(f'/api/v1/tasks/{tid}/files/fixture.png', headers=alice)[0], 200)
        self.assertEqual(self.call(f'/api/v1/tasks/{tid}/files/fixture.png', headers=bob)[0], 404)

    def test_guest_mutation_requires_cookie_and_csrf_guard(self):
        self.assertEqual(self.call('/api/v1/tasks', {'script': 'no cookie'})[0], 403)
        self.assertEqual(self.call('/api/v1/tasks', {'script': 'forged cookie'}, {'Cookie': 'ntf_visitor=forged'})[0], 403)
        guest = self.visitor()
        guest['Sec-Fetch-Site'] = 'cross-site'
        self.assertEqual(self.call('/api/v1/tasks', {'script': 'csrf'}, guest)[0], 403)

    def test_admin_editor_and_apis_require_admin(self):
        visitor = self.visitor()
        self.assertEqual(self.call('/api/v1/admin/config', headers=visitor)[0], 401)
        for action in ('validate', 'restore'):
            self.assertEqual(self.call('/api/v1/admin/config/' + action, {}, visitor)[0], 401)
        self.assertEqual(self.call('/api/v1/admin/config', {}, visitor, 'PUT')[0], 401)
        _, _, page = self.call('/admin', headers=visitor)
        self.assertIn(b'config-editor', page)
        self.assertNotIn(b'http-test-only', page)
        h = {'X-Access-Token': 'http-test-only'}
        _, _, raw = self.call('/api/v1/admin/config', headers=h)
        info = json.loads(raw)
        self.assertNotIn(b'http-test-only', raw)
        body = {'text': info['text'].replace('origin-deepseek-v4-1-flash', 'updated-offline-model'), 'revision': info['revision']}
        code, _, validation = self.call('/api/v1/admin/config/validate', body, h)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(validation)['document']['llm']['model'], 'updated-offline-model')
        self.assertEqual(info['document']['llm']['wire_api'], 'chat')
        code, _, raw = self.call('/api/v1/admin/config', body, h, 'PUT')
        self.assertEqual(code, 200)
        saved = json.loads(raw)
        self.assertEqual(self.call('/api/v1/admin/config', body, h, 'PUT')[0], 409)
        self.assertEqual(self.call('/api/v1/admin/config/restore', {'revision': saved['revision']}, h)[0], 200)
        body['text'] = '[broken'
        self.assertEqual(self.call('/api/v1/admin/config/validate', body, h)[0], 422)

    def test_structured_plan_layout_and_public_progress(self):
        guest = self.visitor()
        self.assertEqual(self.call('/api/v1/tasks', {'script': 'invalid layout', 'aspect_ratio': '1:1'}, guest)[0], 422)
        code, _, raw = self.call('/api/v1/tasks', {'script': '可独立编辑的分镜', 'aspect_ratio': '9:16', 'image_fit': 'cover'}, guest)
        self.assertEqual(code, 202)
        url = json.loads(raw)['status_url']
        for _ in range(50):
            task = json.loads(self.call(url, headers=guest)[2])
            if task['status'] == 'planned':
                break
            time.sleep(.02)
        self.assertEqual(task['status'], 'planned')
        self.assertEqual((task['aspect_ratio'], task['image_fit']), ('9:16', 'cover'))
        self.assertEqual(task['progress']['total'], 1)
        scene = task['scenes'][0]
        plan = {'revision': task['plan_revision'], 'scenes': [{'id': scene['id'], 'narration': '新的旁白', 'visual_prompt': '清晨窗边的一盆绿植'}], 'image_fit': 'contain'}
        self.assertEqual(self.call(url + '/plan', plan, guest, 'PUT')[0], 200)
        after = json.loads(self.call(url, headers=guest)[2])
        self.assertEqual(after['scenes'][0]['id'], scene['id'])
        self.assertEqual(after['scenes'][0]['visual_prompt'], '清晨窗边的一盆绿植')
        self.assertEqual((after['aspect_ratio'], after['image_fit']), ('9:16', 'contain'))
        self.assertEqual(self.call(url + '/plan', plan, guest, 'PUT')[0], 409)
        plan['revision'] = after['plan_revision']
        plan['scenes'][0]['visual_prompt'] = ' '
        self.assertEqual(self.call(url + '/plan', plan, guest, 'PUT')[0], 422)

    def test_planning_brief_round_trip_and_partial_rejection(self):
        guest = self.visitor()
        _, _, session = self.call('/api/v1/session', headers=guest)
        limits = json.loads(session)['limits']
        self.assertEqual((limits['narration_chars_per_second'], limits['min_target_seconds']), (4, 15))
        _, _, home = self.call('/')
        self.assertIn(b'id="purpose"', home)
        self.assertIn(b'id="brief-enabled"', home)
        self.assertIn('添加补充制作要求'.encode(), home)
        review = self.call('/tasks/1/review')[2]
        self.assertLess(review.index(b'id="plan-overview"'), review.index(b'id="generate"'))
        body = {'script': '同一份材料。先有结果，再有问题。', 'purpose': '向同事说明做法', 'audience': '同事',
                'focus': '先讲结果', 'target_duration_seconds': 60}
        code, _, raw = self.call('/api/v1/tasks', body, guest)
        self.assertEqual(code, 202)
        url = json.loads(raw)['status_url']
        for _ in range(50):
            task = json.loads(self.call(url, headers=guest)[2])
            if task['status'] == 'planned':
                break
            time.sleep(.02)
        self.assertEqual(task['narration_budget_chars'], 240)
        self.assertEqual((task['purpose'], task['audience'], task['focus'], task['target_duration_seconds']),
                         ('向同事说明做法', '同事', '先讲结果', 60))
        other = dict(body, purpose='向新同学介绍背景', audience='新同学', focus='先讲来龙去脉', target_duration_seconds=180)
        self.assertEqual(self.call('/api/v1/tasks', other, guest)[0], 202)
        code, _, raw = self.call('/api/v1/tasks', other, guest)
        self.assertEqual(code, 409)
        self.assertIn('相同文稿和制作要求', json.loads(raw)['detail'])
        self.assertEqual(self.call('/api/v1/tasks', {'script': '另一份材料', 'purpose': '只有用途'}, guest)[0], 422)

    def test_task_image_upload_round_trip_and_owner_isolation(self):
        guest, other = self.visitor(), self.visitor()
        code, _, raw = self.call('/api/v1/tasks', {'script': '图片导入接口测试'}, guest)
        tid = json.loads(raw)['task_id']
        for _ in range(50):
            task = json.loads(self.call(f'/api/v1/tasks/{tid}', headers=guest)[2])
            if task['status'] == 'planned':
                break
            time.sleep(.02)
        image = Path(self.temp.name) / 'upload.png'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=32x20',
                        '-frames:v', '1', str(image)], check=True)
        headers = {**guest, 'Content-Type': 'image/png'}
        url = f'/api/v1/tasks/{tid}/images'
        code, _, raw = self.raw(url, image.read_bytes(), headers)
        self.assertEqual(code, 201)
        asset = json.loads(raw)
        code, _, raw = self.raw(url, image.read_bytes(), headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['id'], asset['id'])
        code, _, raw = self.call(url, headers=guest)
        self.assertEqual(json.loads(raw)[0]['id'], asset['id'])
        self.assertEqual(self.call(asset['url'], headers=guest)[0], 200)
        self.assertEqual(self.call(asset['url'], headers=other)[0], 404)
        self.assertEqual(self.raw(url, image.read_bytes(), {**other, 'Content-Type': 'image/png'})[0], 404)

    def test_old_shared_session_does_not_become_admin(self):
        from app.app import signed_session
        cookie = signed_session('http-test-only', int(time.time()) + 100)
        self.assertEqual(self.call('/api/v1/admin/config', headers={'Cookie': 'ntf_session=' + cookie})[0], 401)
        import hashlib, hmac
        message = f'{int(time.time()) + 100}.old-session-nonce'
        legacy = message + '.' + hmac.new(b'http-test-only', message.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(self.call('/api/v1/admin/config', headers={'Cookie': 'ntf_admin=' + legacy})[0], 401)

    def test_openapi_contract_and_no_legacy_routes(self):
        code, _, raw = self.call('/api/v1/openapi.json')
        self.assertEqual(code, 200)
        schema = json.loads(raw)
        self.assertTrue(all(p.startswith('/api/v1/') for p in schema['paths']))
        self.assertIn('post', schema['paths']['/api/v1/tasks'])
        self.assertIn('put', schema['paths']['/api/v1/tasks/{tid}/plan'])
        self.assertIn('202', schema['paths']['/api/v1/tasks']['post']['responses'])
        self.assertIn('TaskDetail', schema['components']['schemas'])
        self.assertIn('preview', schema['components']['schemas']['TaskSummary']['properties'])
        self.assertIn('get', schema['paths']['/api/v1/tasks/{tid}/download'])
        self.assertEqual(set(schema['components']['securitySchemes']), {'AdminToken', 'AdminCookie', 'VisitorCookie'})
        self.assertEqual(schema['components']['securitySchemes']['VisitorCookie']['name'], 'ntf_visitor')
        self.assertIn('security', schema['paths']['/api/v1/admin/config']['put'])
        self.assertNotIn(b'http-test-only', raw)
        self.assertEqual(self.call('/api/tasks')[0], 404)
        self.assertEqual(self.call('/api-docs')[0], 200)

    def test_structured_errors_never_echo_input(self):
        guest = self.visitor()
        code, headers, raw = self.call('/api/v1/tasks', {'script': 123, 'api_key': 'private-value-never-return'}, guest)
        self.assertEqual(code, 422)
        error = json.loads(raw)
        self.assertEqual(error['error']['code'], 'validation_error')
        self.assertTrue(error['error']['fields'])
        self.assertEqual(error['request_id'], headers['X-Request-ID'])
        self.assertNotIn(b'private-value-never-return', raw)
        self.assertIn('no-store', headers['Cache-Control'])
        code, headers, raw = self.call('/api/v1/admin/config')
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(raw)['error']['code'], 'admin_required')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')

    def test_api_configuration_export_and_static_shell(self):
        self.assertEqual(self.call('/api/v1/admin/config/export')[0], 401)
        h = {'X-Access-Token': 'http-test-only'}
        code, headers, body = self.call('/api/v1/admin/config/export', headers=h)
        self.assertEqual(code, 200)
        self.assertIn('application/toml', headers['Content-Type'])
        self.assertIn(b'[llm]', body)
        self.assertNotIn(b'http-test-only', body)
        _, _, page = self.call('/tasks/987/review')
        self.assertNotIn(b'__TASK_ID__', page)
        self.assertNotIn(b'987', page)
        self.assertEqual(self.call('/?page=../config.py')[2], self.call('/')[2])

    def test_api_requires_json_and_full_file_isolation(self):
        guest = self.visitor()
        code, _, _ = self.call('/api/v1/tasks', {'script': 'x'}, {**guest, 'Content-Type': 'text/plain'})
        self.assertEqual(code, 400)

    def test_pages_share_theme_and_accessible_structure(self):
        for path in ('/', '/tasks/987/review', '/admin', '/login', '/space', '/api-docs'):
            with self.subTest(path=path):
                code, _, raw = self.call(path)
                self.assertEqual(code, 200)
                self.assertIn(b'<script src="/static/theme.js"></script>', raw)
                self.assertIn(b'data-action="theme"', raw)
                self.assertLess(raw.index(b'/static/theme.js'), raw.index(b'/static/style.css'))
        _, _, page = self.call('/tasks/987/review')
        self.assertIn(b'<video id="video" controls playsinline', page)
        self.assertIn(b'id="download" class="button primary" download', page)
        self.assertIn(b'id="save-state"', page)
        self.assertNotIn(b'style="display:none', page)
        _, _, home = self.call('/')
        self.assertNotIn(b'class="preview"', home)
        self.assertIn(b'id="task-error"', home)
        self.assertIn(b'id="refresh-tasks"', home)
        self.assertEqual(self.call('/static/theme.js')[0], 200)

    def test_list_preview_survives_response_schema(self):
        import sqlite3
        guest = self.visitor()
        script = '摘要回退 ' + '内容' * 45
        code, _, raw = self.call('/api/v1/tasks', {'script': script}, guest)
        self.assertEqual(code, 202)
        tid = json.loads(raw)['task_id']
        for _ in range(50):
            detail = json.loads(self.call(f'/api/v1/tasks/{tid}', headers=guest)[2])
            if detail['status'] == 'planned':
                break
            time.sleep(.02)
        self.assertEqual(detail['status'], 'planned')
        narration = '第一镜\n' + '新的内容' * 30
        with sqlite3.connect(Path(self.temp.name) / 'tasks.db') as conn:
            conn.execute('UPDATE scenes SET narration=? WHERE task_id=?', (narration, tid))
        rows = json.loads(self.call('/api/v1/tasks', headers=guest)[2])['tasks']
        self.assertEqual(rows[0]['preview'], narration.replace('\n', ' ')[:60])
        with sqlite3.connect(Path(self.temp.name) / 'tasks.db') as conn:
            conn.execute('DELETE FROM scenes WHERE task_id=?', (tid,))
        rows = json.loads(self.call('/api/v1/tasks', headers=guest)[2])['tasks']
        self.assertEqual(rows[0]['preview'], script[:60])

    def test_download_is_only_current_published_video(self):
        import sqlite3
        alice, bob = self.visitor(), self.visitor()
        _, _, raw = self.call('/api/v1/tasks', {'script': 'download authorization fixture'}, alice)
        tid = json.loads(raw)['task_id']
        for _ in range(50):
            if json.loads(self.call(f'/api/v1/tasks/{tid}', headers=alice)[2])['status'] == 'planned':
                break
            time.sleep(.02)
        url = f'/api/v1/tasks/{tid}/download'
        self.assertEqual(self.call(url, headers=alice)[0], 404)
        root = Path(self.temp.name) / str(tid)
        root.mkdir(exist_ok=True)
        (root / 'video.mp4').write_bytes(b'offline download fixture')
        with sqlite3.connect(Path(self.temp.name) / 'tasks.db') as conn:
            conn.execute("UPDATE tasks SET status='done',video_path='video.mp4' WHERE id=?", (tid,))
        code, headers, body = self.call(url, headers=alice)
        self.assertEqual(code, 200)
        self.assertEqual(body, b'offline download fixture')
        self.assertEqual(headers['Content-Type'], 'video/mp4')
        self.assertEqual(headers['Content-Disposition'], f'attachment; filename="notetofilm-{tid}.mp4"')
        self.assertEqual(self.call(url, headers=bob)[0], 404)
        self.assertEqual(self.call(url)[0], 404)
        self.assertEqual(self.call(url, headers={'X-Access-Token': 'http-test-only'})[0], 200)
        with sqlite3.connect(Path(self.temp.name) / 'tasks.db') as conn:
            conn.execute("UPDATE tasks SET video_path='../tasks.db' WHERE id=?", (tid,))
        self.assertEqual(self.call(url, headers=alice)[0], 404)
        with sqlite3.connect(Path(self.temp.name) / 'tasks.db') as conn:
            conn.execute("UPDATE tasks SET video_path='missing.mp4' WHERE id=?", (tid,))
        self.assertEqual(self.call(url, headers=alice)[0], 404)

    def test_z_login_attempt_limit(self):
        codes = [self.call('/api/v1/auth/login', {'token': 'wrong'})[0] for _ in range(12)]
        self.assertIn(429, codes)
        self.assertEqual(codes[-1], 429)

    def test_plan_only_no_external_generation(self):
        h = {'X-Access-Token': 'http-test-only'}
        status, _, data = self.call('/api/v1/tasks', {'script': 'HTTP one\n\nHTTP two'}, h)
        self.assertEqual(status, 202)
        tid = json.loads(data)['task_id']
        for _ in range(30):
            detail = json.loads(self.call(f'/api/v1/tasks/{tid}', headers=h)[2])
            if detail['status'] == 'planned':
                break
            time.sleep(.05)
        self.assertEqual(detail['status'], 'planned')
        self.assertEqual(len(detail['scenes']), 2)
        self.assertTrue(all(s['image'] is None for s in detail['scenes']))
        self.assertEqual(self.call(f'/api/v1/tasks/{tid}/generate', {'confirmed': False, 'revision': detail['plan_revision']}, h)[0], 409)


if __name__ == '__main__':
    unittest.main()
