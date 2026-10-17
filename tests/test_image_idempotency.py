import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi import HTTPException

from app.config import Config
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class ImageIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data=Path(self.temp.name), token='idempotency-admin', base='https://unused.invalid/v1', key='fixture-model')
        self.provider = FakeProviders()
        self.executor = ManualExecutor()
        self.service = self.new_service()
        self.service.open()
        self.tid = self.service.submit('idempotency fixture')['task_id']
        self.executor.run()
        self.service.generate(self.tid, True)
        self.executor.run()
        self.key = 'persistent-image-request-01'

    def new_service(self):
        return Tasks(self.config, self.provider, FakeRenderer(), self.executor, validate_fixture)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def redo(self, key=None, revision=1, idx=0, requester='admin'):
        return self.service.regen(self.tid, idx, 'img', True, revision, key or self.key, requester)

    def assert_status(self, code, fn, *args):
        with self.assertRaises(HTTPException) as error:
            fn(*args)
        self.assertEqual(error.exception.status_code, code)

    def test_replay_during_and_after_completion_never_dispatches_twice(self):
        before = len(self.provider.images)
        first = self.redo()
        second = self.redo()
        self.assertEqual(first['operation_id'], second['operation_id'])
        self.assertTrue(second['replayed'])
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run()
        completed = self.redo()
        self.assertTrue(completed['replayed'])
        self.assertEqual(len(self.provider.images), before + 1)
        self.assertEqual(self.executor.jobs, [])
        self.assert_status(409, self.redo, 'new-image-request-key', 1)
        with self.service.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM image_requests').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count FROM daily_usage WHERE kind='image'").fetchone()[0], before + 1)

    def test_same_key_changed_parameters_conflicts(self):
        self.redo()
        self.assert_status(409, self.redo, self.key, 1, 1)
        self.assertEqual(len(self.executor.jobs), 1)

    def test_parallel_same_key_claims_one_operation(self):
        barrier = Barrier(2)
        def submit(_):
            barrier.wait()
            return self.redo()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(len({r['operation_id'] for r in results}), 1)
        self.assertEqual(sum(r['replayed'] for r in results), 1)
        self.assertEqual(len(self.executor.jobs), 1)

    def test_key_is_scoped_to_requester(self):
        first = self.redo()
        self.executor.run()
        second = self.redo(revision=2, requester='different-requester')
        self.assertNotEqual(first['operation_id'], second['operation_id'])

    def test_completed_key_survives_restart(self):
        first = self.redo()
        self.executor.run()
        count = len(self.provider.images)
        self.service.close()
        self.executor = ManualExecutor()
        self.service = self.new_service()
        self.service.open()
        replay = self.redo()
        self.assertEqual(first['operation_id'], replay['operation_id'])
        self.assertEqual(len(self.provider.images), count)
        self.assertEqual(self.executor.jobs, [])

    def test_claim_before_crash_is_not_dispatched_on_replay(self):
        first = self.redo()
        with self.service.store.connect() as conn:
            job_id = conn.execute('SELECT id FROM jobs WHERE operation_id=?', (first['operation_id'],)).fetchone()[0]
        # 模拟实际认领并进入供应商后进程退出，区别于仍在 queued 的重做。
        with patch.object(self.provider, 'image', side_effect=SystemExit('simulated crash')):
            with self.assertRaises(SystemExit):
                self.service.asset(self.tid, first['operation_id'], 0, 'img', job_id=job_id)
        self.service.close()
        self.executor = ManualExecutor()
        self.service = self.new_service()
        self.service.open()
        replay = self.redo()
        self.assertEqual(first['operation_id'], replay['operation_id'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.service.detail(self.tid)['status'], 'interrupted')
        self.assertEqual(self.executor.jobs, [])
        self.assertTrue(self.service.retry(self.tid, False)['needs_confirm'])

    def test_dispatch_failure_remains_recorded(self):
        with patch.object(self.executor, 'submit', side_effect=RuntimeError('stopped')):
            self.assert_status(503, self.redo)
        self.assertEqual(self.service.detail(self.tid)['status'], 'failed')
        replay = self.redo()
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.executor.jobs, [])

    def test_provider_failure_replay_does_not_charge_again(self):
        self.provider.fail_image = 'first'
        self.redo()
        self.executor.run()
        count = len(self.provider.images)
        replay = self.redo()
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(self.provider.images), count)
        self.assertEqual(self.executor.jobs, [])
        self.assertTrue(self.service.retry(self.tid, False)['needs_confirm'])

    def test_revision_and_key_are_required(self):
        self.assert_status(422, self.service.regen, self.tid, 0, 'img', True)
        self.assertEqual(self.executor.jobs, [])


if __name__ == '__main__':
    unittest.main()
