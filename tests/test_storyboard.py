import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.config import Config
from app.db import Store
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = FakeProviders()
        self.provider.plan_result = [
            {'narration': '第一段旁白', 'visual_prompt': '窗边的橘猫'},
            {'narration': '第二段旁白', 'visual_prompt': '橘猫走向门口'},
            {'narration': '第三段旁白', 'visual_prompt': '花园里的橘猫'},
        ]
        self.executor = ManualExecutor()
        self.service = Tasks(Config(data=Path(self.temp.name), token='story-admin',
                                    base='https://unused.invalid/v1', key='story-model'),
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

    def save(self, scenes=None, **layout):
        self.service.save_plan(self.tid, scenes or self.draft(), self.detail()['plan_revision'], **layout)

    def generate(self):
        self.service.generate(self.tid, True, self.detail()['plan_revision'])
        self.executor.run()

    def test_narration_edit_preserves_images_and_only_regenerates_one_audio(self):
        before = self.detail()
        plan = self.draft()
        plan[1]['narration'] = '改短的第二段旁白'
        self.save(plan)
        pending = self.detail()
        self.assertEqual(pending['missing_images'], 0)
        self.assertEqual(pending['scenes'][1]['image'], before['scenes'][1]['image'])
        self.assertEqual(pending['scenes'][1]['img_ok'], 1)
        self.assertIsNone(pending['scenes'][1]['audio'])
        self.generate()
        after = self.detail()
        self.assertEqual(after['image_requests'], 3)
        self.assertEqual(len(self.provider.images), 3)
        self.assertEqual(sorted(self.provider.audios), sorted(['第一段旁白', '第二段旁白', '第三段旁白', '改短的第二段旁白']))
        self.assertEqual([s['aud_ok'] for s in after['scenes']], [1, 0, 1])
        self.assertEqual([s['img_ok'] for s in after['scenes']], [1, 1, 1])

    def test_visual_edit_preserves_audio_and_only_regenerates_one_image(self):
        before = self.detail()
        plan = self.draft()
        plan[0]['visual_prompt'] = '雨天窗边的橘猫'
        self.save(plan)
        self.assertEqual(self.detail()['missing_images'], 1)
        self.generate()
        after = self.detail()
        self.assertEqual(sorted(self.provider.images), sorted(['窗边的橘猫', '橘猫走向门口', '花园里的橘猫', '雨天窗边的橘猫']))
        self.assertEqual(len(self.provider.audios), 3)
        self.assertEqual(after['scenes'][0]['audio'], before['scenes'][0]['audio'])
        self.assertEqual(after['scenes'][0]['aud_revision'], before['scenes'][0]['aud_revision'])
        self.assertEqual([s['aud_ok'] for s in after['scenes']], [1, 1, 1])
        self.assertEqual([s['img_ok'] for s in after['scenes']], [0, 1, 1])

    def test_reorder_with_edit_keeps_stable_identity_and_unmodified_material(self):
        before = self.detail()['scenes']
        plan = self.draft()[::-1]
        plan[0]['narration'] = '换了位置的第三段旁白'
        self.save(plan)
        self.generate()
        first = self.detail()['scenes'][0]
        self.assertEqual(first['id'], before[2]['id'])
        self.assertEqual(first['image'], before[2]['image'])
        self.assertTrue(first['img_ok'])
        self.assertFalse(first['aud_ok'])
        self.assertEqual(len(self.provider.images), 3)
        with self.assertRaises(HTTPException) as stale:
            self.service.gate(self.tid, 0, 'img', True, before[0]['img_revision'])
        self.assertEqual(stale.exception.status_code, 409)

    def test_layout_change_requires_new_image_approval_without_new_requests(self):
        before = self.detail()
        self.save(aspect_ratio='9:16', image_fit='cover')
        self.generate()
        after = self.detail()
        self.assertEqual((after['aspect_ratio'], after['image_fit']), ('9:16', 'cover'))
        self.assertEqual([s['image'] for s in after['scenes']], [s['image'] for s in before['scenes']])
        self.assertEqual(after['image_requests'], 3)
        self.assertTrue(all(s['aud_ok'] and not s['img_ok'] for s in after['scenes']))
        with self.assertRaises(HTTPException):
            self.service.render(self.tid)
        with self.assertRaises(HTTPException):
            self.service.gate(self.tid, 0, 'img', True, before['scenes'][0]['img_revision'])

    def test_invalid_ids_and_prompts_preserve_the_existing_plan(self):
        before = self.detail()
        bad = [self.draft()[0], self.draft()[0]]
        with self.assertRaises(ValueError):
            self.save(bad)
        for bad in [dict(self.draft()[0], id=999999), dict(self.draft()[0], visual_prompt=' '),
                    dict(self.draft()[0], visual_prompt='a' * 4001)]:
            with self.assertRaises((ValueError, HTTPException)):
                self.save([bad])
        self.assertEqual(self.detail()['scenes'], before['scenes'])
        self.assertEqual(self.detail()['plan_revision'], before['plan_revision'])

    def test_progress_reports_the_active_scene_and_completed_counts(self):
        observed = []
        def image(prompt, output):
            observed.append(self.detail()['progress'])
            output.write_bytes(b'image')
        plan = self.draft()
        plan[1]['visual_prompt'] += '，阳光洒在地板上'
        self.save(plan)
        self.provider.image = image
        self.generate()
        self.assertEqual(len(observed), 1)
        self.assertEqual((observed[0]['stage'], observed[0]['scene_index']), ('img', 1))
        self.assertEqual((observed[0]['images_ready'], observed[0]['audio_ready'], observed[0]['total']), (2, 3, 3))
        self.assertGreaterEqual(observed[0]['elapsed_seconds'], 0)
        self.assertIsNone(self.detail()['progress']['stage'])
        self.assertEqual(self.detail()['progress']['images_ready'], 3)

    def test_legacy_string_plan_still_uses_narration_for_images(self):
        self.save(['传统接口旁白'])
        self.generate()
        self.assertEqual(self.detail()['scenes'][0]['visual_prompt'], '传统接口旁白')
        self.assertEqual(self.provider.images[-1], '传统接口旁白')


class StoryboardMigrationTests(unittest.TestCase):
    def test_v5_upgrade_backfills_prompts_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            store.open()
            with store.connect(write=True) as conn:
                conn.execute("INSERT INTO tasks(id,script,status,video_path,image_requests) VALUES(1,'历史原稿','done','video.mp4',8)")
                conn.execute("INSERT INTO scenes(task_id,idx,narration,img_revision,img_approved,img_state) VALUES(1,0,'历史旁白',7,7,'ready')")
                for name in ('visual_prompt', 'image_width', 'image_height'):
                    conn.execute(f'ALTER TABLE scenes DROP COLUMN {name}')
                for name in ('aspect_ratio', 'image_fit', 'operation_started_at', 'progress_stage', 'progress_scene', 'progress_started_at'):
                    conn.execute(f'ALTER TABLE tasks DROP COLUMN {name}')
                conn.execute('PRAGMA user_version=5')
            store.close()
            store.open()
            try:
                with store.connect() as conn:
                    task = conn.execute('SELECT * FROM tasks').fetchone()
                    scene = conn.execute('SELECT * FROM scenes').fetchone()
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 10)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM image_assets').fetchone()[0], 0)
                    self.assertEqual((task['purpose'], task['audience'], task['focus'], task['target_duration_seconds']), ('', '', '', 0))
                    self.assertEqual((task['status'], task['video_path'], task['image_requests']), ('done', 'video.mp4', 8))
                    self.assertEqual((task['aspect_ratio'], task['image_fit']), ('16:9', 'contain'))
                    self.assertEqual(scene['visual_prompt'], scene['narration'])
                    self.assertEqual((scene['img_revision'], scene['img_approved']), (7, 7))
            finally:
                store.close()
