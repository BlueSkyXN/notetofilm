"""NoteToFilm single-process API server with an optional static frontend."""

import hmac
import ipaddress
import logging
import secrets
from asyncio import Semaphore
from contextlib import asynccontextmanager
from threading import Lock
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from .api import create_router
from .auth import VISITOR_SECONDS, new_visitor, signed_session, valid_session, visitor_owner
from .body_limit import BodyLimitMiddleware
from .config import Config, ROOT
from .tasks import Tasks
from .persistence import PersistenceError

LOG = logging.getLogger('notetofilm.http')
ERROR_CODES = {400: 'bad_request', 401: 'admin_required', 403: 'forbidden', 404: 'not_found',
               405: 'method_not_allowed', 408: 'request_timeout', 409: 'conflict', 413: 'payload_too_large', 422: 'validation_error',
               429: 'quota_exceeded', 500: 'internal_error', 503: 'unavailable', 507: 'insufficient_storage'}


def error_response(request, status, message, fields=None):
    return JSONResponse({'error': {'code': ERROR_CODES.get(status, 'request_failed'), 'message': message,
                                  'fields': fields or []}, 'detail': message,
                         'request_id': request.state.request_id}, status_code=status)


def create_app(config=None, service=None):
    @asynccontextmanager
    async def lifespan(application):
        settings = config or Config.load()
        tasks = service or Tasks(settings)
        tasks.open()
        application.state.tasks = tasks
        application.state.config = settings
        application.state.visitor_key = settings.token or secrets.token_urlsafe(48)
        application.state.login_lock = Lock()
        application.state.login_failures = []
        application.state.image_upload_slots = Semaphore(settings.max_active)
        try:
            yield
        finally:
            tasks.close()

    application = FastAPI(title='NoteToFilm API', version='1.0.0', lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url='/api/v1/openapi.json',
                          description='Cookie-based guest isolation; administrator access via ntf_admin or X-Access-Token. '
                                      'GET /session first, then preserve cookies. Async generation returns 202 and a polling URL.')
    application.include_router(create_router(), prefix='/api/v1')
    application.mount('/static', StaticFiles(directory=ROOT / 'app/static'), name='static')

    @application.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, str(exc.detail))

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = ['.'.join(str(p) for p in item['loc']) for item in exc.errors()]
        return error_response(request, 422, '请求参数未通过校验，请按接口文档检查字段', fields)

    @application.exception_handler(PersistenceError)
    async def storage_error(request, exc):
        return error_response(request, 503, str(exc))

    application.add_middleware(BodyLimitMiddleware, error_response=error_response)

    @application.middleware('http')
    async def guard(request, call_next):
        request.state.request_id = secrets.token_hex(12)
        settings = request.app.state.config
        space_landing = request.url.path == '/space' and bool(settings.space_host)

        def finish(response):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Request-ID'] = request.state.request_id
            if not space_landing:
                response.headers['X-Frame-Options'] = 'DENY'
            ancestors = 'https://huggingface.co' if space_landing else "'none'"
            response.headers['Content-Security-Policy'] = f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; connect-src 'self'; frame-ancestors {ancestors}; base-uri 'self'; form-action 'self'"
            return response

        def reject(status, message):
            return finish(error_response(request, status, message))

        try:
            host = urlsplit('//' + request.headers.get('host', '')).hostname
        except ValueError:
            host = None
        if host not in settings.allowed_hosts:
            return reject(400, '不允许的访问主机名')
        if not settings.token:
            try:
                if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
                    return reject(403, '匿名演示仅允许本机访问')
            except ValueError:
                return reject(403, '无法验证本机访问')
        mutation = request.method not in ('GET', 'HEAD', 'OPTIONS')
        if mutation:
            try:
                origin = request.headers.get('origin')
                if (origin and urlsplit(origin).netloc != request.headers.get('host')) or request.headers.get('sec-fetch-site') == 'cross-site':
                    return reject(403, '拒绝跨站写入请求')
            except ValueError:
                return reject(403, '来源地址无效')
        header = request.headers.get('x-access-token', '')
        admin = bool(settings.token) and ((bool(header) and hmac.compare_digest(header.encode(), settings.token.encode())) or
                                         valid_session(settings.token, request.cookies.get('ntf_admin', '')))
        request.state.admin = admin
        if request.url.path.startswith('/api/v1/admin/') and not admin:
            return reject(401, '请先以管理员身份登录')
        cookie = request.cookies.get('ntf_visitor', '')
        owner = visitor_owner(request.app.state.visitor_key, cookie)
        if not admin and not owner and mutation and request.url.path != '/api/v1/auth/login':
            return reject(403, '请先 GET /api/v1/session 建立访客会话并保存 Cookie')
        fresh = not owner and not admin
        if fresh:
            cookie = new_visitor(request.app.state.visitor_key)
            owner = visitor_owner(request.app.state.visitor_key, cookie)
        request.state.owner = 'admin' if admin else owner
        try:
            response = await call_next(request)
        except Exception as exc:
            LOG.error('request=%s failed=%s', request.state.request_id, type(exc).__name__)
            response = error_response(request, 500, '服务处理失败，请保留请求编号联系管理员；不要盲目重试付费操作')
        if fresh:
            response.set_cookie('ntf_visitor', cookie, max_age=VISITOR_SECONDS,
                                httponly=True, secure=settings.cookie_secure, samesite='strict')
        return finish(response)

    @application.get('/healthz', include_in_schema=False)
    def health_alias(request: Request):
        if request.app.state.tasks.storage_failed:
            return JSONResponse({'status': 'storage_unavailable'}, status_code=503)
        return {'status': 'ok'}

    def static_page(page):
        def frontend():
            return FileResponse(ROOT / f'app/templates/{page}.html')
        return frontend

    for path, page in [('/', 'home'), ('/login', 'login'), ('/admin', 'admin'), ('/space', 'space'),
                       ('/tasks/{tid}/review', 'review'), ('/api-docs', 'api-docs')]:
        application.add_api_route(path, static_page(page), methods=['GET'], include_in_schema=False)

    return application


app = create_app()
