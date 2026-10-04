import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT
from .process import capture


def run_process(command, timeout, cwd=None, stopping=None):
    code, stdout, _ = capture(command, timeout, cwd=cwd, stopping=stopping)
    if code:
        raise RuntimeError(f'{Path(command[0]).name} 执行失败（退出码 {code}）')
    return stdout


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def media_info(path):
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('媒体文件为空或不存在')
    return json.loads(run_process(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 30))


def validate_asset(path, kind, max_audio_seconds=None):
    info = media_info(path)
    stream = next((s for s in info.get('streams', []) if s.get('codec_type') == ('video' if kind == 'img' else 'audio')), None)
    if not stream:
        raise ValueError('媒体缺少所需流')
    if kind == 'img':
        if not stream.get('width') or not stream.get('height'):
            raise ValueError('图片尺寸无效')
    else:
        duration = float(info.get('format', {}).get('duration', 'nan'))
        if not math.isfinite(duration) or duration <= 0 or (max_audio_seconds and duration > max_audio_seconds):
            raise ValueError('音频时长无效或超过限制')


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

    def render(self, task_dir, attempt, scenes):
        target = task_dir / 'renders' / attempt
        target.mkdir(parents=True, exist_ok=False)
        frozen = self.freeze(task_dir, scenes)
        total = sum(float(media_info(task_dir / s['audio_path'])['format']['duration']) for s in scenes)
        if not math.isfinite(total) or total > self.config.max_video_seconds:
            raise ValueError('整片旁白时长超过限制，未进行编码')
        atomic_json(target / 'frozen.json', frozen)
        manifest = {'schema_version': 1, 'scenes': [
            {'id': str(s['idx']), 'image': str((task_dir / s['image_path']).resolve()),
             'audio': str((task_dir / s['audio_path']).resolve())} for s in scenes
        ]}
        atomic_json(target / 'scenes.json', manifest)
        run_process([sys.executable, str(ROOT / 'vendor/compose_video.py'),
                     '--manifest', str(target / 'scenes.json'), '--work-dir', str(target / 'work'),
                     '--output', str(target / 'video.mp4'), '--fps', '2', '--render'],
                    self.config.render_timeout, stopping=self.stopping)
        self.validate_output(target)
        if frozen != self.freeze(task_dir, scenes):
            raise ValueError('合成期间素材发生变化，未发布成片')
        atomic_json(target / 'complete.json', {
            'video_sha256': digest(target / 'video.mp4'),
            'report_sha256': digest(target / 'video.build.json'),
            'frozen_sha256': digest(target / 'frozen.json'),
        })
        return str((target / 'video.mp4').relative_to(task_dir))

    def validate_output(self, target):
        report = json.loads((target / 'video.build.json').read_text())
        if report.get('status') != 'rendered' or report.get('technical_validation') != 'passed':
            raise ValueError('构建报告没有通过技术校验')
        info = media_info(target / 'video.mp4')
        kinds = {s.get('codec_type') for s in info.get('streams', [])}
        actual = float(info.get('format', {}).get('duration', 'nan'))
        expected = float(report['output_duration_seconds'])
        if not {'video', 'audio'}.issubset(kinds) or not math.isfinite(actual) or not math.isfinite(expected) or expected <= 0 or abs(actual - expected) > 0.1:
            raise ValueError('成片媒体流或时长与报告不符')

    def recover(self, task_dir, scenes):
        renders = task_dir / 'renders'
        if not renders.is_dir():
            return None
        frozen = None
        for marker in sorted(renders.glob('*/complete.json'), reverse=True):
            try:
                target = marker.parent
                checks = json.loads(marker.read_text())
                for name, key in [('video.mp4', 'video_sha256'), ('video.build.json', 'report_sha256'), ('frozen.json', 'frozen_sha256')]:
                    if digest(target / name) != checks[key]:
                        raise ValueError('产物摘要不一致')
                if frozen is None:
                    frozen = self.freeze(task_dir, scenes)
                if json.loads((target / 'frozen.json').read_text()) != frozen:
                    continue
                self.validate_output(target)
                return str((target / 'video.mp4').relative_to(task_dir))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired):
                continue
        return None
