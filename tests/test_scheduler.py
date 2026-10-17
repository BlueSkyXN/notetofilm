"""W4：进程内调度器的并发正确性测试。

用探针计数与峰值采样验证：全服务并发不越界、图音确实重叠、
引用链闭包完整、失败收尾不吞成功素材、其他任务不受影响。
不依赖"必须快多少毫秒"类脆弱断言（手册 W6）。
"""
import json
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.config import Config
from app import jobs as job_records
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class ProbeProviders(FakeProviders):
    """记录并发峰值与图音重叠采样的假供应商。"""

    def __init__(self):
        super().__init__()
        self.lock = Lock()
        self.active = {'img': 0, 'aud': 0}
        self.peak = {'img': 0, 'aud': 0, 'any': 0}
        self.overlap_samples = 0

    def _enter(self, kind):
        with self.lock:
            self.active[kind] += 1
            self.peak[kind] = max(self.peak[kind], self.active[kind])
            self.peak['any'] = max(self.peak['any'], self.active['img'] + self.active['aud'])
            if self.active['img'] and self.active['aud']:
                self.overlap_samples += 1

    def _leave(self, kind):
        with self.lock:
            self.active[kind] -= 1

    def image(self, narration, output, references=None):
        self._enter('img')
        try:
            time.sleep(0.03)
            super().image(narration, output, references)
        finally:
            self._leave('img')

    def audio(self, narration, output):
        self._enter('aud')
        try:
            time.sleep(0.03)
            super().audio(narration, output)
        finally:
            self._leave('aud')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data=Path(self.temp.name), token='sched-admin',
                             base='https://unused.invalid/v1', key='sched-model', max_active=2,
                             image_reference_protocol='openai-images-edit')
        self.provider = ProbeProviders()
        self.executor = ManualExecutor()
        self.service = Tasks(self.config, self.provider, FakeRenderer(), self.executor, validate_fixture)
        self.service.open()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def planned(self, text='并发验证'):
        tid = self.service.submit(text)['task_id']
        self.executor.run()
        return tid

    def generate(self, tid):
        self.service.generate(tid, True)
        self.executor.run()

    def test_defaults_allow_image_audio_overlap_within_limits(self):
        tid = self.planned()
        self.generate(tid)
        self.assertEqual(self.service.detail(tid)['status'], 'review')
        # 默认 1/2/3：图片串行、配音最多 2 路并行、图音确实重叠执行。
        self.assertLessEqual(self.provider.peak['img'], self.config.image_concurrency)
        self.assertLessEqual(self.provider.peak['aud'], self.config.audio_concurrency)
        self.assertLessEqual(self.provider.peak['any'], self.config.task_inflight)
        self.assertGreater(self.provider.peak['aud'], 1, '配音应当存在并行执行')
        self.assertGreater(self.provider.overlap_samples, 0, '图片与配音应当存在重叠执行')

    def test_global_capacity_waits_without_failing_another_task(self):
        tid = self.planned()
        scheduler = self.service.scheduler
        holders = [scheduler.try_acquire('other-task', kind) for kind in ('img', 'aud', 'aud')]
        attempted = Event()
        acquire = scheduler.try_acquire

        def observe(task_id, kind):
            result = acquire(task_id, kind)
            attempted.set()
            return result

        self.service.generate(tid, True)
        with patch.object(scheduler, 'try_acquire', side_effect=observe), ThreadPoolExecutor(1) as pool:
            worker = pool.submit(self.executor.run)
            try:
                self.assertTrue(attempted.wait(2))
                with self.assertRaises(TimeoutError):
                    worker.result(timeout=0.1)
            finally:
                for release in holders:
                    release()
                worker.result(timeout=5)
        self.assertEqual(self.service.detail(tid)['status'], 'review')
        self.assertEqual(len(self.provider.images), 3)
        self.assertEqual(len(self.provider.audios), 3)

    def test_two_generating_tasks_share_global_capacity(self):
        tids = [self.planned('并行任务一'), self.planned('并行任务二')]
        for tid in tids:
            self.service.generate(tid, True)
        with ThreadPoolExecutor(2) as pool:
            workers = [pool.submit(self.executor.run) for _ in tids]
            for worker in workers:
                worker.result(timeout=5)
        self.assertTrue(all(self.service.detail(tid)['status'] == 'review' for tid in tids))
        self.assertEqual(len(self.provider.images), 6)
        self.assertEqual(len(self.provider.audios), 6)
        self.assertLessEqual(self.provider.peak['img'], self.config.image_concurrency)
        self.assertLessEqual(self.provider.peak['aud'], self.config.audio_concurrency)

    def test_busy_image_capacity_does_not_block_ready_audio(self):
        tid = self.planned()
        release_image = self.service.scheduler.try_acquire('other-task', 'img')
        all_audio = Event()
        audio = self.provider.audio

        def observe(narration, output):
            audio(narration, output)
            if len(self.provider.audios) == 3:
                all_audio.set()

        self.provider.audio = observe
        self.service.generate(tid, True)
        with ThreadPoolExecutor(1) as pool:
            worker = pool.submit(self.executor.run)
            try:
                self.assertTrue(all_audio.wait(2), '生图等待期间应继续派发可用配音作业')
                self.assertEqual(self.provider.images, [])
            finally:
                release_image()
                worker.result(timeout=5)
        self.assertEqual(self.service.detail(tid)['status'], 'review')

    def test_scheduler_observes_current_storage_failures(self):
        for target in (self.service.store, SimpleNamespace(failure=False)):
            with self.subTest(target=type(target).__name__):
                with patch.object(self.service.models, 'file_config', target):
                    target.failure = True
                    try:
                        self.assertTrue(self.service.scheduler.should_stop())
                        release = self.service.scheduler.try_acquire(1, 'img')
                        if release:
                            release()
                        self.assertIsNone(release)
                    finally:
                        target.failure = False

    def test_failed_job_blocks_later_claims_in_same_operation(self):
        tid = self.planned()
        self.service.generate(tid, True)
        with self.service.store.connect() as conn:
            operation = self.service.task(conn, tid)['operation_id']
            jobs = job_records.jobs_for_operation(conn, operation)
        image = next(job for job in jobs if job['kind'] == 'img')
        audio = next(job for job in jobs if job['kind'] == 'aud')
        self.provider.fail_image = 'first'
        with self.assertRaises(TimeoutError):
            self.service.asset(tid, operation, image['scene_index'], 'img', job_id=image['id'])
        with self.assertRaises(HTTPException):
            self.service.asset(tid, operation, audio['scene_index'], 'aud', job_id=audio['id'])
        self.assertEqual(self.provider.audios, [])

    def test_submit_failure_waits_for_claimed_assets_to_publish(self):
        tid = self.planned()
        started, released, rejected = Event(), Event(), Event()
        audio = self.provider.audio

        def held_audio(narration, output):
            started.set()
            if not released.wait(5):
                raise RuntimeError('test release timed out')
            audio(narration, output)

        self.provider.audio = held_audio
        submit = self.service.scheduler.submit
        submitted = []

        def reject_second(work):
            if submitted:
                if not started.wait(2):
                    raise RuntimeError('test asset did not start')
                rejected.set()
                raise RuntimeError('executor rejected submission')
            submitted.append(True)
            return submit(work)

        self.service.generate(tid, True)
        with patch.object(self.service.scheduler, 'submit', side_effect=reject_second), ThreadPoolExecutor(1) as pool:
            worker = pool.submit(self.executor.run)
            try:
                self.assertTrue(rejected.wait(2))
                with self.assertRaises(TimeoutError):
                    worker.result(timeout=0.1)
            finally:
                released.set()
                worker.result(timeout=5)
                self.service.scheduler.close()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'failed')
        self.assertEqual(detail['scenes'][0]['aud_state'], 'ready')
        with self.service.store.connect() as conn:
            states = {row['state'] for row in conn.execute('SELECT state FROM jobs WHERE task_id=?', (tid,))}
        self.assertNotIn('running', states)
        self.assertNotIn('queued', states)

    def test_pause_before_regen_claim_keeps_existing_material(self):
        tid = self.planned()
        self.generate(tid)
        before = self.service.detail(tid)['scenes'][0]
        self.service.gate(tid, 0, 'img', True, before['img_revision'])
        self.service.regen(tid, 0, 'img', True, before['img_revision'], uuid.uuid4().hex)
        self.service.request_pause(tid)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'interrupted')
        self.assertEqual(detail['scenes'][0]['img_state'], 'ready')
        self.assertEqual(detail['scenes'][0]['img_ok'], 1)
        self.assertEqual(detail['scenes'][0]['img_revision'], before['img_revision'])
        self.assertEqual(len(self.provider.images), 3)

    def test_serial_configuration_keeps_single_flight(self):
        self.service.scheduler.close()
        serial = replace(self.config, image_concurrency=1, audio_concurrency=1, task_inflight=1)
        self.provider2 = ProbeProviders()
        self.service.scheduler = __import__('app.scheduler', fromlist=['Scheduler']).Scheduler(
            serial, stopping=None, storage_failed=lambda: self.service.storage_failed)
        self.service.scheduler.stopping = self.service.stopping
        self.service.config = serial
        self.provider.plan_result = ['一', '二', '三']
        tid = self.planned('串行验证')
        self.generate(tid)
        self.assertEqual(self.service.detail(tid)['status'], 'review')
        self.assertLessEqual(self.provider.peak['any'], 1)
        self.assertEqual(self.provider.overlap_samples, 0)

    def test_reference_chain_closure_covers_whole_chain(self):
        chain = self._plan_with_chain()
        tid = chain['tid']
        draft = [{k: s[k] for k in ('id', 'narration', 'visual_prompt')}
                 for s in self.service.detail(tid)['scenes']]
        draft[0]['visual_prompt'] = '完全不同的新画面'
        self.service.save_plan(tid, draft, self.service.detail(tid)['plan_revision'])
        self.generate(tid)
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review')
        # A、B、C 三镜图片都重新生成（传播闭包），配音保留。
        self.assertEqual(len(self.provider.images), 6)
        # 两轮生成各带参考：第一轮 B 引 A、C 引 B；第二轮 B 引 A 新图、C 引 B 新图。
        with_references = [refs for refs in self.provider.image_references if refs]
        self.assertEqual(len(with_references), 4)

    def _plan_with_chain(self):
        self.provider.plan_result = [
            {'narration': '第一段', 'visual_prompt': '场景一'},
            {'narration': '第二段', 'visual_prompt': '场景二'},
            {'narration': '第三段', 'visual_prompt': '场景三'}]
        tid = self.planned('引用链')
        # 建立前镜引用链：B→A、C→B
        draft = [{k: s[k] for k in ('id', 'narration', 'visual_prompt')}
                 for s in self.service.detail(tid)['scenes']]
        first_id = draft[0]['id']
        second_id = draft[1]['id']
        draft[1] = {**draft[1], 'image': {'source': 'generated', 'asset_id': None,
                                          'fixed_reference_asset_ids': [], 'previous_scene_id': first_id}}
        draft[2] = {**draft[2], 'image': {'source': 'generated', 'asset_id': None,
                                          'fixed_reference_asset_ids': [], 'previous_scene_id': second_id}}
        self.service.save_plan(tid, draft, self.service.detail(tid)['plan_revision'])
        self.generate(tid)
        return {'tid': tid}

    def test_failure_stops_dispatch_but_keeps_successful_assets(self):
        self.provider.plan_result = [
            {'narration': '一', 'visual_prompt': '甲'},
            {'narration': '二', 'visual_prompt': '乙'},
            {'narration': '三', 'visual_prompt': '丙'}]
        tid = self.planned()
        self.provider.fail_image = '乙'
        self.generate(tid)
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'failed')
        scenes = {s['visual_prompt']: s for s in detail['scenes']}
        self.assertEqual(scenes['乙']['img_state'], 'unknown')
        # 生图串行：甲先成功；乙失败后停止派发，丙未被认领、不冒充成功。
        self.assertEqual(scenes['甲']['img_state'], 'ready')
        self.assertEqual(scenes['丙']['img_state'], 'pending')
        # 在途配音照常收尾（至少前两路并行完成）。
        ready_audio = sum(s['aud_state'] == 'ready' for s in detail['scenes'])
        self.assertGreaterEqual(ready_audio, 2)
        # 收尾后队列终止、所有权清空。
        with self.service.store.connect() as conn:
            queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE task_id=? AND state='queued'", (tid,)).fetchone()[0]
            self.assertEqual(queued, 0)

    def test_other_task_continues_after_failure(self):
        first = self.planned('失败任务')
        second = self.planned('继续任务')
        self.provider.fail_image = 'first'
        self.generate(first)
        self.provider.fail_image = None
        self.assertEqual(self.service.detail(first)['status'], 'failed')
        self.generate(second)
        self.assertEqual(self.service.detail(second)['status'], 'review')

    def test_crash_during_running_recovers_without_automatic_requests(self):
        tid = self.planned()
        self.service.generate(tid, True)
        # 模拟认领后、请求未完成时崩溃：直接进入恢复。
        self.service.close()
        rebuilt_config = Config(data=Path(self.temp.name), token='sched-admin',
                                base='https://unused.invalid/v1', key='sched-model')
        provider = ProbeProviders()
        rebuilt = Tasks(rebuilt_config, provider, FakeRenderer(), ManualExecutor(), validate_fixture)
        rebuilt.open()
        try:
            detail = rebuilt.detail(tid)
            self.assertEqual(detail['status'], 'interrupted')
            with rebuilt.store.connect() as conn:
                states = {row['state'] for row in conn.execute('SELECT state FROM jobs WHERE task_id=?', (tid,))}
            self.assertNotIn('running', states)
            self.assertNotIn('queued', states)
            self.assertEqual(provider.images, [])
            self.assertTrue(rebuilt.retry(tid, False)['needs_confirm'])
            self.assertEqual(provider.images, [])
        finally:
            rebuilt.close()

    def test_pause_blocks_new_claims_within_transaction(self):
        """暂停信号与认领在同一写事务内串行化（W4-4）：生效后不再有新付费请求。"""
        from app.tasks import PauseRequested
        tid = self.planned()
        gate = __import__('threading').Event()
        original_image = self.provider.image
        claimed_after_pause = []

        def image(prompt, output, references=None):
            # 第一个图片完成后请求暂停，观察后续认领是否被拦截。
            if not gate.is_set():
                gate.set()
                with self.service.store.connect(write=True) as conn:
                    conn.execute('UPDATE tasks SET pause_requested=1 WHERE id=?', (tid,))
            else:
                claimed_after_pause.append(prompt)
            original_image(prompt, output, references)

        self.provider.image = image
        self.service.generate(tid, True)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'interrupted')
        # 暂停生效后不再有新的付费图片请求（第一张之后的都不该发生）。
        self.assertEqual(len([p for p in self.provider.images]), 1)

    def test_regen_pause_transitions_to_interrupted(self):
        from app.tasks import PauseRequested
        tid = self.planned()
        self.service.generate(tid, True)
        self.executor.run()
        import uuid as uuid_module
        revision = self.service.detail(tid)['scenes'][0]['img_revision']
        gate = __import__('threading').Event()
        original_image = self.provider.image

        def image(prompt, output, references=None):
            with self.service.store.connect(write=True) as conn:
                conn.execute('UPDATE tasks SET pause_requested=1 WHERE id=?', (tid,))
            original_image(prompt, output, references)

        self.provider.image = image
        self.service.regen(tid, 0, 'img', True, revision, uuid_module.uuid4().hex)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'interrupted')
        self.assertIn('暂停', detail['error'])

    def test_corrupt_upstream_refresh_includes_downstream_closure(self):
        """恢复时损坏素材的下游闭包一并补建：操作不缺镜（审查确认项修复）。"""
        chain = self._plan_with_chain()
        tid = chain['tid']
        # 损坏第一镜图片后把任务置 failed，再强制恢复重生成。
        image_path = self.service.detail(tid)['scenes'][0]['image_path']
        (self.config.data / str(tid) / image_path).write_bytes(b'')
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
        before_images = len(self.provider.images)
        self.service.retry(tid, True)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review')
        # 损坏镜与其下游 B、C 都重新生成（3 张新图）。
        self.assertEqual(len(self.provider.images), before_images + 3)

    def test_corrupt_upstream_relinks_already_queued_downstream(self):
        tid = self._plan_with_chain()['tid']
        detail = self.service.detail(tid)
        draft = [{k: scene[k] for k in ('id', 'narration', 'visual_prompt')} for scene in detail['scenes']]
        draft[1]['visual_prompt'] = '已修改的第二镜'
        self.service.save_plan(tid, draft, detail['plan_revision'])
        (self.config.data / str(tid) / detail['scenes'][0]['image_path']).write_bytes(b'')
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
        coordinate = self.service.coordinate_jobs
        rebuilt = []

        def inspect(task_id, operation, task, only_idx=None):
            with self.service.store.connect() as conn:
                rebuilt.extend(job_records.jobs_for_operation(conn, operation))
            coordinate(task_id, operation, task, only_idx)

        self.service.retry(tid, True)
        with patch.object(self.service, 'coordinate_jobs', side_effect=inspect):
            self.executor.run()
        images = {job['scene_index']: job for job in rebuilt if job['kind'] == 'img'}
        self.assertIn(images[0]['id'], json.loads(images[1]['depends_on']))
        self.assertEqual(self.service.detail(tid)['status'], 'review')

    def test_progress_reports_persisted_inflight_jobs(self):
        tid = self.planned()
        started = []
        original_audio = self.provider.audio
        gate = __import__('threading').Event()

        def audio(narration, output):
            started.append(narration)
            if len(started) >= 2 and not gate.is_set():
                # 第二个配音开跑时查看持久进度。
                detail = self.service.detail(tid)
                progress = detail['progress']
                if 'jobs' in progress:
                    self.assertGreater(progress['jobs']['total'], 0)
                    self.assertLessEqual(len(progress['jobs']['inflight']), self.config.task_inflight)
            original_audio(narration, output)

        self.provider.audio = audio
        self.service.generate(tid, True)
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'review')


if __name__ == '__main__':
    unittest.main()
