"""参考关系的纯解析：分镜图片配置的读取、顺序与合法性校验。

本模块不做 I/O：不读取数据库或文件、不引入 FastAPI、不调用外部服务。
调用层读取事实后传入，返回值只依据输入。
"""
import json

DEFAULT_SPEC = {'source': 'generated', 'asset_id': None,
                'fixed_reference_asset_ids': [], 'previous_scene_id': None}


def image_spec(scene):
    """解析镜头的图片配置；缺失或损坏时回落为默认生成配置。

    每次返回独立的列表对象，调用方修改返回值不会污染默认配置。
    """
    if not scene.get('image_spec_json'):
        return _fresh_default()
    try:
        value = json.loads(scene['image_spec_json'])
    except (TypeError, ValueError):
        return _fresh_default()
    if not isinstance(value, dict) or value.get('source') not in ('generated', 'asset'):
        return _fresh_default()
    references = value.get('fixed_reference_asset_ids')
    if not isinstance(references, list):
        references = []
    return {'source': value['source'], 'asset_id': value.get('asset_id'), 'fixed_reference_asset_ids': references,
            'previous_scene_id': value.get('previous_scene_id')}


def _fresh_default():
    return {'source': DEFAULT_SPEC['source'], 'asset_id': None,
            'fixed_reference_asset_ids': list(DEFAULT_SPEC['fixed_reference_asset_ids']),
            'previous_scene_id': None}


def image_source(scene):
    return image_spec(scene)['source']


def fixed_reference_ids(scene):
    return image_spec(scene)['fixed_reference_asset_ids']


def previous_scene_id(scene):
    return image_spec(scene)['previous_scene_id']


def reference_order_ok(scene, scene_by_id):
    """单镜的前镜引用是否指向排序在前的稳定镜头。"""
    previous_id = previous_scene_id(scene)
    if previous_id is None:
        return True
    upstream = scene_by_id.get(previous_id)
    return upstream is not None and upstream['idx'] < scene['idx']
