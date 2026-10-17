"""W3：持久作业记录的行为测试。

覆盖：作业随生成创建并进入终态、输入快照不含凭据（I12）、条件认领唯一、
历史作业在分镜编辑后保留、重启恢复规则、v9→v10 迁移与旧配置补默认。
"""
import json
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from app.config import Config
from app.full_config import encode_full, read_toml, validate_full
from app import jobs as job_records
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class JobRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data=Path(self.temp.name), token='jobs-admin',
                             base='https://unused.invalid/v1', key='jobs-model', max_active=2)
        self.providers = FakeProviders()
        self.executor = ManualExecutor()
        self.service = Tasks(self.config, self.providers, FakeRenderer(), self.executor, validate_fixture)
        self.service.open()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def review(self):
        tid = self.service.submit('任务作业台账')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'review')
        return tid

    def all_jobs(self, tid):
        with self.service.store.connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM jobs WHERE task_id=? ORDER BY id', (tid,))]

    def test_generation_creates_terminal_jobs_without_secrets(self):
        tid = self.review()
        rows = self.all_jobs(tid)
        self.assertEqual(len(rows), 6)  # 3 img + 3 aud（FakeProviders.plan_result 3 镜）
        self.assertEqual({row['state'] for row in rows}, {'succeeded'})
        for row in rows:
            snapshot = json.loads(row['input_json'])
            self.assertIn('prompt', snapshot)
            # I12：输入快照不含密钥、令牌或完整配置。
            dumped = json.dumps(snapshot)
            self.assertNotIn('jobs-model', dumped)
            self.assertNotIn('jobs-admin', dumped)
            self.assertNotIn('api_key', dumped)
            self.assertTrue(row['input_digest'])
            result = json.loads(row['result_json'])
            self.assertEqual(result['scene_id'], row['scene_id'])
            self.assertIn('sha256', result)
            self.assertIn('revision', result)

    def test_conditional_claim_allows_exactly_one_winner(self):
        tid = self.review()
        job = self.all_jobs(tid)[0]
        # 把作业重置回 queued 模拟两个执行者争抢。
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE jobs SET state='queued',started_at=NULL WHERE id=?", (job['id'],))
        barrier = Barrier(2)
        outcomes = []

        def contender(name):
            def run():
                barrier.wait()
                with self.service.store.connect(write=True) as conn:
                    outcomes.append((name, job_records.claim(
                        conn, job['id'], json.dumps({'contender': name}), name, 1)))
            return run

        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(contender('a'))
            pool.submit(contender('b'))
            pool.shutdown(wait=True)
        self.assertEqual(sorted(won for _, won in outcomes), [False, True])
        with self.service.store.connect() as conn:
            row = job_records.job_row(conn, job['id'])
        self.assertEqual(row['state'], 'running')
        # 胜出者的输入快照生效，落败者没有留下痕迹。
        self.assertIn('contender', row['input_json'])

    def test_regen_creates_new_operation_jobs_and_keeps_history(self):
        tid = self.review()
        before = self.all_jobs(tid)
        revision = self.service.detail(tid)['scenes'][0]['img_revision']
        self.service.regen(tid, 0, 'img', True, revision, uuid.uuid4().hex)
        self.executor.run()
        after = self.all_jobs(tid)
        self.assertEqual(len(after), len(before) + 1)
        self.assertTrue(all(row['state'] == 'succeeded' for row in before))
        new_job = after[-1]
        self.assertNotEqual(new_job['operation_id'], before[0]['operation_id'])

    def test_plan_edit_and_scene_delete_keep_job_history(self):
        tid = self.review()
        self.assertEqual(len(self.all_jobs(tid)), 6)
        draft = [{k: s[k] for k in ('id', 'narration', 'visual_prompt')}
                 for s in self.service.detail(tid)['scenes']]
        # 删第三镜、重排并修改一镜画面，使保存后确有素材需要重做。
        shortened = draft[:2][::-1]
        shortened[0]['visual_prompt'] = '全新画面'
        self.service.save_plan(tid, shortened, self.service.detail(tid)['plan_revision'])
        self.assertEqual(len(self.all_jobs(tid)), 6)  # 历史作业不被级联删除
        self.service.generate(tid, True)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review')
        self.assertGreater(len(self.all_jobs(tid)), 6)

    def test_restart_recovers_running_and_cancels_queued_jobs(self):
        tid = self.review()
        rows = self.all_jobs(tid)
        with self.service.store.connect(write=True) as conn:
            # 模拟中断现场：一个作业 running、一个 queued。
            conn.execute("UPDATE jobs SET state='running' WHERE id=?", (rows[0]['id'],))
            conn.execute("UPDATE jobs SET state='queued' WHERE id=?", (rows[1]['id'],))
        self.service.store.recover()
        after = {row['id']: row for row in self.all_jobs(tid)}
        expected = {'unknown' if rows[0]['kind'] == 'img' else 'failed', 'cancelled'}
        self.assertIn(after[rows[0]['id']]['state'], expected)
        self.assertEqual(after[rows[1]['id']]['state'], 'cancelled')

    def test_pause_cancels_queued_jobs(self):
        tid = self.service.submit('暂停作业')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        self.service.request_pause(tid)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'interrupted')
        states = {row['state'] for row in self.all_jobs(tid)}
        self.assertNotIn('queued', states)

    def test_v9_database_upgrades_to_v10_and_keeps_data(self):
        tid = self.review()
        with self.service.store.connect(write=True) as conn:
            conn.execute('DROP TABLE jobs')
            conn.execute('PRAGMA user_version=9')
        self.service.store.close()
        self.service.store.open()  # 重新迁移
        with self.service.store.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 10)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks WHERE owner='admin'").fetchone()[0], 1)
        # 作业历史在降级时被删除属于人为操作；镜头与额度保持。
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review')
        self.assertEqual(detail['image_requests'], 3)

    def test_failed_generation_records_sanitized_error(self):
        # FakeProviders.image 收到的是画面描述，失败键按 visual_prompt 设置。
        self.providers.fail_image = '画面一'
        self.providers.plan_result = [{'narration': '第一段旁白', 'visual_prompt': '画面一'}]
        tid = self.service.submit('失败脱敏')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        self.executor.run()
        rows = self.all_jobs(tid)
        failed = [row for row in rows if row['kind'] == 'img']
        self.assertEqual(failed[0]['state'], 'unknown')
        # 非用户可见异常统一为固定文案：不落内部细节，也不含凭据。
        self.assertIn('未自动重试', failed[0]['error'])
        self.assertNotIn('jobs-model', failed[0]['error'])
        self.assertNotIn('TimeoutError', failed[0]['error'])

    def test_output_directory_failure_finishes_claimed_jobs_without_requests(self):
        tid = self.service.submit('输出目录创建失败')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        with patch.object(Path, 'mkdir', side_effect=OSError('output directory unavailable')):
            self.executor.run()
        rows = self.all_jobs(tid)
        self.assertTrue(any(row['started_at'] for row in rows))
        self.assertTrue(all(row['state'] in ('failed', 'cancelled') for row in rows))
        self.assertEqual(self.providers.images, [])
        self.assertEqual(self.providers.audios, [])
        self.assertEqual(self.service.detail(tid)['status'], 'failed')

    def test_publication_failure_leaves_no_running_job(self):
        tid = self.service.submit('发布版本冲突')['task_id']
        self.executor.run()

        def changed_revision(path, kind):
            validate_fixture(path, kind)
            if kind == 'img':
                with self.service.store.connect(write=True) as conn:
                    conn.execute('UPDATE scenes SET img_revision=img_revision+1 WHERE task_id=? AND idx=0', (tid,))

        self.service.validator = changed_revision
        self.service.generate(tid, True)
        self.executor.run()
        rows = self.all_jobs(tid)
        self.assertEqual(self.service.detail(tid)['status'], 'failed')
        self.assertTrue(all(row['state'] in job_records.TERMINAL_STATES for row in rows))

    def test_old_publication_failure_does_not_modify_new_operation_scene(self):
        tid = self.review()
        revision = self.service.detail(tid)['scenes'][0]['img_revision']
        operation = self.service.regen(tid, 0, 'img', True, revision, uuid.uuid4().hex)['operation_id']
        job = next(row for row in self.all_jobs(tid) if row['operation_id'] == operation)

        def transfer_operation(path, kind):
            validate_fixture(path, kind)
            with self.service.store.connect(write=True) as conn:
                conn.execute("UPDATE tasks SET operation_id='new-operation' WHERE id=?", (tid,))

        self.service.validator = transfer_operation
        with self.assertRaises(RuntimeError):
            self.service.asset(tid, operation, 0, 'img', job_id=job['id'])
        detail = self.service.detail(tid)
        self.assertEqual(detail['operation_id'], 'new-operation')
        self.assertEqual(detail['scenes'][0]['img_state'], 'running')
        self.assertEqual(detail['scenes'][0]['img_revision'], revision)
        self.assertEqual(self.all_jobs(tid)[-1]['state'], 'unknown')


class ConfigCompatibilityTests(unittest.TestCase):
    def test_missing_concurrency_defaults_are_filled(self):
        document = {'schema_version': 1, 'server': {'admin_token': 't', 'local_only': False, 'cookie_secure': False,
                                                    'allowed_hosts': ['127.0.0.1'], 'webhook': ''},
                    'storage': {'work_dir': '/tmp/x', 'local_backup_dir': '', 'config_bucket': '',
                                'data_bucket': '', 'hf_token': ''},
                    'credentials': {'shared': '', 'llm': '', 'image': ''},
                    'limits': {'max_active': 3}, 'llm': {}, 'image': {}, 'tts': {}}
        # 直接验证正规化行为（不经过完整 validate_full，模型分节由其他测试覆盖）。
        from app.full_config import normalize_limits
        normalize_limits(document)
        self.assertEqual(document['limits']['image_concurrency'], 1)
        self.assertEqual(document['limits']['audio_concurrency'], 2)
        self.assertEqual(document['limits']['task_inflight'], 3)

    def test_unknown_limit_field_still_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.toml'
            path.write_text(encode_full({'schema_version': 1,
                                         'server': {'admin_token': 't', 'local_only': False, 'cookie_secure': False,
                                                    'allowed_hosts': ['127.0.0.1'], 'webhook': ''},
                                         'storage': {'work_dir': str(Path(folder) / 'data'), 'local_backup_dir': '',
                                                     'config_bucket': '', 'data_bucket': '', 'hf_token': ''},
                                         'credentials': {'shared': 'k', 'llm': '', 'image': ''},
                                         'limits': {name: 1 for name in (
                                             'max_active', 'max_render', 'max_script', 'max_scenes', 'max_narration',
                                             'render_timeout', 'max_image_requests', 'min_free_mb', 'max_audio_seconds',
                                             'max_video_seconds', 'max_daily_tasks', 'max_visitor_tasks', 'max_daily_llm',
                                             'max_daily_images', 'max_daily_audio', 'max_daily_renders',
                                             'request_body_timeout', 'max_upload_image_bytes', 'max_image_pixels',
                                             'image_concurrency', 'audio_concurrency', 'task_inflight')},
                                         'llm': {'base_url': '', 'model': 'm', 'credential_ref': 'shared', 'wire_api': 'chat'},
                                         'image': {'base_url': '', 'model': 'm', 'credential_ref': 'shared', 'size': '1024x768',
                                                   'quality': 'low', 'timeout_seconds': 300, 'style': 's',
                                                   'reference_protocol': 'none'},
                                         'tts': {'provider': 'edge-tts', 'voice': 'zh-CN-X'}}))
            document = read_toml(path.read_text())
            document['limits']['mystery'] = 1
            with self.assertRaises(ValueError):
                validate_full(document, path)


if __name__ == '__main__':
    unittest.main()
