import asyncio
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import edge_tts

from .process import capture
from .errors import ProviderError, upstream_error
from .tts_compat import configure_synthesis_endpoint

configure_synthesis_endpoint()

MAX_RESPONSE = 32 * 1024 * 1024
NARRATION_CHARS_PER_SECOND = 4
MIN_TARGET_SECONDS = 15
MAX_TARGET_SECONDS = 600
MULTIPART_BOUNDARY = 'NoteToFilm-6f5c2a8d0f4b47c9a7d1e3b5'


def narration_budget_chars(seconds, max_script):
    if not seconds:
        return None
    return min(int(seconds) * NARRATION_CHARS_PER_SECOND, max_script)


def max_target_seconds(max_video_seconds):
    return min(MAX_TARGET_SECONDS, max_video_seconds)


def plan_prompt(config, script, brief=None):
    budget = ''
    purpose = ''
    picture = '画面中不出现文字，不把抽象旁白直接当作画面描述。'
    if brief:
        chars = narration_budget_chars(brief['target_duration_seconds'], config.max_script)
        budget = (f'全片旁白合计不超过 {chars} 字。这是按每秒 {NARRATION_CHARS_PER_SECOND} 字、'
                  f'目标 {brief["target_duration_seconds"]} 秒估算的旁白预算，不是精确配音时长。')
        purpose = (f'按用途组织镜头顺序和详略：用途是「{brief["purpose"]}」，受众是「{brief["audience"]}」，'
                   f'重点是「{brief["focus"]}」。不同用途应采用不同的叙述顺序和每镜侧重，但只能重组原文已有事实。')
        picture = '画面中不出现文字、数字、按钮、图表或界面标注，不把抽象旁白直接当作画面描述。需要说明的数字和步骤只写在旁白里。'
    return (f'把文稿拆成分镜，每镜包含 narration（旁白）和 visual_prompt（画面描述）。最多 {config.max_scenes} 镜。'
            f'每镜旁白不超过 {config.max_narration} 字，{budget}表达完整意思，忠实原文，不新增或省略事实。'
            f'{purpose}'
            '画面描述用具体可见的主体、环境、动作和构图表达该镜内容，控制在 500 字以内；'
            f'同一角色与场景保持外观一致，{picture}'
            '只输出 JSON 对象数组，每个对象只有 narration 和 visual_prompt 两个非空字符串字段。'
            '不能完整处理时请报错，不要截断。\n文稿：\n' + script)


def encode_multipart(fields, files):
    body = bytearray()
    for name, value in fields:
        body.extend(f'--{MULTIPART_BOUNDARY}\r\n'.encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b'\r\n')
    for name, filename, content_type, content in files:
        body.extend(f'--{MULTIPART_BOUNDARY}\r\n'.encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        body.extend(f'Content-Type: {content_type}\r\nContent-Transfer-Encoding: binary\r\n\r\n'.encode())
        body.extend(content)
        body.extend(b'\r\n')
    body.extend(f'--{MULTIPART_BOUNDARY}--\r\n'.encode())
    return bytes(body), f'multipart/form-data; boundary={MULTIPART_BOUNDARY}'


def bounded_request(url, payload=None, headers=None, timeout=120, download=False, stopping=None, body=None):
    message = json.dumps({'url': url, 'data': base64.b64encode(body if body is not None else json.dumps(payload).encode()).decode() if payload is not None or body is not None else None,
                          'headers': {'User-Agent': 'NoteToFilm/1.0', **(headers or {})}, 'timeout': timeout, 'download': download})
    code, result, error = capture([sys.executable, str(Path(__file__).with_name('request_worker.py'))],
                                 timeout, input_text=message, stopping=stopping)
    if code:
        try:
            details = json.loads(error)
        except (ValueError, TypeError):
            details = {}
        raise upstream_error(details.get('kind'), details.get('status')) from None
    return base64.b64decode(result, validate=True)


class Providers:
    def __init__(self, config):
        self.config = config
        self.stopping = None

    def post(self, path, payload, timeout):
        base, key = self.config.llm_connection if path in ('/chat/completions', '/responses') else self.config.image_connection
        try:
            result = json.loads(bounded_request(base + path, payload,
                               {'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}, timeout, stopping=self.stopping))
        except subprocess.TimeoutExpired:
            raise upstream_error('timeout') from None
        except (ValueError, UnicodeError):
            raise ProviderError('upstream_format', '上游没有返回有效 JSON，请检查接口协议；未自动重试。') from None
        if not isinstance(result, dict):
            raise ProviderError('upstream_format', '上游响应结构无效，请检查接口协议；未自动重试。')
        return result

    def post_multipart(self, path, fields, files, timeout):
        base, key = self.config.image_connection
        body, content_type = encode_multipart(fields, files)
        try:
            result = json.loads(bounded_request(base + path, body=body,
                               headers={'Content-Type': content_type, 'Authorization': f'Bearer {key}'}, timeout=timeout, stopping=self.stopping))
        except subprocess.TimeoutExpired:
            raise upstream_error('timeout') from None
        except (ValueError, UnicodeError):
            raise ProviderError('upstream_format', '上游没有返回有效 JSON，请检查接口协议；未自动重试。') from None
        if not isinstance(result, dict):
            raise ProviderError('upstream_format', '上游响应结构无效，请检查接口协议；未自动重试。')
        return result

    def plan(self, script, brief=None):
        c = self.config
        if not c.llm_connection[1]:
            return [p.strip() for p in re.split(r'\n\s*\n', script) if p.strip()]
        prompt = plan_prompt(c, script, brief)
        if c.llm_wire_api == 'responses':
            result = self.post('/responses', {
                'model': c.llm_model, 'input': [{'role': 'user', 'content': prompt}],
                'store': False, 'stream': False,
            }, 120)
            if result.get('status') != 'completed':
                raise ProviderError('model_incomplete', '分镜模型未完整结束，未采用部分结果；请检查上游状态后再恢复。')
            chunks = []
            output = result.get('output')
            if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
                raise ProviderError('upstream_format', 'Responses 接口的 output 结构无效，请检查所选协议和网关。')
            for item in output:
                if item.get('type') != 'message' or item.get('role') != 'assistant':
                    continue
                content = item.get('content')
                if item.get('status', 'completed') != 'completed' or not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
                    raise ProviderError('upstream_format', 'Responses 接口返回的文本消息不完整，未采用部分结果。')
                for part in content:
                    if part.get('type') == 'refusal':
                        raise ProviderError('model_refused', '分镜模型拒绝处理此文稿，未生成图音；请调整文稿后重新提交。')
                    if part.get('type') == 'output_text' and isinstance(part.get('text'), str):
                        chunks.append(part['text'])
            text = ''.join(chunks).strip()
        else:
            result = self.post('/chat/completions', {
                'model': c.llm_model, 'temperature': 0.3,
                'messages': [{'role': 'user', 'content': prompt}],
            }, 120)
            try:
                choice = result['choices'][0]
                if choice.get('finish_reason') not in (None, 'stop'):
                    raise ProviderError('model_incomplete', '分镜模型未完整结束，未采用部分结果；请检查上游状态后再恢复。')
                text = choice['message']['content'].strip()
            except (KeyError, IndexError, TypeError, AttributeError):
                raise ProviderError('upstream_format', '聊天接口缺少有效文本，请检查所选协议和模型。') from None
        if not text:
            raise ProviderError('upstream_format', '分镜接口未返回可用文本，未生成图音；请检查所选协议和模型。')
        return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', text))

    def image(self, visual_prompt, output, references=None):
        c = self.config
        references = references or []
        if len(references) > 16:
            raise ProviderError('image_reference_limit', '参考图最多 16 张；请减少参考后重试。')
        prompt = f'{c.style}。画面描述：{visual_prompt}'
        if references:
            if c.image_reference_protocol != 'openai-images-edit':
                raise ProviderError('image_reference_unsupported', '当前图片配置未启用参考图协议；未发起外部请求。')
            result = self.post_multipart('/images/edits', [
                ('model', c.image_model), ('prompt', prompt), ('size', c.image_size),
                ('quality', c.image_quality), ('n', 1),
            ], [('image[]', f'reference-{index + 1}.' + ('png' if item['mime'] == 'image/png' else 'jpg'),
                            item['mime'], item['content']) for index, item in enumerate(references)], c.image_timeout)
        else:
            result = self.post('/images/generations', {
                'model': c.image_model, 'prompt': prompt,
                'size': c.image_size, 'quality': c.image_quality, 'n': 1,
            }, c.image_timeout)
        try:
            data = result['data'][0]
            if data.get('b64_json'):
                raw = base64.b64decode(data['b64_json'], validate=True)
            else:
                raw = bounded_request(data['url'], timeout=60, download=True, stopping=self.stopping)
        except subprocess.TimeoutExpired:
            raise upstream_error('timeout') from None
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError('upstream_format', '生图接口未返回可用图片，请检查图片模型与接口契约；未自动重试。') from None
        if len(raw) > MAX_RESPONSE or not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff')):
            raise ValueError('上游图片格式或大小无效')
        output.write_bytes(raw)

    def notify(self, tid, status):
        if not self.config.webhook:
            return
        payload = {'msg_type': 'text', 'content': {'text': f'NoteToFilm 任务 {tid}：{status}'}}
        bounded_request(self.config.webhook, payload, {'Content-Type': 'application/json'}, timeout=10, stopping=self.stopping)

    def audio(self, narration, output):
        async def generate():
            task = asyncio.create_task(edge_tts.Communicate(narration, self.config.voice).save(str(output)))
            try:
                async with asyncio.timeout(120):
                    while not task.done():
                        if self.stopping is not None and self.stopping.is_set():
                            raise InterruptedError('Service is stopping')
                        await asyncio.wait({task}, timeout=.2)
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        try:
            asyncio.run(generate())
        except InterruptedError:
            raise
        except TimeoutError:
            raise ProviderError('tts_timeout', '配音请求超过 120 秒，请检查微软语音服务连接后再恢复。') from None
        except Exception as exc:
            status = getattr(exc, 'status', None)
            suffix = f'（HTTP {status}）' if type(status) is int and 400 <= status <= 599 else ''
            raise ProviderError('tts_failed', f'微软配音服务请求失败{suffix}，请检查网络、音色及服务状态后再恢复。') from None
