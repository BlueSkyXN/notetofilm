"""分镜图片、参考依赖和配置快照的离线回归。"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException
from app.config import Config
from app.render import run_process, validate_asset
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor


class ImageEditingRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        root = Path(cls.media.name)
        cls.pngs = []
        for color in ('red', 'blue'):
            target = root / f'{color}.png'
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                         f'color=c={color}:s=64x36', '-frames:v', '1', str(target)], 30)
            cls.pngs.append(target.read_bytes())
        audio = root / 'fixture.mp3'
        run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                     'sine=frequency=440:duration=0.4', '-c:a', 'libmp3lame', str(audio)], 30)
        cls.mp3 = audio.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        png, audio = self.pngs[0], self.mp3

        class Offline(FakeProviders):
            def image(self, prompt, output, references=None):
                self.images.append(prompt)
                self.image_references.append(references or [])
                output.write_bytes(png)

            def audio(self, narration, output):
                self.audios.append(narration)
                output.write_bytes(audio)

        self.provider = Offline()
        self.executor = ManualExecutor()
        self.service = Tasks(Config(data=Path(self.temp.name), token='offline-audit-admin',
                                    base='https://unused.invalid/v1', key='offline-audit-model',
                                    image_reference_protocol='openai-images-edit'),
                             self.provider, FakeRenderer(), self.executor, validate_asset)
        self.service.open()
        self.tid = self.service.submit('audit fixture')['task_id']
        self.executor.run()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def detail(self):
        return self.service.detail(self.tid)

    def plan(self, explicit=True):
        rows = []
        for scene in self.detail()['scenes']:
            row = {key: scene[key] for key in ('id', 'narration', 'visual_prompt')}
            if explicit:
                row['image'] = {'source': scene['image_source'],
                                'asset_id': scene['image_asset_id'] if scene['image_source'] == 'asset' else None,
                                'fixed_reference_asset_ids': scene['fixed_reference_asset_ids'],
                                'previous_scene_id': scene['previous_scene_id']}
            rows.append(row)
        return rows

    def save(self, rows):
        return self.service.save_plan(self.tid, rows, self.detail()['plan_revision'])

    def generate(self):
        self.service.generate(self.tid, True, self.detail()['plan_revision'])
        self.executor.run()

    def uploads(self):
        return [self.service.upload_image(self.tid, content, 'image/png')[0] for content in self.pngs]

    def select_uploads(self):
        assets = self.uploads()
        rows = self.plan()
        for i, row in enumerate(rows):
            row['image'] = {'source': 'asset', 'asset_id': assets[i % 2]['id']}
        self.save(rows)
        return assets

    def chain(self):
        rows = self.plan()
        for i in (1, 2):
            rows[i]['image']['previous_scene_id'] = rows[i - 1]['id']
        self.save(rows)
        self.generate()
        self.assertEqual(self.detail()['status'], 'review')

    def test_replace_uploaded_image_applies_selected_asset(self):
        assets = self.select_uploads()
        rows = self.plan()
        rows[0]['image']['asset_id'] = assets[1]['id']
        self.save(rows)
        self.assertEqual(self.detail()['scenes'][0]['image_asset_id'], assets[1]['id'])

    def test_new_scene_can_select_uploaded_image(self):
        assets = self.uploads()
        rows = self.plan()
        rows.append({'narration': '新增镜头', 'visual_prompt': '新增画面',
                     'image': {'source': 'asset', 'asset_id': assets[0]['id']}})
        self.save(rows)
        self.assertEqual(len(self.detail()['scenes']), 4)
        scene = self.detail()['scenes'][-1]
        self.assertEqual(scene['image_asset_id'], assets[0]['id'])
        self.assertEqual((scene['img_state'], scene['img_ok']), ('ready', 0))

    def test_narration_edit_preserves_uploaded_image_approvals(self):
        self.select_uploads()
        self.generate()
        for s in self.detail()['scenes']:
            self.service.gate(self.tid, s['idx'], 'img', True, s['img_revision'])
        rows = self.plan()
        rows[1]['narration'] = '只修改第二镜旁白'
        self.save(rows)
        self.assertEqual([s['img_ok'] for s in self.detail()['scenes']], [1, 1, 1])

    def test_narration_edit_keeps_generated_asset_identity(self):
        self.generate()
        before = [s['image_asset_id'] for s in self.detail()['scenes']]
        rows = self.plan()
        rows[1]['narration'] = '只修改第二镜旁白'
        self.save(rows)
        self.assertEqual([s['image_asset_id'] for s in self.detail()['scenes']], before)

    def test_capabilities_use_frozen_task_configuration(self):
        before = self.detail()['image_capabilities']
        info = self.service.models.info()
        self.service.models.save(info['text'].replace('reference_protocol = "openai-images-edit"',
                                                      'reference_protocol = "none"'), info['revision'])
        self.assertEqual(self.detail()['image_capabilities'], before)

    def test_full_generation_refreshes_transitive_dependencies(self):
        self.chain()
        rows = self.plan()
        rows[0]['visual_prompt'] = '第一镜的新画面'
        self.save(rows)
        self.generate()
        self.assertEqual(self.detail()['status'], 'review',
                         [(s['idx'], s['img_state']) for s in self.detail()['scenes']])

    def test_legacy_plan_reorder_checks_preserved_reference(self):
        self.chain()
        before = self.detail()
        with self.assertRaises(HTTPException):
            self.save(self.plan(explicit=False)[::-1])
        self.assertEqual(self.detail()['scenes'], before['scenes'])
        self.assertEqual(self.detail()['plan_revision'], before['plan_revision'])

    def test_string_plan_reorder_checks_preserved_reference(self):
        self.chain()
        with self.assertRaises(HTTPException):
            self.save([s['narration'] for s in self.detail()['scenes']][::-1])

    def test_switch_from_uploaded_to_generated_image(self):
        self.select_uploads()
        rows = self.plan()
        rows[0]['image'] = {'source': 'generated'}
        self.save(rows)
        self.assertEqual(self.detail()['scenes'][0]['image_source'], 'generated')
        self.assertEqual(self.detail()['image_requests_needed'], 1)
        self.generate()
        self.assertEqual(self.provider.images, ['first'])

    def test_changed_upload_invalidates_and_refreshes_reference_chain(self):
        assets = self.uploads()
        rows = self.plan()
        rows[0]['image'] = {'source': 'asset', 'asset_id': assets[0]['id']}
        rows[1]['image']['previous_scene_id'] = rows[0]['id']
        rows[2]['image']['previous_scene_id'] = rows[1]['id']
        self.save(rows)
        self.generate()
        rows = self.plan()
        rows[0]['image']['asset_id'] = assets[1]['id']
        self.save(rows)
        self.assertEqual(self.detail()['image_requests_needed'], 2)
        self.generate()
        self.assertEqual(self.detail()['status'], 'review')
        self.assertEqual(self.provider.images, ['second', 'third', 'second', 'third'])
        self.assertEqual(len(self.provider.audios), 3)
        generated = self.detail()['image_assets'][-2]
        self.assertEqual(generated['context']['previous_scene']['asset_id'], assets[1]['id'])

    def test_reference_chain_budget_is_checked_before_any_request(self):
        self.chain()
        rows = self.plan()
        rows[0]['visual_prompt'] = '上游修改'
        self.save(rows)
        self.service.config = replace(self.service.config, max_image_requests=5)
        self.assertEqual(self.detail()['image_requests_needed'], 3)
        with self.assertRaises(HTTPException) as error:
            self.service.generate(self.tid, True, self.detail()['plan_revision'])
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(self.detail()['image_requests'], 3)
        self.assertEqual(self.executor.jobs, [])

    def test_generated_asset_identity_survives_later_reference_regeneration(self):
        self.chain()
        before = self.detail()
        rows = self.plan()
        rows[2]['visual_prompt'] = '只改最后一镜画面'
        self.save(rows)
        self.generate()
        after = self.detail()
        self.assertEqual(len(after['image_assets']), len(before['image_assets']) + 1)
        self.assertEqual(after['scenes'][1]['image_asset_id'], before['scenes'][1]['image_asset_id'])
        self.assertEqual(after['image_assets'][-1]['context']['previous_scene']['asset_id'], before['scenes'][1]['image_asset_id'])

    def test_render_records_actual_references_and_copy_resets_source_bindings(self):
        assets = self.uploads()
        rows = self.plan()
        rows[0]['image'] = {'source': 'asset', 'asset_id': assets[0]['id']}
        rows[1]['image'] = {'source': 'generated', 'fixed_reference_asset_ids': [assets[1]['id']],
                            'previous_scene_id': rows[0]['id']}
        self.save(rows)
        self.generate()
        detail = self.detail()
        with self.service.store.connect() as conn:
            frozen = self.service.render_scenes(conn, self.tid, self.service.scenes(conn, self.tid))
        references = frozen[1]['image_references']
        self.assertEqual(references['fixed_references'], [{'asset_id': assets[1]['id'], 'sha256': assets[1]['sha256']}])
        self.assertEqual(references['previous_scene']['asset_id'], assets[0]['id'])
        self.assertEqual(references['previous_scene']['sha256'], assets[0]['sha256'])
        copied = self.service.detail(self.service.copy_next(self.tid)['task_id'])
        self.assertEqual([s['narration'] for s in copied['scenes']], [s['narration'] for s in detail['scenes']])
        self.assertEqual([s['visual_prompt'] for s in copied['scenes']], [s['visual_prompt'] for s in detail['scenes']])
        self.assertTrue(set(s['id'] for s in copied['scenes']).isdisjoint(s['id'] for s in detail['scenes']))
        self.assertEqual(copied['image_assets'], [])
        for scene in copied['scenes']:
            self.assertEqual(scene['image_source'], 'generated')
            self.assertIsNone(scene['image_asset_id'])
            self.assertEqual(scene['fixed_reference_asset_ids'], [])
            self.assertIsNone(scene['previous_scene_id'])
            self.assertIsNone(scene['image'])
            self.assertIsNone(scene['audio'])
