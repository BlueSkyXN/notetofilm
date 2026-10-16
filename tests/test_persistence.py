import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.full_config import encode_full, read_toml
from app.persistence import PersistenceError
from app.render import Renderer, run_process, validate_asset
from app.tasks import Tasks
from test_full_config import file_settings
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = file_settings(self.root, persistent=True)
        self.executor = ManualExecutor()
        self.provider = FakeProviders()
        self.service = Tasks(self.config, self.provider, FakeRenderer(), self.executor, validate_fixture)
        self.service.open()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def fresh_config(self):
        document = read_toml(self.config.config_path.read_text())
        document['storage']['work_dir'] = str(self.root / 'rebuilt')
        self.config.config_path.write_text(encode_full(document))
        return Config.load({'NOTETOFILM_CONFIG_FILE': str(self.config.config_path)})

    def test_failed_checkpoint_blocks_provider_and_keeps_previous_snapshot(self):
        latest = (self.config.backup_dir / 'latest.json').read_bytes()
        with patch.object(self.service.store.snapshots.bucket, 'put', side_effect=OSError('offline')):
            with self.assertRaises(PersistenceError):
                self.service.submit('不能越过持久化失败')
        self.assertTrue(self.service.store.failure)
        self.assertEqual(self.executor.jobs, [])
        self.assertEqual((self.config.backup_dir / 'latest.json').read_bytes(), latest)
        with self.assertRaises(PersistenceError):
            self.service.submit('再次提交仍应被阻止')

    def test_unknown_paid_result_is_retained_across_rebuild(self):
        tid = self.service.submit('中断恢复')['task_id']
        self.executor.run()
        original = self.provider.image
        bucket = self.service.store.snapshots.bucket
        put = bucket.put
        def image(*args, **kwargs):
            original(*args, **kwargs)
            bucket.put = lambda *args: (_ for _ in ()).throw(OSError('offline'))
        self.provider.image = image
        self.service.generate(tid, True)
        self.executor.run()
        self.assertEqual(len(self.provider.images), 1)
        self.assertTrue(self.service.store.failure)
        bucket.put = put
        self.service.close()
        provider = FakeProviders()
        rebuilt = Tasks(self.fresh_config(), provider, FakeRenderer(), ManualExecutor(), validate_fixture)
        rebuilt.open()
        try:
            task = rebuilt.detail(tid)
            self.assertEqual(task['status'], 'interrupted')
            self.assertEqual(task['image_requests'], 1)
            self.assertEqual(task['scenes'][0]['img_state'], 'unknown')
            self.assertEqual(provider.images, [])
            self.assertTrue(rebuilt.retry(tid, False)['needs_confirm'])
            self.assertEqual(provider.images, [])
        finally:
            rebuilt.close()

    def test_corrupt_restore_fails_before_replacing_existing_database(self):
        manifest = json.loads((self.config.backup_dir / 'latest.json').read_text())
        (self.config.backup_dir / 'objects' / manifest['files']['tasks.db']).write_bytes(b'broken')
        self.service.close()
        before = (self.config.data / 'tasks.db').read_bytes()
        broken = Tasks(self.config, FakeProviders(), FakeRenderer(), ManualExecutor(), validate_fixture)
        with self.assertRaises(PersistenceError):
            broken.open()
        self.assertEqual((self.config.data / 'tasks.db').read_bytes(), before)

    def test_unpublished_snapshot_is_not_used_after_partial_upload(self):
        latest = (self.config.backup_dir / 'latest.json').read_bytes()
        bucket = self.service.store.snapshots.bucket
        original = bucket.put
        def partial(name, content):
            if name == 'latest.json':
                raise OSError('connection lost before commit')
            original(name, content)
        with patch.object(bucket, 'put', side_effect=partial):
            with self.assertRaises(PersistenceError):
                self.service.submit('未确认的保存')
        self.assertEqual((self.config.backup_dir / 'latest.json').read_bytes(), latest)
        self.service.close()
        rebuilt = Tasks(self.fresh_config(), FakeProviders(), FakeRenderer(), ManualExecutor(), validate_fixture)
        rebuilt.open()
        try:
            with rebuilt.store.connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
            self.assertEqual(rebuilt.executor.jobs, [])
        finally:
            rebuilt.close()

    def test_historical_audio_survives_rebuild_without_temporary_files(self):
        tid = self.service.submit('旧配音保留')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        self.executor.run()
        old = self.service.detail(tid)['scenes'][0]['audio_path']
        previous = (self.config.data / str(tid) / old).read_bytes()
        partial = self.config.data / str(tid) / 'assets/unfinished/000-aud.part.mp3'
        partial.parent.mkdir()
        partial.write_bytes(b'not published')
        self.service.regen(tid, 0, 'aud', True)
        self.executor.run()
        self.assertNotEqual(self.service.detail(tid)['scenes'][0]['audio_path'], old)
        self.service.close()
        config = self.fresh_config()
        rebuilt = Tasks(config, FakeProviders(), FakeRenderer(), ManualExecutor(), validate_fixture)
        rebuilt.open()
        try:
            self.assertEqual((config.data / str(tid) / old).read_bytes(), previous)
            self.assertFalse((config.data / str(tid) / 'assets/unfinished/000-aud.part.mp3').exists())
        finally:
            rebuilt.close()

    def test_real_media_completed_video_and_owner_survive_empty_work_directory(self):
        self.service.close()
        media = self.root / 'fixtures'
        media.mkdir()
        run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x36',
                     '-frames:v', '1', str(media / 'image.png')], 30)
        run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=0.4',
                     str(media / 'audio.mp3')], 30)
        class Offline(FakeProviders):
            def image(self, prompt, output, references=None):
                self.images.append(prompt)
                output.write_bytes((media / 'image.png').read_bytes())
            def audio(self, text, output):
                self.audios.append(text)
                output.write_bytes((media / 'audio.mp3').read_bytes())
        executor = ManualExecutor()
        provider = Offline()
        self.service = Tasks(self.config, provider, Renderer(self.config), executor, validate_asset)
        self.service.open()
        tid = self.service.submit('重建后继续下载', owner='visitor-owner')['task_id']
        executor.run()
        self.service.generate(tid, True)
        executor.run()
        for scene in self.service.detail(tid)['scenes']:
            for kind in ('img', 'aud'):
                self.service.gate(tid, scene['idx'], kind, True, scene[kind + '_revision'])
        self.service.render(tid)
        executor.run()
        task = self.service.detail(tid)
        self.assertEqual(task['status'], 'done')
        video = self.service.allowed_file(tid, task['video_path']).read_bytes()
        self.service.revise(tid)
        self.service.close()
        config = self.fresh_config()
        rebuilt = Tasks(config, Offline(), Renderer(config), ManualExecutor(), validate_asset)
        rebuilt.open()
        try:
            rebuilt.authorize(tid, 'visitor-owner')
            restored = rebuilt.detail(tid)
            self.assertEqual(restored['status'], 'review')
            self.assertEqual(len(restored['versions']), 1)
            self.assertEqual(rebuilt.allowed_file(tid, task['video_path']).read_bytes(), video)
            self.assertTrue(all(s['img_ok'] and s['aud_ok'] for s in restored['scenes']))
            self.assertEqual(rebuilt.providers.images, [])
            self.assertEqual(rebuilt.providers.audios, [])
        finally:
            rebuilt.close()
