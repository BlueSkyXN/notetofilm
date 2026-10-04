import hashlib
import json
import os
import re
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

from fastapi import HTTPException

SCHEMA = {
    'llm': {'base_url': str, 'model': str, 'api_key_env': str},
    'image': {'base_url': str, 'model': str, 'api_key_env': str, 'size': str,
              'quality': str, 'timeout_seconds': int, 'style': str},
    'tts': {'provider': str, 'voice': str},
}
SECRET_NAMES = {'OPENAI_API_KEY', 'LLM_API_KEY', 'IMAGE_API_KEY'}


def encode(document):
    blocks = []
    for section, fields in SCHEMA.items():
        lines = [f'[{section}]']
        for name in fields:
            value = document[section][name]
            lines.append(f'{name} = {json.dumps(value, ensure_ascii=False)}')
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks) + '\n'


def revision(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ModelConfig:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data / 'config.toml'
        self.previous = settings.data / 'config.previous.toml'
        self.lock = RLock()
        self.secret_values = {'OPENAI_API_KEY': settings.key, 'LLM_API_KEY': settings.llm_key,
                              'IMAGE_API_KEY': settings.image_key}
        self.active_text = ''
        self.active = None

    def initial(self):
        c = self.settings
        return {
            'llm': {'base_url': c.llm_connection[0], 'model': c.llm_model,
                    'api_key_env': 'LLM_API_KEY' if c.llm_base else 'OPENAI_API_KEY'},
            'image': {'base_url': c.image_connection[0], 'model': c.image_model,
                      'api_key_env': 'IMAGE_API_KEY' if c.image_base else 'OPENAI_API_KEY',
                      'size': c.image_size, 'quality': c.image_quality, 'timeout_seconds': c.image_timeout,
                      'style': c.style},
            'tts': {'provider': 'edge-tts', 'voice': c.voice},
        }

    def parse(self, text):
        if len(text.encode()) > 32768:
            raise ValueError('配置不得超过 32 KiB')
        if any(value and value in text for value in [self.settings.token, *self.secret_values.values()]):
            raise ValueError('配置不能包含真实密钥或管理员令牌，请使用 api_key_env 引用')
        if re.search(r'(?:sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,}|Bearer\s+\S+)', text):
            raise ValueError('配置包含疑似密钥，未保存')
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            raise ValueError('TOML 语法无效，请检查引号、字段名及分节') from None
        if set(document) != set(SCHEMA):
            raise ValueError('配置只允许 llm、image、tts 三个分节，且必须完整')
        for section, fields in SCHEMA.items():
            values = document[section]
            if not isinstance(values, dict) or set(values) != set(fields):
                raise ValueError(f'{section} 字段不完整或包含不支持的字段')
            for name, expected in fields.items():
                value = values[name]
                if type(value) is not expected or (isinstance(value, str) and len(value) > 4000):
                    raise ValueError(f'{section}.{name} 类型或长度无效')
                if isinstance(value, str) and any(secret and secret in value for secret in [self.settings.token, *self.secret_values.values()]):
                    raise ValueError('配置不能包含真实密钥或管理员令牌，请使用 api_key_env 引用')
        for section in ('llm', 'image'):
            values = document[section]
            base = values['base_url']
            if base:
                url = urlsplit(base)
                if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.port not in (None, 443):
                    raise ValueError(f'{section}.base_url 必须为无凭据、无查询参数的 HTTPS 地址（443 端口）')
                if any(c.isspace() for c in base):
                    raise ValueError(f'{section}.base_url 不能包含空白')
            if values['api_key_env'] not in SECRET_NAMES:
                raise ValueError(f'{section}.api_key_env 只能引用模型密钥，不能读取其他环境变量')
            if not values['model'].strip() or len(values['model']) > 200:
                raise ValueError(f'{section}.model 无效')
        image = document['image']
        if not re.fullmatch(r'\d{2,4}x\d{2,4}|auto', image['size']):
            raise ValueError('image.size 应为 1024x768 等尺寸或 auto')
        if image['quality'] not in ('low', 'medium', 'high', 'auto') or not 1 <= image['timeout_seconds'] <= 600:
            raise ValueError('图片质量或超时设置无效')
        if document['tts']['provider'] != 'edge-tts' or not re.fullmatch(r'[A-Za-z0-9-]{3,100}', document['tts']['voice']):
            raise ValueError('TTS 仅支持 edge-tts，音色名称无效')
        return document

    def effective(self, document):
        llm, image, tts = document['llm'], document['image'], document['tts']
        for name, item in [('llm', llm), ('image', image)]:
            if item['base_url'] and not self.secret_values[item['api_key_env']]:
                raise ValueError(f'{name} 引用的密钥尚未配置，请先在部署环境或 Secrets 设置')
        return replace(self.settings, base='', key='',
                       llm_base=llm['base_url'].rstrip('/'), llm_key=self.secret_values[llm['api_key_env']] if llm['base_url'] else '',
                       llm_model=llm['model'], image_base=image['base_url'].rstrip('/'),
                       image_key=self.secret_values[image['api_key_env']] if image['base_url'] else '',
                       image_model=image['model'], image_size=image['size'], image_quality=image['quality'],
                       image_timeout=image['timeout_seconds'], style=image['style'], voice=tts['voice'])

    def write(self, path, text):
        fd, temporary = tempfile.mkstemp(prefix='.config-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def open(self):
        with self.lock:
            if not self.path.exists():
                text = encode(self.initial())
                document = self.parse(text)
                self.effective(document)
                self.write(self.path, text)
            text = self.path.read_text()
            document = self.parse(text)
            self.effective(document)
            self.active_text, self.active = text, document

    def snapshot(self):
        with self.lock:
            return encode(self.active)

    def current(self):
        with self.lock:
            return self.effective(self.active)

    def for_task(self, text):
        return self.effective(self.parse(text)) if text else self.current()

    def info(self):
        with self.lock:
            return {'text': self.active_text, 'revision': revision(self.active_text),
                    'secrets': {name: bool(value) for name, value in self.secret_values.items()},
                    'has_previous': self.previous.is_file(), 'ephemeral': bool(self.settings.space_host),
                    'storage': 'data/config.toml', 'external_change': self.path.read_text() != self.active_text}

    def validate(self, text):
        try:
            document = self.parse(text)
            self.effective(document)
            return document
        except ValueError as exc:
            if any(value and value in str(exc) for value in [self.settings.token, *self.secret_values.values()]):
                raise ValueError('配置参数无效，未保存') from None
            raise

    def save(self, text, expected_revision, confirm_binding=False):
        with self.lock:
            if expected_revision != revision(self.active_text) or self.path.read_text() != self.active_text:
                raise HTTPException(409, '配置已变化；请重新加载。若文件由外部修改，请核对并重启后再编辑')
            document = self.validate(text)
            changed = any((document[k]['base_url'], document[k]['api_key_env']) !=
                          (self.active[k]['base_url'], self.active[k]['api_key_env']) for k in ('llm', 'image'))
            if changed and not confirm_binding:
                raise HTTPException(409, '网关地址或密钥引用发生变化，请确认该网关有权接收选定密钥及文稿')
            self.write(self.previous, self.active_text)
            self.write(self.path, text)
            self.active_text, self.active = text, document
            return self.info()

    def restore(self, expected_revision, confirm_binding=False):
        with self.lock:
            if not self.previous.is_file():
                raise HTTPException(409, '没有可恢复的上一版配置')
            return self.save(self.previous.read_text(), expected_revision, confirm_binding)
