import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from app.app import signed_session, valid_session
from app.config import Config
from app.db import Store
from app.providers import Providers
from app.render import Renderer, atomic_json, digest, run_process, validate_asset
from app.layout import video_layout


class ConfigTests(unittest.TestCase):
    def test_empty_models_use_defaults(self):
        c = Config.from_env({'ACCESS_TOKEN': 'test', 'LLM_MODEL': '', 'IMAGE_MODEL': ''})
        c.validate()
        self.assertEqual(c.llm_model, 'origin-deepseek-v4-1-flash')
        self.assertEqual(c.image_model, 'gpt-image-2.5-sunburst')

    def test_hf_host_and_secure_cookie(self):
        c = Config.from_env({'ACCESS_TOKEN': 'test', 'SPACE_HOST': 'owner-notetofilm.hf.space', 'COOKIE_SECURE': '0'})
        c.validate()
        self.assertIn('owner-notetofilm.hf.space', c.allowed_hosts)
        self.assertIn('127.0.0.1', c.allowed_hosts)
        self.assertTrue(c.cookie_secure)
        self.assertEqual(c.voice, 'zh-CN-YunxiNeural')

    def test_hf_host_rejects_injection_and_anonymous(self):
        for host in ('evil.invalid', '*.hf.space', 'owner.hf.space/path', 'owner.hf.space\"'):
            with self.assertRaises(ValueError):
                Config.from_env({'ACCESS_TOKEN': 'test', 'SPACE_HOST': host}).validate()
        with self.assertRaises(ValueError):
            Config.from_env({'SPACE_HOST': 'owner.hf.space', 'ALLOW_UNAUTHENTICATED_LOCAL': '1'}).validate()

    def test_unsafe_default_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_env({}).validate()
        Config.from_env({'ALLOW_UNAUTHENTICATED_LOCAL': '1'}).validate()

    def test_key_only_allowed_for_toml_config(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {'VIDEO_DATA_DIR': folder, 'ACCESS_TOKEN': 'test'}
            Config.from_env({**env, 'OPENAI_API_KEY': 'test'}).validate()
            with self.assertRaises(ValueError):
                Config.from_env({**env, 'OPENAI_BASE': 'https://gateway.example/v1'}).validate()
            (Path(folder) / 'config.toml').write_text('')
            Config.from_env({**env, 'OPENAI_BASE': 'https://gateway.example/v1'}).validate()

    def test_zero_capacity_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_env({'ACCESS_TOKEN': 'test', 'MAX_ACTIVE_TASKS': '0'})

    def test_v3_migration_adds_idempotency_without_changing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            store.open()
            with store.connect(write=True) as conn:
                conn.execute("INSERT INTO tasks(id,script,status,video_path) VALUES(1,'keep','done','video.mp4')")
                conn.execute("INSERT INTO scenes(task_id,idx,narration) VALUES(1,0,'keep scene')")
                conn.execute('DROP TABLE image_requests')
                conn.execute('PRAGMA user_version=3')
            media = Path(folder) / 'video.mp4'
            media.write_bytes(b'unchanged history')
            store.close()
            store.open()
            try:
                with store.connect() as conn:
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 9)
                    self.assertEqual(conn.execute('SELECT purpose,audience,focus,target_duration_seconds FROM tasks').fetchone()[:], ('', '', '', 0))
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM image_requests').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT script,status,video_path FROM tasks').fetchone()[:], ('keep', 'done', 'video.mp4'))
                    self.assertEqual(conn.execute('SELECT narration FROM scenes').fetchone()[0], 'keep scene')
                self.assertEqual(media.read_bytes(), b'unchanged history')
            finally:
                store.close()

    def test_blank_line_segmentation(self):
        c = Config.from_env({'ALLOW_UNAUTHENTICATED_LOCAL': '1'})
        self.assertEqual(Providers(c).plan('line 1\nline 2\n\nparagraph 2'), ['line 1\nline 2', 'paragraph 2'])

    def test_signed_session_expiry_and_tamper(self):
        cookie = signed_session('secret', int(time.time()) + 100)
        self.assertTrue(valid_session('secret', cookie))
        self.assertFalse(valid_session('other', cookie))
        self.assertFalse(valid_session('secret', cookie + 'a'))
        self.assertFalse(valid_session('secret', signed_session('secret', int(time.time()) - 1)))
        self.assertNotIn('secret', cookie)

    def test_legacy_migration_preserves_files_and_requires_plan(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            conn = sqlite3.connect(root / 'tasks.db')
            conn.executescript("CREATE TABLE tasks(id INTEGER PRIMARY KEY,script TEXT,status TEXT,error TEXT,created_at TEXT);"
                               "CREATE TABLE scenes(id INTEGER PRIMARY KEY,task_id INTEGER,idx INTEGER,narration TEXT,img_ok INTEGER,aud_ok INTEGER);"
                               "INSERT INTO tasks VALUES(1,'legacy','review',NULL,NULL);"
                               "INSERT INTO scenes VALUES(1,1,0,'prefix',1,1);")
            conn.close()
            media = root / 'legacy.mp4'
            media.write_bytes(b'keep')
            store = Store(root)
            store.open()
            try:
                with store.connect() as conn:
                    row = conn.execute('SELECT * FROM tasks').fetchone()
                    self.assertEqual(row['status'], 'interrupted')
                    self.assertEqual(row['plan_complete'], 0)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM scenes').fetchone()[0], 1)
                self.assertEqual(media.read_bytes(), b'keep')
            finally:
                store.close()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe required')
class RenderTests(unittest.TestCase):
    def test_multiscene_portrait_fit_pixels_and_layout_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                         'color=c=red:s=640x360,drawbox=x=160:y=0:w=320:h=360:color=blue:t=fill',
                         '-frames:v', '1', str(root / 'wide.png')], 30)
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                         'sine=duration=0.7', str(root / 'audio.mp3')], 30)
            scenes = [{'idx': idx, 'narration': f'镜 {idx}', 'img_revision': 1, 'aud_revision': 1,
                       'image_path': 'wide.png', 'audio_path': 'audio.mp3'} for idx in range(3)]
            renderer = Renderer(Config(data=root))
            for fit in ('contain', 'cover'):
                with self.subTest(fit=fit):
                    layout = video_layout('9:16', fit)
                    output = renderer.render(root, fit, scenes, layout)
                    report = json.loads((root / output).with_suffix('.build.json').read_text())
                    self.assertEqual((report['width'], report['height'], report['scene_count']), (1080, 1920, 3))
                    self.assertEqual(report['technical_validation'], 'passed')
                    pixel = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(root / output),
                                                     '-vf', 'format=rgb24,crop=1:1:10:10', '-frames:v', '1', '-f', 'rawvideo', '-'])
                    if fit == 'contain':
                        self.assertTrue(all(channel > 235 for channel in pixel), pixel)
                    else:
                        self.assertTrue(pixel[2] > 200 and pixel[0] < 35 and pixel[1] < 35, pixel)
                    self.assertEqual(renderer.recover(root, scenes, layout), output)
                    self.assertIsNone(renderer.recover(root, scenes, video_layout()))
            self.assertNotEqual(renderer.recover(root, scenes, video_layout('9:16', 'cover')),
                                renderer.recover(root, scenes, video_layout('9:16', 'contain')))

    def test_service_end_to_end_without_external_calls(self):
        from app.tasks import Tasks
        from test_tasks import ManualExecutor, FakeProviders
        class LocalProviders(FakeProviders):
            def image(self, narration, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180', '-frames:v', '1', str(output)], 30)
            def audio(self, narration, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.2', '-c:a', 'libmp3lame', str(output)], 30)
        with tempfile.TemporaryDirectory() as root:
            config = Config(data=Path(root), token='test', base='https://unused.invalid/v1', key='fixture')
            providers = LocalProviders()
            providers.plan_result = ['offline full pipeline']
            executor = ManualExecutor()
            service = Tasks(config, providers=providers, executor=executor)
            service.open()
            try:
                tid = service.submit('offline full pipeline')['task_id']
                executor.run()
                self.assertEqual(service.detail(tid)['status'], 'planned')
                service.generate(tid, True)
                executor.run()
                self.assertEqual(service.detail(tid)['status'], 'review')
                for kind in ('img', 'aud'):
                    service.gate(tid, 0, kind, True, 1)
                service.models.secret_values['OPENAI_API_KEY'] = ''
                self.assertFalse(service.detail(tid)['model_available']['image'])
                service.render(tid)
                executor.run()
                detail = service.detail(tid)
                self.assertEqual(detail['status'], 'done')
                self.assertTrue(service.allowed_file(tid, detail['video_path']).is_file())
                with service.store.connect(write=True) as conn:
                    conn.execute("UPDATE tasks SET status='interrupted',video_path=NULL WHERE id=?", (tid,))
                service.retry(tid, False)
                executor.run()
                self.assertEqual(service.detail(tid)['status'], 'done')
            finally:
                service.close()

    def test_local_render_recovery_and_corruption(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180', '-frames:v', '1', str(root / 'image.png')], 30)
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.2', '-c:a', 'libmp3lame', str(root / 'audio.mp3')], 30)
            validate_asset(root / 'image.png', 'img')
            validate_asset(root / 'audio.mp3', 'aud')
            scenes = [{'id': 12, 'idx': 0, 'image_asset_id': 7,
                       'image_references': {'fixed_references': [{'asset_id': 3, 'sha256': 'a' * 64}], 'previous_scene': None},
                       'narration': 'offline fixture', 'img_revision': 1, 'aud_revision': 1,
                       'image_path': 'image.png', 'audio_path': 'audio.mp3'}]
            renderer = Renderer(Config(data=root, token='test'))
            (root / 'video.mp4').write_bytes(b'old incomplete file')
            path = renderer.render(root, 'attempt-test', scenes)
            self.assertTrue((root / path).is_file())
            self.assertEqual(renderer.recover(root, scenes), path)
            self.assertEqual((root / 'video.mp4').read_bytes(), b'old incomplete file')
            target = (root / path).parent
            frozen = json.loads((target / 'frozen.json').read_text())
            self.assertEqual(frozen['schema_version'], 2)
            self.assertEqual(frozen['scenes'][0]['scene_id'], 12)
            self.assertEqual(frozen['scenes'][0]['image_asset_id'], 7)
            self.assertEqual(frozen['scenes'][0]['image_references'], scenes[0]['image_references'])
            for changed in ({'id': 13}, {'image_asset_id': 8}, {'image_references': {}}):
                with self.subTest(changed=changed):
                    self.assertIsNone(renderer.recover(root, [{**scenes[0], **changed}]))
            marker = json.loads((target / 'complete.json').read_text())
            # 旧清单按原字段验证，新清单则同时核对稳定 ID 与实际参考。
            for saved, expected in ((renderer.freeze(root, scenes, version=1), path),
                                    ({'schema_version': 99, 'scenes': []}, None), (frozen, path)):
                atomic_json(target / 'frozen.json', saved)
                marker['frozen_sha256'] = digest(target / 'frozen.json')
                atomic_json(target / 'complete.json', marker)
                self.assertEqual(renderer.recover(root, scenes), expected)
            (root / path).write_bytes(b'')
            self.assertIsNone(renderer.recover(root, scenes))

    def test_audio_duration_limit(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'audio.mp3'
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=1.2', str(file)], 30)
            with self.assertRaises(ValueError):
                validate_asset(file, 'aud', max_audio_seconds=1)

    def test_video_duration_rejected_before_encoding(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180', '-frames:v', '1', str(root / 'image.png')], 30)
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=1.2', str(root / 'audio.mp3')], 30)
            scenes = [{'idx': 0, 'narration': 'fixture', 'img_revision': 1, 'aud_revision': 1,
                       'image_path': 'image.png', 'audio_path': 'audio.mp3'}]
            with self.assertRaises(ValueError):
                Renderer(Config(data=root, max_video_seconds=1)).render(root, 'too-long', scenes)
            self.assertFalse((root / 'renders/too-long/video.mp4').exists())

    def test_empty_legacy_output_is_not_completed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'video.mp4').write_bytes(b'')
            (root / 'video.build.json').write_text('{}')
            self.assertIsNone(Renderer(Config(data=root)).recover(root, []))


if __name__ == '__main__':
    unittest.main()
