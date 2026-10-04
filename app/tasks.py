import logging
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi import HTTPException

from .db import Store, now, script_digest
from .providers import Providers
from .model_config import ModelConfig
from .render import Renderer, run_process, validate_asset

LOG = logging.getLogger('notetofilm.tasks')


class Tasks:
    def __init__(self, config, providers=None, renderer=None, executor=None, validator=validate_asset):
        self.config = config
        self.store = Store(config.data)
        self.providers = providers or Providers(config)
        self.injected_provider = providers is not None
        self.models = ModelConfig(config)
        self.renderer = renderer or Renderer(config)
        self.executor = executor
        self.validator = (lambda path, kind: validate_asset(path, kind, config.max_audio_seconds)) if validator is validate_asset else validator
        self.closed = False
        self.stopping = Event()
        if hasattr(self.providers, 'stopping'):
            self.providers.stopping = self.stopping
        if hasattr(self.renderer, 'stopping'):
            self.renderer.stopping = self.stopping

    def open(self):
        self.config.validate()
        self.store.open()
        try:
            self.models.open()
            with self.store.connect(write=True) as conn:
                conn.execute("UPDATE tasks SET model_config=? WHERE model_config=''", (self.models.snapshot(),))
            self.reconcile_legacy_outputs()
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=self.config.max_active, thread_name_prefix='notetofilm')
        except Exception:
            self.store.close()
            raise

    def reconcile_legacy_outputs(self):
        with self.store.connect() as conn:
            legacy = conn.execute("SELECT id FROM tasks WHERE status='done' AND video_path IS NULL").fetchall()
        for row in legacy:
            try:
                self.renderer.validate_output(self.config.data / str(row['id']))
            except Exception:
                with self.store.connect(write=True) as conn:
                    conn.execute("UPDATE tasks SET status='interrupted',error=? WHERE id=?",
                                 ('旧成片未通过媒体与构建报告检查，文件保留，请核对后恢复。', row['id']))
            else:
                with self.store.connect(write=True) as conn:
                    conn.execute("UPDATE tasks SET video_path='video.mp4' WHERE id=?", (row['id'],))

    def close(self):
        self.closed = True
        self.stopping.set()
        try:
            if self.executor:
                self.executor.shutdown(wait=True)
        finally:
            self.store.close()

    def task(self, conn, tid):
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        if row is None:
            raise HTTPException(404, '任务不存在')
        return dict(row)

    def authorize(self, tid, owner, admin=False):
        with self.store.connect() as conn:
            task = self.task(conn, tid)
        if not admin and task['owner'] != owner:
            raise HTTPException(404, '任务不存在')

    def task_config(self, task):
        return self.models.for_task(task['model_config']) if task.get('model_config') else self.config

    def provider_for(self, tid):
        if self.injected_provider:
            return self.providers
        with self.store.connect() as conn:
            settings = self.task_config(self.task(conn, tid))
        provider = Providers(settings)
        provider.stopping = self.stopping
        return provider

    def reserve(self, conn, kind, limit):
        day = now()[:10]
        row = conn.execute('SELECT count FROM daily_usage WHERE day=? AND kind=?', (day, kind)).fetchone()
        if row and row['count'] >= limit:
            raise HTTPException(429, '今日服务请求额度已用完，请明天再试或联系管理员')
        conn.execute('INSERT INTO daily_usage(day,kind,count) VALUES(?,?,1) '
                     'ON CONFLICT(day,kind) DO UPDATE SET count=count+1', (day, kind))

    def scenes(self, conn, tid):
        return [dict(s) for s in conn.execute('SELECT * FROM scenes WHERE task_id=? ORDER BY idx', (tid,))]

    def idle(self, task, allowed):
        if task['archived']:
            raise HTTPException(409, '任务已归档，不能修改；文件仍保留')
        if task['operation_id']:
            raise HTTPException(409, '任务有操作正在执行，请等待完成')
        if task['status'] not in allowed:
            raise HTTPException(409, '当前任务状态不允许此操作')

    def capacity(self, conn, kind):
        if shutil.disk_usage(self.config.data).free < self.config.min_free_mb * 1024 * 1024:
            raise HTTPException(507, '磁盘可用空间低于安全阈值，请归档数据后再试')
        if self.closed:
            raise HTTPException(503, '服务正在关闭')
        active = conn.execute('SELECT COUNT(*) FROM tasks WHERE operation_id IS NOT NULL').fetchone()[0]
        if active >= self.config.max_active:
            raise HTTPException(429, '进行中操作已达上限，请稍后再试')
        if kind == 'render':
            count = conn.execute("SELECT COUNT(*) FROM tasks WHERE operation_kind='render' AND operation_id IS NOT NULL").fetchone()[0]
            if count >= self.config.max_render:
                raise HTTPException(429, '合成任务已达上限，请稍后再试')

    def claim(self, conn, tid, kind, status, idx=None):
        self.capacity(conn, kind)
        if kind == 'render':
            self.reserve(conn, 'render', self.config.max_daily_renders)
        operation = uuid.uuid4().hex
        conn.execute('UPDATE tasks SET status=?,error=NULL,operation_id=?,operation_kind=?,operation_scene=? WHERE id=?',
                     (status, operation, kind, idx, tid))
        return operation

    def dispatch(self, tid, operation, work):
        try:
            self.executor.submit(self.worker, tid, operation, work)
        except Exception:
            self.fail(tid, operation, '执行器无法启动任务，未自动重试')
            raise HTTPException(503, '执行器无法启动任务，请稍后恢复')

    def worker(self, tid, operation, work):
        try:
            if self.stopping.is_set():
                raise InterruptedError('Service is stopping')
            work()
        except InterruptedError:
            self.fail(tid, operation, '服务正在关闭，已有素材保留，请恢复后继续。')
            with self.store.connect(write=True) as conn:
                conn.execute("UPDATE tasks SET status='interrupted' WHERE id=? AND status='failed'", (tid,))
        except HTTPException as exc:
            self.fail(tid, operation, str(exc.detail))
        except Exception as exc:
            LOG.error('task=%s operation=%s failed=%s', tid, operation, type(exc).__name__)
            self.fail(tid, operation, '处理失败，已有成功素材保留；请检查配置、素材状态后恢复。')

    def fail(self, tid, operation, message):
        with self.store.connect(write=True) as conn:
            if self.task(conn, tid)['operation_id'] != operation:
                return
            conn.execute("UPDATE scenes SET img_state='unknown',img_approved=0,img_ok=0,img_error=? WHERE task_id=? AND img_state='running'",
                         ('请求结果未知，重试可能再次计费。', tid))
            conn.execute("UPDATE scenes SET aud_state='failed',aud_approved=0,aud_ok=0,aud_error=? WHERE task_id=? AND aud_state='running'",
                         ('配音失败，请重试。', tid))
            conn.execute("UPDATE tasks SET status='failed',error=?,operation_id=NULL,operation_kind=NULL,operation_scene=NULL WHERE id=?",
                         (message, tid))

    def finish(self, tid, operation, status, video=None):
        try:
            self.providers.notify(tid, status)
        except Exception as exc:
            LOG.warning('task=%s notification_failed=%s', tid, type(exc).__name__)
        with self.store.connect(write=True) as conn:
            conn.execute('UPDATE tasks SET status=?,error=NULL,video_path=COALESCE(?,video_path),'
                         'operation_id=NULL,operation_kind=NULL,operation_scene=NULL WHERE id=? AND operation_id=?',
                         (status, video, tid, operation))
        LOG.info('task=%s operation=%s status=%s', tid, operation, status)

    def validate_plan(self, plan):
        c = self.config
        if not isinstance(plan, list) or not 1 <= len(plan) <= c.max_scenes:
            raise ValueError('分镜数量超过限制或结果无效；未截断、未生图')
        if any(not isinstance(s, str) or not s.strip() or len(s.strip()) > c.max_narration for s in plan):
            raise ValueError('旁白为空或超过单镜长度限制；未生图')
        if sum(len(s) for s in plan) > c.max_script:
            raise ValueError('分镜总长度超过限制；未生图')
        return [s.strip() for s in plan]

    def submit(self, script, owner="admin"):
        script = script.replace('\r\n', '\n').replace('\r', '\n').strip()
        if not script or len(script) > self.config.max_script:
            raise HTTPException(422, f'文稿长度必须为 1–{self.config.max_script} 字符')
        fingerprint = script_digest(script)
        snapshot = self.models.snapshot()
        with self.store.connect(write=True) as conn:
            duplicate = conn.execute("SELECT id FROM tasks WHERE script_hash=? AND owner=? AND status!='done' AND archived=0 LIMIT 1", (fingerprint, owner)).fetchone()
            if duplicate:
                raise HTTPException(409, f'相同文稿已有任务 {duplicate[0]}，请打开原任务继续')
            self.capacity(conn, 'plan')
            day = now()[:10]
            if owner != 'admin':
                count = conn.execute('SELECT COUNT(*) FROM tasks WHERE owner=? AND substr(created_at,1,10)=?', (owner, day)).fetchone()[0]
                if count >= self.config.max_visitor_tasks:
                    raise HTTPException(429, '当前访客今日任务数已达上限')
            self.reserve(conn, 'task', self.config.max_daily_tasks)
            tid = conn.execute('INSERT INTO tasks(script,script_hash,status,created_at,owner,model_config) VALUES(?,?,?,?,?,?)',
                               (script, fingerprint, 'submitted', now(), owner, snapshot)).lastrowid
            operation = self.claim(conn, tid, 'plan', 'generating')
        self.dispatch(tid, operation, lambda: self.plan(tid, operation, script))
        return {'task_id': tid}

    def plan(self, tid, operation, script):
        provider = self.provider_for(tid)
        with self.store.connect(write=True) as conn:
            if self.task_config(self.task(conn, tid)).llm_connection[1]:
                self.reserve(conn, 'llm', self.config.max_daily_llm)
        try:
            plan = self.validate_plan(provider.plan(script))
        except ValueError:
            self.fail(tid, operation, '分镜结果无效或超出限制，请缩短文稿或检查模型输出；未截断内容、未生成图音。')
            return
        with self.store.connect(write=True) as conn:
            if self.task(conn, tid)['operation_id'] != operation:
                return
            conn.execute('DELETE FROM scenes WHERE task_id=?', (tid,))
            for idx, narration in enumerate(plan):
                conn.execute('INSERT INTO scenes(task_id,idx,narration) VALUES(?,?,?)', (tid, idx, narration))
            conn.execute("UPDATE tasks SET plan_complete=1,expected_scenes=?,plan_revision=plan_revision+1,status='planned',error=NULL,"
                         'operation_id=NULL,operation_kind=NULL,operation_scene=NULL WHERE id=?', (len(plan), tid))

    def verify_plan(self, task, scenes):
        if not task['plan_complete'] or len(scenes) != task['expected_scenes'] or not scenes or [s['idx'] for s in scenes] != list(range(len(scenes))):
            raise HTTPException(409, '完整分镜计划缺失，请人工确认后重新规划')

    def save_plan(self, tid, plan, revision):
        plan = self.validate_plan(plan)
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            self.idle(task, {'planned'})
            if task['plan_revision'] != revision:
                raise HTTPException(409, '分镜已被更新，请刷新后重新编辑')
            conn.execute('DELETE FROM scenes WHERE task_id=?', (tid,))
            for idx, narration in enumerate(plan):
                conn.execute('INSERT INTO scenes(task_id,idx,narration) VALUES(?,?,?)', (tid, idx, narration))
            conn.execute('UPDATE tasks SET expected_scenes=?,plan_revision=plan_revision+1 WHERE id=?', (len(plan), tid))
        return {'ok': True}

    def image_budget(self, task, amount):
        if self.task_config(task).image_connection[1] and task['image_requests'] + amount > self.config.max_image_requests:
            raise HTTPException(429, '此任务生图请求数已达配置上限；不会继续调用付费接口')

    def generate(self, tid, confirmed, revision=None):
        if not confirmed:
            raise HTTPException(409, '请先确认分镜及图片数量；生成将调用外部服务')
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            self.idle(task, {'planned'})
            self.verify_plan(task, self.scenes(conn, tid))
            if revision is not None and task['plan_revision'] != revision:
                raise HTTPException(409, '分镜计划已更新，请刷新后确认')
            self.image_budget(task, task['expected_scenes'])
            operation = self.claim(conn, tid, 'generate', 'generating')
        self.dispatch(tid, operation, lambda: self.generate_missing(tid, operation))
        return {'ok': True}

    def generate_missing(self, tid, operation, recover_video=False, retry_images_confirmed=False):
        with self.store.connect() as conn:
            task, scenes = self.task(conn, tid), self.scenes(conn, tid)
        self.verify_plan(task, scenes)
        if recover_video and all(self.approved(s) for s in scenes):
            recovered = self.renderer.recover(self.config.data / str(tid), scenes)
            if recovered:
                self.finish(tid, operation, 'done', recovered)
                return
        for scene in scenes:
            for kind in ('img', 'aud'):
                state = scene[f'{kind}_state']
                path = scene['image_path' if kind == 'img' else 'audio_path']
                if recover_video and kind == 'img' and self.task_config(task).image_connection[1] and not retry_images_confirmed and (state not in ('ready', 'placeholder') or not path or not (self.config.data / str(tid) / path).is_file()):
                    self.fail(tid, operation, '图片状态已变化，需人工确认后重新请求生图，未自动计费。')
                    return
                if state in ('ready', 'placeholder') and path:
                    try:
                        self.validator(self.config.data / str(tid) / path, kind)
                        continue
                    except Exception:
                        with self.store.connect(write=True) as conn:
                            conn.execute(f"UPDATE scenes SET {kind}_state='failed',{kind}_approved=0,{kind}_ok=0,{kind}_error=? WHERE task_id=? AND idx=?",
                                         ('已有素材损坏或缺失，请重新生成。', tid, scene['idx']))
                        if kind == 'img' and self.task_config(task).image_connection[1] and not retry_images_confirmed:
                            self.fail(tid, operation, '检测到图片损坏，需再次人工确认后请求生图，未自动计费。')
                            return
                self.asset(tid, operation, scene['idx'], kind)
        self.finish(tid, operation, 'review')

    def asset(self, tid, operation, idx, kind):
        if self.stopping.is_set():
            raise InterruptedError('Service is stopping')
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            if task['operation_id'] != operation:
                raise RuntimeError('操作所有权失效')
            settings = self.task_config(task)
            if kind == 'img' and settings.image_connection[1]:
                self.image_budget(task, 1)
                self.reserve(conn, 'image', self.config.max_daily_images)
                conn.execute('UPDATE tasks SET image_requests=image_requests+1 WHERE id=?', (tid,))
            if kind == 'aud':
                self.reserve(conn, 'audio', self.config.max_daily_audio)
            scene = dict(conn.execute('SELECT * FROM scenes WHERE task_id=? AND idx=?', (tid, idx)).fetchone())
            conn.execute(f"UPDATE scenes SET {kind}_state='running',{kind}_approved=0,{kind}_ok=0,{kind}_error=NULL WHERE task_id=? AND idx=?", (tid, idx))
        task_dir = self.config.data / str(tid)
        folder = task_dir / 'assets' / operation
        folder.mkdir(parents=True, exist_ok=True)
        suffix = '.png' if kind == 'img' else '.mp3'
        output = folder / f'{idx:03d}-{kind}{suffix}'
        temp = folder / f'{idx:03d}-{kind}.part{suffix}'
        provider = self.provider_for(tid)
        placeholder = kind == 'img' and not settings.image_connection[1]
        try:
            if placeholder:
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-f', 'lavfi', '-i',
                             'color=c=0x223344:s=640x360', '-frames:v', '1', str(temp)], 30, stopping=self.stopping)
            elif kind == 'img':
                provider.image(scene['narration'], temp)
            else:
                provider.audio(scene['narration'], temp)
            self.validator(temp, kind)
            temp.replace(output)
        except Exception:
            with self.store.connect(write=True) as conn:
                conn.execute(f'UPDATE scenes SET {kind}_state=?,{kind}_error=? WHERE task_id=? AND idx=?',
                             ('unknown' if kind == 'img' and settings.image_connection[1] else 'failed',
                              '素材生成失败；生图结果可能未知，未自动重试。', tid, idx))
            raise
        with self.store.connect(write=True) as conn:
            if self.task(conn, tid)['operation_id'] != operation:
                raise RuntimeError('任务所有权已变化，未发布素材')
            column = 'image_path' if kind == 'img' else 'audio_path'
            conn.execute(f'UPDATE scenes SET {kind}_state=?,{kind}_revision={kind}_revision+1,'
                         f'{kind}_approved=0,{kind}_ok=0,{kind}_error=NULL,{column}=? WHERE task_id=? AND idx=?',
                         ('placeholder' if placeholder else 'ready', str(output.relative_to(task_dir)), tid, idx))
            if kind == 'img':
                conn.execute('UPDATE scenes SET img_placeholder=? WHERE task_id=? AND idx=?', (int(placeholder), tid, idx))

    def regen(self, tid, idx, kind, confirmed):
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            self.idle(task, {'review'})
            if kind == 'img' and (not self.task_config(task).image_connection[1] or not confirmed):
                raise HTTPException(409, '重做图需要配置网关并确认付费请求')
            scene = conn.execute('SELECT id FROM scenes WHERE task_id=? AND idx=?', (tid, idx)).fetchone()
            if scene is None:
                raise HTTPException(404, '镜不存在')
            if kind == 'img':
                self.image_budget(task, 1)
            operation = self.claim(conn, tid, kind, 'review', idx)
            conn.execute(f"UPDATE scenes SET {kind}_state='running',{kind}_approved=0,{kind}_ok=0 WHERE task_id=? AND idx=?", (tid, idx))
        def work():
            self.asset(tid, operation, idx, kind)
            self.finish(tid, operation, 'review')
        self.dispatch(tid, operation, work)
        return {'ok': True, 'operation_id': operation}

    def gate(self, tid, idx, kind, passed, revision):
        if kind not in ('img', 'aud'):
            raise HTTPException(422, 'gate 必须是 img 或 aud')
        with self.store.connect(write=True) as conn:
            self.idle(self.task(conn, tid), {'review'})
            row = conn.execute('SELECT * FROM scenes WHERE task_id=? AND idx=?', (tid, idx)).fetchone()
            if row is None:
                raise HTTPException(404, '镜不存在')
            if row[f'{kind}_revision'] != revision:
                raise HTTPException(409, '素材已更新，请刷新并复核新版本')
            if passed and (row[f'{kind}_state'] != 'ready' or revision < 1 or (kind == 'img' and row['img_placeholder'])):
                raise HTTPException(409, '素材未就绪或仍为占位图')
            conn.execute(f'UPDATE scenes SET {kind}_approved=?,{kind}_ok=? WHERE task_id=? AND idx=?',
                         (revision if passed else 0, int(passed), tid, idx))
        return {'ok': True}

    def approved(self, scene):
        return not scene['img_placeholder'] and all(
            scene[f'{k}_state'] == 'ready' and scene[f'{k}_revision'] > 0 and
            scene[f'{k}_approved'] == scene[f'{k}_revision'] for k in ('img', 'aud'))

    def render(self, tid):
        with self.store.connect(write=True) as conn:
            task, scenes = self.task(conn, tid), self.scenes(conn, tid)
            self.idle(task, {'review'})
            self.verify_plan(task, scenes)
            if not all(self.approved(s) for s in scenes):
                raise HTTPException(409, '尚未全部确认当前版本的图音素材')
            operation = self.claim(conn, tid, 'render', 'rendering')
        def work():
            video = self.renderer.render(self.config.data / str(tid), operation, scenes)
            self.finish(tid, operation, 'done', video)
        self.dispatch(tid, operation, work)
        return {'ok': True}

    def retry(self, tid, force):
        with self.store.connect(write=True) as conn:
            task, scenes = self.task(conn, tid), self.scenes(conn, tid)
            self.idle(task, {'failed', 'interrupted'})
            has_plan = task['plan_complete'] and len(scenes) == task['expected_scenes'] and bool(scenes)
            image_missing = any(s['img_state'] not in ('ready', 'placeholder') or not s['image_path'] or
                                not (self.config.data / str(tid) / s['image_path']).is_file() for s in scenes)
            needs_confirm = not has_plan or (bool(self.task_config(task).image_connection[1]) and image_missing)
            if needs_confirm and not force:
                return {'needs_confirm': True, 'message': '恢复需要重新规划或请求缺失图片；结果未知的请求可能重复计费，是否继续？'}
            if has_plan:
                needed = sum(s['img_state'] not in ('ready', 'placeholder') or not s['image_path'] or
                             not (self.config.data / str(tid) / s['image_path']).is_file() for s in scenes)
                self.image_budget(task, needed)
            operation = self.claim(conn, tid, 'resume' if has_plan else 'plan', 'generating')
        work = (lambda: self.generate_missing(tid, operation, recover_video=True, retry_images_confirmed=force)) if has_plan else (lambda: self.plan(tid, operation, task['script']))
        self.dispatch(tid, operation, work)
        return {'ok': True, 'message': '恢复已发起；成功素材和历史文件不会删除'}

    def archive(self, tid):
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            self.idle(task, {'planned', 'review', 'done', 'failed', 'interrupted'})
            conn.execute('UPDATE tasks SET archived=1 WHERE id=?', (tid,))
        return {'ok': True, 'message': '已归档，不删除文件；可重新提交同一文稿'}

    def detail(self, tid):
        with self.store.connect() as conn:
            task = self.task(conn, tid)
            scenes = self.scenes(conn, tid)
        task.pop('script_hash', None)
        task['busy'] = bool(task['operation_id'])
        task['configured'] = bool(self.task_config(task).image_connection[1])
        task['config_revision'] = script_digest(task.pop('model_config'))[:12]
        task.pop('owner', None)
        task['image_request_limit'] = self.config.max_image_requests
        task['video'] = f'/api/v1/tasks/{tid}/files/{task["video_path"]}' if task['video_path'] else None
        for scene in scenes:
            for kind, col, label in [('img', 'image_path', 'image'), ('aud', 'audio_path', 'audio')]:
                scene[f'{kind}_ok'] = int(scene[f'{kind}_state'] == 'ready' and scene[f'{kind}_revision'] > 0 and scene[f'{kind}_approved'] == scene[f'{kind}_revision'])
                scene[f'{kind}_busy'] = scene[f'{kind}_state'] == 'running'
                scene[label] = f'/api/v1/tasks/{tid}/files/{scene[col]}' if scene[col] else None
        task['scenes'] = scenes
        return task

    def listing(self, owner=None):
        with self.store.connect() as conn:
            rows = conn.execute('SELECT t.id,t.status,t.archived,t.created_at,COUNT(s.id) AS scenes,'
                                'substr(replace(COALESCE((SELECT narration FROM scenes WHERE task_id=t.id ORDER BY idx LIMIT 1),'
                                "t.script),char(10),' '),1,60) AS preview FROM tasks t "
                                'LEFT JOIN scenes s ON t.id=s.task_id WHERE (? IS NULL OR t.owner=?) '
                                'GROUP BY t.id ORDER BY t.id DESC LIMIT 200', (owner, owner)).fetchall()
            return {'tasks': [dict(r) for r in rows]}

    def allowed_file(self, tid, name):
        with self.store.connect() as conn:
            task = self.task(conn, tid)
            names = {task['video_path']} if task['status'] == 'done' else set()
            for scene in self.scenes(conn, tid):
                names.update((scene['image_path'], scene['audio_path']))
        root = (self.config.data / str(tid)).resolve()
        path = (root / name).resolve()
        if name not in names or not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, '文件不可用')
        return path
