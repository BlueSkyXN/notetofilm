import asyncio
import re
from time import monotonic

from starlette.requests import Request


class BodyLimitMiddleware:
    def __init__(self, app, error_response):
        self.app = app
        self.error_response = error_response

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        settings = scope['app'].state.config
        limit = settings.max_script * 6 + 196608
        image_upload = scope['method'] == 'POST' and re.fullmatch(r'/api/v1/tasks/\d+/images', scope['path'])
        if image_upload:
            limit = settings.max_upload_image_bytes
        if scope['method'] in ('PUT', 'POST') and re.fullmatch(r'/api/v1/tasks/\d+/plan(/impact)?', scope['path']):
            # 画面描述有独立限额，并覆盖 JSON 对中文使用 Unicode 转义的情况；
            # 预演与保存必须接受同样的合法请求体。
            limit += min(settings.max_scenes, 100) * 4000 * 6
        headers = scope['headers']
        lengths = [value for name, value in headers if name.lower() == b'content-length']
        transfers = [value for name, value in headers if name.lower() == b'transfer-encoding']

        async def reject(status, message):
            response = self.error_response(request, status, message)
            if scope.get('http_version') in ('1.0', '1.1'):
                response.headers['Connection'] = 'close'
            await response(scope, receive, send)

        if len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers):
            return await reject(400, '请求体定界头重复或冲突')
        declared = None
        if lengths:
            value = lengths[0]
            if not re.fullmatch(rb'[0-9]{1,20}', value):
                return await reject(400, '请求体长度无效')
            declared = int(value)
            if declared > limit:
                return await reject(413, '请求体超过实际读取上限')
        if transfers and transfers[0].strip().lower() != b'chunked':
            return await reject(400, '不支持的请求体传输编码')
        deadline = monotonic() + settings.request_body_timeout
        body = bytearray()
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return await reject(408, '请求体读取超时，请检查连接后重新提交')
            try:
                message = await asyncio.wait_for(receive(), remaining)
            except TimeoutError:
                return await reject(408, '请求体读取超时，请检查连接后重新提交')
            if message['type'] == 'http.disconnect':
                return await reject(400, '请求体连接已中断，未执行操作')
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > limit:
                return await reject(413, '请求体超过实际读取上限')
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        if declared is not None and declared != len(body):
            return await reject(400, '声明长度与实际请求体不一致')
        if body and scope['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            content_type = request.headers.get('content-type', '').split(';')[0].strip().lower()
            if image_upload and content_type not in ('image/png', 'image/jpeg'):
                return await reject(400, '图片上传仅接受 image/png 或 image/jpeg')
            if not image_upload and content_type != 'application/json':
                return await reject(400, '写入接口仅接受 application/json')
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)
