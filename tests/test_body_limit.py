import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.app import create_app
from app.auth import new_visitor
from app.config import Config
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class BodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Config(data=Path(self.temp.name), token='body-test-admin', request_body_timeout=1)
        self.executor = ManualExecutor()
        self.service = Tasks(self.settings, FakeProviders(), FakeRenderer(), self.executor, validate_fixture)
        self.app = create_app(self.settings, self.service)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.limit = self.settings.max_script * 6 + 196608

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)
        self.temp.cleanup()

    async def call(self, body=b'', extra=(), route='/api/v1/tasks', delay=0, disconnect=False, method='POST',
                   content_type='application/json'):
        cookie = new_visitor(self.settings.token)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
                 'scheme': 'http', 'path': route, 'raw_path': route.encode(), 'query_string': b'',
                 'root_path': '', 'headers': [(b'host', b'localhost'), (b'content-type', content_type.encode()),
                 (b'cookie', ('ntf_visitor=' + cookie).encode()), *extra],
                 'client': ('127.0.0.1', 10000), 'server': ('localhost', 80)}
        offset = 0
        first = True
        messages = []
        blocked = asyncio.Event()
        async def receive():
            nonlocal offset, first
            if disconnect:
                return {'type': 'http.disconnect'}
            if first or offset < len(body):
                first = False
                if delay:
                    await asyncio.sleep(delay)
                chunk = body[offset:offset + 8192]
                offset += len(chunk)
                return {'type': 'http.request', 'body': chunk, 'more_body': offset < len(body)}
            await blocked.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            messages.append(message)
        await asyncio.wait_for(self.app(scope, receive, send), 3)
        starts = [m for m in messages if m['type'] == 'http.response.start']
        if not starts:
            return None, {}, None
        headers = dict(starts[0]['headers'])
        raw = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        return starts[0]['status'], headers, json.loads(raw)

    def assert_no_business_effects(self):
        with self.service.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_usage').fetchone()[0], 0)
        self.assertEqual(self.executor.jobs, [])
        self.assertEqual(self.app.state.login_failures, [])

    async def test_large_body_rejected_with_and_without_declared_length(self):
        body = b'{"script":"short"}' + b' ' * self.limit
        for extra in ((), ((b'content-length', str(len(body)).encode()),),
                      ((b'transfer-encoding', b'chunked'),), ((b'content-length', b'16'),)):
            with self.subTest(headers=extra):
                status, headers, result = await self.call(body, extra)
                self.assertEqual(status, 413)
                self.assertEqual(result['error']['code'], 'payload_too_large')
                self.assertEqual(headers[b'x-request-id'].decode(), result['request_id'])
                self.assertIn(b"script-src 'self'", headers[b'content-security-policy'])
                self.assertEqual(headers[b'connection'], b'close')
                self.assert_no_business_effects()

    async def test_conflicting_and_duplicate_framing_rejected(self):
        for extra in (((b'content-length', b'2'), (b'transfer-encoding', b'chunked')),
                      ((b'content-length', b'2'), (b'content-length', b'2')),
                      ((b'content-length', b'-1'),), ((b'content-length', b'2, 2'),),
                      ((b'transfer-encoding', b'gzip'),),
                      ((b'transfer-encoding', b'chunked'), (b'transfer-encoding', b'chunked'))):
            status, _, _ = await self.call(b'{}', extra)
            self.assertEqual(status, 400)
            self.assert_no_business_effects()

    async def test_actual_length_mismatch_is_rejected(self):
        status, _, _ = await self.call(b'{"script":"small"}', ((b'content-length', b'2'),))
        self.assertEqual(status, 400)
        self.assert_no_business_effects()

    async def test_slow_body_uses_total_deadline(self):
        self.app.state.config = replace(self.settings, request_body_timeout=.03)
        status, _, body = await self.call(b' ' * 20000, delay=.02)
        self.assertEqual(status, 408)
        self.assertEqual(body['error']['code'], 'request_timeout')
        self.assert_no_business_effects()

    async def test_disconnect_never_dispatches(self):
        status, _, _ = await self.call(disconnect=True)
        self.assertEqual(status, 400)
        self.assert_no_business_effects()

    async def test_legal_chunked_body_at_limit(self):
        raw = b'{"script":"boundary"}'
        body = raw + b' ' * (self.limit - len(raw))
        status, _, result = await self.call(body, ((b'transfer-encoding', b'chunked'),))
        self.assertEqual(status, 202)
        self.assertGreater(result['task_id'], 0)
        self.assertEqual(len(self.executor.jobs), 1)

    async def test_overlimit_login_does_not_count_attempt(self):
        status, _, _ = await self.call(b'{"token":"wrong"}' + b' ' * self.limit, route='/api/v1/auth/login')
        self.assertEqual(status, 413)
        self.assert_no_business_effects()

    async def test_plan_allows_bounded_visual_prompts_beyond_script_body_limit(self):
        tid = self.service.submit('large structured plan')['task_id']
        self.executor.run()
        revision = self.service.detail(tid)['plan_revision']
        plan = {'revision': revision, 'scenes': [{'narration': '短旁白', 'visual_prompt': '画' * 4000} for _ in range(30)]}
        body = json.dumps(plan).encode()
        self.assertGreater(len(body), self.limit)
        route = f'/api/v1/tasks/{tid}/plan'
        auth = ((b'x-access-token', b'body-test-admin'),)
        status, _, _ = await self.call(body, auth, route=route, method='PUT')
        self.assertEqual(status, 200)
        self.assertEqual(self.service.detail(tid)['expected_scenes'], 30)
        limit = self.limit + 30 * 4000 * 6
        status, _, _ = await self.call(body + b' ' * limit, auth, route=route, method='PUT')
        self.assertEqual(status, 413)

    async def test_image_upload_uses_image_body_limit_and_content_type(self):
        tid = self.service.submit('upload boundary')['task_id']
        self.executor.run()
        route = f'/api/v1/tasks/{tid}/images'
        auth = ((b'x-access-token', b'body-test-admin'),)
        original = self.app.state.config
        self.app.state.config = replace(original, max_upload_image_bytes=16)
        try:
            status, _, result = await self.call(b'x', auth, route=route, content_type='application/json')
            self.assertEqual(status, 400)
            status, _, result = await self.call(b'x' * 17, auth, route=route, content_type='image/png')
            self.assertEqual(status, 413)
            status, _, result = await self.call(b'x', auth, route=route, content_type='image/png')
            self.assertEqual(status, 422)
            with self.service.store.connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0], 0)
        finally:
            self.app.state.config = original


if __name__ == '__main__':
    unittest.main()
