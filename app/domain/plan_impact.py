"""分镜保存与素材发布的影响判定：素材动作、版本与确认失效、生图请求范围。

纯规则模块：不读取数据库或文件、不引入 FastAPI、不调用外部服务。
调用层读取事实（资产存在性、文件可用性、布局、版本下限）后传入；
返回值由调用层转换为写事务与 HTTP 错误。行为与 v0.3 的实现逐条对齐。
"""
import json

from .references import image_source, image_spec, previous_scene_id


class ImpactError(Exception):
    """纯规则判定的请求违规；调用层转换为对应状态码的 HTTP 错误。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def next_revision(floor, scenes):
    """全任务下一个素材版本号；floor 防止镜头删除后版本号复用。"""
    return max([floor, *[scene[f'{kind}_revision'] for scene in scenes for kind in ('img', 'aud')]]) + 1


def image_request_scene_ids(scenes, available_ids):
    """完整生成需要请求生图的镜头集合：缺失可用图或前镜在请求范围内。

    按镜头顺序单遍扫描完成传播闭包；调用方以文件可用性事实构造
    available_ids（ready/placeholder 且文件存在的镜头 id 集合）。
    """
    pending = set()
    for scene in scenes:
        if image_source(scene) == 'generated' and (
                scene['id'] not in available_ids or previous_scene_id(scene) in pending):
            pending.add(scene['id'])
    return pending


def dependents_to_invalidate(scenes, upstream_id):
    """上游图片发布后需要立即失效的直接依赖；不扩大到更下游。"""
    return [dict(scene) for scene in scenes if previous_scene_id(scene) == upstream_id
            and scene['img_state'] == 'ready' and scene['image_path']]


def match_plan_to_scenes(old, plan):
    """把新分镜匹配到旧镜头以保留素材。

    显式标识必须存在于旧镜头；无标识的兼容请求按相同旁白与画面描述匹配，
    先同位置后首个未占用。显式标识先预留，避免兼容请求抢占另一镜的素材。
    """
    old_by_id = {scene['id']: scene for scene in old}
    if any('id' in scene and scene['id'] not in old_by_id for scene in plan):
        raise ImpactError(409, '分镜标识已失效，请刷新后重新编辑')
    reserved = {scene['id'] for scene in plan if 'id' in scene}
    used = set()
    matched = []
    for idx, scene in enumerate(plan):
        matches = [s for s in old if s['id'] not in used | reserved and
                   all(s[k] == scene[k] for k in ('narration', 'visual_prompt'))]
        previous = old_by_id[scene['id']] if 'id' in scene else next(
            (s for s in matches if s['idx'] == idx), matches[0] if matches else None)
        if previous:
            used.add(previous['id'])
        matched.append(previous)
    return matched


def _apply_text_change(row, previous, floor, layout_changed):
    """旁白、画面描述、位置与布局变化对素材版本与确认的影响。"""
    for kind, field, path in (('img', 'visual_prompt', 'image_path'), ('aud', 'narration', 'audio_path')):
        changed = previous[field] != row[field]
        if changed or previous['idx'] != row['idx'] or (kind == 'img' and layout_changed):
            row[f'{kind}_revision'] = floor
            row[f'{kind}_approved'] = floor if not changed and not (kind == 'img' and layout_changed) and previous[f'{kind}_state'] == 'ready' and previous[f'{kind}_revision'] > 0 and previous[f'{kind}_approved'] == previous[f'{kind}_revision'] else 0
            row[f'{kind}_ok'] = int(row[f'{kind}_approved'] == floor)
        if changed:
            row[f'{kind}_state'] = 'pending'
            row[f'{kind}_error'] = None
            row[path] = None
            if kind == 'img':
                row.update(img_placeholder=0, image_width=None, image_height=None)
            else:
                row['audio_duration_seconds'] = None


def _asset_ready(row, asset, floor):
    """镜头改用已有图片：状态就绪，版本不低于下限。"""
    row.update(image_spec_json=json.dumps({'source': 'asset', 'asset_id': asset['id']}, ensure_ascii=False),
               image_asset_id=asset['id'], image_path=asset['path'], img_state='ready', img_placeholder=0,
               image_width=asset['width'], image_height=asset['height'], img_error=None)
    row['img_revision'] = max(row.get('img_revision') or 0, floor)
    row.setdefault('img_approved', 0)
    row['img_ok'] = int(row['img_approved'] == row['img_revision'])


def build_scene_rows(old, plan, facts):
    """计算保存分镜后每镜的完整行。

    facts: task_id、floor（版本下限）、layout_changed、assets（属于此任务的
    资产行映射，仅含存在性事实）。返回 (行列表, 将重做上游的镜头 id 集合)；
    新镜不含 id，由调用层在 INSERT 后回填，再调用 finalize_impact。
    违规抛 ImpactError。
    """
    matched = match_plan_to_scenes(old, plan)
    assets = facts['assets']
    floor = facts['floor']
    layout_changed = facts['layout_changed']
    rows = []
    changed_upstream_ids = set()
    for idx, scene in enumerate(plan):
        previous = matched[idx]
        if previous:
            row = dict(previous)
            row['idx'] = idx
            row.update({k: scene[k] for k in ('narration', 'visual_prompt')})
            _apply_text_change(row, previous, floor, layout_changed)
        else:
            # 新镜行补齐 scenes 表的默认列：保存路径显式插入这些值，
            # 预演路径没有数据库回填，两侧行结构保持一致。
            row = {'task_id': facts['task_id'], 'idx': idx, 'narration': scene['narration'],
                   'visual_prompt': scene['visual_prompt'], 'img_revision': floor, 'aud_revision': floor,
                   'img_approved': 0, 'aud_approved': 0, 'img_ok': 0, 'aud_ok': 0,
                   'img_state': 'pending', 'aud_state': 'pending',
                   'img_error': None, 'aud_error': None, 'image_path': None, 'audio_path': None,
                   'img_placeholder': 0, 'audio_duration_seconds': None,
                   'image_width': None, 'image_height': None, 'image_asset_id': None}
        explicit_image = scene.get('image')
        previous_spec = image_spec(previous) if previous else {'source': 'generated', 'asset_id': None,
                                                              'fixed_reference_asset_ids': [],
                                                              'previous_scene_id': None}
        if explicit_image:
            if explicit_image['source'] == 'generated':
                missing_refs = [asset_id for asset_id in explicit_image['fixed_reference_asset_ids']
                                if asset_id not in assets]
                if missing_refs:
                    raise ImpactError(422, '固定参考图不存在或不属于此任务')
                next_spec = {'source': 'generated', 'asset_id': None,
                             'fixed_reference_asset_ids': explicit_image['fixed_reference_asset_ids'],
                             'previous_scene_id': explicit_image['previous_scene_id']}
            else:
                next_spec = {'source': 'asset', 'asset_id': explicit_image['asset_id'],
                             'fixed_reference_asset_ids': [], 'previous_scene_id': None}
        else:
            next_spec = previous_spec
        if next_spec['source'] == 'asset':
            asset = assets.get(next_spec['asset_id'])
            if asset is None:
                raise ImpactError(422, '所选图片不存在或不属于此任务')
            changed = not previous or previous_spec != next_spec or previous['image_path'] != asset['path']
            if changed:
                row.update(img_revision=floor, img_approved=0, img_ok=0)
            _asset_ready(row, asset, row['img_revision'])
        else:
            row['image_spec_json'] = json.dumps(next_spec, ensure_ascii=False)
            reference_changed = previous_spec != next_spec
            if not previous or reference_changed:
                row.update(img_state='pending', img_placeholder=0, image_path=None, img_revision=floor,
                           img_approved=0, img_ok=0, image_width=None, image_height=None, img_error=None)
            if row.get('img_state') == 'pending' and not row.get('image_path'):
                row['image_asset_id'] = None
        if previous and (row.get('image_path') != previous['image_path'] or
                         row.get('img_state') == 'pending' and not row.get('image_path')):
            changed_upstream_ids.add(previous['id'])
        row.pop('image', None)
        rows.append(row)
    return rows, changed_upstream_ids


def finalize_impact(rows, changed_upstream_ids):
    """行回填 id 后计算：前镜顺序校验与上游重做导致的直接下游失效。"""
    positions = {row['id']: row['idx'] for row in rows}
    invalidations = []
    for row in rows:
        upstream_id = previous_scene_id(row)
        if upstream_id is not None and (upstream_id not in positions or positions[upstream_id] >= row['idx']):
            raise ImpactError(409, '前镜引用必须是排序在前的稳定镜头；请重新选择引用')
        if image_source(row) != 'generated' or upstream_id not in changed_upstream_ids:
            continue
        if row['img_state'] == 'ready' and row['image_path']:
            invalidations.append({'id': row['id'], 'error': '上游图片会重做；此镜需重新生成或改选固定参考。'})
    return {'invalidations': invalidations}
