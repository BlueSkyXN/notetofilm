import logging
import math
import shutil
import uuid
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event

from fastapi import HTTPException

from .db import Store, now, script_digest
from .domain import plan_impact
from .domain import references as reference_rules
from . import jobs as job_records
from .providers import (MIN_TARGET_SECONDS, NARRATION_CHARS_PER_SECOND, Providers,
                         max_target_seconds, narration_budget_chars)
from .model_config import ModelConfig
from .image_assets import validate_and_normalize
from .render import Renderer, digest, duration_error, run_process, validate_asset
from .errors import UserFacingError
from .layout import video_layout
from .persistence import DirectoryBucket, HFBucket, Snapshots

LOG = logging.getLogger('notetofilm.tasks')


class Tasks:
    def __init__(self, config, providers=None, renderer=None, executor=None, validator=validate_asset):
        self.config = config
        bucket = HFBucket(config.data_bucket, config.hf_token) if config.data_bucket else DirectoryBucket(config.backup_dir) if config.backup_dir else None
        self.store = Store(config.data, Snapshots(bucket) if bucket else None)
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

    @property
    def storage_failed(self):
        return self.store.failure or bool(self.models.file_config and self.models.file_config.failure)

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

    def task_config(self, task, required=()):
        try:
            return self.models.for_task(task.get('model_config', ''), required)
        except ValueError:
            raise HTTPException(503, '此任务所需的模型配置当前不可执行，请联系管理员；历史素材仍可查看和下载') from None

    def has_gateway(self, task, kind):
        return self.models.has_gateway(task.get('model_config', ''), kind)

    def provider_for(self, tid, required=()):
        with self.store.connect() as conn:
            task = self.task(conn, tid)
            settings = self.task_config(task, required)
        if self.injected_provider:
            return self.providers
        if 'image' in required:
            settings = replace(settings, style=f'{settings.style}。按 {task["aspect_ratio"]} 画幅构图，主体保持在画面中央')
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

    @staticmethod
    def image_asset(tid, row):
        try:
            context = json.loads(row['context_json'])
        except (TypeError, ValueError):
            context = {}
        return {'id': row['id'], 'source': row['source'], 'url': f'/api/v1/tasks/{tid}/files/{row["path"]}',
                'sha256': row['sha256'], 'mime': row['mime'], 'width': row['width'], 'height': row['height'],
                'byte_size': row['byte_size'], 'origin_scene_id': row['origin_scene_id'],
                'context': context if isinstance(context, dict) else {}, 'created_at': row['created_at']}

    def image_asset_rows(self, conn, tid):
        return conn.execute('SELECT * FROM image_assets WHERE task_id=? ORDER BY id', (tid,)).fetchall()

    def render_scenes(self, conn, tid, scenes):
        assets = {row['id']: row for row in self.image_asset_rows(conn, tid)}
        result = []
        for scene in scenes:
            asset = assets.get(scene.get('image_asset_id'))
            context = self.image_asset(tid, asset)['context'] if asset else {}
            references = {'fixed_references': context.get('fixed_references', []),
                          'previous_scene': context.get('previous_scene')}
            result.append({**scene, 'image_references': references})
        return result

    def upload_image(self, tid, content, content_type):
        task_dir = self.config.data / str(tid)
        asset = validate_and_normalize(task_dir, content, content_type,
                                       self.config.max_upload_image_bytes, self.config.max_image_pixels)
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            if task['archived'] or task['operation_id']:
                raise HTTPException(409, '任务已归档或正在处理，暂时不能导入图片')
            existing = conn.execute('SELECT * FROM image_assets WHERE task_id=? AND sha256=? AND source=?',
                                    (tid, asset['sha256'], 'upload')).fetchone()
            if existing:
                row = existing
                created = False
            else:
                cursor = conn.execute('INSERT INTO image_assets(task_id,source,path,sha256,mime,width,height,byte_size,'
                                      'origin_scene_id,context_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                      (tid, 'upload', asset['path'], asset['sha256'], asset['mime'], asset['width'],
                                       asset['height'], asset['byte_size'], None, json.dumps({'source': 'upload'}, ensure_ascii=False), now()))
                row = conn.execute('SELECT * FROM image_assets WHERE id=?', (cursor.lastrowid,)).fetchone()
                created = True
        return self.image_asset(tid, row), created

    def idle(self, task, allowed):
        if task['archived']:
            raise HTTPException(409, '任务已归档，不能修改；文件仍保留')
        if task['operation_id']:
            raise HTTPException(409, '任务有操作正在执行，请等待完成')
        if task['status'] not in allowed:
            raise HTTPException(409, '当前任务状态不允许此操作')

    def capacity(self, conn, kind):
        if shutil.disk_usage(self.config.data).free < self.config.min_free_mb * 1024 * 1024:
            raise HTTPException(507, '磁盘可用空间低于安全阈值，请管理员备份并清理或迁移存储；任务归档不会释放空间')
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
        started = now()
        conn.execute('UPDATE tasks SET status=?,error=NULL,operation_id=?,operation_kind=?,operation_scene=?,'
                     'operation_started_at=?,progress_stage=?,progress_scene=?,progress_started_at=? WHERE id=?',
                     (status, operation, kind, idx, started, kind, idx, started, tid))
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
        except UserFacingError as exc:
            LOG.error('task=%s operation=%s code=%s', tid, operation, exc.code)
            if not self.store.failure:
                self.fail(tid, operation, str(exc))
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
            job_records.cancel_queued(conn, operation, '操作失败，剩余队列终止。')
            conn.execute("UPDATE tasks SET status='failed',error=?,pause_requested=0,operation_id=NULL,operation_kind=NULL,operation_scene=NULL WHERE id=?",
                         (message, tid))

    def finish(self, tid, operation, status, video=None):
        try:
            self.providers.notify(tid, status)
        except Exception as exc:
            LOG.warning('task=%s notification_failed=%s', tid, type(exc).__name__)
        with self.store.connect(write=True) as conn:
            job_records.cancel_queued(conn, operation, '操作完成，未执行的队列终止。')
            conn.execute('UPDATE tasks SET status=?,error=NULL,pause_requested=0,video_path=COALESCE(?,video_path),'
                         'operation_id=NULL,operation_kind=NULL,operation_scene=NULL WHERE id=? AND operation_id=?',
                         (status, video, tid, operation))
        LOG.info('task=%s operation=%s status=%s', tid, operation, status)

    def validate_plan(self, plan):
        c = self.config
        if not isinstance(plan, list) or not 1 <= len(plan) <= c.max_scenes:
            raise ValueError('分镜数量超过限制或结果无效；未截断、未生图')
        result, ids = [], set()
        for scene in plan:
            if isinstance(scene, str):
                scene = {'narration': scene, 'visual_prompt': scene}
            if not isinstance(scene, dict) or set(scene) - {'id', 'narration', 'visual_prompt', 'image'}:
                raise ValueError('分镜字段无效')
            item = {}
            for field, limit in (('narration', c.max_narration), ('visual_prompt', 4000)):
                text = scene.get(field)
                if not isinstance(text, str) or not text.strip() or len(text.strip()) > limit:
                    raise ValueError('旁白或画面描述为空或超过单镜长度限制；未生图')
                item[field] = text.strip()
            if scene.get('id') is not None:
                identity = scene['id']
                if type(identity) is not int or identity < 1 or identity in ids:
                    raise ValueError('分镜标识无效或重复')
                ids.add(identity)
                item['id'] = identity
            image = scene.get('image')
            if image is not None:
                references = image.get('fixed_reference_asset_ids', [])
                previous = image.get('previous_scene_id')
                if (not isinstance(image, dict) or not set(image) <= {'source', 'asset_id', 'fixed_reference_asset_ids', 'previous_scene_id'} or
                        image.get('source') not in ('generated', 'asset')):
                    raise ValueError('图片来源设置无效')
                if image['source'] == 'asset' and (type(image.get('asset_id')) is not int or image['asset_id'] < 1):
                    raise ValueError('使用已有图片时必须选择有效图片')
                if not isinstance(references, list) or any(type(value) is not int or value < 1 for value in references) or len(references) > 16 or len(set(references)) != len(references):
                    raise ValueError('固定参考图设置无效')
                if image['source'] == 'asset' and references:
                    raise ValueError('直接使用已有图片时不能配置生成参考')
                if image['source'] == 'asset' and previous is not None:
                    raise ValueError('直接使用已有图片时不能引用前镜')
                if previous is not None and (type(previous) is not int or previous < 1):
                    raise ValueError('前镜引用无效')
                item['image'] = {'source': image['source'], 'asset_id': image.get('asset_id'),
                                 'fixed_reference_asset_ids': references, 'previous_scene_id': previous}
            result.append(item)
        if sum(len(s['narration']) for s in result) > c.max_script:
            raise ValueError('分镜总长度超过限制；未生图')
        return result

    def normalize_brief(self, purpose='', audience='', focus='', target_duration_seconds=None):
        texts = [' '.join((value or '').split()) for value in (purpose, audience, focus)]
        has_duration = target_duration_seconds not in (None, '')
        if not any(texts) and not has_duration:
            return None
        if not all(texts) or not has_duration:
            raise HTTPException(422, '用途、受众、重点和目标时长需要一起填写')
        if type(target_duration_seconds) is not int:
            raise HTTPException(422, '目标时长必须是整数秒')
        upper = max_target_seconds(self.config.max_video_seconds)
        if upper < MIN_TARGET_SECONDS:
            raise HTTPException(422, '当前成片时长上限低于 15 秒，无法设置目标时长')
        if not MIN_TARGET_SECONDS <= target_duration_seconds <= upper:
            raise HTTPException(422, f'目标时长必须为 {MIN_TARGET_SECONDS}–{upper} 秒')
        for name, text, cap in (('用途', texts[0], 40), ('受众', texts[1], 40), ('重点', texts[2], 80)):
            if len(text) > cap:
                raise HTTPException(422, f'{name}不能超过 {cap} 字')
        return {'purpose': texts[0], 'audience': texts[1], 'focus': texts[2],
                'target_duration_seconds': target_duration_seconds}

    def stored_brief(self, task):
        if not task.get('target_duration_seconds'):
            return None
        return {key: task[key] for key in ('purpose', 'audience', 'focus', 'target_duration_seconds')}

    def clean_title(self, title):
        title = ' '.join((title or '').split())
        if len(title) > 80:
            raise HTTPException(422, '项目名称不能超过 80 字')
        return title

    def submit(self, script, owner="admin", aspect_ratio='16:9', image_fit='contain',
               purpose='', audience='', focus='', target_duration_seconds=None, title=''):
        video_layout(aspect_ratio, image_fit)
        script = script.replace('\r\n', '\n').replace('\r', '\n').strip()
        if not script or len(script) > self.config.max_script:
            raise HTTPException(422, f'文稿长度必须为 1–{self.config.max_script} 字符')
        title = self.clean_title(title)
        brief = self.normalize_brief(purpose, audience, focus, target_duration_seconds)
        fingerprint = script_digest(script if brief is None else '\n'.join([
            script, brief['purpose'], brief['audience'], brief['focus'], str(brief['target_duration_seconds'])]))
        snapshot = self.models.snapshot()
        self.task_config({'model_config': snapshot}, ('llm',))
        with self.store.connect(write=True) as conn:
            duplicate = conn.execute("SELECT id FROM tasks WHERE script_hash=? AND owner=? AND status!='done' AND archived=0 LIMIT 1", (fingerprint, owner)).fetchone()
            if duplicate:
                raise HTTPException(409, f'相同文稿和制作要求已有任务 {duplicate[0]}，请打开原任务继续')
            self.capacity(conn, 'plan')
            day = now()[:10]
            if owner != 'admin':
                count = conn.execute('SELECT COUNT(*) FROM tasks WHERE owner=? AND substr(created_at,1,10)=?', (owner, day)).fetchone()[0]
                if count >= self.config.max_visitor_tasks:
                    raise HTTPException(429, '当前访客今日任务数已达上限')
            self.reserve(conn, 'task', self.config.max_daily_tasks)
            tid = conn.execute('INSERT INTO tasks(script,script_hash,status,created_at,owner,model_config,aspect_ratio,image_fit,'
                               'purpose,audience,focus,target_duration_seconds,title) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (script, fingerprint, 'submitted', now(), owner, snapshot, aspect_ratio, image_fit,
                                *([brief['purpose'], brief['audience'], brief['focus'], brief['target_duration_seconds']] if brief else ('', '', '', 0)), title)).lastrowid
            operation = self.claim(conn, tid, 'plan', 'generating')
        self.dispatch(tid, operation, lambda: self.plan(tid, operation, script))
        return {'task_id': tid}

    def plan(self, tid, operation, script):
        provider = self.provider_for(tid, ('llm',))
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            if self.task_config(task).llm_connection[1]:
                self.reserve(conn, 'llm', self.config.max_daily_llm)
            brief = self.stored_brief(task)
        try:
            plan = self.validate_plan(provider.plan(script, brief))
        except ValueError:
            self.fail(tid, operation, '分镜结果无效或超出限制，请缩短文稿或检查模型输出；未截断内容、未生成图音。')
            return
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            if task['operation_id'] != operation:
                return
            old = self.scenes(conn, tid)
            floor = self.next_revision(task, old) if old or task['material_revision_floor'] else 0
            conn.execute('DELETE FROM scenes WHERE task_id=?', (tid,))
            changed_upstream_ids = set()
            for idx, scene in enumerate(plan):
                conn.execute('INSERT INTO scenes(task_id,idx,narration,visual_prompt,img_revision,aud_revision) VALUES(?,?,?,?,?,?)',
                             (tid, idx, scene['narration'], scene['visual_prompt'], floor, floor))
            conn.execute("UPDATE tasks SET plan_complete=1,expected_scenes=?,plan_revision=plan_revision+1,status='planned',error=NULL,"
                         'operation_id=NULL,operation_kind=NULL,operation_scene=NULL,material_revision_floor=? WHERE id=?', (len(plan), floor, tid))

    def verify_plan(self, task, scenes):
        if not task['plan_complete'] or len(scenes) != task['expected_scenes'] or not scenes or [s['idx'] for s in scenes] != list(range(len(scenes))):
            raise HTTPException(409, '完整分镜计划缺失，请人工确认后重新规划')

    def next_revision(self, task, scenes):
        return plan_impact.next_revision(task['material_revision_floor'], scenes)

    def missing_images(self, tid, scenes):
        return sum(not self.image_available(tid, s) for s in scenes)

    @staticmethod
    def image_spec(scene):
        return reference_rules.image_spec(scene)

    @classmethod
    def image_source(cls, scene):
        return reference_rules.image_source(scene)

    @classmethod
    def fixed_reference_ids(cls, scene):
        return reference_rules.fixed_reference_ids(scene)

    @classmethod
    def previous_scene_id(cls, scene):
        return reference_rules.previous_scene_id(scene)

    def image_available(self, tid, scene):
        return scene['img_state'] in ('ready', 'placeholder') and bool(scene['image_path']) and \
            (self.config.data / str(tid) / scene['image_path']).is_file()

    def image_request_scene_ids(self, tid, scenes):
        available = {scene['id'] for scene in scenes if self.image_available(tid, scene)}
        return plan_impact.image_request_scene_ids(scenes, available)

    def image_requests_needed(self, tid, scenes):
        return len(self.image_request_scene_ids(tid, scenes))

    def image_capabilities(self, task):
        try:
            protocol = self.models.document_for(task.get('model_config', ''))['image']['reference_protocol']
        except (KeyError, TypeError, ValueError):
            protocol = 'none'
        available = self.models.availability(task.get('model_config', '')).get('image', False)
        supported = protocol == 'openai-images-edit' and self.has_gateway(task, 'image') and available
        return {'reference_protocol': protocol, 'fixed_references_supported': supported,
                'max_fixed_references': 16}

    def verify_image_references(self, conn, tid, task, scenes, force=False):
        capabilities = self.image_capabilities(task)
        scene_by_id = {scene['id']: scene for scene in scenes}
        pending = self.image_request_scene_ids(tid, scenes)
        for scene in scenes:
            references = self.fixed_reference_ids(scene)
            previous_id = self.previous_scene_id(scene)
            if not references and previous_id is None or self.image_source(scene) != 'generated':
                continue
            if not force and scene['id'] not in pending:
                continue
            if not capabilities['fixed_references_supported'] or not self.has_gateway(task, 'image'):
                raise HTTPException(409, '当前任务配置未启用参考图协议，不能发起带参考的生图请求')
            settings = self.task_config(task, ('image',))
            if not settings.image_connection[1]:
                raise HTTPException(409, '图片网关密钥不可用，不能发起带参考的生图请求')
            if len(references) + (1 if previous_id is not None else 0) > capabilities['max_fixed_references']:
                raise HTTPException(422, f"固定与前镜参考合计最多 {capabilities['max_fixed_references']} 张")
            if previous_id is not None and not reference_rules.reference_order_ok(scene, scene_by_id):
                raise HTTPException(409, '前镜引用必须是排序在前的稳定镜头；请重新选择引用')
            for asset_id in references:
                asset = conn.execute('SELECT * FROM image_assets WHERE id=? AND task_id=?', (asset_id, tid)).fetchone()
                path = self.config.data / str(tid) / asset['path'] if asset else None
                if not asset or not path.is_file():
                    raise HTTPException(409, '固定参考图缺失，请重新选择或上传；未发起外部请求')
                if asset['byte_size'] > 50 * 1024 * 1024:
                    raise HTTPException(422, '固定参考图超过供应商 50MB 上限')

    @staticmethod
    def audio_duration(scenes):
        durations = [s.get('audio_duration_seconds') for s in scenes]
        if not scenes or any(s['aud_state'] != 'ready' for s in scenes):
            return None
        if any(type(d) not in (float, int) or not math.isfinite(d) or d <= 0 for d in durations):
            return None
        return sum(durations)

    def save_plan(self, tid, plan, revision, aspect_ratio=None, image_fit=None, target_duration_seconds=None):
        plan = self.validate_plan(plan)
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            old = self.scenes(conn, tid)
            layout, layout_changed, floor, assets = self._plan_change_checks(
                conn, tid, task, old, plan, revision, aspect_ratio, image_fit, target_duration_seconds)
            duration, fingerprint = task['target_duration_seconds'], task['script_hash']
            if target_duration_seconds is not None and target_duration_seconds != task['target_duration_seconds']:
                brief = self.normalize_brief(task['purpose'], task['audience'], task['focus'], target_duration_seconds)
                duration = brief['target_duration_seconds']
                fingerprint = script_digest('\n'.join([task['script'], brief['purpose'], brief['audience'],
                                                       brief['focus'], str(duration)]))
            try:
                rows, changed_upstream_ids = plan_impact.build_scene_rows(
                    old, plan, {'task_id': tid, 'floor': floor, 'layout_changed': layout_changed, 'assets': assets})
            except plan_impact.ImpactError as exc:
                raise HTTPException(exc.status, exc.message) from None
            conn.execute('DELETE FROM scenes WHERE task_id=?', (tid,))
            for row in rows:
                columns = ','.join(row)
                cursor = conn.execute(f'INSERT INTO scenes({columns}) VALUES({",".join("?" for _ in row)})', tuple(row.values()))
                if 'id' not in row:
                    row['id'] = cursor.lastrowid
            try:
                invalidations = plan_impact.finalize_impact(rows, changed_upstream_ids)['invalidations']
            except plan_impact.ImpactError as exc:
                raise HTTPException(exc.status, exc.message) from None
            for invalidation in invalidations:
                conn.execute("UPDATE scenes SET img_state='pending',img_revision=?,img_approved=0,img_ok=0, "
                             "img_error=? "
                             'WHERE task_id=? AND id=?', (floor, invalidation['error'], tid, invalidation['id']))
            conn.execute("UPDATE tasks SET expected_scenes=?,plan_revision=plan_revision+1,status='planned',error=NULL,"
                         'video_path=NULL,material_revision_floor=?,aspect_ratio=?,image_fit=?,target_duration_seconds=?,script_hash=? WHERE id=?',
                         (len(plan), floor, layout['aspect_ratio'], layout['image_fit'], duration, fingerprint, tid))
        return {'ok': True}

    def _plan_change_checks(self, conn, tid, task, old, plan, revision, aspect_ratio, image_fit, target_duration_seconds):
        """保存与预演共用的只读校验；返回 (layout, layout_changed, floor, assets)。

        校验顺序与 save_plan 一致：idle 状态、完整计划、制作要求与重复、
        版本号、布局。任何违规抛 HTTPException，不产生写入。
        """
        self.idle(task, {'planned', 'review', 'failed', 'interrupted'})
        self.verify_plan(task, old)
        duration = task['target_duration_seconds']
        fingerprint = task['script_hash']
        if target_duration_seconds is not None and target_duration_seconds != duration:
            if not self.stored_brief(task):
                raise HTTPException(409, '这个任务没有制作要求，不能单独修改目标时长')
            brief = self.normalize_brief(task['purpose'], task['audience'], task['focus'], target_duration_seconds)
            duration = brief['target_duration_seconds']
            fingerprint = script_digest('\n'.join([task['script'], brief['purpose'], brief['audience'],
                                                   brief['focus'], str(duration)]))
            duplicate = conn.execute("SELECT id FROM tasks WHERE script_hash=? AND owner=? AND status!='done' "
                                     'AND archived=0 AND id!=? LIMIT 1',
                                     (fingerprint, task['owner'], tid)).fetchone()
            if duplicate:
                raise HTTPException(409, f'相同文稿和制作要求已有任务 {duplicate[0]}，请打开原任务继续')
        if task['plan_revision'] != revision:
            raise HTTPException(409, '分镜已被更新，请刷新后重新编辑')
        layout = video_layout(aspect_ratio or task['aspect_ratio'], image_fit or task['image_fit'])
        layout_changed = any(layout[k] != task[k] for k in ('aspect_ratio', 'image_fit'))
        floor = self.next_revision(task, old)
        assets = {row['id']: dict(row) for row in self.image_asset_rows(conn, tid)}
        return layout, layout_changed, floor, assets

    def preview_plan_impact(self, tid, plan, revision, aspect_ratio=None, image_fit=None, target_duration_seconds=None):
        """服务端修改影响预演：与保存同一套校验和规则，只读、不触发快照。"""
        plan = self.validate_plan(plan)
        with self.store.connect() as conn:
            task = self.task(conn, tid)
            old = self.scenes(conn, tid)
            layout, layout_changed, floor, assets = self._plan_change_checks(
                conn, tid, task, old, plan, revision, aspect_ratio, image_fit, target_duration_seconds)
        try:
            matched = plan_impact.match_plan_to_scenes(old, plan)
            rows, changed_upstream_ids = plan_impact.build_scene_rows(
                old, plan, {'task_id': tid, 'floor': floor, 'layout_changed': layout_changed, 'assets': assets})
        except plan_impact.ImpactError as exc:
            raise HTTPException(exc.status, exc.message) from None
        for row in rows:
            row.setdefault('id', -(row['idx'] + 1))
        try:
            invalidations = plan_impact.finalize_impact(rows, changed_upstream_ids)['invalidations']
        except plan_impact.ImpactError as exc:
            raise HTTPException(exc.status, exc.message) from None
        invalidated = {item['id'] for item in invalidations}
        available = set()
        for row in rows:
            path = self.config.data / str(tid) / row['image_path'] if row['image_path'] else None
            if row['img_state'] in ('ready', 'placeholder') and path and path.is_file():
                available.add(row['id'])
        scenes = []
        for idx, (row, previous) in enumerate(zip(rows, matched)):
            if previous is None:
                image_action = 'select' if row.get('image_asset_id') else 'redo'
            elif row['img_state'] == 'pending':
                image_action = 'redo'
            elif plan_impact.image_spec(previous) != plan_impact.image_spec(row) or row['image_path'] != previous['image_path']:
                image_action = 'select'
            else:
                image_action = 'keep'
            scenes.append({
                'id': row['id'] if row['id'] > 0 else None,
                'index': idx,
                'image': image_action,
                'audio': 'redo' if (previous is None or row['aud_state'] == 'pending') else 'keep',
                'img_approval_kept': row['img_state'] == 'ready' and row['img_revision'] > 0 and row['img_approved'] == row['img_revision'],
                'aud_approval_kept': row['aud_state'] == 'ready' and row['aud_revision'] > 0 and row['aud_approved'] == row['aud_revision'],
                'invalidated_by_upstream': row['id'] in invalidated,
            })
        removed = [scene['id'] for scene in old if scene['id'] not in {item['id'] for item in scenes}]
        return {'ok': True, 'plan_revision': task['plan_revision'], 'image_requests_needed':
                len(plan_impact.image_request_scene_ids(rows, available)),
                'scenes': scenes, 'removed_scene_ids': removed}

    def image_budget(self, task, amount):
        if amount and self.has_gateway(task, 'image') and task['image_requests'] + amount > self.config.max_image_requests:
            raise HTTPException(429, '此任务生图请求数已达配置上限；不会继续调用付费接口')

    def generate(self, tid, confirmed, revision=None, preview=False):
        if not confirmed:
            raise HTTPException(409, '请先确认分镜及图片数量；生成将调用外部服务')
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            self.idle(task, {'planned'})
            scenes = self.scenes(conn, tid)
            self.verify_plan(task, scenes)
            if revision is not None and task['plan_revision'] != revision:
                raise HTTPException(409, '分镜计划已更新，请刷新后确认')
            if preview and not any(scene['idx'] == 0 for scene in scenes):
                raise HTTPException(409, '没有可预演的第一镜')
            budget = narration_budget_chars(task.get('target_duration_seconds') or 0, self.config.max_script)
            used = sum(len(scene['narration']) for scene in scenes)
            if budget is not None and used > budget:
                raise HTTPException(409, f'旁白共 {used} 字，超过目标时长对应的 {budget} 字预算。请缩短旁白或提高目标时长后再生成')
            selected = [scene for scene in scenes if scene['idx'] == 0] if preview else scenes
            unavailable_assets = [scene for scene in selected if self.image_source(scene) == 'asset' and not self.image_available(tid, scene)]
            if unavailable_assets:
                raise HTTPException(409, '所选已有图片缺失，请重新上传或改回生成画面；不会自动请求生图')
            self.verify_image_references(conn, tid, task, selected)
            needed = self.image_requests_needed(tid, selected)
            self.task_config(task, ('image',) if needed else ())
            self.image_budget(task, needed)
            operation = self.claim(conn, tid, 'generate', 'generating')
            self.create_generation_jobs(conn, tid, operation, task, selected)
        self.dispatch(tid, operation, lambda: self.generate_missing(tid, operation, only_idx=0 if preview else None))
        return {'ok': True}

    def create_generation_jobs(self, conn, tid, operation, task, selected):
        """预建本操作的素材作业闭包：缺失图片含下游传播、缺失配音独立。

        生图作业依赖同在本次范围内的前镜作业；使用已有图片的镜头只复用
        校验，不建付费图片作业。与 claim 同一写事务（I1）。
        """
        pending_images = self.image_request_scene_ids(tid, selected)
        audio_pending = {scene['id'] for scene in selected
                         if scene['aud_state'] != 'ready' or not scene['audio_path']}
        entries = []
        image_job_ids = {}
        for scene in selected:
            if scene['id'] in pending_images:
                previous_id = self.previous_scene_id(scene)
                depends = [image_job_ids[previous_id]] if previous_id in image_job_ids else []
                job_id = job_records.create_jobs(
                    conn, tid, operation, task['plan_revision'],
                    [{'scene_id': scene['id'], 'scene_index': scene['idx'], 'kind': 'img', 'depends_on': depends}])[0]
                image_job_ids[scene['id']] = job_id
        for scene in selected:
            if scene['id'] in audio_pending:
                entries.append({'scene_id': scene['id'], 'scene_index': scene['idx'], 'kind': 'aud'})
        job_records.create_jobs(conn, tid, operation, task['plan_revision'], entries)

    def request_pause(self, tid):
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            if task['archived']:
                raise HTTPException(409, '任务已归档，不能修改')
            if not task['operation_id'] or task['operation_kind'] not in ('generate', 'resume', 'img', 'aud'):
                raise HTTPException(409, '当前没有进行中的生成请求')
            conn.execute('UPDATE tasks SET pause_requested=1 WHERE id=?', (tid,))
        return {'ok': True, 'message': '将在当前请求完成后暂停，已完成的素材会保留。'}

    def pause_requested(self, tid, operation):
        with self.store.connect() as conn:
            task = self.task(conn, tid)
            return task['operation_id'] == operation and bool(task['pause_requested'])

    def finish_paused(self, tid, operation):
        with self.store.connect(write=True) as conn:
            if self.task(conn, tid)['operation_id'] != operation:
                return
            job_records.cancel_queued(conn, operation, '已暂停，旧队列终止；恢复会创建新操作。')
            conn.execute("UPDATE tasks SET status='interrupted',error=?,pause_requested=0,operation_id=NULL,"
                         'operation_kind=NULL,operation_scene=NULL WHERE id=?',
                         ('已在当前请求完成后暂停。已完成的素材保留，恢复会继续补齐尚未生成的部分。', tid))

    def generation_status(self, tid, scenes):
        def present(scene, kind):
            column = 'image_path' if kind == 'img' else 'audio_path'
            return scene[f'{kind}_state'] in ('ready', 'placeholder') and bool(scene[column]) and (self.config.data / str(tid) / scene[column]).is_file()
        return 'review' if scenes and all(present(scene, 'img') and present(scene, 'aud') for scene in scenes) else 'planned'

    def generate_missing(self, tid, operation, recover_video=False, retry_images_confirmed=False, only_idx=None):
        with self.store.connect() as conn:
            task, scenes = self.task(conn, tid), self.scenes(conn, tid)
            job_map = {}
            for job in job_records.jobs_for_operation(conn, operation):
                job_map.setdefault(job['scene_id'], {})[job['kind']] = job['id']
        self.verify_plan(task, scenes)
        if recover_video and all(self.approved(s) for s in scenes):
            with self.store.connect() as conn:
                frozen_scenes = self.render_scenes(conn, tid, scenes)
            recovered = self.renderer.recover(self.config.data / str(tid), frozen_scenes, video_layout(task['aspect_ratio'], task['image_fit']))
            if recovered:
                self.finish(tid, operation, 'done', recovered)
                return
        for scene in scenes:
            if only_idx is not None and scene['idx'] != only_idx:
                continue
            # 上一镜生成后可能刚使当前镜头失效，不能沿用循环开始前的状态。
            with self.store.connect() as conn:
                scene = dict(conn.execute('SELECT * FROM scenes WHERE task_id=? AND id=?', (tid, scene['id'])).fetchone())
            for kind in ('img', 'aud'):
                if self.pause_requested(tid, operation):
                    self.finish_paused(tid, operation)
                    return
                state = scene[f'{kind}_state']
                path = scene['image_path' if kind == 'img' else 'audio_path']
                if recover_video and kind == 'img' and self.has_gateway(task, 'image') and not retry_images_confirmed and (state not in ('ready', 'placeholder') or not path or not (self.config.data / str(tid) / path).is_file()):
                    self.fail(tid, operation, '图片状态已变化，需人工确认后重新请求生图，未自动计费。')
                    return
                if state in ('ready', 'placeholder') and path:
                    if kind == 'img' and self.image_source(scene) == 'asset':
                        try:
                            self.validator(self.config.data / str(tid) / path, kind)
                            continue
                        except Exception:
                            self.fail(tid, operation, '所选已有图片损坏或缺失，请重新上传；不会自动请求生图。')
                            return
                    try:
                        duration = self.validator(self.config.data / str(tid) / path, kind)
                        if kind == 'aud' and type(duration) in (int, float):
                            with self.store.connect(write=True) as conn:
                                conn.execute('UPDATE scenes SET audio_duration_seconds=? WHERE task_id=? AND idx=?', (duration, tid, scene['idx']))
                        elif kind == 'img' and isinstance(duration, dict):
                            with self.store.connect(write=True) as conn:
                                conn.execute('UPDATE scenes SET image_width=?,image_height=? WHERE task_id=? AND idx=?',
                                             (duration.get('width'), duration.get('height'), tid, scene['idx']))
                        continue
                    except Exception:
                        with self.store.connect(write=True) as conn:
                            conn.execute(f"UPDATE scenes SET {kind}_state='failed',{kind}_approved=0,{kind}_ok=0,{kind}_error=? WHERE task_id=? AND idx=?",
                                         ('已有素材损坏或缺失，请重新生成。', tid, scene['idx']))
                        if kind == 'img' and self.has_gateway(task, 'image') and not retry_images_confirmed:
                            self.fail(tid, operation, '检测到图片损坏，需再次人工确认后请求生图，未自动计费。')
                            return
                self.asset(tid, operation, scene['idx'], kind, job_id=job_map.get(scene['id'], {}).get(kind))
        with self.store.connect() as conn:
            scenes = self.scenes(conn, tid)
        self.finish(tid, operation, self.generation_status(tid, scenes))

    def asset(self, tid, operation, idx, kind, job_id=None):
        if self.stopping.is_set():
            raise InterruptedError('Service is stopping')
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            if task['operation_id'] != operation:
                raise RuntimeError('操作所有权失效')
            settings = self.task_config(task, ('image',) if kind == 'img' else ())
            scene = dict(conn.execute('SELECT * FROM scenes WHERE task_id=? AND idx=?', (tid, idx)).fetchone())
            reference_records = []
            if kind == 'img':
                for asset_id in self.fixed_reference_ids(scene):
                    row = conn.execute('SELECT * FROM image_assets WHERE id=? AND task_id=?', (asset_id, tid)).fetchone()
                    path = self.config.data / str(tid) / row['path'] if row else None
                    if not row or not path.is_file():
                        raise HTTPException(409, '固定参考图缺失，请求未发起；请重新选择或上传')
                    reference_records.append({'kind': 'fixed', 'id': row['id'], 'sha256': row['sha256'],
                                              'mime': row['mime'], 'path': path})
                previous_id = self.previous_scene_id(scene)
                if previous_id is not None:
                    upstream = conn.execute('SELECT * FROM scenes WHERE task_id=? AND id=?', (tid, previous_id)).fetchone()
                    if upstream is None or upstream['idx'] >= scene['idx']:
                        raise HTTPException(409, '前镜引用必须是排序在前的稳定镜头；请求未发起')
                    if upstream['img_state'] != 'ready' or upstream['img_placeholder'] or not upstream['image_path']:
                        raise HTTPException(409, '前镜图片尚未就绪或仍是占位图；请先生成上游镜头')
                    if not upstream['image_asset_id']:
                        path = self.config.data / str(tid) / upstream['image_path']
                        if not path.is_file():
                            raise HTTPException(409, '前镜图片文件缺失；请求未发起')
                        dimensions = self.validator(path, 'img')
                        legacy_context = {'visual_prompt': upstream['visual_prompt'], 'legacy_registration': True}
                        asset_id = conn.execute('INSERT INTO image_assets(task_id,source,path,sha256,mime,width,height,byte_size,'
                                                 'origin_scene_id,context_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                                 (tid, 'generated', upstream['image_path'], digest(path),
                                                  'image/png' if path.suffix == '.png' else 'image/jpeg',
                                                  dimensions.get('width') or 0, dimensions.get('height') or 0,
                                                  path.stat().st_size, upstream['id'],
                                                  json.dumps(legacy_context, ensure_ascii=False), now())).lastrowid
                        conn.execute('UPDATE scenes SET image_asset_id=? WHERE task_id=? AND id=?', (asset_id, tid, upstream['id']))
                        upstream = conn.execute('SELECT * FROM scenes WHERE task_id=? AND id=?', (tid, previous_id)).fetchone()
                    row = conn.execute('SELECT * FROM image_assets WHERE id=? AND task_id=?',
                                       (upstream['image_asset_id'], tid)).fetchone()
                    path = self.config.data / str(tid) / row['path'] if row else None
                    if not row or not path.is_file():
                        raise HTTPException(409, '前镜参考图缺失；请求未发起')
                    reference_records.append({'kind': 'previous_scene', 'scene_id': upstream['id'], 'id': row['id'],
                                              'sha256': row['sha256'], 'mime': row['mime'], 'path': path})
                if reference_records and (settings.image_reference_protocol != 'openai-images-edit' or not settings.image_connection[1]):
                    raise HTTPException(409, '当前任务配置未启用参考图协议，请求未发起')
            if kind == 'img' and settings.image_connection[1]:
                self.image_budget(task, 1)
                self.reserve(conn, 'image', self.config.max_daily_images)
                conn.execute('UPDATE tasks SET image_requests=image_requests+1 WHERE id=?', (tid,))
            if kind == 'aud':
                self.reserve(conn, 'audio', self.config.max_daily_audio)
            if job_id is not None:
                # 输入冻结与条件认领同在额度事务内；快照确认后才调用供应商（I1）。
                references_meta = []
                for record in reference_records:
                    meta = {'kind': record['kind'], 'asset_id': record['id'], 'sha256': record['sha256']}
                    if record['kind'] == 'previous_scene':
                        meta['scene_id'] = record['scene_id']
                    references_meta.append(meta)
                input_json, input_digest = job_records.input_snapshot(kind, scene, settings, references_meta)
                if not job_records.claim(conn, job_id, input_json, input_digest,
                                         expected_material_revision=scene[f'{kind}_revision']):
                    raise HTTPException(409, '作业已被认领或已终止，未发起外部请求')
            conn.execute('UPDATE tasks SET progress_stage=?,progress_scene=?,progress_started_at=? WHERE id=?', (kind, idx, now(), tid))
            conn.execute(f"UPDATE scenes SET {kind}_state='running',{kind}_approved=0,{kind}_ok=0,{kind}_error=NULL WHERE task_id=? AND idx=?", (tid, idx))
        task_dir = self.config.data / str(tid)
        folder = task_dir / 'assets' / operation
        folder.mkdir(parents=True, exist_ok=True)
        suffix = '.png' if kind == 'img' else '.mp3'
        output = folder / f'{idx:03d}-{kind}{suffix}'
        temp = folder / f'{idx:03d}-{kind}.part{suffix}'
        provider = self.provider_for(tid, ('image',) if kind == 'img' else ())
        placeholder = kind == 'img' and not settings.image_connection[1]
        references = []
        reference_context = []
        previous_context = None
        for record in reference_records:
            content = record['path'].read_bytes()
            if digest(record['path']) != record['sha256']:
                raise HTTPException(409, '固定参考图在请求前发生变化；未发起外部请求，请重新选择')
            references.append({'mime': record['mime'], 'content': content})
            if record['kind'] == 'fixed':
                reference_context.append({'asset_id': record['id'], 'sha256': record['sha256']})
            else:
                previous_context = {'scene_id': record['scene_id'], 'asset_id': record['id'], 'sha256': record['sha256']}
        try:
            if placeholder:
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-f', 'lavfi', '-i',
                             'color=c=0x223344:s=640x360', '-frames:v', '1', str(temp)], 30, stopping=self.stopping)
            elif kind == 'img':
                if references:
                    provider.image(scene['visual_prompt'], temp, references=references)
                else:
                    provider.image(scene['visual_prompt'], temp)
            else:
                provider.audio(scene['narration'], temp)
            duration = self.validator(temp, kind)
            temp.replace(output)
        except Exception as exc:
            message = str(exc) if isinstance(exc, UserFacingError) else ('素材生成失败；生图结果可能未知，未自动重试。' if kind == 'img' else '配音生成或校验失败，请核对旁白与音色后恢复。')
            final_state = 'unknown' if kind == 'img' and settings.image_connection[1] else 'failed'
            with self.store.connect(write=True) as conn:
                conn.execute(f'UPDATE scenes SET {kind}_state=?,{kind}_error=? WHERE task_id=? AND idx=?',
                             (final_state, message, tid, idx))
                if job_id is not None:
                    job_records.finish(conn, job_id, final_state,
                                       error=job_records.sanitize_error(message, (settings.image_connection[1], settings.key)))
            raise
        with self.store.connect(write=True) as conn:
            if self.task(conn, tid)['operation_id'] != operation:
                raise RuntimeError('任务所有权已变化，未发布素材')
            current = conn.execute('SELECT * FROM scenes WHERE task_id=? AND idx=?', (tid, idx)).fetchone()
            if job_id is not None:
                job = job_records.job_row(conn, job_id)
                if job and job['expected_material_revision'] is not None and \
                        current[f'{kind}_revision'] != job['expected_material_revision']:
                    raise RuntimeError('素材版本已变化，未发布')
            column = 'image_path' if kind == 'img' else 'audio_path'
            relative = str(output.relative_to(task_dir))
            conn.execute(f'UPDATE scenes SET {kind}_state=?,{kind}_revision={kind}_revision+1,'
                         f'{kind}_approved=0,{kind}_ok=0,{kind}_error=NULL,{column}=? WHERE task_id=? AND idx=?',
                         ('placeholder' if placeholder else 'ready', relative, tid, idx))
            if kind == 'img':
                dimensions = duration if isinstance(duration, dict) else {}
                conn.execute('UPDATE scenes SET img_placeholder=?,image_width=?,image_height=? WHERE task_id=? AND idx=?',
                             (int(placeholder), dimensions.get('width'), dimensions.get('height'), tid, idx))
                if not placeholder:
                    context = {'visual_prompt': scene['visual_prompt'], 'model': settings.image_model,
                               'size': settings.image_size, 'quality': settings.image_quality,
                               'reference_protocol': settings.image_reference_protocol,
                               'fixed_references': reference_context, 'previous_scene': previous_context}
                    asset_id = conn.execute('INSERT INTO image_assets(task_id,source,path,sha256,mime,width,height,byte_size,'
                                 'origin_scene_id,context_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                 (tid, 'generated', relative, digest(output), 'image/png' if output.suffix == '.png' else 'image/jpeg',
                                  dimensions.get('width') or 0, dimensions.get('height') or 0, output.stat().st_size,
                                  scene['id'], json.dumps(context, ensure_ascii=False), now())).lastrowid
                    conn.execute('UPDATE scenes SET image_asset_id=? WHERE task_id=? AND idx=?', (asset_id, tid, idx))
                    current_scenes = self.scenes(conn, tid)
                    dependent_floor = self.next_revision(task, current_scenes)
                    for dependent in plan_impact.dependents_to_invalidate(current_scenes, scene['id']):
                        conn.execute("UPDATE scenes SET img_state='pending',img_revision=?,img_approved=0,img_ok=0,"
                                     "img_error='前镜图片已更新；请重新生成或改选固定参考。' WHERE task_id=? AND id=?",
                                     (dependent_floor, tid, dependent['id']))
            else:
                conn.execute('UPDATE scenes SET audio_duration_seconds=? WHERE task_id=? AND idx=?',
                             (duration if type(duration) in (int, float) else None, tid, idx))
            if job_id is not None:
                # 成功结果要等媒体校验、版本发布和持久化确认后才可见（手册 W3）。
                result = {'scene_id': scene['id'], 'kind': kind, 'path': relative,
                          'sha256': digest(output), 'revision': current[f'{kind}_revision'] + 1}
                if kind == 'img':
                    result['placeholder'] = bool(placeholder)
                    if not placeholder:
                        result['asset_id'] = asset_id
                job_records.finish(conn, job_id, 'succeeded', result=result)

    def regen(self, tid, idx, kind, confirmed, expected_revision=None, request_key=None, requester=None):
        if kind not in ('img', 'aud'):
            raise HTTPException(422, '素材类型无效')
        if kind == 'img' and (type(expected_revision) is not int or expected_revision < 0 or not request_key):
            raise HTTPException(422, '生图重做必须提供当前素材版本和幂等键')
        parameter_hash = script_digest(f'{tid}:{idx}:{kind}:{expected_revision}:{confirmed}')
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            owner = requester or task['owner']
            if kind == 'img':
                previous = conn.execute('SELECT * FROM image_requests WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
                if previous:
                    if previous['parameter_hash'] != parameter_hash:
                        raise HTTPException(409, '相同幂等键已用于不同参数，未发起新的生图请求')
                    return {'ok': True, 'operation_id': previous['operation_id'], 'replayed': True}
            self.idle(task, {'review'})
            scene = conn.execute('SELECT * FROM scenes WHERE task_id=? AND idx=?', (tid, idx)).fetchone()
            if scene is None:
                raise HTTPException(404, '镜不存在')
            if kind == 'img':
                if scene['img_revision'] != expected_revision:
                    raise HTTPException(409, '图片已更新，请刷新后确认是否重做')
                if self.image_source(dict(scene)) == 'asset':
                    raise HTTPException(409, '此镜使用已有图片；请在分镜中重新选择图片，不会自动请求生图')
                if not self.has_gateway(task, 'image') or not confirmed:
                    raise HTTPException(409, '重做图需要配置网关并确认付费请求')
                self.verify_image_references(conn, tid, task, self.scenes(conn, tid), force=True)
                self.task_config(task, ('image',))
                self.image_budget(task, 1)
            operation = self.claim(conn, tid, kind, 'review', idx)
            job_id = job_records.create_jobs(conn, tid, operation, task['plan_revision'],
                                              [{'scene_id': scene['id'], 'scene_index': idx, 'kind': kind}])[0]
            conn.execute(f"UPDATE scenes SET {kind}_state='running',{kind}_approved=0,{kind}_ok=0 WHERE task_id=? AND idx=?", (tid, idx))
            if kind == 'img':
                conn.execute('INSERT INTO image_requests(owner,request_key,parameter_hash,task_id,operation_id,created_at) VALUES(?,?,?,?,?,?)',
                             (owner, request_key, parameter_hash, tid, operation, now()))
        def work():
            self.asset(tid, operation, idx, kind, job_id=job_id)
            self.finish(tid, operation, 'review')
        self.dispatch(tid, operation, work)
        return {'ok': True, 'operation_id': operation, 'replayed': False}

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
            total = self.audio_duration(scenes)
            if total is not None and total > self.config.max_video_seconds:
                raise HTTPException(409, str(duration_error(total, self.config.max_video_seconds)))
            scenes = self.render_scenes(conn, tid, scenes)
            operation = self.claim(conn, tid, 'render', 'rendering')
        def work():
            video = self.renderer.render(self.config.data / str(tid), operation, scenes, video_layout(task['aspect_ratio'], task['image_fit']))
            self.finish(tid, operation, 'done', video)
        self.dispatch(tid, operation, work)
        return {'ok': True}

    def retry(self, tid, force):
        with self.store.connect(write=True) as conn:
            task, scenes = self.task(conn, tid), self.scenes(conn, tid)
            self.idle(task, {'failed', 'interrupted'})
            has_plan = task['plan_complete'] and len(scenes) == task['expected_scenes'] and bool(scenes)
            asset_missing = any(self.image_source(s) == 'asset' and not self.image_available(tid, s) for s in scenes)
            generation_needed = self.image_requests_needed(tid, scenes)
            needs_confirm = not has_plan or (self.has_gateway(task, 'image') and generation_needed > 0)
            if has_plan and asset_missing:
                raise HTTPException(409, '所选已有图片缺失，请重新上传或改回生成画面；不会自动请求生图')
            if needs_confirm and not force:
                return {'needs_confirm': True, 'message': '恢复需要重新规划或请求缺失图片；结果未知的请求可能重复计费，是否继续？'}
            if has_plan:
                self.verify_image_references(conn, tid, task, scenes)
                needed = generation_needed
                if needed:
                    self.task_config(task, ('image',))
                self.image_budget(task, needed)
            else:
                self.task_config(task, ('llm',))
            operation = self.claim(conn, tid, 'resume' if has_plan else 'plan', 'generating')
            if has_plan:
                self.create_generation_jobs(conn, tid, operation, task, scenes)
        work = (lambda: self.generate_missing(tid, operation, recover_video=True, retry_images_confirmed=force)) if has_plan else (lambda: self.plan(tid, operation, task['script']))
        self.dispatch(tid, operation, work)
        return {'ok': True, 'message': '恢复已发起；成功素材和历史文件不会删除'}

    def rename(self, tid, title):
        title = self.clean_title(title)
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            self.idle(task, {'planned', 'review', 'done', 'failed', 'interrupted'})
            conn.execute('UPDATE tasks SET title=? WHERE id=?', (title, tid))
        return {'ok': True, 'message': '项目名称已保存' if title else '项目名称已清除'}

    def revise(self, tid):
        with self.store.connect(write=True) as conn:
            task = self.task(conn, tid)
            self.idle(task, {'done'})
            if not task['video_path'] or not (self.config.data / str(tid) / task['video_path']).is_file():
                raise HTTPException(409, '当前成片文件不存在，不能保留为旧版本')
            count = conn.execute('SELECT COUNT(*) FROM video_versions WHERE task_id=?', (tid,)).fetchone()[0]
            label = f'版本 {count + 1}'
            conn.execute('INSERT INTO video_versions(task_id,video_path,label,created_at) VALUES(?,?,?,?)',
                         (tid, task['video_path'], label, now()))
            conn.execute("UPDATE tasks SET status='review',video_path=NULL,error=NULL WHERE id=?", (tid,))
        return {'ok': True, 'message': f'已保留{label}。可以修改分镜；未改动的素材会继续沿用，旧成片仍可下载。'}

    def copy_next(self, tid):
        with self.store.connect(write=True) as conn:
            source = self.task(conn, tid)
            if source['archived'] or source['operation_id'] or not source['plan_complete']:
                raise HTTPException(409, '分镜计划完整且当前没有操作进行时，才能复制为新草稿')
            scenes = self.scenes(conn, tid)
            if not scenes:
                raise HTTPException(409, '没有可复制的分镜')
            owner = source['owner']
            self.capacity(conn, 'plan')
            day = now()[:10]
            if owner != 'admin':
                count = conn.execute('SELECT COUNT(*) FROM tasks WHERE owner=? AND substr(created_at,1,10)=?', (owner, day)).fetchone()[0]
                if count >= self.config.max_visitor_tasks:
                    raise HTTPException(429, '当前访客今日任务数已达上限')
            self.reserve(conn, 'task', self.config.max_daily_tasks)
            sequence = conn.execute('SELECT COUNT(*) FROM tasks WHERE copied_from=?', (tid,)).fetchone()[0] + 1
            base = source['title'] or f'任务 {tid}'
            suffix = '新草稿' if sequence == 1 else f'新草稿 {sequence}'
            title = f'{base} · {suffix}'
            if len(title) > 80:
                title = title[:80].rstrip()
            fingerprint = script_digest('\n'.join([
                source['script'], source['purpose'], source['audience'], source['focus'],
                str(source['target_duration_seconds']), f'copy:{tid}:{sequence}']))
            created = now()
            new_id = conn.execute('INSERT INTO tasks(script,script_hash,status,created_at,owner,model_config,aspect_ratio,image_fit,'
                                  'purpose,audience,focus,target_duration_seconds,title,copied_from,plan_complete,expected_scenes,plan_revision) '
                                  'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (source['script'], fingerprint, 'planned', created, owner, source['model_config'],
                                   source['aspect_ratio'], source['image_fit'], source['purpose'], source['audience'],
                                   source['focus'], source['target_duration_seconds'], title, tid, 1, len(scenes), 1)).lastrowid
            for scene in scenes:
                conn.execute('INSERT INTO scenes(task_id,idx,narration,visual_prompt) VALUES(?,?,?,?)',
                             (new_id, scene['idx'], scene['narration'], scene['visual_prompt']))
        return {'ok': True, 'task_id': new_id, 'message': '已复制原文、旁白、画面描述和制作要求。请核对旧稿内容，重新选择图片并生成配音。'}

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
        available = self.models.availability(task.get('model_config', ''))
        task['configured'] = self.has_gateway(task, 'image') and available['image']
        task['model_available'] = available
        task['image_capabilities'] = self.image_capabilities(task)
        task['config_revision'] = script_digest(task.pop('model_config'))[:12]
        task.pop('owner', None)
        task['image_request_limit'] = self.config.max_image_requests
        task['missing_images'] = self.missing_images(tid, scenes)
        task['image_requests_needed'] = self.image_requests_needed(tid, scenes)
        task['audio_duration_seconds'] = self.audio_duration(scenes)
        task['max_video_seconds'] = self.config.max_video_seconds
        task['narration_budget_chars'] = narration_budget_chars(task.get('target_duration_seconds') or 0, self.config.max_script)
        task['narration_chars'] = sum(len(scene['narration']) for scene in scenes)
        task['narration_chars_per_second'] = NARRATION_CHARS_PER_SECOND
        with self.store.connect() as conn:
            versions = conn.execute('SELECT id,label,created_at,video_path FROM video_versions WHERE task_id=? ORDER BY id', (tid,)).fetchall()
            assets = self.image_asset_rows(conn, tid)
        task['versions'] = [{'id': row['id'], 'label': row['label'], 'created_at': row['created_at'],
                             'video': f'/api/v1/tasks/{tid}/files/{row["video_path"]}'} for row in versions]
        task['image_assets'] = [self.image_asset(tid, row) for row in assets]
        def elapsed(value):
            return max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds())) if value and task['busy'] else 0
        task['progress'] = {
            'stage': task['progress_stage'] if task['busy'] else None,
            'scene_index': task['progress_scene'] if task['busy'] else None,
            'elapsed_seconds': elapsed(task['progress_started_at']),
            'operation_elapsed_seconds': elapsed(task['operation_started_at']),
            'images_ready': sum(s['img_state'] == 'ready' and bool(s['image_path']) for s in scenes),
            'audio_ready': sum(s['aud_state'] == 'ready' and bool(s['audio_path']) for s in scenes),
            'total': task['expected_scenes'],
        }
        task['video'] = f'/api/v1/tasks/{tid}/files/{task["video_path"]}' if task['video_path'] else None
        for scene in scenes:
            scene['image_source'] = self.image_source(scene)
            scene['fixed_reference_asset_ids'] = self.fixed_reference_ids(scene)
            scene['previous_scene_id'] = self.previous_scene_id(scene)
            if scene['image_source'] == 'asset' and not scene.get('image_asset_id'):
                try:
                    spec = json.loads(scene.get('image_spec_json') or '{}')
                    scene['image_asset_id'] = spec.get('asset_id')
                except (TypeError, ValueError):
                    scene['image_asset_id'] = None
            scene.pop('image_spec_json', None)
            for kind, col, label in [('img', 'image_path', 'image'), ('aud', 'audio_path', 'audio')]:
                scene[f'{kind}_ok'] = int(scene[f'{kind}_state'] == 'ready' and scene[f'{kind}_revision'] > 0 and scene[f'{kind}_approved'] == scene[f'{kind}_revision'])
                scene[f'{kind}_busy'] = scene[f'{kind}_state'] == 'running'
                scene[label] = f'/api/v1/tasks/{tid}/files/{scene[col]}' if scene[col] else None
        task['scenes'] = scenes
        return task

    def listing(self, owner=None):
        with self.store.connect() as conn:
            rows = conn.execute('SELECT t.id,t.status,t.archived,t.created_at,t.title,COUNT(s.id) AS scenes,'
                                'substr(replace(COALESCE((SELECT narration FROM scenes WHERE task_id=t.id ORDER BY idx LIMIT 1),'
                                "t.script),char(10),' '),1,60) AS preview FROM tasks t "
                                'LEFT JOIN scenes s ON t.id=s.task_id WHERE (? IS NULL OR t.owner=?) '
                                'GROUP BY t.id ORDER BY t.id DESC LIMIT 200', (owner, owner)).fetchall()
            return {'tasks': [dict(r) for r in rows]}

    def allowed_file(self, tid, name):
        with self.store.connect() as conn:
            task = self.task(conn, tid)
            names = {task['video_path']} if task['status'] == 'done' and task['video_path'] else set()
            names.update(row['video_path'] for row in conn.execute('SELECT video_path FROM video_versions WHERE task_id=?', (tid,)))
            names.update(row['path'] for row in self.image_asset_rows(conn, tid))
            for scene in self.scenes(conn, tid):
                names.update((scene['image_path'], scene['audio_path']))
        root = (self.config.data / str(tid)).resolve()
        path = (root / name).resolve()
        if name not in names or not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, '文件不可用')
        return path
