"""W4：进程内调度器的并发正确性测试。

用探针计数与峰值采样验证：全服务并发不越界、图音确实重叠、
引用链闭包完整、失败收尾不吞成功素材、其他任务不受影响。
不依赖"必须快多少毫秒"类脆弱断言（手册 W6）。
"""
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from threading import Lock

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

    def test_serial_configuration_keeps_single_flight(self):
        self.service.scheduler.close()
        serial = replace(self.config, image_concurrency=1, audio_concurrency=1, task_inflight=1)
        self.provider2 = ProbeProviders()
        self.service.scheduler = __import__('app.scheduler', fromlist=['Scheduler']).Scheduler(
            serial, stopping=None, storage_failed=self.service.storage_failed)
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
