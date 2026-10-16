import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.app import create_app
from app.config import Config
from app.full_config import MASK, encode_full, from_legacy
from app.model_config import ModelConfig, encode
from app.tasks import Tasks
from app.persistence import DirectoryBucket, PersistenceError
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor, validate_fixture


def file_settings(root, persistent=False):
    root = Path(root)
    path = root / 'config/config.toml'
    legacy = Config(data=root / 'work', token='file-admin-secret', base='https://gateway.example/v1', key='file-model-secret')
    document = from_legacy(legacy, ModelConfig(legacy).initial())
    if persistent:
        document['storage']['local_backup_dir'] = str(root / 'durable')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encode_full(document))
    return Config.load({'NOTETOFILM_CONFIG_FILE': str(path)})


class FullConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_import_then_environment_is_ignored(self):
        path = self.root / 'config/config.toml'
        env = {'NOTETOFILM_CONFIG_FILE': str(path), 'VIDEO_DATA_DIR': str(self.root / 'work'),
               'ADMIN_TOKEN': 'import-admin', 'OPENAI_BASE': 'https://gateway.example/v1',
               'OPENAI_API_KEY': 'import-model', 'MAX_DAILY_IMAGE_REQUESTS': '11'}
        legacy = Config.from_env(env)
        models = ModelConfig(legacy)
        legacy.data.mkdir()
        old = encode(models.initial()).replace('origin-deepseek-v4-1-flash', 'saved-model')
        (legacy.data / 'config.toml').write_text(old)
        config = Config.load(env)
        self.assertEqual(config.llm_model, 'saved-model')
        self.assertEqual(config.max_daily_images, 11)
        self.assertEqual(tomllib.loads(path.read_text())['credentials']['shared'], 'import-model')
        self.assertEqual((legacy.data / 'config.toml').read_text(), old)
        reloaded = Config.load({**env, 'ADMIN_TOKEN': 'different-admin', 'OPENAI_API_KEY': '',
                                'LLM_MODEL': 'ignored-model', 'MAX_DAILY_IMAGE_REQUESTS': 'invalid'})
        self.assertEqual(reloaded.token, 'import-admin')
        self.assertEqual(reloaded.llm_key, 'import-model')
        self.assertEqual(reloaded.llm_model, 'saved-model')
        self.assertEqual(reloaded.max_daily_images, 11)

    def test_masked_roundtrip_secret_update_export_and_restart_settings(self):
        config = file_settings(self.root)
        models = ModelConfig(config)
        models.open()
        info = models.info()
        self.assertNotIn('file-model-secret', json.dumps(info))
        self.assertNotIn('file-admin-secret', json.dumps(info))
        self.assertEqual(info['document']['credentials']['shared'], MASK)
        document = info['document']
        document['llm']['model'] = 'updated-model'
        document['credentials']['shared'] = 'replacement-model-secret'
        document['limits']['max_daily_images'] = 12
        validated = models.validate(encode_full(document))
        self.assertEqual(validated['credentials']['shared'], MASK)
        saved = models.save(encode_full(document), info['revision'])
        self.assertEqual(models.current().llm_key, 'replacement-model-secret')
        self.assertIn('max_daily_images', saved['restart_required'])
        self.assertNotIn('replacement-model-secret', models.export())
        self.assertIn('replacement-model-secret', models.export(True))
        self.assertEqual(Config.load({'NOTETOFILM_CONFIG_FILE': str(config.config_path)}).max_daily_images, 12)
        models.restore(saved['revision'])
        self.assertEqual(models.current().llm_key, 'file-model-secret')

    def test_old_model_api_keeps_full_credentials_and_service_settings(self):
        config = file_settings(self.root)
        models = ModelConfig(config)
        models.open()
        legacy = models.snapshot().replace('origin-deepseek-v4-1-flash', 'legacy-api-model')
        models.save(legacy, models.info()['revision'])
        full = tomllib.loads(models.export(True))
        self.assertEqual(full['llm']['model'], 'legacy-api-model')
        self.assertEqual(full['server']['admin_token'], 'file-admin-secret')
        self.assertEqual(full['credentials']['shared'], 'file-model-secret')

    def test_conflict_and_invalid_file_do_not_fall_back_to_environment(self):
        config = file_settings(self.root)
        models = ModelConfig(config)
        models.open()
        info = models.info()
        models.save(info['text'].replace('origin-deepseek-v4-1-flash', 'new-model'), info['revision'])
        with self.assertRaises(HTTPException):
            models.save(info['text'], info['revision'])
        config.config_path.write_text('[broken')
        with self.assertRaises(ValueError):
            Config.load({'NOTETOFILM_CONFIG_FILE': str(config.config_path), 'ADMIN_TOKEN': 'fallback'})
        self.assertEqual(config.config_path.read_text(), '[broken')

    def test_bucket_bootstrap_imports_environment_once(self):
        path = self.root / 'mount/config.toml'
        path.parent.mkdir()
        storage = {'work_dir': str(self.root / 'work'), 'local_backup_dir': '',
                   'config_bucket': 'owner/config', 'data_bucket': 'owner/data', 'hf_token': 'private-hf-token'}
        import_text = encode_full({'storage': storage}).replace('schema_version = 1', 'bootstrap_from_environment = true')
        path.write_text(import_text)
        remote = DirectoryBucket(self.root / 'remote')
        remote.put('config.toml', import_text.encode())
        with patch('app.full_config.HFBucket', return_value=remote):
            config = Config.load({'NOTETOFILM_CONFIG_FILE': str(path), 'ADMIN_TOKEN': 'boot-admin',
                                  'VIDEO_DATA_DIR': str(self.root / 'old-work'),
                                  'OPENAI_API_KEY': 'boot-model', 'LLM_MODEL': 'boot-llm'})
            self.assertEqual(config.token, 'boot-admin')
            full = tomllib.loads(remote.read('config.toml').decode())
            self.assertNotIn('bootstrap_from_environment', full)
            self.assertEqual(full['credentials']['shared'], 'boot-model')
            # 挂载仍有旧引导文件时，远端完整文件优先；无需原环境变量。
            restored = Config.load({'NOTETOFILM_CONFIG_FILE': str(path)})
            self.assertEqual(restored.llm_model, 'boot-llm')
            self.assertEqual(restored.token, 'boot-admin')

    def test_failed_config_write_blocks_further_model_use(self):
        config = file_settings(self.root)
        models = ModelConfig(config)
        models.open()
        info = models.info()
        original = config.config_path.read_bytes()
        with patch.object(models.file_config.bucket, 'put', side_effect=OSError('private upstream detail')):
            with self.assertRaises(PersistenceError) as error:
                models.save(info['text'], info['revision'])
        self.assertNotIn('private upstream detail', str(error.exception))
        self.assertEqual(config.config_path.read_bytes(), original)
        for action in (models.current, models.snapshot, lambda: models.for_task('')):
            with self.assertRaises(PersistenceError):
                action()

    def test_task_database_and_snapshot_do_not_contain_plaintext_credentials(self):
        config = file_settings(self.root, persistent=True)
        executor = ManualExecutor()
        service = Tasks(config, FakeProviders(), FakeRenderer(), executor, validate_fixture)
        service.open()
        try:
            tid = service.submit('配置隔离验收')['task_id']
            executor.run()
            self.assertEqual(service.detail(tid)['status'], 'planned')
            with service.store.connect() as conn:
                snapshot = conn.execute('SELECT model_config FROM tasks').fetchone()[0]
            self.assertNotIn('file-model-secret', snapshot)
            self.assertNotIn('file-admin-secret', snapshot)
            for path in config.backup_dir.rglob('*'):
                if path.is_file():
                    content = path.read_bytes()
                    self.assertNotIn(b'file-model-secret', content)
                    self.assertNotIn(b'file-admin-secret', content)
        finally:
            service.close()

    def test_storage_paths_and_unknown_fields_are_rejected(self):
        config = file_settings(self.root)
        original = tomllib.loads(config.config_path.read_text())
        for changes in ({'local_backup_dir': str(config.data / 'backups')},
                        {'config_bucket': 'owner/shared', 'data_bucket': 'owner/shared', 'hf_token': 'test'},
                        {'data_bucket': 'owner/bucket'}):
            document = tomllib.loads(encode_full(original))
            document['storage'].update(changes)
            config.config_path.write_text(encode_full(document))
            with self.assertRaises(ValueError):
                # 使用纯校验，不在参数测试中访问网络。
                from app.full_config import validate_full
                validate_full(document, config.config_path)


class FullConfigurationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = file_settings(self.temp.name, persistent=True)
        self.service = Tasks(self.config, FakeProviders(), FakeRenderer(), ManualExecutor(), validate_fixture)
        self.client = TestClient(create_app(self.config, self.service), base_url='http://localhost')
        self.client.__enter__()
        self.headers = {'X-Access-Token': 'file-admin-secret'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_only_explicit_admin_export_includes_secrets(self):
        url = '/api/v1/admin/config'
        self.assertEqual(self.client.get(url + '/export?include_secrets=true').status_code, 401)
        info = self.client.get(url, headers=self.headers).json()
        self.assertTrue(info['complete_config'])
        self.assertNotIn('file-model-secret', json.dumps(info))
        text = info['text'].replace('origin-deepseek-v4-1-flash', 'http-saved-model')
        body = {'text': text, 'revision': info['revision']}
        validated = self.client.post(url + '/validate', headers=self.headers, json=body)
        self.assertEqual(validated.status_code, 200)
        self.assertNotIn('file-admin-secret', validated.text)
        saved = self.client.put(url, headers=self.headers, json=body)
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn('file-model-secret', saved.text)
        self.assertEqual(self.service.models.current().llm_model, 'http-saved-model')
        self.assertEqual(self.service.models.current().llm_key, 'file-model-secret')
        safe = self.client.get(url + '/export', headers=self.headers)
        self.assertNotIn('file-model-secret', safe.text)
        full = self.client.get(url + '/export?include_secrets=true', headers=self.headers)
        self.assertEqual(full.status_code, 200)
        self.assertIn('file-model-secret', full.text)
        self.assertIn('no-store', full.headers['Cache-Control'])
        self.assertEqual(self.client.put(url, headers=self.headers, json=body).status_code, 409)

    def test_failed_config_save_returns_503_and_stops_task_admission(self):
        url = '/api/v1/admin/config'
        info = self.client.get(url, headers=self.headers).json()
        with patch.object(self.service.models.file_config.bucket, 'put', side_effect=OSError('network detail')):
            response = self.client.put(url, headers=self.headers, json={'text': info['text'], 'revision': info['revision']})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('network detail', response.text)
        for path in ('/healthz', '/api/v1/health'):
            self.assertEqual(self.client.get(path).status_code, 503)
        self.assertEqual(self.client.post('/api/v1/tasks', headers=self.headers, json={'script': '禁止继续提交'}).status_code, 503)
        self.assertEqual(self.service.executor.jobs, [])
