import asyncio
import base64
import json
import re
import sys
from pathlib import Path

import edge_tts

from .process import capture

MAX_RESPONSE = 32 * 1024 * 1024


def bounded_request(url, payload=None, headers=None, timeout=120, download=False, stopping=None):
    message = json.dumps({'url': url, 'data': base64.b64encode(json.dumps(payload).encode()).decode() if payload is not None else None,
                          'headers': headers or {}, 'timeout': timeout, 'download': download})
    code, result, _ = capture([sys.executable, str(Path(__file__).with_name('request_worker.py'))],
                              timeout, input_text=message, stopping=stopping)
    if code:
        raise RuntimeError('上游请求失败；未自动重试')
    return base64.b64decode(result, validate=True)


class Providers:
    def __init__(self, config):
        self.config = config
        self.stopping = None

    def post(self, path, payload, timeout):
        base, key = self.config.llm_connection if path == '/chat/completions' else self.config.image_connection
        return json.loads(bounded_request(base + path, payload,
                          {'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}, timeout, stopping=self.stopping))

    def plan(self, script):
        c = self.config
        if not c.llm_connection[1]:
            return [p.strip() for p in re.split(r'\n\s*\n', script) if p.strip()]
        result = self.post('/chat/completions', {
            'model': c.llm_model, 'temperature': 0.3,
            'messages': [{'role': 'user', 'content':
                f'把文稿拆成分镜旁白。每镜表达完整意思，忠实原文，不新增或省略事实。最多 {c.max_scenes} 镜。'
                '只输出 JSON 字符串数组。不能完整处理时请报错，不要截断。\n文稿：\n' + script}],
        }, 120)
        text = result['choices'][0]['message']['content'].strip()
        return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', text))

    def image(self, narration, output):
        c = self.config
        result = self.post('/images/generations', {
            'model': c.image_model, 'prompt': f'{c.style}。用一幅画面表达以下内容：{narration}',
            'size': c.image_size, 'quality': c.image_quality, 'n': 1,
        }, c.image_timeout)
        data = result['data'][0]
        if data.get('b64_json'):
            raw = base64.b64decode(data['b64_json'], validate=True)
        else:
            raw = bounded_request(data['url'], timeout=60, download=True, stopping=self.stopping)
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
        asyncio.run(generate())
