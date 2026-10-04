import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

from fastapi import HTTPException

from app.config import Config
from app.tasks import Tasks


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        fn, args = self.jobs.pop(0)
        fn(*args)

    def shutdown(self, wait=True):
        pass


class FakeProviders:
    def __init__(self):
        self.plan_result = ['first', 'second', 'third']
        self.images = []
        self.audios = []
        self.fail_audio = None
        self.fail_image = None

    def plan(self, script):
        return self.plan_result

    def image(self, narration, output):
        self.images.append(narration)
        if narration == self.fail_image:
            raise TimeoutError('fixture')
        output.write_bytes(b'image fixture')

    def audio(self, narration, output):
        self.audios.append(narration)
        if narration == self.fail_audio:
            raise TimeoutError('fixture')
        output.write_bytes(b'audio fixture')

    def notify(self, tid, status):
        pass


class FakeRenderer:
    def __init__(self):
        self.calls = []

    def render(self, task_dir, operation, scenes):
        self.calls.append((operation, scenes))
        return f'renders/{operation}/video.mp4'

    def recover(self, task_dir, scenes):
        return None


def validate_fixture(path, kind):
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('invalid fixture')


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data=Path(self.temp.name), token='test-only', base='https://unused.invalid/v1', key='test-only', max_active=2)
        self.providers = FakeProviders()
        self.renderer = FakeRenderer()
        self.executor = ManualExecutor()
        self.service = Tasks(self.config, self.providers, self.renderer, self.executor, validate_fixture)
        self.service.open()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def planned(self, text='fixture'):
        tid = self.service.submit(text)['task_id']
        self.executor.run()
        return tid

    def review(self, text='fixture'):
        tid = self.planned(text)
        self.service.generate(tid, True)
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'review')
        return tid

    def approve(self, tid):
        for scene in self.service.detail(tid)['scenes']:
            for kind in ('img', 'aud'):
                self.service.gate(tid, scene['idx'], kind, True, scene[f'{kind}_revision'])

    def raises_status(self, code, fn, *args):
        with self.assertRaises(HTTPException) as context:
            fn(*args)
        self.assertEqual(context.exception.status_code, code)

    def test_plan_saved_before_generation_and_confirmation_required(self):
        tid = self.planned()
        detail = self.service.detail(tid)
        self.assertEqual(detail['expected_scenes'], 3)
        self.assertEqual(len(detail['scenes']), 3)
        self.assertEqual(self.providers.images, [])
        self.assertTrue(all(s['img_state'] == 'pending' for s in detail['scenes']))
        self.raises_status(409, self.service.generate, tid, False)

    def test_parallel_identical_submission(self):
        barrier = Barrier(2)
        def submit():
            barrier.wait()
            try:
                return self.service.submit('identical')
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sum(isinstance(v, dict) for v in values), 1)
        self.assertIn(409, values)
        self.assertEqual(len(self.executor.jobs), 1)

    def test_parallel_capacity(self):
        self.service.config = replace(self.config, max_active=1)
        barrier = Barrier(2)
        def submit(i):
            barrier.wait()
            try:
                return self.service.submit(f'input {i}')
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(submit, range(2)))
        self.assertEqual(sum(isinstance(v, dict) for v in values), 1)
        self.assertIn(429, values)

    def test_regen_blocks_second_regen_gate_and_render(self):
        tid = self.review()
        self.approve(tid)
        self.service.regen(tid, 0, 'img', True)
        detail = self.service.detail(tid)
        self.assertTrue(detail['busy'])
        self.raises_status(409, self.service.regen, tid, 0, 'img', True)
        self.raises_status(409, self.service.gate, tid, 0, 'img', True, 1)
        self.raises_status(409, self.service.render, tid)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertFalse(detail['busy'])
        self.assertEqual(detail['scenes'][0]['img_revision'], 2)
        self.raises_status(409, self.service.gate, tid, 0, 'img', True, 1)
        self.assertEqual(detail['scenes'][0]['aud_ok'], 1)

    def test_partial_failure_preserves_full_plan_and_successful_image(self):
        tid = self.planned()
        self.providers.fail_audio = 'second'
        self.service.generate(tid, True)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'failed')
        self.assertEqual(len(detail['scenes']), 3)
        self.assertEqual(detail['scenes'][2]['img_state'], 'pending')
        self.assertTrue(self.service.retry(tid, False)['needs_confirm'])
        self.providers.fail_audio = None
        self.service.retry(tid, True)
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'review')
        self.assertEqual(self.providers.images, ['first', 'second', 'third'])

    def test_uncertain_paid_result_requires_confirmation_and_clears_busy(self):
        tid = self.review()
        self.providers.fail_image = 'first'
        self.service.regen(tid, 0, 'img', True)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertFalse(detail['busy'])
        self.assertEqual(detail['scenes'][0]['img_state'], 'unknown')
        before = len(self.providers.images)
        self.assertTrue(self.service.retry(tid, False)['needs_confirm'])
        self.assertEqual(before, len(self.providers.images))

    def test_render_capacity_and_frozen_versions(self):
        a, b = self.review('a'), self.review('b')
        self.approve(a)
        self.approve(b)
        self.service.render(a)
        self.raises_status(429, self.service.render, b)
        self.raises_status(409, self.service.regen, a, 0, 'aud', True)
        self.executor.run()
        self.assertEqual(self.service.detail(a)['status'], 'done')
        self.assertEqual(self.renderer.calls[0][1][0]['img_revision'], 1)

    def test_regen_uses_shared_capacity(self):
        a, b = self.review('a'), self.review('b')
        self.service.config = replace(self.config, max_active=1)
        self.service.regen(a, 0, 'aud', True)
        self.raises_status(429, self.service.regen, b, 0, 'aud', True)

    def test_restart_covers_submitted_and_running_operations(self):
        tid = self.review()
        self.service.regen(tid, 0, 'img', True)
        with self.service.store.connect(write=True) as conn:
            submitted = conn.execute("INSERT INTO tasks(script,status) VALUES('fixture','submitted')").lastrowid
        self.service.store.recover()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'interrupted')
        self.assertEqual(detail['scenes'][0]['img_state'], 'unknown')
        self.assertFalse(detail['busy'])
        self.assertEqual(self.service.detail(submitted)['status'], 'interrupted')

    def test_second_process_cannot_open_same_store(self):
        other = Tasks(self.config, executor=ManualExecutor())
        with self.assertRaises(RuntimeError):
            other.open()

    def test_invalid_plan_not_truncated_or_charged(self):
        self.providers.plan_result = ['x'] * 31
        tid = self.planned()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'failed')
        self.assertEqual(detail['scenes'], [])
        self.assertEqual(self.providers.images, [])

    def test_placeholder_only_blocks_image_gate(self):
        tid = self.review()
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE scenes SET img_state='placeholder',img_placeholder=1 WHERE task_id=?", (tid,))
        self.service.gate(tid, 0, 'aud', True, 1)
        self.raises_status(409, self.service.gate, tid, 0, 'img', True, 1)

    def test_dispatch_failure_releases_capacity(self):
        def reject(*args):
            raise RuntimeError('executor stopped')
        self.executor.submit = reject
        self.raises_status(503, self.service.submit, 'rejected')
        tid = self.service.listing()['tasks'][0]['id']
        self.assertFalse(self.service.detail(tid)['busy'])
        self.assertEqual(self.service.detail(tid)['status'], 'failed')

    def test_unknown_and_unpublished_files_not_served(self):
        tid = self.review()
        folder = self.config.data / str(tid)
        (folder / 'video.mp4').write_bytes(b'partial')
        self.raises_status(404, self.service.allowed_file, tid, 'video.mp4')
        self.raises_status(404, self.service.allowed_file, tid, '../tasks.db')

    def test_media_versions_do_not_overwrite(self):
        tid = self.review()
        old = self.service.detail(tid)['scenes'][0]['image_path']
        self.service.regen(tid, 0, 'img', True)
        self.executor.run()
        new = self.service.detail(tid)['scenes'][0]['image_path']
        self.assertNotEqual(old, new)
        self.assertTrue((self.config.data / str(tid) / old).is_file())

    def test_failed_regen_keeps_old_file_and_invalidates_approval(self):
        tid = self.review()
        self.approve(tid)
        before = self.service.detail(tid)['scenes'][0]
        self.providers.fail_image = 'first'
        self.service.regen(tid, 0, 'img', True)
        self.executor.run()
        after = self.service.detail(tid)['scenes'][0]
        self.assertEqual(before['image_path'], after['image_path'])
        self.assertTrue((self.config.data / str(tid) / before['image_path']).is_file())
        self.assertEqual(after['img_ok'], 0)
        self.assertEqual(after['img_revision'], before['img_revision'])

    def test_recovery_preserves_successful_approval_and_no_paid_retries(self):
        tid = self.review()
        self.approve(tid)
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
        before = len(self.providers.images)
        self.service.retry(tid, False)
        self.raises_status(409, self.service.retry, tid, True)
        self.executor.run()
        self.assertEqual(len(self.providers.images), before)
        self.assertTrue(all(s['img_ok'] and s['aud_ok'] for s in self.service.detail(tid)['scenes']))

    def test_legacy_invalid_done_is_downgraded(self):
        tid = self.review()
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='done',video_path=NULL WHERE id=?", (tid,))
        self.service.reconcile_legacy_outputs()
        self.assertEqual(self.service.detail(tid)['status'], 'interrupted')

    def test_unique_scene_index_enforced(self):
        tid = self.planned()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.service.store.connect(write=True) as conn:
                conn.execute("INSERT INTO scenes(task_id,idx,narration) VALUES(?,0,'duplicate')", (tid,))

    def test_input_limit_rejected_before_task_creation(self):
        self.service.config = replace(self.config, max_script=10)
        self.raises_status(422, self.service.submit, 'a' * 11)
        self.assertEqual(self.service.listing()['tasks'], [])

    def test_shutdown_does_not_start_next_paid_request(self):
        tid = self.planned()
        self.service.generate(tid, True)
        self.service.stopping.set()
        self.executor.run()
        self.assertEqual(self.providers.images, [])
        self.assertEqual(self.service.detail(tid)['status'], 'interrupted')

    def test_edit_plan_and_reject_stale_generation(self):
        tid = self.planned()
        revision = self.service.detail(tid)['plan_revision']
        self.service.save_plan(tid, ['edited one', 'edited two'], revision)
        task = self.service.detail(tid)
        self.assertEqual(task['plan_revision'], revision + 1)
        self.assertEqual(task['expected_scenes'], 2)
        self.raises_status(409, self.service.save_plan, tid, ['stale'], revision)
        self.raises_status(409, self.service.generate, tid, True, revision)
        self.service.generate(tid, True, task['plan_revision'])
        self.executor.run()
        self.assertEqual(self.providers.images, ['edited one', 'edited two'])

    def test_replanning_invalidates_old_plan_revision(self):
        tid = self.planned()
        revision = self.service.detail(tid)['plan_revision']
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='interrupted',plan_complete=0 WHERE id=?", (tid,))
        self.providers.plan_result = ['new plan']
        self.service.retry(tid, True)
        self.executor.run()
        task = self.service.detail(tid)
        self.assertEqual(task['status'], 'planned')
        self.assertEqual(task['plan_revision'], revision + 1)
        self.raises_status(409, self.service.generate, tid, True, revision)
        self.service.generate(tid, True, task['plan_revision'])
        self.executor.run()
        self.assertEqual(self.providers.images, ['new plan'])

    def test_archive_releases_duplicate_without_deleting(self):
        tid = self.review()
        path = self.service.detail(tid)['scenes'][0]['image_path']
        self.service.archive(tid)
        self.assertTrue((self.config.data / str(tid) / path).is_file())
        self.raises_status(409, self.service.regen, tid, 0, 'aud', True)
        new = self.service.submit('fixture')['task_id']
        self.assertNotEqual(tid, new)

    def test_archive_busy_rejected(self):
        tid = self.review()
        self.service.regen(tid, 0, 'aud', True)
        self.raises_status(409, self.service.archive, tid)

    def test_image_request_limit_includes_failures(self):
        self.service.config = replace(self.config, max_image_requests=3)
        tid = self.review()
        self.assertEqual(self.service.detail(tid)['image_requests'], 3)
        self.raises_status(429, self.service.regen, tid, 0, 'img', True)
        self.assertEqual(len(self.providers.images), 3)

    def test_image_request_failure_not_free_budget(self):
        tid = self.review()
        self.providers.fail_image = 'first'
        self.service.regen(tid, 0, 'img', True)
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['image_requests'], 4)
        self.assertEqual(self.service.detail(tid)['scenes'][0]['img_state'], 'unknown')

    def test_image_limit_preflight_no_partial_generation(self):
        self.service.config = replace(self.config, max_image_requests=2)
        tid = self.planned()
        self.raises_status(429, self.service.generate, tid, True)
        self.assertEqual(self.providers.images, [])

    def test_low_disk_rejects_before_creation(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch('app.tasks.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            self.raises_status(507, self.service.submit, 'input')
        self.assertEqual(self.service.listing()['tasks'], [])

    def test_confirmed_corrupt_recovery_finishes_without_second_confirmation(self):
        tid = self.review()
        image = self.service.detail(tid)['scenes'][0]['image_path']
        (self.config.data / str(tid) / image).write_bytes(b'')
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
        self.service.retry(tid, True)
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'review')
        self.assertEqual(self.service.detail(tid)['scenes'][0]['img_revision'], 2)

    def test_existing_corrupt_image_not_automatically_recharged(self):
        tid = self.review()
        image = self.service.detail(tid)['scenes'][0]['image_path']
        (self.config.data / str(tid) / image).write_bytes(b'')
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
        count = len(self.providers.images)
        self.service.retry(tid, False)
        self.executor.run()
        self.assertEqual(len(self.providers.images), count)
        self.assertEqual(self.service.detail(tid)['status'], 'failed')
        self.assertTrue(self.service.retry(tid, False)['needs_confirm'])


if __name__ == '__main__':
    unittest.main()
