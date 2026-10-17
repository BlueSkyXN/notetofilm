"""W2：服务端修改影响预演接口的行为测试。

覆盖：预演与保存结果一致、预演只读不写库不触发快照、校验链与保存一致
（旧版本 409、忙任务 409、制作要求与重复检查）、响应字段正确。
请求体额度与 HTTP 层用例在 test_body_limit.py 与 test_http.py。
"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.config import Config
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class PlanImpactPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = FakeProviders()
        self.provider.plan_result = [
            {'narration': '第一段旁白', 'visual_prompt': '窗边的橘猫'},
            {'narration': '第二段旁白', 'visual_prompt': '橘猫走向门口'},
            {'narration': '第三段旁白', 'visual_prompt': '花园里的橘猫'},
        ]
        self.executor = ManualExecutor()
        self.service = Tasks(Config(data=Path(self.temp.name), token='impact-admin',
                                    base='https://unused.invalid/v1', key='impact-model',
                                    image_reference_protocol='openai-images-edit'),
                             self.provider, FakeRenderer(), self.executor, validate_fixture)
        self.service.open()
        self.tid = self.service.submit('三个镜头')['task_id']
        self.executor.run()
        self.generate()
        for scene in self.detail()['scenes']:
            for kind in ('img', 'aud'):
                self.service.gate(self.tid, scene['idx'], kind, True, scene[kind + '_revision'])

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def detail(self):
        return self.service.detail(self.tid)

    def draft(self):
        return [{k: s[k] for k in ('id', 'narration', 'visual_prompt')} for s in self.detail()['scenes']]

    def generate(self):
        self.service.generate(self.tid, True, self.detail()['plan_revision'])
        self.executor.run()

    def preview(self, scenes, revision=None, **kwargs):
        return self.service.preview_plan_impact(self.tid, scenes, revision if revision is not None
                                                else self.detail()['plan_revision'], **kwargs)

    def test_preview_matches_saved_state(self):
        plan = self.draft()
        plan[1]['narration'] = '改短的第二段旁白'
        preview = self.preview(plan)
        self.assertEqual(preview['plan_revision'], self.detail()['plan_revision'])
        self.assertEqual(preview['image_requests_needed'], 0)
        self.assertEqual([scene['image'] for scene in preview['scenes']], ['keep', 'keep', 'keep'])
        self.assertEqual([scene['audio'] for scene in preview['scenes']], ['keep', 'redo', 'keep'])
        self.assertEqual([scene['aud_approval_kept'] for scene in preview['scenes']], [True, False, True])
        self.service.save_plan(self.tid, plan, self.detail()['plan_revision'])
        saved = self.detail()
        self.assertEqual(saved['scenes'][1]['aud_state'], 'pending')
        self.assertEqual(saved['scenes'][1]['img_ok'], 1)
        self.assertEqual(saved['image_requests_needed'], preview['image_requests_needed'])
        for idx, scene in enumerate(preview['scenes']):
            row = saved['scenes'][idx]
            self.assertEqual(scene['img_approval_kept'], bool(row['img_ok']))
            self.assertEqual(scene['aud_approval_kept'], bool(row['aud_ok']))

    def test_preview_reports_upstream_invalidation_for_kept_reference(self):
        # 先保存一版带前镜引用链的分镜，再只改上游画面：保持引用的直接下游被失效。
        plan = self.draft()
        ids = [scene['id'] for scene in plan]
        for idx in (1, 2):
            plan[idx] = {**plan[idx], 'image': {'source': 'generated', 'asset_id': None,
                                                'fixed_reference_asset_ids': [],
                                                'previous_scene_id': ids[idx - 1]}}
        self.service.save_plan(self.tid, plan, self.detail()['plan_revision'])
        self.generate()
        changed = self.draft()
        changed[0] = {**changed[0], 'visual_prompt': '完全新的画面'}
        preview = self.preview(changed)
        revision_before = self.detail()['scenes'][1]['img_revision']
        self.assertEqual(preview['scenes'][0]['image'], 'redo')
        self.assertTrue(preview['scenes'][1]['invalidated_by_upstream'])
        self.assertFalse(preview['scenes'][2]['invalidated_by_upstream'])
        self.service.save_plan(self.tid, changed, self.detail()['plan_revision'])
        after = self.detail()
        self.assertEqual(after['image_requests_needed'], preview['image_requests_needed'])
        # 落库效果：失效的直接下游 pending、版本抬高、错误文案指向上游重做。
        self.assertEqual(after['scenes'][1]['img_state'], 'pending')
        self.assertGreater(after['scenes'][1]['img_revision'], revision_before)
        self.assertIn('上游图片会重做', after['scenes'][1]['img_error'])

    def test_preview_reports_new_and_removed_scenes(self):
        plan = self.draft()[:2] + [{'narration': '全新镜头', 'visual_prompt': '新画面'}]
        preview = self.preview(plan)
        self.assertEqual(preview['removed_scene_ids'], [self.detail()['scenes'][2]['id']])
        self.assertEqual(preview['scenes'][2]['id'], None)
        self.assertEqual(preview['scenes'][2]['image'], 'redo')
        self.assertEqual(preview['scenes'][2]['audio'], 'redo')
        self.assertEqual(preview['image_requests_needed'], 1)

    def test_preview_reports_layout_change(self):
        preview = self.preview(self.draft(), aspect_ratio='9:16')
        self.assertEqual([scene['image'] for scene in preview['scenes']], ['keep', 'keep', 'keep'])
        self.assertEqual([scene['img_approval_kept'] for scene in preview['scenes']], [False, False, False])
        self.assertEqual([scene['aud_approval_kept'] for scene in preview['scenes']], [True, True, True])
        self.assertEqual(preview['image_requests_needed'], 0)

    def test_replacing_upstream_image_previews_downstream_requests_and_approvals(self):
        plan = self.draft()
        for idx in (1, 2):
            plan[idx]['image'] = {'source': 'generated', 'asset_id': None,
                                  'fixed_reference_asset_ids': [], 'previous_scene_id': plan[idx - 1]['id']}
        self.save(plan)
        self.generate()
        for scene in self.detail()['scenes']:
            self.service.gate(self.tid, scene['idx'], 'img', True, scene['img_revision'])
        plan = self.draft()
        plan[0]['image'] = {'source': 'asset', 'asset_id': self.detail()['scenes'][2]['image_asset_id']}
        preview = self.preview(plan)
        self.save(plan)
        saved = self.detail()
        self.assertEqual(saved['image_requests_needed'], 2)
        self.assertEqual(preview['image_requests_needed'], saved['image_requests_needed'])
        self.assertEqual(preview['scenes'][1]['image'], 'redo')
        self.assertFalse(preview['scenes'][1]['img_approval_kept'])
        self.assertTrue(preview['scenes'][1]['aud_approval_kept'])

    def test_preview_is_read_only(self):
        before = self.detail()
        snapshot_count = []
        original = self.service.store.snapshots.checkpoint if self.service.store.snapshots else None
        if original:
            def counting(root, database):
                snapshot_count.append(1)
                return original(root, database)
            self.service.store.snapshots.checkpoint = counting
        try:
            self.preview(self.draft())
        finally:
            if original:
                self.service.store.snapshots.checkpoint = original
        after = self.detail()
        self.assertEqual(snapshot_count, [])
        self.assertEqual(after['plan_revision'], before['plan_revision'])
        self.assertEqual([s['narration'] for s in after['scenes']], [s['narration'] for s in before['scenes']])
        self.assertEqual(after['status'], before['status'])

    def test_preview_rejects_stale_revision(self):
        with self.assertRaises(HTTPException) as error:
            self.preview(self.draft(), revision=self.detail()['plan_revision'] - 1)
        self.assertEqual(error.exception.status_code, 409)

    def test_preview_rejects_busy_task(self):
        # 保存回到 planned 后发起生成，ManualExecutor 未运行即任务忙。
        self.save(self.draft())
        self.service.generate(self.tid, True, self.detail()['plan_revision'])
        try:
            with self.assertRaises(HTTPException) as error:
                self.preview(self.draft())
            self.assertEqual(error.exception.status_code, 409)
        finally:
            self.executor.run()

    def save(self, scenes=None):
        self.service.save_plan(self.tid, scenes or self.draft(), self.detail()['plan_revision'])

    def test_preview_rejects_invalid_ids_and_forward_references(self):
        plan = self.draft()
        plan[1] = {**plan[1], 'id': 9999}
        with self.assertRaises(HTTPException) as error:
            self.preview(plan)
        self.assertEqual(error.exception.status_code, 409)
        plan = self.draft()
        plan[0] = {**plan[0], 'image': {'source': 'generated', 'asset_id': None,
                                        'fixed_reference_asset_ids': [], 'previous_scene_id': plan[2]['id']}}
        with self.assertRaises(HTTPException) as error:
            self.preview(plan)
        self.assertEqual(error.exception.status_code, 409)

    def test_preview_requires_brief_for_duration_change(self):
        with self.assertRaises(HTTPException) as error:
            self.preview(self.draft(), target_duration_seconds=60)
        self.assertEqual(error.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
