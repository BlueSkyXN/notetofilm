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
from app.providers import Providers, bounded_request, encode_multipart, plan_prompt
from app.errors import ProviderError
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
        if self.path == '/denied':
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'raw upstream secret must never leave worker')
            return
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

    def test_gateway_requests_identify_the_application(self):
        with patch('app.providers.capture', return_value=(0, base64.b64encode(b'{}').decode(), '')) as call:
            bounded_request('https://gateway.example/v1/responses', {}, {'Authorization': 'Bearer fixture'})
            options = json.loads(call.call_args.kwargs['input_text'])
            self.assertEqual(options['headers']['User-Agent'], 'NoteToFilm/1.0')
            self.assertEqual(options['headers']['Authorization'], 'Bearer fixture')

    def test_edge_728_synthesis_compatibility_keeps_voice_listing(self):
        from urllib.parse import parse_qs, urlsplit
        from edge_tts import communicate, constants
        from app.tts_compat import configure_synthesis_endpoint
        voice_list = constants.VOICE_LIST
        with patch.object(communicate, 'WSS_URL', constants.WSS_URL):
            configure_synthesis_endpoint()
            url = urlsplit(communicate.WSS_URL)
            self.assertEqual(url.scheme, 'wss')
            self.assertEqual(url.hostname, 'api.msedgeservices.com')
            self.assertEqual(set(parse_qs(url.query)), {'Ocp-Apim-Subscription-Key'})
            self.assertEqual(constants.VOICE_LIST, voice_list)

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

    def test_http_error_retains_status_without_upstream_body(self):
        before = Handler.requests
        with self.assertRaises(ProviderError) as error:
            bounded_request(self.url + '/denied', {}, timeout=3)
        self.assertIn('HTTP 401', str(error.exception))
        self.assertNotIn('raw upstream secret', str(error.exception))
        self.assertEqual(Handler.requests - before, 1)

    def test_responses_uses_llm_gateway_and_extracts_message_text_after_reasoning(self):
        config = Config(data=Path('.'), llm_wire_api='responses', llm_model='gpt-6.1-sol',
                        llm_base='https://chat.invalid/v1', llm_key='chat-fixture',
                        image_base='https://image.invalid/v1', image_key='image-fixture')
        result = {'status': 'completed', 'output': [
            {'type': 'reasoning', 'summary': []},
            {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': '["first",'}, {'type': 'output_text', 'text': '"second"]'}]},
        ]}
        with patch('app.providers.bounded_request', return_value=json.dumps(result).encode()) as call:
            self.assertEqual(Providers(config).plan('fixture'), ['first', 'second'])
            self.assertEqual(call.call_args.args[0], 'https://chat.invalid/v1/responses')
            payload = call.call_args.args[1]
            self.assertEqual(payload['model'], 'gpt-6.1-sol')
            self.assertFalse(payload['store'])
            self.assertFalse(payload['stream'])
            self.assertNotIn('temperature', payload)
            self.assertIn('fixture', payload['input'][0]['content'])
            self.assertEqual(call.call_args.args[2]['Authorization'], 'Bearer chat-fixture')

    def test_responses_incomplete_refusal_and_missing_text_are_not_plans(self):
        provider = Providers(Config(data=Path('.'), base='https://unused.invalid/v1', key='fixture', llm_wire_api='responses'))
        for result in [
            {'status': 'incomplete', 'output': []},
            {'status': 'completed', 'output': None},
            {'status': 'completed', 'output': ['malformed']},
            {'status': 'completed', 'output': [{'type': 'reasoning'}]},
            {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'refusal', 'refusal': 'private upstream text'}]}]},
        ]:
            with self.subTest(result=result), patch.object(provider, 'post', return_value=result), self.assertRaises(ProviderError) as error:
                provider.plan('fixture')
            self.assertNotIn('private upstream text', str(error.exception))

    def test_chat_completions_contract_remains_supported(self):
        provider = Providers(Config(data=Path('.'), base='https://unused.invalid/v1', key='fixture'))
        result = {'choices': [{'finish_reason': 'stop', 'message': {'content': '["scene"]'}}]}
        with patch.object(provider, 'post', return_value=result) as call:
            self.assertEqual(provider.plan('fixture'), ['scene'])
            self.assertEqual(call.call_args.args[0], '/chat/completions')

    def test_plan_prompt_changes_structure_and_budget_without_changing_image_contract(self):
        config = Config(data=Path('.'), max_scenes=30, max_narration=2000, max_script=20000)
        plain = plan_prompt(config, '同一份材料')
        self.assertIn('画面中不出现文字，不把抽象旁白直接当作画面描述。', plain)
        self.assertNotIn('旁白预算', plain)
        short = {'purpose': '向同事说明做法', 'audience': '同事', 'focus': '先讲结果', 'target_duration_seconds': 60}
        long = {'purpose': '向新同学介绍背景', 'audience': '新同学', 'focus': '先讲来龙去脉', 'target_duration_seconds': 180}
        short_prompt = plan_prompt(config, '同一份材料', short)
        long_prompt = plan_prompt(config, '同一份材料', long)
        self.assertNotEqual(short_prompt, long_prompt)
        self.assertIn('向同事说明做法', short_prompt)
        self.assertIn('全片旁白合计不超过 240 字', short_prompt)
        self.assertIn('全片旁白合计不超过 720 字', long_prompt)
        self.assertIn('向新同学介绍背景', long_prompt)
        for prompt in (short_prompt, long_prompt):
            self.assertIn('不同用途应采用不同的叙述顺序和每镜侧重', prompt)
            self.assertIn('画面中不出现文字、数字、按钮、图表或界面标注', prompt)
            self.assertIn('每个对象只有 narration 和 visual_prompt 两个非空字符串字段', prompt)
        provider = Providers(config)
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'image.png'
            raw = b'\x89PNG\r\n\x1a\nfixture'
            with patch.object(provider, 'post', return_value={'data': [{'b64_json': base64.b64encode(raw).decode()}]}) as call:
                provider.image('窗边的猫', output)
            payload = call.call_args.args[1]
            self.assertEqual(set(payload), {'model', 'prompt', 'size', 'quality', 'n'})
            self.assertEqual(payload['prompt'], f'{config.style}。画面描述：窗边的猫')
            self.assertNotIn('向同事说明做法', payload['prompt'])

    def test_image_references_use_images_edit_multipart_without_changing_plain_contract(self):
        config = Config(data=Path('.'), base='https://unused.invalid/v1', key='fixture',
                        image_reference_protocol='openai-images-edit')
        provider = Providers(config)
        reference = {'mime': 'image/png', 'content': b'\x89PNG\r\n\x1a\nreference'}
        raw = b'\x89PNG\r\n\x1a\nfixture'
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'image.png'
            with patch.object(provider, 'post_multipart', return_value={'data': [{'b64_json': base64.b64encode(raw).decode()}]}) as edit:
                provider.image('新场景，保持人物一致', output, references=[reference])
            path, fields, files, timeout = edit.call_args.args
            self.assertEqual(path, '/images/edits')
            self.assertEqual(fields, [('model', config.image_model), ('prompt', f'{config.style}。画面描述：新场景，保持人物一致'),
                                      ('size', config.image_size), ('quality', config.image_quality), ('n', 1)])
            self.assertEqual(files, [('image[]', 'reference-1.png', 'image/png', reference['content'])])
            self.assertEqual(timeout, config.image_timeout)
            self.assertEqual(output.read_bytes(), raw)
            with patch.object(provider, 'post', return_value={'data': [{'b64_json': base64.b64encode(raw).decode()}]}) as plain:
                provider.image('普通生图', output)
            self.assertEqual(plain.call_args.args[0], '/images/generations')

    def test_multipart_encoder_and_reference_protocol_guard(self):
        body, content_type = encode_multipart([('prompt', '中文 prompt'), ('n', 1)],
                                               [('image[]', 'reference-1.png', 'image/png', b'\x89PNG')])
        self.assertIn('boundary=NoteToFilm-6f5c2a8d0f4b47c9a7d1e3b5', content_type)
        self.assertIn('Content-Disposition: form-data; name="prompt"\r\n\r\n中文 prompt\r\n'.encode(), body)
        self.assertIn(b'Content-Disposition: form-data; name="image[]"; filename="reference-1.png"\r\n', body)
        self.assertTrue(body.endswith(b'--NoteToFilm-6f5c2a8d0f4b47c9a7d1e3b5--\r\n'))
        provider = Providers(Config(data=Path('.'), base='https://unused.invalid/v1', key='fixture'))
        with patch.object(provider, 'post_multipart', side_effect=AssertionError('must not call')) as edit:
            with self.assertRaises(ProviderError):
                provider.image('fixture', Path('/tmp/not-used'), references=[{'mime': 'image/png', 'content': b'x'}])
        edit.assert_not_called()

    def test_multipart_post_sends_raw_body_and_content_type(self):
        provider = Providers(Config(data=Path('.'), image_base='https://image.invalid/v1', image_key='image'))
        with patch('app.providers.bounded_request', return_value=b'{}') as call:
            provider.post_multipart('/images/edits', [('model', 'fixture')], [], 30)
        body = call.call_args.kwargs['body']
        headers = call.call_args.kwargs['headers']
        self.assertEqual(call.call_args.args[0], 'https://image.invalid/v1/images/edits')
        self.assertIsNone(call.call_args.kwargs.get('payload'))
        self.assertIn(b'name="model"', body)
        self.assertTrue(headers['Content-Type'].startswith('multipart/form-data; boundary='))

    def test_structured_storyboard_is_accepted_by_both_protocols(self):
        from dataclasses import replace
        plan = [{'narration': '旁白内容', 'visual_prompt': '窗边的橘猫'}]
        c = Config(data=Path('.'), base='https://unused.invalid/v1', key='fixture')
        for protocol in ('chat', 'responses'):
            result = ({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(plan)}}]}
                      if protocol == 'chat' else {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
                      'content': [{'type': 'output_text', 'text': json.dumps(plan)}]}]})
            provider = Providers(replace(c, llm_wire_api=protocol))
            with self.subTest(protocol=protocol), patch.object(provider, 'post', return_value=result):
                self.assertEqual(provider.plan('原文'), plan)

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
