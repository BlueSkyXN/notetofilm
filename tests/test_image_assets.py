import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.config import Config
from app.image_assets import validate_and_normalize
from app.render import Renderer, run_process, validate_asset
from app.tasks import Tasks
from test_tasks import FakeProviders, FakeRenderer, ManualExecutor


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class ImageAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        png = self.root / 'input.png'
        run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                     'color=c=blue:s=32x20', '-frames:v', '1', str(png)], 30)
        self.png = png.read_bytes()
        self.config = Config(data=self.root / 'data', token='image-assets-test', max_active=1,
                             base='https://unused.invalid/v1', key='image-assets-fixture')
        self.executor = ManualExecutor()
        self.service = Tasks(self.config, FakeProviders(), FakeRenderer(), self.executor, validate_asset)
        self.service.open()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def planned(self, script='image asset fixture'):
        tid = self.service.submit(script)['task_id']
        self.executor.run()
        return tid

    def test_upload_is_registered_deduplicated_and_readable_without_generation(self):
        tid = self.planned()
        before = self.service.detail(tid)
        first, created = self.service.upload_image(tid, self.png, 'image/png')
        second, duplicated = self.service.upload_image(tid, self.png, 'image/png')

        self.assertTrue(created)
        self.assertFalse(duplicated)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual((first['width'], first['height'], first['source']), (32, 20, 'upload'))
        after = self.service.detail(tid)
        self.assertEqual(len(after['image_assets']), 1)
        self.assertEqual(after['scenes'], before['scenes'])
        self.assertEqual(after['image_requests'], before['image_requests'])
        relative = first['url'].split('/files/', 1)[1]
        self.assertTrue(self.service.allowed_file(tid, relative).read_bytes())

    def test_invalid_type_content_and_limits_do_not_publish_assets(self):
        tid = self.planned()
        for content, content_type, status in ((b'not an image', 'image/png', 422),
                                                (self.png, 'image/jpeg', 422),
                                                (self.png, 'application/octet-stream', 422)):
            with self.subTest(content_type=content_type):
                with self.assertRaises(HTTPException) as error:
                    self.service.upload_image(tid, content, content_type)
                self.assertEqual(error.exception.status_code, status)
        with self.assertRaises(HTTPException) as error:
            validate_and_normalize(self.config.data / str(tid), self.png, 'image/png',
                                   len(self.png) - 1, self.config.max_image_pixels)
        self.assertEqual(error.exception.status_code, 413)
        detail = self.service.detail(tid)
        self.assertEqual(detail['image_assets'], [])
        self.assertEqual(detail['image_requests'], 0)

    def test_generated_images_are_registered_with_scene_context(self):
        class LocalProviders(FakeProviders):
            def image(self, prompt, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             'color=c=green:s=64x36', '-frames:v', '1', str(output)], 30)

            def audio(self, narration, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             'sine=frequency=440:duration=0.3', '-c:a', 'libmp3lame', str(output)], 30)

        self.service.providers = LocalProviders()
        self.service.providers.plan_result = ['generated asset fixture']
        tid = self.service.submit('generated asset fixture')['task_id']
        self.executor.run()
        self.service.generate(tid, True)
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review', detail['error'])
        self.assertEqual(len(detail['image_assets']), 1)
        asset = detail['image_assets'][0]
        self.assertEqual(asset['source'], 'generated')
        self.assertEqual(asset['origin_scene_id'], detail['scenes'][0]['id'])
        self.assertEqual(asset['context']['visual_prompt'], detail['scenes'][0]['visual_prompt'])

    def test_fixed_reference_is_frozen_and_blocked_until_protocol_is_enabled(self):
        class LocalProviders(FakeProviders):
            def image(self, prompt, output, references=None):
                self.images.append(prompt)
                self.image_references.append(references or [])
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             'color=c=purple:s=64x36', '-frames:v', '1', '-y', str(output)], 30)

            def audio(self, narration, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             'sine=frequency=440:duration=0.3', '-c:a', 'libmp3lame', str(output)], 30)

        self.service.providers = LocalProviders()
        self.service.providers.plan_result = ['固定参考']
        default_tid = self.planned('固定参考未启用协议')
        reference, _ = self.service.upload_image(default_tid, self.png, 'image/png')
        default_detail = self.service.detail(default_tid)
        self.assertFalse(default_detail['image_capabilities']['fixed_references_supported'])
        scene = default_detail['scenes'][0]
        plan = [{'id': scene['id'], 'narration': scene['narration'], 'visual_prompt': scene['visual_prompt'],
                 'image': {'source': 'generated', 'asset_id': None, 'fixed_reference_asset_ids': [reference['id']]}}]
        self.service.save_plan(default_tid, plan, default_detail['plan_revision'])
        with self.assertRaises(HTTPException) as error:
            self.service.generate(default_tid, True, self.service.detail(default_tid)['plan_revision'])
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.executor.jobs, [])
        self.assertEqual(self.service.detail(default_tid)['image_requests'], 0)

        info = self.service.models.info()
        self.service.models.save(info['text'].replace('reference_protocol = "none"', 'reference_protocol = "openai-images-edit"'),
                                 info['revision'])
        tid = self.service.submit('固定参考已启用协议')['task_id']
        self.executor.run()
        selected, _ = self.service.upload_image(tid, self.png, 'image/png')
        reference_bytes = self.service.allowed_file(tid, selected['url'].split('/files/', 1)[1]).read_bytes()
        detail = self.service.detail(tid)
        scene = detail['scenes'][0]
        plan = [{'id': scene['id'], 'narration': scene['narration'], 'visual_prompt': scene['visual_prompt'],
                 'image': {'source': 'generated', 'asset_id': None, 'fixed_reference_asset_ids': [selected['id']]}}]
        self.service.save_plan(tid, plan, detail['plan_revision'])
        detail = self.service.detail(tid)
        self.assertTrue(detail['image_capabilities']['fixed_references_supported'])
        self.assertEqual(detail['scenes'][0]['fixed_reference_asset_ids'], [selected['id']])
        self.service.generate(tid, True, detail['plan_revision'])
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review', detail['error'])
        self.assertEqual(self.service.providers.image_references, [[{'mime': 'image/png', 'content': reference_bytes}]])
        generated = next(asset for asset in detail['image_assets'] if asset['source'] == 'generated')
        self.assertEqual(generated['context']['reference_protocol'], 'openai-images-edit')
        self.assertEqual(generated['context']['fixed_references'], [{'asset_id': selected['id'], 'sha256': selected['sha256']}])

    def test_previous_scene_reference_resolves_actual_upstream_and_invalidates_dependents(self):
        class LocalProviders(FakeProviders):
            def image(self, prompt, output, references=None):
                self.images.append(prompt)
                self.image_references.append(references or [])
                color = 'orange' if not references else 'teal'
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             f'color=c={color}:s=64x36', '-frames:v', '1', '-y', str(output)], 30)

            def audio(self, narration, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             'sine=frequency=440:duration=0.3', '-c:a', 'libmp3lame', str(output)], 30)

        self.service.providers = LocalProviders()
        self.service.providers.plan_result = ['第一镜', '引用第一镜']
        info = self.service.models.info()
        self.service.models.save(info['text'].replace('reference_protocol = "none"', 'reference_protocol = "openai-images-edit"'),
                                 info['revision'])
        tid = self.service.submit('前镜引用')['task_id']
        self.executor.run()
        detail = self.service.detail(tid)
        first, second = detail['scenes']
        scenes = [
            {'id': first['id'], 'narration': first['narration'], 'visual_prompt': first['visual_prompt'],
             'image': {'source': 'generated', 'asset_id': None, 'fixed_reference_asset_ids': [], 'previous_scene_id': None}},
            {'id': second['id'], 'narration': second['narration'], 'visual_prompt': second['visual_prompt'],
             'image': {'source': 'generated', 'asset_id': None, 'fixed_reference_asset_ids': [], 'previous_scene_id': first['id']}},
        ]
        self.service.save_plan(tid, scenes, detail['plan_revision'])
        with self.assertRaises(HTTPException) as error:
            self.service.save_plan(tid, list(reversed(scenes)), self.service.detail(tid)['plan_revision'])
        self.assertEqual(error.exception.status_code, 409)
        detail = self.service.detail(tid)
        self.assertEqual(detail['scenes'][1]['previous_scene_id'], first['id'])
        self.service.generate(tid, True, detail['plan_revision'])
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review', detail['error'])
        self.assertEqual(self.service.providers.image_references[0], [])
        self.assertEqual(len(self.service.providers.image_references[1]), 1)
        assets = {asset['origin_scene_id']: asset for asset in detail['image_assets'] if asset['source'] == 'generated'}
        first_asset = assets[first['id']]
        self.assertEqual(assets[second['id']]['context']['previous_scene'],
                         {'scene_id': first['id'], 'asset_id': first_asset['id'], 'sha256': first_asset['sha256']})

        dependent_before = self.service.detail(tid)['scenes'][1]
        self.service.regen(tid, 0, 'img', True, self.service.detail(tid)['scenes'][0]['img_revision'],
                           'previous-reference-regen-key')
        self.executor.run()
        dependent_after = self.service.detail(tid)['scenes'][1]
        self.assertEqual(dependent_after['img_state'], 'pending')
        self.assertEqual(dependent_after['image_path'], dependent_before['image_path'])
        self.assertEqual(dependent_after['aud_state'], dependent_before['aud_state'])
        self.assertIn('前镜图片已更新', dependent_after['img_error'])

    def test_all_asset_scenes_generate_audio_and_render_without_image_requests(self):
        class ValidAudioProviders(FakeProviders):
            def audio(self, narration, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             'sine=frequency=440:duration=0.3', '-c:a', 'libmp3lame', str(output)], 30)

        self.service.providers = ValidAudioProviders()
        tid = self.planned('three uploaded images')
        asset_ids = []
        for color in ('blue', 'green', 'red'):
            image = self.root / f'{color}.png'
            run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                         f'color=c={color}:s=64x36', '-frames:v', '1', str(image)], 30)
            asset, _ = self.service.upload_image(tid, image.read_bytes(), 'image/png')
            asset_ids.append(asset['id'])
        self.assertEqual(len(set(asset_ids)), 3)

        detail = self.service.detail(tid)
        scenes = [{'id': scene['id'], 'narration': scene['narration'], 'visual_prompt': scene['visual_prompt'],
                   'image': {'source': 'asset', 'asset_id': asset_ids[index]}}
                  for index, scene in enumerate(detail['scenes'])]
        self.service.save_plan(tid, scenes, detail['plan_revision'])
        self.service.generate(tid, True, self.service.detail(tid)['plan_revision'])
        self.executor.run()
        detail = self.service.detail(tid)
        self.assertEqual(detail['status'], 'review', detail['error'])
        self.assertEqual((detail['missing_images'], detail['image_requests_needed'], detail['image_requests']), (0, 0, 0))
        self.assertEqual(self.service.providers.images, [])
        self.assertEqual([scene['image_source'] for scene in detail['scenes']], ['asset'] * 3)
        for scene in detail['scenes']:
            self.service.gate(tid, scene['idx'], 'img', True, scene['img_revision'])
            self.service.gate(tid, scene['idx'], 'aud', True, scene['aud_revision'])
        self.service.render(tid)
        self.executor.run()
        self.assertEqual(self.service.detail(tid)['status'], 'done')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class UploadedAssetRenderTests(unittest.TestCase):
    def test_three_uploaded_images_compose_an_mp4_without_image_gateway(self):
        class LocalProviders(FakeProviders):
            def audio(self, narration, output):
                run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                             'sine=frequency=440:duration=0.3', '-c:a', 'libmp3lame', str(output)], 30)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = Config(data=root, token='uploaded-render-test', max_active=1)
            providers = LocalProviders()
            providers.plan_result = ['第一镜', '第二镜', '第三镜']
            executor = ManualExecutor()
            service = Tasks(config, providers, Renderer(config), executor, validate_asset)
            service.open()
            try:
                tid = service.submit('三张上传图片')['task_id']
                executor.run()
                assets = []
                for color in ('blue', 'green', 'red'):
                    image = root / f'{color}.png'
                    run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                                 f'color=c={color}:s=320x180', '-frames:v', '1', str(image)], 30)
                    asset, _ = service.upload_image(tid, image.read_bytes(), 'image/png')
                    assets.append(asset)
                detail = service.detail(tid)
                scenes = [{'id': scene['id'], 'narration': scene['narration'], 'visual_prompt': scene['visual_prompt'],
                           'image': {'source': 'asset', 'asset_id': assets[index]['id']}}
                          for index, scene in enumerate(detail['scenes'])]
                service.save_plan(tid, scenes, detail['plan_revision'])
                service.generate(tid, True, service.detail(tid)['plan_revision'])
                executor.run()
                detail = service.detail(tid)
                for scene in detail['scenes']:
                    service.gate(tid, scene['idx'], 'img', True, scene['img_revision'])
                    service.gate(tid, scene['idx'], 'aud', True, scene['aud_revision'])
                service.render(tid)
                executor.run()
                detail = service.detail(tid)
                self.assertEqual((detail['status'], detail['image_requests'], detail['image_requests_needed']),
                                 ('done', 0, 0))
                video = service.allowed_file(tid, detail['video_path'])
                self.assertIn(b'ftyp', video.read_bytes()[:32])
                self.assertEqual(providers.images, [])
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
