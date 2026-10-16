"""W1：分镜影响与参考关系纯函数的表驱动测试。

每个用例分别断言素材动作、版本变化、确认保留／失效和生图请求范围，
不依赖数据库与文件；事实由测试构造后传入。
"""
import json
import unittest

from app.domain import plan_impact
from app.domain.references import image_source, image_spec, previous_scene_id


def generated_spec(refs=(), prev=None):
    return json.dumps({'source': 'generated', 'asset_id': None, 'fixed_reference_asset_ids': list(refs),
                       'previous_scene_id': prev}, ensure_ascii=False)


def old_scene(scene_id, idx=0, *, narration='旁白', visual_prompt='画面', img_state='ready', aud_state='ready',
              img_revision=1, aud_revision=1, img_approved=1, aud_approved=1, image_path='a/001-img.png',
              audio_path='a/001-aud.mp3', image_spec_json=None, image_asset_id=10, img_placeholder=0,
              audio_duration_seconds=1.2, image_width=320, image_height=180, img_error=None, aud_error=None):
    spec = generated_spec() if image_spec_json is None else image_spec_json
    return {'id': scene_id, 'task_id': 1, 'idx': idx, 'narration': narration, 'visual_prompt': visual_prompt,
            'img_state': img_state, 'aud_state': aud_state, 'img_revision': img_revision,
            'aud_revision': aud_revision, 'img_approved': img_approved, 'aud_approved': aud_approved,
            'img_ok': int(img_approved == img_revision and img_state == 'ready'),
            'aud_ok': int(aud_approved == aud_revision and aud_state == 'ready'),
            'image_path': image_path, 'audio_path': audio_path, 'img_error': img_error, 'aud_error': aud_error,
            'image_spec_json': spec, 'image_asset_id': image_asset_id, 'img_placeholder': img_placeholder,
            'audio_duration_seconds': audio_duration_seconds, 'image_width': image_width,
            'image_height': image_height}


def scene(*, scene_id=None, narration='旁白', visual_prompt='画面', image=None):
    item = {'narration': narration, 'visual_prompt': visual_prompt}
    if scene_id is not None:
        item['id'] = scene_id
    if image is not None:
        item['image'] = image
    return item


ASSETS = {10: {'id': 10, 'path': 'a/upload1.png', 'width': 320, 'height': 180},
          11: {'id': 11, 'path': 'a/upload2.png', 'width': 640, 'height': 360}}


def build(old, plan, floor=2, layout_changed=False, assets=ASSETS):
    return plan_impact.build_scene_rows(
        old, plan, {'task_id': 1, 'floor': floor, 'layout_changed': layout_changed, 'assets': assets})


class PlanImpactTests(unittest.TestCase):
    def assertImpactError(self, callable, status, message_part):
        with self.assertRaises(plan_impact.ImpactError) as error:
            callable()
        self.assertEqual(error.exception.status, status)
        self.assertIn(message_part, error.exception.message)

    def test_unchanged_plan_keeps_material_and_approval(self):
        old = [old_scene(1)]
        rows, changed = build(old, [scene()])
        self.assertEqual(changed, set())
        row = rows[0]
        self.assertEqual(row['id'], 1)
        self.assertEqual((row['img_revision'], row['img_approved'], row['img_ok']), (1, 1, 1))
        self.assertEqual((row['aud_revision'], row['aud_approved'], row['aud_ok']), (1, 1, 1))
        self.assertEqual(row['img_state'], 'ready')
        self.assertEqual(row['image_path'], 'a/001-img.png')

    def test_narration_change_redoes_audio_only(self):
        old = [old_scene(1)]
        rows, changed = build(old, [scene(scene_id=1, narration='新旁白')])
        self.assertEqual(changed, set())
        row = rows[0]
        self.assertEqual((row['aud_state'], row['aud_revision'], row['aud_approved'], row['aud_ok']),
                         ('pending', 2, 0, 0))
        self.assertIsNone(row['audio_path'])
        self.assertIsNone(row['audio_duration_seconds'])
        self.assertEqual((row['img_state'], row['img_revision'], row['img_approved'], row['img_ok']),
                         ('ready', 1, 1, 1))
        self.assertEqual(row['image_path'], 'a/001-img.png')

    def test_uploaded_asset_scene_keeps_image_and_approval_when_narration_changes(self):
        spec = json.dumps({'source': 'asset', 'asset_id': 10}, ensure_ascii=False)
        old = [old_scene(1, image_spec_json=spec, image_path='a/upload1.png')]
        rows, changed = build(old, [scene(scene_id=1, narration='新旁白')])
        self.assertEqual(changed, set())
        row = rows[0]
        self.assertEqual((row['img_state'], row['img_revision'], row['img_approved'], row['img_ok']),
                         ('ready', 1, 1, 1))
        self.assertEqual(row['image_path'], 'a/upload1.png')
        self.assertEqual(row['image_asset_id'], 10)
        self.assertEqual((row['aud_state'], row['aud_revision'], row['aud_approved'], row['aud_ok']),
                         ('pending', 2, 0, 0))

    def test_visual_prompt_change_redoes_image_only(self):
        old = [old_scene(1)]
        rows, changed = build(old, [scene(scene_id=1, visual_prompt='新画面')])
        self.assertEqual(changed, {1})
        row = rows[0]
        self.assertEqual((row['img_state'], row['img_revision'], row['img_approved'], row['img_ok']),
                         ('pending', 2, 0, 0))
        self.assertIsNone(row['image_path'])
        self.assertEqual((row['img_placeholder'], row['image_width'], row['image_height']), (0, None, None))
        self.assertEqual((row['aud_state'], row['aud_revision'], row['aud_approved']), ('ready', 1, 1))

    def test_layout_change_keeps_files_but_requires_reapproval(self):
        old = [old_scene(1)]
        rows, changed = build(old, [scene()], layout_changed=True)
        self.assertEqual(changed, set())
        row = rows[0]
        self.assertEqual((row['img_revision'], row['img_approved'], row['img_ok']), (2, 0, 0))
        self.assertEqual(row['img_state'], 'ready')
        self.assertEqual(row['image_path'], 'a/001-img.png')
        self.assertEqual((row['aud_revision'], row['aud_approved'], row['aud_ok']), (1, 1, 1))

    def test_reorder_keeps_material_and_moves_approval(self):
        old = [old_scene(1, idx=0), old_scene(2, idx=1)]
        plan = [scene(scene_id=2), scene(scene_id=1)]
        rows, changed = build(old, plan)
        self.assertEqual(changed, set())
        self.assertEqual([row['id'] for row in rows], [2, 1])
        for row in rows:
            self.assertEqual((row['img_revision'], row['img_approved'], row['img_ok']), (2, 2, 1))
            self.assertEqual((row['aud_revision'], row['aud_approved'], row['aud_ok']), (2, 2, 1))
            self.assertEqual(row['img_state'], 'ready')

    def test_switch_asset_resets_approval(self):
        spec = json.dumps({'source': 'asset', 'asset_id': 10}, ensure_ascii=False)
        old = [old_scene(1, image_spec_json=spec)]
        plan = [scene(scene_id=1, image={'source': 'asset', 'asset_id': 11})]
        rows, changed = build(old, plan)
        self.assertEqual(changed, {1})
        row = rows[0]
        self.assertEqual((row['img_state'], row['img_revision'], row['img_approved'], row['img_ok']),
                         ('ready', 2, 0, 0))
        self.assertEqual(row['image_path'], 'a/upload2.png')
        self.assertEqual(row['image_asset_id'], 11)
        self.assertEqual((row['image_width'], row['image_height']), (640, 360))

    def test_same_asset_keeps_approval(self):
        spec = json.dumps({'source': 'asset', 'asset_id': 10}, ensure_ascii=False)
        old = [old_scene(1, image_spec_json=spec, image_path='a/upload1.png', image_width=320, image_height=180)]
        plan = [scene(scene_id=1, image={'source': 'asset', 'asset_id': 10})]
        rows, changed = build(old, plan)
        self.assertEqual(changed, set())
        row = rows[0]
        self.assertEqual((row['img_revision'], row['img_approved'], row['img_ok']), (1, 1, 1))
        self.assertEqual(row['image_path'], 'a/upload1.png')

    def test_reference_change_redoes_image(self):
        old = [old_scene(1, image_spec_json=generated_spec(refs=[10]))]
        plan = [scene(scene_id=1, image={'source': 'generated', 'asset_id': None,
                                         'fixed_reference_asset_ids': [11], 'previous_scene_id': None})]
        rows, changed = build(old, plan)
        self.assertEqual(changed, {1})
        row = rows[0]
        self.assertEqual(row['img_state'], 'pending')
        self.assertEqual(row['img_revision'], 2)
        self.assertIsNone(row['image_path'])
        self.assertEqual(image_spec(row)['fixed_reference_asset_ids'], [11])
        # 确认要素：参考变化使图片确认与缓存标记一并失效。
        self.assertEqual((row['img_approved'], row['img_ok']), (0, 0))
        self.assertEqual(row['img_placeholder'], 0)

    def test_new_scene_starts_fresh_without_id(self):
        old = [old_scene(1)]
        rows, changed = build(old, [scene(scene_id=1), scene(narration='新镜')])
        self.assertEqual(changed, set())
        added = rows[1]
        self.assertNotIn('id', added)
        self.assertEqual(added['idx'], 1)
        self.assertEqual((added['img_revision'], added['aud_revision']), (2, 2))
        self.assertEqual((added['img_approved'], added['aud_approved'], added['img_ok'], added['aud_ok']),
                         (0, 0, 0, 0))
        self.assertEqual(added['img_state'], 'pending')

    def test_upstream_redo_invalidates_direct_dependent_only(self):
        old = [old_scene(1, idx=0), old_scene(2, idx=1, image_spec_json=generated_spec(prev=1)),
               old_scene(3, idx=2, image_spec_json=generated_spec(prev=2))]
        plan = [scene(scene_id=1, visual_prompt='新画面'), scene(scene_id=2), scene(scene_id=3)]
        rows, changed = build(old, plan)
        self.assertEqual(changed, {1})
        result = plan_impact.finalize_impact(rows, changed)
        self.assertEqual([item['id'] for item in result['invalidations']], [2])
        self.assertIn('上游图片会重做', result['invalidations'][0]['error'])

    def test_upstream_redo_skips_pending_and_asset_dependents(self):
        asset_spec = json.dumps({'source': 'asset', 'asset_id': 10}, ensure_ascii=False)
        old = [old_scene(1, idx=0),
               old_scene(2, idx=1, image_spec_json=asset_spec, image_path='a/upload1.png'),
               old_scene(3, idx=2, image_spec_json=generated_spec(prev=1), img_state='pending',
                         image_path=None, image_asset_id=None)]
        plan = [scene(scene_id=1, visual_prompt='新画面'), scene(scene_id=2), scene(scene_id=3)]
        rows, changed = build(old, plan)
        result = plan_impact.finalize_impact(rows, changed)
        self.assertEqual(result['invalidations'], [])

    def test_explicit_id_must_exist_in_old_plan(self):
        old = [old_scene(1)]
        self.assertImpactError(lambda: build(old, [scene(scene_id=99)]), 409, '分镜标识已失效')

    def test_missing_reference_asset_rejected(self):
        old = [old_scene(1)]
        plan = [scene(scene_id=1, image={'source': 'generated', 'asset_id': None,
                                         'fixed_reference_asset_ids': [99], 'previous_scene_id': None})]
        self.assertImpactError(lambda: build(old, plan), 422, '固定参考图不存在')

    def test_missing_asset_rejected(self):
        old = [old_scene(1)]
        plan = [scene(scene_id=1, image={'source': 'asset', 'asset_id': 99})]
        self.assertImpactError(lambda: build(old, plan), 422, '所选图片不存在')

    def test_forward_reference_rejected(self):
        old = [old_scene(1, idx=0), old_scene(2, idx=1)]
        plan = [scene(scene_id=1, image={'source': 'generated', 'asset_id': None,
                                         'fixed_reference_asset_ids': [], 'previous_scene_id': 2}),
                scene(scene_id=2)]
        rows, changed = build(old, plan)
        self.assertImpactError(lambda: plan_impact.finalize_impact(rows, changed), 409, '排序在前')

    def test_match_prefers_same_position_and_reserves_ids(self):
        old = [old_scene(1, idx=0, narration='n1', visual_prompt='v1'),
               old_scene(2, idx=1, narration='n2', visual_prompt='v2'),
               old_scene(3, idx=2, narration='n3', visual_prompt='v3')]
        plan = [scene(scene_id=3, narration='n3', visual_prompt='v3'),
                scene(narration='n2', visual_prompt='v2'),
                scene(narration='n1', visual_prompt='v1')]
        rows, changed = build(old, plan)
        self.assertEqual([row['id'] for row in rows], [3, 2, 1])
        self.assertEqual(changed, set())


class RequestScopeTests(unittest.TestCase):
    def test_request_scope_propagates_through_chain(self):
        scenes = [old_scene(1, idx=0), old_scene(2, idx=1, image_spec_json=generated_spec(prev=1)),
                  old_scene(3, idx=2, image_spec_json=generated_spec(prev=2)),
                  old_scene(4, idx=3, image_spec_json=json.dumps({'source': 'asset', 'asset_id': 10},
                                                                 ensure_ascii=False))]
        available = {1, 3, 4}
        self.assertEqual(plan_impact.image_request_scene_ids(scenes, available), {2, 3})

    def test_placeholder_counts_as_available_for_requests(self):
        scenes = [old_scene(1, img_state='placeholder', img_placeholder=1)]
        self.assertEqual(plan_impact.image_request_scene_ids(scenes, {1}), set())

    def test_dependents_returns_direct_ready_dependencies_only(self):
        scenes = [old_scene(1, idx=0), old_scene(2, idx=1, image_spec_json=generated_spec(prev=1)),
                  old_scene(3, idx=2, image_spec_json=generated_spec(prev=2))]
        dependents = plan_impact.dependents_to_invalidate(scenes, 1)
        self.assertEqual([row['id'] for row in dependents], [2])

    def test_next_revision_takes_maximum(self):
        scenes = [old_scene(1, img_revision=3, aud_revision=5), old_scene(2, img_revision=7, aud_revision=2)]
        self.assertEqual(plan_impact.next_revision(4, scenes), 8)
        self.assertEqual(plan_impact.next_revision(9, scenes), 10)


class ReferenceRuleTests(unittest.TestCase):
    def test_source_and_previous_readers(self):
        scene_row = old_scene(1, image_spec_json=generated_spec(refs=[10], prev=None))
        self.assertEqual(image_source(scene_row), 'generated')
        self.assertIsNone(previous_scene_id(scene_row))
        broken = old_scene(2, image_spec_json='{not json')
        self.assertEqual(image_source(broken), 'generated')

    def test_reference_order_check(self):
        from app.domain.references import reference_order_ok
        by_id = {1: old_scene(1, idx=0), 2: old_scene(2, idx=1)}
        self.assertTrue(reference_order_ok(old_scene(2, idx=1, image_spec_json=generated_spec(prev=1)), by_id))
        self.assertFalse(reference_order_ok(old_scene(1, idx=0, image_spec_json=generated_spec(prev=2)), by_id))


if __name__ == '__main__':
    unittest.main()
