import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from app.config import Config
from app.errors import MediaError
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class PlanRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = FakeProviders()
        self.executor = ManualExecutor()
        self.service = Tasks(Config(data=Path(self.temp.name), token='repair-admin',
                                    base='https://unused.invalid/v1', key='repair-model'),
                             self.provider, FakeRenderer(), self.executor, validate_fixture)
        self.service.open()
        self.tid = self.service.submit('repair fixture')['task_id']
        self.executor.run()
        self.service.generate(self.tid, True)
        self.executor.run()
        for scene in self.service.detail(self.tid)['scenes']:
            for kind in ('img', 'aud'):
                self.service.gate(self.tid, scene['idx'], kind, True, scene[kind + '_revision'])

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def edit(self, plan):
        self.service.save_plan(self.tid, plan, self.service.detail(self.tid)['plan_revision'])

    def generate(self):
        self.service.generate(self.tid, True, self.service.detail(self.tid)['plan_revision'])
        self.executor.run()

    def test_only_changed_scene_is_regenerated_and_reapproved(self):
        before = self.service.detail(self.tid)
        self.service.config = replace(self.service.config, max_image_requests=4)
        self.edit(['first', 'shorter second', 'third'])
        changed = self.service.detail(self.tid)
        self.assertEqual(changed['status'], 'planned')
        self.assertEqual(changed['missing_images'], 1)
        self.assertEqual(changed['scenes'][0]['image'], before['scenes'][0]['image'])
        self.assertEqual(changed['scenes'][0]['img_ok'], 1)
        self.assertIsNone(changed['scenes'][1]['image'])
        self.assertGreater(changed['scenes'][1]['img_revision'], before['scenes'][1]['img_revision'])
        self.assertTrue((self.service.config.data / str(self.tid) / before['scenes'][1]['image_path']).is_file())
        self.generate()
        after = self.service.detail(self.tid)
        self.assertEqual(after['status'], 'review')
        self.assertEqual(after['image_requests'], 4)
        self.assertEqual(sorted(self.provider.images), sorted(['first', 'second', 'third', 'shorter second']))
        self.assertEqual(sorted(self.provider.audios), sorted(['first', 'second', 'third', 'shorter second']))
        self.assertEqual([s['img_ok'] for s in after['scenes']], [1, 0, 1])

    def test_reordering_preserves_materials_but_rejects_stale_index_approval(self):
        before = self.service.detail(self.tid)
        self.edit(['third', 'first', 'second'])
        self.service.models.secret_values['OPENAI_API_KEY'] = ''
        self.generate()
        after = self.service.detail(self.tid)
        self.assertEqual(after['status'], 'review')
        self.assertEqual(after['scenes'][0]['image'], before['scenes'][2]['image'])
        self.assertTrue(all(s['img_ok'] and s['aud_ok'] for s in after['scenes']))
        self.assertEqual(after['image_requests'], 3)
        with self.assertRaises(HTTPException) as error:
            self.service.gate(self.tid, 0, 'img', True, before['scenes'][0]['img_revision'])
        self.assertEqual(error.exception.status_code, 409)

    def test_deleted_high_revision_is_not_reused_after_multiple_edits(self):
        with self.service.store.connect(write=True) as conn:
            conn.execute('UPDATE scenes SET img_revision=100 WHERE task_id=? AND idx=2', (self.tid,))
        self.edit(['first'])
        self.edit(['first', 'new scene'])
        self.assertGreater(self.service.detail(self.tid)['scenes'][1]['img_revision'], 100)

    def test_overlong_task_can_be_shortened_without_recharging_images(self):
        with self.service.store.connect(write=True) as conn:
            conn.execute('UPDATE scenes SET audio_duration_seconds=1.2 WHERE task_id=?', (self.tid,))
        self.service.config = replace(self.service.config, max_video_seconds=2)
        self.assertAlmostEqual(self.service.detail(self.tid)['audio_duration_seconds'], 3.6)
        with self.assertRaises(HTTPException) as error:
            self.service.render(self.tid)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn('超过 2 秒', error.exception.detail)
        with self.service.store.connect() as conn:
            self.assertIsNone(conn.execute("SELECT count FROM daily_usage WHERE kind='render'").fetchone())
        self.edit(['first'])
        self.generate()
        self.assertEqual(self.service.detail(self.tid)['image_requests'], 3)
        self.service.render(self.tid)
        self.executor.run()
        self.assertEqual(self.service.detail(self.tid)['status'], 'done')

    def test_safe_media_failure_is_visible_and_failed_task_is_editable(self):
        def fail(*args):
            raise MediaError('video_too_long', '整片旁白超限，请编辑分镜。')
        self.service.renderer.render = fail
        self.service.render(self.tid)
        self.executor.run()
        failed = self.service.detail(self.tid)
        self.assertEqual(failed['error'], '整片旁白超限，请编辑分镜。')
        self.edit(['first'])
        self.assertEqual(self.service.detail(self.tid)['status'], 'planned')

    def test_unknown_failure_does_not_expose_exception_text(self):
        def fail(*args):
            raise RuntimeError('upstream-secret-example-value')
        self.service.renderer.render = fail
        self.service.render(self.tid)
        self.executor.run()
        self.assertNotIn('upstream-secret', self.service.detail(self.tid)['error'])

    def test_completed_or_busy_task_cannot_be_edited(self):
        self.service.render(self.tid)
        with self.assertRaises(HTTPException):
            self.edit(['first'])
        self.executor.run()
        with self.assertRaises(HTTPException):
            self.edit(['first'])
