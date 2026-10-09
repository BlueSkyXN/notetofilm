import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.config import Config
from app.model_config import ModelConfig, encode
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


class GatewayPolicyTests(unittest.TestCase):
    def test_environment_and_toml_share_policy(self):
        valid = ['', 'https://gateway.example/v1', 'https://gateway.example:443/v1']
        invalid = ['http://127.0.0.1:8000/v1', 'https://gateway.example:8443/v1',
                   'https://gateway.example:bad/v1', 'https://gateway.example:99999/v1',
                   'https://user:password@gateway.example/v1', 'https://gateway.example/v1?key=value',
                   'https://gateway.example/v1#fragment', 'https://gate way.example/v1',
                   'https://gateway.example/\nv1', 'https:///v1']
        with tempfile.TemporaryDirectory() as folder:
            for base in valid + invalid:
                with self.subTest(base=base):
                    config = Config(data=Path(folder), token='policy-admin', base=base, key='policy-model')
                    models = ModelConfig(config)
                    if base in valid:
                        config.validate()
                        models.parse(encode(models.initial()))
                    else:
                        for check in (config.validate, lambda: models.parse(encode(models.initial()))):
                            with self.assertRaises(ValueError) as error:
                                check()
                            self.assertNotIn(base, str(error.exception))


class ConfigurationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data=Path(self.temp.name), token='history-admin', base='https://old.invalid/v1', key='history-model')
        self.provider = FakeProviders()
        self.executor = ManualExecutor()
        self.renderer = FakeRenderer()
        self.service = self.new_service(self.config)
        self.service.open()
        self.tid = self.service.submit('history fixture')['task_id']
        self.executor.run()
        self.service.generate(self.tid, True)
        self.executor.run()

    def new_service(self, config):
        return Tasks(config, self.provider, self.renderer, self.executor, validate_fixture)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def restart_without_old_key(self, migrate=True):
        config = replace(self.config, base='', key='', llm_base='https://new-chat.invalid/v1', llm_key='new-chat-model',
                         image_base='https://new-image.invalid/v1', image_key='new-image-model')
        if migrate:
            (config.data / 'config.toml').write_text(encode(ModelConfig(config).initial()))
        self.service.close()
        self.executor = ManualExecutor()
        self.service = self.new_service(config)
        self.service.open()

    def approve(self):
        for s in self.service.detail(self.tid)['scenes']:
            for kind in ('img', 'aud'):
                self.service.gate(self.tid, s['idx'], kind, True, s[kind + '_revision'])

    def test_old_completed_detail_and_files_survive_migration(self):
        root = self.config.data / str(self.tid)
        (root / 'video.mp4').write_bytes(b'completed fixture')
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='done',video_path='video.mp4' WHERE id=?", (self.tid,))
        self.restart_without_old_key()
        detail = self.service.detail(self.tid)
        self.assertEqual(detail['status'], 'done')
        self.assertEqual(detail['model_available'], {'llm': False, 'image': False})
        self.assertFalse(detail['configured'])
        self.assertTrue(detail['video'])
        self.assertTrue(self.service.allowed_file(self.tid, 'video.mp4').is_file())
        self.assertNotIn('old.invalid', json.dumps(detail))

    def test_existing_config_opens_when_keys_are_absent(self):
        self.restart_without_old_key(migrate=False)
        self.assertFalse(self.service.detail(self.tid)['model_available']['image'])
        with self.assertRaises(HTTPException) as error:
            self.service.submit('new task missing llm')
        self.assertEqual(error.exception.status_code, 503)

    def test_local_render_does_not_need_keys(self):
        self.restart_without_old_key()
        self.approve()
        self.service.render(self.tid)
        self.executor.run()
        self.assertEqual(self.service.detail(self.tid)['status'], 'done')
        self.assertEqual(len(self.renderer.calls), 1)

    def test_existing_completion_marker_recovery_does_not_need_keys(self):
        self.restart_without_old_key()
        self.approve()
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='interrupted' WHERE id=?", (self.tid,))
        count = len(self.provider.images)
        with patch.object(self.renderer, 'recover', return_value='renders/existing/video.mp4'):
            self.service.retry(self.tid, False)
            self.executor.run()
        self.assertEqual(self.service.detail(self.tid)['status'], 'done')
        self.assertEqual(len(self.provider.images), count)

    def test_audio_only_regeneration_does_not_need_image_or_llm_key(self):
        self.restart_without_old_key()
        count = len(self.provider.images)
        self.service.regen(self.tid, 0, 'aud', True)
        self.executor.run()
        detail = self.service.detail(self.tid)
        self.assertEqual(detail['status'], 'review')
        self.assertEqual(detail['scenes'][0]['aud_revision'], 2)
        self.assertEqual(len(self.provider.images), count)

    def test_new_image_call_requires_only_image_key(self):
        self.service.models.secret_values['OPENAI_API_KEY'] = ''
        doc = self.service.models.document_for()
        doc['image']['api_key_env'] = 'IMAGE_API_KEY'
        self.service.models.secret_values['IMAGE_API_KEY'] = 'separate-image-model'
        with self.service.store.connect(write=True) as conn:
            conn.execute('UPDATE tasks SET model_config=? WHERE id=?', (encode(doc), self.tid))
        self.service.regen(self.tid, 0, 'img', True, 1, 'image-only-request-key')
        self.executor.run()
        self.assertEqual(self.service.detail(self.tid)['scenes'][0]['img_revision'], 2)

    def test_planning_requires_only_llm_key(self):
        self.service.models.secret_values['OPENAI_API_KEY'] = ''
        self.service.models.secret_values['LLM_API_KEY'] = 'separate-chat-model'
        doc = self.service.models.document_for()
        doc['llm']['api_key_env'] = 'LLM_API_KEY'
        self.service.models.active_text = encode(doc)
        self.service.models.active = doc
        tid = self.service.submit('plan without unrelated image key')['task_id']
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'planned')
        self.assertFalse(self.service.detail(tid)['model_available']['image'])

    def test_retry_missing_audio_reuses_images_without_provider_keys(self):
        self.restart_without_old_key()
        before = len(self.provider.images)
        with self.service.store.connect(write=True) as conn:
            conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (self.tid,))
            conn.execute("UPDATE scenes SET aud_state='failed',audio_path=NULL WHERE task_id=? AND idx=0", (self.tid,))
        self.service.retry(self.tid, False)
        self.executor.run()
        self.assertEqual(self.service.detail(self.tid)['status'], 'review')
        self.assertEqual(len(self.provider.images), before)

    def test_missing_image_key_rejects_before_request_or_quota(self):
        self.restart_without_old_key()
        count = len(self.provider.images)
        with self.assertRaises(HTTPException) as error:
            self.service.regen(self.tid, 0, 'img', True, 1, 'missing-key-request')
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(len(self.provider.images), count)
        self.assertFalse(self.service.detail(self.tid)['busy'])
        with self.service.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM image_requests').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
