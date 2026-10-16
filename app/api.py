import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyCookie, APIKeyHeader
from starlette.concurrency import run_in_threadpool

from .auth import SESSION_SECONDS, VISITOR_SECONDS, signed_session
from .providers import NARRATION_CHARS_PER_SECOND, MIN_TARGET_SECONDS, max_target_seconds
from .persistence import PersistenceError
from .schemas import (Accepted, ConfigEdit, ConfigRestore, ConfigValidation, Confirm, ErrorResponse, Gate, Generate,
                      Health, ImageAsset, Login, ModelSettings, Ok, Plan, RegenImage, Rename, SessionInfo, Submit,
                      TaskDetail, TaskList)

admin_header = APIKeyHeader(name='X-Access-Token', scheme_name='AdminToken', auto_error=False)
admin_cookie = APIKeyCookie(name='ntf_admin', scheme_name='AdminCookie', auto_error=False)
visitor_cookie = APIKeyCookie(name='ntf_visitor', scheme_name='VisitorCookie', auto_error=False)


def visitor_access(request: Request, cookie: Annotated[str | None, Depends(visitor_cookie)],
                   header: Annotated[str | None, Depends(admin_header)],
                   admin: Annotated[str | None, Depends(admin_cookie)]):
    if request.app.state.tasks.storage_failed:
        raise PersistenceError()
    return request.state.owner


def administrator(request: Request, header: Annotated[str | None, Depends(admin_header)],
                  cookie: Annotated[str | None, Depends(admin_cookie)]):
    if not request.state.admin:
        raise HTTPException(401, '请先以管理员身份登录')


def owned(request, tid):
    tasks = request.app.state.tasks
    tasks.authorize(tid, request.state.owner, request.state.admin)
    return tasks


def accepted(tid, result=None):
    result = result or {}
    confirmation = bool(result.get('needs_confirm'))
    return Accepted(task_id=tid, status_url=f'/api/v1/tasks/{tid}', accepted=not confirmation,
                    needs_confirm=confirmation, message=result.get('message'),
                    operation_id=result.get('operation_id'), replayed=result.get('replayed', False))


def create_router():
    router = APIRouter(responses={code: {'model': ErrorResponse} for code in (400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 503, 507)})
    tasks = APIRouter(prefix='/tasks', tags=['Tasks'], dependencies=[Depends(visitor_access)])
    admin = APIRouter(prefix='/admin/config', tags=['Admin configuration'], dependencies=[Depends(administrator)])

    @router.get('/health', tags=['System'], response_model=Health)
    def health(request: Request):
        if request.app.state.tasks.storage_failed:
            raise PersistenceError()
        return Health()

    @router.get('/session', tags=['Session'], response_model=SessionInfo)
    def session(request: Request):
        c = request.app.state.config
        return SessionInfo(admin=request.state.admin, ephemeral=bool(c.space_host and not c.durable), visitor_expires_in=VISITOR_SECONDS,
                           limits={'script_chars': c.max_script, 'scenes': c.max_scenes, 'daily_tasks': c.max_daily_tasks,
                                   'visitor_daily_tasks': c.max_visitor_tasks, 'daily_images': c.max_daily_images,
                                   'narration_chars_per_second': NARRATION_CHARS_PER_SECOND,
                                   'min_target_seconds': MIN_TARGET_SECONDS,
                                   'max_target_seconds': max_target_seconds(c.max_video_seconds)})

    @router.post('/auth/login', tags=['Session'], response_model=Ok)
    def login(body: Login, request: Request):
        import hmac
        settings = request.app.state.config
        with request.app.state.login_lock:
            failures = request.app.state.login_failures
            cutoff = time.monotonic() - 60
            failures[:] = [t for t in failures if t > cutoff]
            if len(failures) >= 10:
                raise HTTPException(429, '登录尝试过多，请一分钟后再试')
            if not settings.token or not hmac.compare_digest(body.token.encode(), settings.token.encode()):
                failures.append(time.monotonic())
                raise HTTPException(401, '管理员令牌错误')
        response = JSONResponse({'ok': True})
        response.set_cookie('ntf_admin', signed_session(settings.token, int(time.time()) + SESSION_SECONDS),
                            max_age=SESSION_SECONDS, httponly=True, secure=settings.cookie_secure, samesite='strict')
        return response

    @router.post('/auth/logout', tags=['Session'], response_model=Ok)
    def logout():
        response = JSONResponse({'ok': True})
        response.delete_cookie('ntf_admin')
        return response

    @tasks.get('', response_model=TaskList)
    def listing(request: Request):
        return request.app.state.tasks.listing(None if request.state.admin else request.state.owner)

    @tasks.post('', status_code=202, response_model=Accepted)
    def submit(body: Submit, request: Request, response: Response):
        result = request.app.state.tasks.submit(body.script, request.state.owner, body.aspect_ratio, body.image_fit,
                                                 body.purpose, body.audience, body.focus, body.target_duration_seconds, body.title)
        response.headers['Location'] = f'/api/v1/tasks/{result["task_id"]}'
        return accepted(result['task_id'])

    @tasks.get('/{tid}', response_model=TaskDetail)
    def detail(tid: int, request: Request):
        return owned(request, tid).detail(tid)

    @tasks.get('/{tid}/images', response_model=list[ImageAsset])
    def images(tid: int, request: Request):
        service = owned(request, tid)
        with service.store.connect() as conn:
            return [service.image_asset(tid, row) for row in service.image_asset_rows(conn, tid)]

    @tasks.post('/{tid}/images', status_code=201, response_model=ImageAsset)
    async def upload_image(tid: int, request: Request, response: Response):
        content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        content = await request.body()
        async with request.app.state.image_upload_slots:
            asset, created = await run_in_threadpool(lambda: owned(request, tid).upload_image(tid, content, content_type))
        if not created:
            response.status_code = 200
        return asset

    @tasks.post('/{tid}/generate', status_code=202, response_model=Accepted)
    def generate(tid: int, body: Generate, request: Request):
        return accepted(tid, owned(request, tid).generate(tid, body.confirmed, body.revision, body.preview))

    @tasks.put('/{tid}/plan', response_model=Ok)
    def save_plan(tid: int, body: Plan, request: Request):
        try:
            scenes = [s if isinstance(s, str) else s.model_dump(exclude_none=True) for s in body.scenes]
            return owned(request, tid).save_plan(tid, scenes, body.revision, body.aspect_ratio, body.image_fit, body.target_duration_seconds)
        except ValueError:
            raise HTTPException(422, '分镜数量、旁白或画面描述无效或超出限制，未保存') from None

    @tasks.post('/{tid}/pause', response_model=Ok)
    def pause(tid: int, request: Request):
        return owned(request, tid).request_pause(tid)

    @tasks.put('/{tid}/title', response_model=Ok)
    def rename(tid: int, body: Rename, request: Request):
        return owned(request, tid).rename(tid, body.title)

    @tasks.post('/{tid}/revise', response_model=Ok)
    def revise(tid: int, request: Request):
        return owned(request, tid).revise(tid)

    @tasks.post('/{tid}/copy', status_code=202, response_model=Accepted)
    def copy_next(tid: int, request: Request, response: Response):
        result = owned(request, tid).copy_next(tid)
        response.headers['Location'] = f'/api/v1/tasks/{result["task_id"]}'
        return accepted(result['task_id'], result)

    @tasks.post('/{tid}/archive', response_model=Ok)
    def archive(tid: int, request: Request):
        return owned(request, tid).archive(tid)

    @tasks.put('/{tid}/scenes/{idx}/gate', response_model=Ok)
    def gate(tid: int, idx: int, body: Gate, request: Request):
        return owned(request, tid).gate(tid, idx, body.gate, body.passed, body.revision)

    @tasks.post('/{tid}/scenes/{idx}/regen-image', status_code=202, response_model=Accepted)
    def regen_image(tid: int, idx: int, body: RegenImage, request: Request):
        return accepted(tid, owned(request, tid).regen(tid, idx, 'img', body.confirmed,
                        body.expected_revision, body.idempotency_key, request.state.owner))

    @tasks.post('/{tid}/scenes/{idx}/regen-audio', status_code=202, response_model=Accepted)
    def regen_audio(tid: int, idx: int, request: Request):
        return accepted(tid, owned(request, tid).regen(tid, idx, 'aud', True))

    @tasks.post('/{tid}/retry', status_code=202, response_model=Accepted, responses={200: {'model': Accepted}})
    def retry(tid: int, body: Confirm, request: Request, response: Response):
        result = owned(request, tid).retry(tid, body.confirmed)
        if result.get('needs_confirm'):
            response.status_code = 200
        return accepted(tid, result)

    @tasks.post('/{tid}/render', status_code=202, response_model=Accepted)
    def render(tid: int, request: Request):
        return accepted(tid, owned(request, tid).render(tid))

    @tasks.get('/{tid}/download', response_class=FileResponse, responses={200: {'content': {'video/mp4': {}}}})
    def download(tid: int, request: Request):
        service = owned(request, tid)
        with service.store.connect() as conn:
            task = service.task(conn, tid)
        if task['status'] != 'done' or not task['video_path']:
            raise HTTPException(404, '成片尚不可下载')
        return FileResponse(service.allowed_file(tid, task['video_path']), media_type='video/mp4',
                            filename=f'notetofilm-{tid}.mp4', content_disposition_type='attachment')

    @tasks.get('/{tid}/files/{name:path}', response_class=FileResponse, responses={200: {'content': {'application/octet-stream': {}}}})
    def files(tid: int, name: str, request: Request):
        return FileResponse(owned(request, tid).allowed_file(tid, name))

    @admin.get('', response_model=ModelSettings)
    def config(request: Request):
        return request.app.state.tasks.models.info()

    @admin.post('/validate', response_model=ConfigValidation)
    def validate_config(body: ConfigEdit, request: Request):
        try:
            document = request.app.state.tasks.models.validate(body.text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {'ok': True, 'message': '语法和参数校验通过；未调用模型，尚未保存', 'document': document}

    @admin.put('', response_model=ModelSettings)
    def save_config(body: ConfigEdit, request: Request):
        try:
            return request.app.state.tasks.models.save(body.text, body.revision, body.confirm_binding)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @admin.post('/restore', response_model=ModelSettings)
    def restore_config(body: ConfigRestore, request: Request):
        try:
            return request.app.state.tasks.models.restore(body.revision, body.confirm_binding)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @admin.get('/export', response_class=Response, responses={200: {'content': {'application/toml': {}}}})
    def export_config(request: Request, include_secrets: bool = False):
        text = request.app.state.tasks.models.export(include_secrets)
        return Response(text, media_type='application/toml', headers={'Content-Disposition': 'attachment; filename="config.toml"'})

    router.include_router(tasks)
    router.include_router(admin)
    return router
