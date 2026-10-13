import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT
from .process import capture
from .errors import MediaError
from .layout import video_layout


def run_process(command, timeout, cwd=None, stopping=None):
    code, stdout, _ = capture(command, timeout, cwd=cwd, stopping=stopping)
    if code:
        raise MediaError('media_command_failed', f'{Path(command[0]).name} 执行失败（退出码 {code}），请检查媒体文件与编码器。')
    return stdout


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def media_info(path):
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError('media_missing', '媒体文件为空或不存在，请重新生成缺失素材。')
    return json.loads(run_process(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 30))


def validate_asset(path, kind, max_audio_seconds=None):
    info = media_info(path)
    stream = next((s for s in info.get('streams', []) if s.get('codec_type') == ('video' if kind == 'img' else 'audio')), None)
    if not stream:
        raise MediaError('media_invalid', '媒体缺少所需的图片或音频流，请重新生成此素材。')
    if kind == 'img':
        if not stream.get('width') or not stream.get('height'):
            raise MediaError('media_invalid', '图片尺寸无效，请重新生成此图。')
        return {'width': stream['width'], 'height': stream['height']}
    else:
        duration = float(info.get('format', {}).get('duration', 'nan'))
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError('media_invalid', '音频时长无效，请重新生成此段配音。')
        if max_audio_seconds and duration > max_audio_seconds:
            raise MediaError('audio_too_long', f'单镜配音 {duration:.1f} 秒，超过 {max_audio_seconds} 秒上限；请编辑分镜，缩短或拆分旁白后重新生成。')
        return duration


def duration_error(total, limit):
    return MediaError('video_too_long', f'整片旁白 {total:.1f} 秒，超过 {limit} 秒上限；请编辑分镜，缩短或删除部分旁白后重新生成。')


def atomic_json(path, value):
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


class Renderer:
    def __init__(self, config):
        self.config = config
        self.stopping = None

    def freeze(self, task_dir, scenes):
        return [{
            'idx': s['idx'], 'narration': s['narration'],
            'img_revision': s['img_revision'], 'aud_revision': s['aud_revision'],
            'image_path': s['image_path'], 'audio_path': s['audio_path'],
            'image_sha256': digest(task_dir / s['image_path']),
            'audio_sha256': digest(task_dir / s['audio_path']),
        } for s in scenes]

    def render(self, task_dir, attempt, scenes, layout=None):
        layout = layout or video_layout()
        target = task_dir / 'renders' / attempt
        target.mkdir(parents=True, exist_ok=False)
        frozen = self.freeze(task_dir, scenes)
        total = sum(float(media_info(task_dir / s['audio_path'])['format']['duration']) for s in scenes)
        if not math.isfinite(total) or total <= 0:
            raise MediaError('media_invalid', '整片旁白时长无效，请检查配音素材。')
        if total > self.config.max_video_seconds:
            raise duration_error(total, self.config.max_video_seconds)
        atomic_json(target / 'frozen.json', frozen)
        atomic_json(target / 'layout.json', layout)
        manifest = {'schema_version': 1, 'scenes': [
            {'id': str(s['idx']), 'image': str((task_dir / s['image_path']).resolve()),
             'audio': str((task_dir / s['audio_path']).resolve())} for s in scenes
        ]}
        atomic_json(target / 'scenes.json', manifest)
        run_process([sys.executable, str(ROOT / 'vendor/compose_video.py'),
                     '--manifest', str(target / 'scenes.json'), '--work-dir', str(target / 'work'),
                     '--output', str(target / 'video.mp4'), '--fps', '2',
                     '--width', str(layout['width']), '--height', str(layout['height']),
                     '--image-fit', layout['image_fit'], '--render'],
                    self.config.render_timeout, stopping=self.stopping)
        self.validate_output(target, layout)
        if frozen != self.freeze(task_dir, scenes):
            raise ValueError('合成期间素材发生变化，未发布成片')
        atomic_json(target / 'complete.json', {
            'video_sha256': digest(target / 'video.mp4'),
            'report_sha256': digest(target / 'video.build.json'),
            'frozen_sha256': digest(target / 'frozen.json'),
            'layout_sha256': digest(target / 'layout.json'),
        })
        return str((target / 'video.mp4').relative_to(task_dir))

    def validate_output(self, target, layout=None):
        report = json.loads((target / 'video.build.json').read_text())
        if report.get('status') != 'rendered' or report.get('technical_validation') != 'passed':
            raise ValueError('构建报告没有通过技术校验')
        info = media_info(target / 'video.mp4')
        kinds = {s.get('codec_type') for s in info.get('streams', [])}
        actual = float(info.get('format', {}).get('duration', 'nan'))
        expected = float(report['output_duration_seconds'])
        if not {'video', 'audio'}.issubset(kinds) or not math.isfinite(actual) or not math.isfinite(expected) or expected <= 0 or abs(actual - expected) > 0.1:
            raise ValueError('成片媒体流或时长与报告不符')
        if layout:
            video = next(s for s in info['streams'] if s.get('codec_type') == 'video')
            if any(video.get(k) != layout[k] or report.get(k) != layout[k] for k in ('width', 'height')) or report.get('image_fit', 'contain') != layout['image_fit']:
                raise ValueError('成片画幅或适配方式与当前设置不符')

    def recover(self, task_dir, scenes, layout=None):
        layout = layout or video_layout()
        renders = task_dir / 'renders'
        if not renders.is_dir():
            return None
        frozen = None
        for marker in sorted(renders.glob('*/complete.json'), reverse=True):
            try:
                target = marker.parent
                checks = json.loads(marker.read_text())
                if 'layout_sha256' in checks:
                    if digest(target / 'layout.json') != checks['layout_sha256'] or json.loads((target / 'layout.json').read_text()) != layout:
                        continue
                elif layout != video_layout():
                    # 旧构建只能证明默认横屏完整保留，不能用于其他画幅。
                    continue
                for name, key in [('video.mp4', 'video_sha256'), ('video.build.json', 'report_sha256'), ('frozen.json', 'frozen_sha256')]:
                    if digest(target / name) != checks[key]:
                        raise ValueError('产物摘要不一致')
                if frozen is None:
                    frozen = self.freeze(task_dir, scenes)
                if json.loads((target / 'frozen.json').read_text()) != frozen:
                    continue
                self.validate_output(target, layout)
                return str((target / 'video.mp4').relative_to(task_dir))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired):
                continue
        return None
