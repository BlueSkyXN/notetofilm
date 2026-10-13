import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import new_visitor, visitor_owner
from app.config import Config
from app.model_config import ModelConfig, encode
from app.providers import Providers
from app.tasks import Tasks
from test_tasks import ManualExecutor, FakeProviders, FakeRenderer, validate_fixture


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data=Path(self.temp.name), token='administrator-secret-value',
                             base='https://gateway.example/v1', key='model-secret-value')
        self.models = ModelConfig(self.config)
        self.models.open()

    def tearDown(self):
        self.temp.cleanup()

    def test_initial_migration_and_no_secret_values(self):
        info = self.models.info()
        self.assertIn('api_key_env = "OPENAI_API_KEY"', info['text'])
        self.assertNotIn(self.config.key, json.dumps(info))
        self.assertNotIn(self.config.token, json.dumps(info))
        self.assertEqual(self.models.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(info['secrets']['OPENAI_API_KEY'])

    def test_save_apply_and_previous_restore(self):
        initial = self.models.info()
        text = initial['text'].replace('origin-deepseek-v4-1-flash', 'another-model')
        saved = self.models.save(text, initial['revision'])
        self.assertEqual(self.models.current().llm_model, 'another-model')
        restored = self.models.restore(saved['revision'])
        self.assertEqual(restored['text'], initial['text'])
        self.assertEqual(self.models.current().llm_model, 'origin-deepseek-v4-1-flash')

    def test_file_takes_precedence_after_restart(self):
        info = self.models.info()
        self.models.save(info['text'].replace('origin-deepseek-v4-1-flash', 'chosen-model'), info['revision'])
        second = ModelConfig(replace(self.config, llm_model='env-changed'))
        second.open()
        self.assertEqual(second.current().llm_model, 'chosen-model')

    def test_legacy_toml_and_snapshot_default_to_chat(self):
        legacy = self.models.info()['text'].replace('wire_api = "chat"\n', '')
        self.models.path.write_text(legacy)
        second = ModelConfig(self.config)
        second.open()
        self.assertEqual(second.current().llm_wire_api, 'chat')
        self.assertEqual(second.for_task(legacy).llm_wire_api, 'chat')
        self.assertEqual(second.path.read_text(), legacy)
        with self.assertRaises(ValueError):
            second.validate(legacy.replace('[llm]', '[llm]\nwire_api = "unsupported"'))

    def test_reference_protocol_defaults_and_freezes_per_task(self):
        legacy = self.models.info()['text'].replace('reference_protocol = "none"\n', '')
        self.models.path.write_text(legacy)
        second = ModelConfig(self.config)
        second.open()
        original = second.snapshot()
        self.assertEqual(second.current().image_reference_protocol, 'none')
        info = second.info()
        edited = encode(info['document']).replace('reference_protocol = "none"', 'reference_protocol = "openai-images-edit"')
        second.save(edited, info['revision'])
        self.assertEqual(second.current().image_reference_protocol, 'openai-images-edit')
        self.assertEqual(second.for_task(original).image_reference_protocol, 'none')
        with self.assertRaises(ValueError):
            second.validate(edited.replace('reference_protocol = "openai-images-edit"', 'reference_protocol = "unsupported"'))

    def test_wire_api_is_saved_and_frozen_per_task(self):
        original = self.models.snapshot()
        info = self.models.info()
        self.models.save(info['text'].replace('wire_api = "chat"', 'wire_api = "responses"'), info['revision'])
        self.assertEqual(self.models.current().llm_wire_api, 'responses')
        self.assertEqual(self.models.for_task(original).llm_wire_api, 'chat')

    def test_bad_syntax_unknown_fields_and_plain_secrets_rejected(self):
        original = self.models.info()['text']
        for text in ('[broken', original + '\n[auth]\npassword="x"', original + '\n# ' + self.config.key,
                     original.replace('api_key_env', 'api_key'), original.replace('OPENAI_API_KEY', 'ADMIN_TOKEN')):
            with self.assertRaises(ValueError):
                self.models.validate(text)
        self.assertEqual(self.models.info()['text'], original)

    def test_toml_unicode_escape_cannot_hide_secret(self):
        text = self.models.info()['text'].replace('origin-deepseek-v4-1-flash', '\\u006dodel-secret-value')
        with self.assertRaises(ValueError):
            self.models.validate(text)

    def test_key_only_environment_with_existing_toml(self):
        second = ModelConfig(replace(self.config, base=''))
        second.open()
        self.assertEqual(second.current().llm_base, 'https://gateway.example/v1')
        self.assertEqual(second.current().llm_key, self.config.key)

    def test_binding_change_needs_confirmation(self):
        info = self.models.info()
        changed = info['text'].replace('gateway.example', 'other.example')
        with self.assertRaises(HTTPException) as error:
            self.models.save(changed, info['revision'])
        self.assertEqual(error.exception.status_code, 409)
        self.models.save(changed, info['revision'], True)
        self.assertIn('other.example', self.models.current().llm_base)
        with self.assertRaises(HTTPException):
            self.models.restore(self.models.info()['revision'])

    def test_missing_key_and_insecure_endpoint_rejected(self):
        original = self.models.info()['text']
        for text in (original.replace('OPENAI_API_KEY', 'LLM_API_KEY'), original.replace('https:', 'http:'),
                     original.replace('gateway.example', 'user:password@gateway.example'),
                     original.replace('/v1', '/v1?key=x'), original.replace('timeout_seconds = 300', 'timeout_seconds = true')):
            with self.assertRaises(ValueError):
                self.models.validate(text)

    def test_stale_and_external_edits_rejected(self):
        info = self.models.info()
        self.models.save(info['text'].replace('origin-deepseek-v4-1-flash', 'next-model'), info['revision'])
        with self.assertRaises(HTTPException):
            self.models.save(info['text'], info['revision'])
        info = self.models.info()
        self.models.path.write_text(info['text'] + '\n# external')
        with self.assertRaises(HTTPException):
            self.models.save(info['text'], info['revision'])
        self.assertTrue(self.models.info()['external_change'])

    def test_simultaneous_saves_only_one_wins(self):
        info = self.models.info()
        def save(model):
            try:
                self.models.save(info['text'].replace('origin-deepseek-v4-1-flash', model), info['revision'])
                return 200
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(save, ['a-model', 'b-model'])), [200, 409])

    def test_validation_never_calls_provider(self):
        with patch('app.providers.bounded_request', side_effect=AssertionError('unexpected paid request')):
            info = self.models.info()
            self.models.validate(info['text'])
            self.models.save(info['text'], info['revision'])

    def test_failed_file_replace_keeps_active_version(self):
        info = self.models.info()
        with patch.object(self.models, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.models.save(info['text'].replace('origin-deepseek-v4-1-flash', 'next-model'), info['revision'])
        self.assertEqual(self.models.info()['revision'], info['revision'])
        self.assertEqual(self.models.path.read_text(), info['text'])

    def test_visitor_cookie_unforgeable_and_separate_from_admin(self):
        from app.app import signed_session, valid_session
        import time
        cookie = new_visitor('key')
        self.assertTrue(visitor_owner('key', cookie))
        self.assertIsNone(visitor_owner('other-key', cookie))
        self.assertIsNone(visitor_owner('key', cookie + 'x'))
        self.assertFalse(valid_session('key', cookie))
        self.assertIsNone(visitor_owner('key', signed_session('key', int(time.time()) + 100)))


class TaskConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.executor = ManualExecutor()
        config = Config(data=Path(self.temp.name), token='administrator-secret-value', base='https://gateway.example/v1', key='model-secret-value')
        self.service = Tasks(config, FakeProviders(), FakeRenderer(), self.executor, validate_fixture)
        self.service.open()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_snapshot_is_pinned_and_secret_free(self):
        self.service.injected_provider = False
        first = self.service.submit('first script', 'visitor-a')['task_id']
        info = self.service.models.info()
        self.service.models.save(info['text'].replace('origin-deepseek-v4-1-flash', 'new-model'), info['revision'])
        second = self.service.submit('second script', 'visitor-b')['task_id']
        with patch.object(Providers, 'plan', autospec=True, return_value=['one scene']) as planner:
            self.executor.run()
            self.executor.run()
            self.assertEqual([x.args[0].config.llm_model for x in planner.call_args_list], ['origin-deepseek-v4-1-flash', 'new-model'])
        with self.service.store.connect() as conn:
            rows = conn.execute('SELECT model_config FROM tasks').fetchall()
        self.assertTrue(all(self.service.config.key not in r['model_config'] for r in rows))
        self.assertNotEqual(self.service.detail(first)['config_revision'], self.service.detail(second)['config_revision'])
        self.assertNotIn('model_config', self.service.detail(first))
        self.assertNotIn('owner', self.service.detail(first))
        self.assertNotIn('gateway.example', json.dumps(self.service.detail(first)))

    def test_duplicate_is_per_owner(self):
        first = self.service.submit('same script', 'a')['task_id']
        second = self.service.submit('same script', 'b')['task_id']
        self.assertNotEqual(first, second)
        self.assertEqual([t['id'] for t in self.service.listing('a')['tasks']], [first])
        with self.assertRaises(HTTPException):
            self.service.authorize(first, 'b')
        self.service.authorize(first, 'admin', True)

    def test_global_task_limit_survives_new_visitors_and_archive(self):
        self.service.config = replace(self.service.config, max_daily_tasks=1)
        tid = self.service.submit('one', 'a')['task_id']
        self.executor.run()
        self.service.archive(tid)
        with self.assertRaises(HTTPException) as exc:
            self.service.submit('two', 'new-visitor')
        self.assertEqual(exc.exception.status_code, 429)

    def test_visitor_task_limit(self):
        self.service.config = replace(self.service.config, max_visitor_tasks=1)
        self.service.submit('one', 'a')
        self.executor.run()
        with self.assertRaises(HTTPException):
            self.service.submit('two', 'a')
        self.service.submit('two', 'b')

    def test_daily_image_limit_checked_before_provider(self):
        self.service.config = replace(self.service.config, max_daily_images=1)
        tid = self.service.submit('script')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        self.executor.run()
        self.assertEqual(len(self.service.providers.images), 1)
        self.assertEqual(self.service.detail(tid)['status'], 'failed')
        self.assertEqual(self.service.detail(tid)['image_requests'], 1)

    def test_limits_persist_across_restart(self):
        self.service.config = replace(self.service.config, max_daily_tasks=1)
        tid = self.service.submit('one', 'a')['task_id']
        self.executor.run()
        snapshot = self.service.detail(tid)['config_revision']
        self.service.close()
        self.service = Tasks(self.service.config, FakeProviders(), FakeRenderer(), ManualExecutor(), validate_fixture)
        self.service.open()
        self.assertEqual(self.service.detail(tid)['config_revision'], snapshot)
        with self.assertRaises(HTTPException):
            self.service.submit('two', 'new-visitor')

    def test_audio_budget_stops_before_second_call(self):
        self.service.config = replace(self.service.config, max_daily_audio=1)
        tid = self.service.submit('audio cap')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        self.executor.run()
        self.assertEqual(len(self.service.providers.audios), 1)
        self.assertEqual(self.service.detail(tid)['status'], 'failed')
        self.assertIn('额度', self.service.detail(tid)['error'])

    def test_render_budget_is_transactional(self):
        with self.service.store.connect(write=True) as conn:
            self.service.reserve(conn, 'render', 1)
        with self.assertRaises(HTTPException):
            with self.service.store.connect(write=True) as conn:
                self.service.reserve(conn, 'render', 1)
        with self.service.store.connect() as conn:
            count = conn.execute("SELECT count FROM daily_usage WHERE kind='render'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_daily_llm_limit_prevents_replanning_call(self):
        self.service.config = replace(self.service.config, max_daily_llm=1)
        self.service.submit('first')
        self.executor.run()
        tid = self.service.submit('second')['task_id']
        with patch.object(self.service.providers, 'plan', side_effect=AssertionError('must not call')):
            self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
