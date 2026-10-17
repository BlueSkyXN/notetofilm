import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .gateway import validate_gateway

ROOT = Path(__file__).resolve().parent.parent


def value(env, name, default=''):
    return env.get(name, '').strip() or default


def positive(env, name, default):
    number = int(value(env, name, str(default)))
    if number < 1:
        raise ValueError(f'{name} 必须是正整数')
    return number


@dataclass(frozen=True)
class Config:
    data: Path
    config_path: Path | None = None
    backup_dir: Path | None = None
    config_bucket: str = ''
    data_bucket: str = ''
    hf_token: str = field(default='', repr=False)
    credentials: dict | None = field(default=None, repr=False)
    token: str = ''
    local_only: bool = False
    cookie_secure: bool = False
    base: str = ''
    key: str = ''
    llm_model: str = 'origin-deepseek-v4-1-flash'
    llm_wire_api: str = 'chat'
    image_model: str = 'gpt-image-2.5-sunburst'
    image_reference_protocol: str = 'none'
    image_size: str = '1024x768'
    image_timeout: int = 300
    voice: str = 'zh-CN-YunxiNeural'
    style: str = '扁平插画风格，蓝白主色调，简洁构图，画面中不出现文字'
    webhook: str = ''
    max_active: int = 3
    max_render: int = 1
    max_script: int = 20000
    max_scenes: int = 30
    max_narration: int = 2000
    render_timeout: int = 1800
    max_image_requests: int = 60
    min_free_mb: int = 256
    max_audio_seconds: int = 180
    max_video_seconds: int = 900
    allowed_hosts: tuple = ('127.0.0.1', 'localhost', '::1')
    llm_base: str = ''
    llm_key: str = ''
    image_base: str = ''
    image_key: str = ''
    space_host: str = ''
    image_quality: str = 'low'
    max_daily_tasks: int = 20
    max_visitor_tasks: int = 5
    max_daily_llm: int = 40
    max_daily_images: int = 60
    max_daily_audio: int = 120
    max_daily_renders: int = 30
    request_body_timeout: int = 30
    max_upload_image_bytes: int = 10 * 1024 * 1024
    max_image_pixels: int = 24_000_000
    image_concurrency: int = 1
    audio_concurrency: int = 2
    task_inflight: int = 3

    @property
    def llm_connection(self):
        return (self.llm_base, self.llm_key) if self.llm_base else (self.base, self.key)

    @property
    def image_connection(self):
        return (self.image_base, self.image_key) if self.image_base else (self.base, self.key)

    @property
    def durable(self):
        return bool(self.data_bucket or self.backup_dir)

    @classmethod
    def load(cls, env=None):
        from .full_config import load_config
        return load_config(os.environ if env is None else env)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        space_host = value(env, 'SPACE_HOST').lower()
        hosts = [h.strip() for h in value(env, 'ALLOWED_HOSTS', '127.0.0.1,localhost,::1').split(',') if h.strip()]
        if space_host:
            hosts.append(space_host)
        return cls(
            data=Path(value(env, 'VIDEO_DATA_DIR', str(ROOT / 'data'))).expanduser().resolve(),
            token=value(env, 'ADMIN_TOKEN', value(env, 'ACCESS_TOKEN')),
            local_only=value(env, 'ALLOW_UNAUTHENTICATED_LOCAL') == '1',
            cookie_secure=bool(space_host) or value(env, 'COOKIE_SECURE') == '1',
            base=value(env, 'OPENAI_BASE').rstrip('/'), key=value(env, 'OPENAI_API_KEY'),
            llm_model=value(env, 'LLM_MODEL', 'origin-deepseek-v4-1-flash'),
            llm_wire_api=value(env, 'LLM_WIRE_API', 'chat'),
            image_model=value(env, 'IMAGE_MODEL', 'gpt-image-2.5-sunburst'),
            image_reference_protocol=value(env, 'IMAGE_REFERENCE_PROTOCOL', 'none'),
            image_size=value(env, 'IMAGE_SIZE', '1024x768'),
            image_timeout=positive(env, 'IMAGE_TIMEOUT', 300),
            voice=value(env, 'TTS_VOICE', 'zh-CN-YunxiNeural'),
            style=value(env, 'STYLE_PRESET', cls.style), webhook=value(env, 'FEISHU_WEBHOOK'),
            max_active=positive(env, 'MAX_ACTIVE_TASKS', 3),
            max_render=positive(env, 'MAX_RENDER_TASKS', 1),
            max_script=positive(env, 'MAX_SCRIPT_CHARS', 20000),
            max_scenes=positive(env, 'MAX_SCENES', 30),
            max_narration=positive(env, 'MAX_NARRATION_CHARS', 2000),
            render_timeout=positive(env, 'RENDER_TIMEOUT', 1800),
            max_image_requests=positive(env, 'MAX_IMAGE_REQUESTS_PER_TASK', 60),
            min_free_mb=positive(env, 'MIN_FREE_DISK_MB', 256),
            max_audio_seconds=positive(env, 'MAX_AUDIO_SECONDS', 180),
            max_video_seconds=positive(env, 'MAX_VIDEO_SECONDS', 900),
            allowed_hosts=tuple(dict.fromkeys(hosts)), space_host=space_host,
            llm_base=value(env, 'LLM_BASE').rstrip('/'), llm_key=value(env, 'LLM_API_KEY'),
            image_base=value(env, 'IMAGE_BASE').rstrip('/'), image_key=value(env, 'IMAGE_API_KEY'),
            max_daily_tasks=positive(env, 'MAX_DAILY_TASKS', 20),
            max_visitor_tasks=positive(env, 'MAX_VISITOR_DAILY_TASKS', 5),
            max_daily_llm=positive(env, 'MAX_DAILY_LLM_REQUESTS', 40),
            max_daily_images=positive(env, 'MAX_DAILY_IMAGE_REQUESTS', 60),
            max_daily_audio=positive(env, 'MAX_DAILY_AUDIO_REQUESTS', 120),
            max_daily_renders=positive(env, 'MAX_DAILY_RENDER_REQUESTS', 30),
            request_body_timeout=positive(env, 'REQUEST_BODY_TIMEOUT', 30),
            max_upload_image_bytes=positive(env, 'MAX_UPLOAD_IMAGE_BYTES', 10 * 1024 * 1024),
            max_image_pixels=positive(env, 'MAX_IMAGE_PIXELS', 24_000_000),
        )

    def validate(self):
        if self.llm_wire_api not in ('chat', 'responses'):
            raise ValueError('LLM_WIRE_API 必须为 chat 或 responses')
        if self.image_reference_protocol not in ('none', 'openai-images-edit'):
            raise ValueError('IMAGE_REFERENCE_PROTOCOL 必须为 none 或 openai-images-edit')
        if self.space_host:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.hf\.space', self.space_host):
                raise ValueError('SPACE_HOST 必须是明确的 HF Space 主机名')
            if not self.token or not self.cookie_secure:
                raise ValueError('HF Space 必须配置 ACCESS_TOKEN 并使用安全 Cookie')
        if not self.token and not self.local_only:
            raise ValueError('必须配置 ACCESS_TOKEN；仅本机演示可显式设置 ALLOW_UNAUTHENTICATED_LOCAL=1')
        for label, base, key in [('OPENAI', self.base, self.key), ('LLM', self.llm_base, self.llm_key), ('IMAGE', self.image_base, self.image_key)]:
            if base and not key and not (self.config_path or self.data / 'config.toml').is_file():
                raise ValueError(f'{label} 地址已配置但缺少 API key')
            validate_gateway(base)
        if not self.allowed_hosts or any('/' in h or '*' in h for h in self.allowed_hosts):
            raise ValueError('ALLOWED_HOSTS 必须是明确的主机名列表，不允许通配符')
        if self.max_active < 1 or not 1 <= self.max_render <= self.max_active:
            raise ValueError('并发配置必须满足 1 <= MAX_RENDER_TASKS <= MAX_ACTIVE_TASKS')
        if self.request_body_timeout <= 0 or self.max_script < 1:
            raise ValueError('请求体读取超时和文稿限额必须为正数')
        if min(self.max_image_requests, self.min_free_mb, self.max_audio_seconds, self.max_video_seconds,
               self.max_upload_image_bytes, self.max_image_pixels,
               self.image_concurrency, self.audio_concurrency, self.task_inflight) < 1:
            raise ValueError('费用、磁盘、媒体、上传和并发限制必须是正数')
        if self.webhook and urlsplit(self.webhook).scheme != 'https':
            raise ValueError('FEISHU_WEBHOOK 必须使用 HTTPS')
