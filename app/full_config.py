"""完整明文配置文件；环境变量只用于旧部署的一次性导入。"""
import copy
import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from .config import Config, ROOT
from .persistence import DirectoryBucket, HFBucket, PersistenceError

MASK = '__UNCHANGED__'
REFERENCES = {'shared': 'OPENAI_API_KEY', 'llm': 'LLM_API_KEY', 'image': 'IMAGE_API_KEY'}
LIMITS = ('max_active', 'max_render', 'max_script', 'max_scenes', 'max_narration', 'render_timeout',
          'max_image_requests', 'min_free_mb', 'max_audio_seconds', 'max_video_seconds', 'max_daily_tasks',
          'max_visitor_tasks', 'max_daily_llm', 'max_daily_images', 'max_daily_audio', 'max_daily_renders',
          'request_body_timeout', 'max_upload_image_bytes', 'max_image_pixels')
SECRET_FIELDS = (('credentials', 'shared'), ('credentials', 'llm'), ('credentials', 'image'),
                 ('server', 'admin_token'), ('server', 'webhook'), ('storage', 'hf_token'))
SERVER_FIELDS = {'admin_token': str, 'local_only': bool, 'cookie_secure': bool, 'allowed_hosts': list, 'webhook': str}
STORAGE_FIELDS = {'work_dir': str, 'local_backup_dir': str, 'config_bucket': str, 'data_bucket': str, 'hf_token': str}


def encode_full(document):
    lines = ['schema_version = 1']
    for section, values in document.items():
        if section == 'schema_version':
            continue
        lines += ['', f'[{section}]']
        lines += [f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in values.items()]
    return '\n'.join(lines) + '\n'


def read_toml(text):
    if len(text.encode()) > 32768:
        raise ValueError('配置不得超过 32 KiB')
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError('TOML 语法无效，请检查分节、字段和引号') from None


def model_document(document):
    result = {name: copy.deepcopy(document[name]) for name in ('llm', 'image', 'tts')}
    for name in ('llm', 'image'):
        result[name]['api_key_env'] = REFERENCES[result[name].pop('credential_ref')]
    return result


def credentials(document):
    return {key: document['credentials'][name] for name, key in REFERENCES.items()}


def from_legacy(settings, models):
    document = {
        'schema_version': 1,
        'server': {'admin_token': settings.token, 'local_only': settings.local_only,
                   'cookie_secure': settings.cookie_secure, 'allowed_hosts': list(settings.allowed_hosts),
                   'webhook': settings.webhook},
        'storage': {'work_dir': str(settings.data), 'local_backup_dir': '', 'config_bucket': '',
                    'data_bucket': '', 'hf_token': ''},
        'credentials': {'shared': settings.key, 'llm': settings.llm_key, 'image': settings.image_key},
        'limits': {name: getattr(settings, name) for name in LIMITS},
        **copy.deepcopy(models),
    }
    inverse = {value: key for key, value in REFERENCES.items()}
    for name in ('llm', 'image'):
        document[name]['credential_ref'] = inverse[document[name].pop('api_key_env')]
    return document


def redacted(document):
    result = copy.deepcopy(document)
    for section, key in SECRET_FIELDS:
        if result[section][key]:
            result[section][key] = MASK
    return result


def validate_full(document, path, space_host=''):
    from .model_config import ModelConfig, SCHEMA, encode
    expected = {'schema_version', 'server', 'storage', 'credentials', 'limits', 'llm', 'image', 'tts'}
    if set(document) != expected or type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise ValueError('需要完整的 schema_version = 1 配置文件')
    sections = {'server': SERVER_FIELDS, 'storage': STORAGE_FIELDS,
                'credentials': {name: str for name in REFERENCES}, 'limits': {name: int for name in LIMITS}}
    for name in ('llm', 'image', 'tts'):
        fields = dict(SCHEMA[name])
        if name != 'tts':
            fields.pop('api_key_env')
            fields['credential_ref'] = str
        sections[name] = fields
    for section, fields in sections.items():
        values = document[section]
        if not isinstance(values, dict) or set(values) != set(fields):
            raise ValueError(f'{section} 字段不完整或含有未知字段')
        for name, kind in fields.items():
            value = values[name]
            if type(value) is not kind or (isinstance(value, str) and (len(value) > 4096 or value == MASK)):
                raise ValueError(f'{section}.{name} 类型或取值无效')
    if any(document[name]['credential_ref'] not in REFERENCES for name in ('llm', 'image')):
        raise ValueError('credential_ref 必须引用 shared、llm 或 image')
    if any(value < 1 for value in document['limits'].values()):
        raise ValueError('限制参数必须为正整数')
    server, storage = document['server'], document['storage']
    if not all(isinstance(host, str) for host in server['allowed_hosts']):
        raise ValueError('allowed_hosts 必须是主机名数组')
    for name in ('config_bucket', 'data_bucket'):
        if storage[name] and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', storage[name]):
            raise ValueError(f'storage.{name} 应为 namespace/bucket')
    if (storage['config_bucket'] or storage['data_bucket']) and not storage['hf_token']:
        raise ValueError('使用 HF bucket 时需要在 storage.hf_token 中填写访问凭据')
    if storage['config_bucket'] and storage['config_bucket'] == storage['data_bucket']:
        raise ValueError('config bucket 和 data bucket 必须分开')
    if storage['local_backup_dir'] and storage['data_bucket']:
        raise ValueError('本地持久目录和 data bucket 只能选择一个')
    def resolve(value):
        candidate = Path(value).expanduser()
        return (candidate if candidate.is_absolute() else path.parent / candidate).resolve()
    if not storage['work_dir']:
        raise ValueError('storage.work_dir 不能为空')
    work = resolve(storage['work_dir'])
    backup = resolve(storage['local_backup_dir']) if storage['local_backup_dir'] else None
    if backup and (backup.is_relative_to(work) or work.is_relative_to(backup)):
        raise ValueError('本地持久目录与运行目录必须相互独立')
    if path.resolve().is_relative_to(work):
        raise ValueError('完整配置文件必须放在运行数据目录之外')
    hosts = list(server['allowed_hosts'])
    if space_host and space_host not in hosts:
        hosts.append(space_host)
    secrets = credentials(document)
    llm, image, tts = document['llm'], document['image'], document['tts']
    settings = Config(data=work, config_path=path, backup_dir=backup, config_bucket=storage['config_bucket'],
                      data_bucket=storage['data_bucket'], hf_token=storage['hf_token'], credentials=secrets,
                      token=server['admin_token'], local_only=server['local_only'],
                      cookie_secure=bool(space_host) or server['cookie_secure'], allowed_hosts=tuple(hosts),
                      webhook=server['webhook'], space_host=space_host,
                      key=secrets['OPENAI_API_KEY'], llm_base=llm['base_url'],
                      llm_key=secrets[REFERENCES[llm['credential_ref']]], llm_model=llm['model'],
                      llm_wire_api=llm['wire_api'], image_base=image['base_url'],
                      image_key=secrets[REFERENCES[image['credential_ref']]], image_model=image['model'],
                      image_reference_protocol=image['reference_protocol'], image_size=image['size'],
                      image_quality=image['quality'], image_timeout=image['timeout_seconds'], style=image['style'],
                      voice=tts['voice'], **document['limits'])
    settings.validate()
    ModelConfig(replace(settings, config_path=None)).parse(encode(model_document(document)))
    return settings


def repository(path, document):
    storage = document['storage']
    return HFBucket(storage['config_bucket'], storage['hf_token']) if storage['config_bucket'] else DirectoryBucket(path.parent)


def load_config(env):
    from .model_config import ModelConfig
    mounted = Path('/mnt/notetofilm-config/config.toml')
    default = mounted if mounted.parent.is_dir() else ROOT / 'config/config.toml'
    if not env.get('NOTETOFILM_CONFIG_FILE') and not default.is_file() and not mounted.parent.is_dir() and env.get('VIDEO_DATA_DIR'):
        # 显式使用旧数据目录的已有启动脚本继续工作；迁移时指定独立配置文件路径。
        return Config.from_env(env)
    path = Path(env.get('NOTETOFILM_CONFIG_FILE') or default).expanduser().resolve()
    document = read_toml(path.read_text()) if path.is_file() else None
    # 首次部署的引导文件只提供存储信息。旧模型参数和密钥仅在此时导入一次。
    if document and document.get('storage', {}).get('config_bucket'):
        remote = repository(path, document).read('config.toml')
        if remote is None:
            raise PersistenceError()
        document = read_toml(remote.decode())
    if document is None or document.get('bootstrap_from_environment') is True:
        seed = document
        legacy = Config.from_env(env)
        models = ModelConfig(legacy)
        previous = legacy.data / 'config.toml'
        model = models.parse(previous.read_text()) if previous.is_file() else models.initial()
        document = from_legacy(legacy, model)
        if seed:
            if set(seed) != {'bootstrap_from_environment', 'storage'} or set(seed['storage']) != set(STORAGE_FIELDS):
                raise ValueError('引导配置只允许 bootstrap_from_environment 与完整 storage 分节')
            document['storage'] = seed['storage']
        validate_full(document, path, env.get('SPACE_HOST', '').lower())
        content = encode_full(document).encode()
        repo = repository(path, document)
        name = 'config.toml' if document['storage']['config_bucket'] else path.name
        repo.put(name, content)
        if repo.read(name) != content:
            raise PersistenceError()
    return validate_full(document, path, env.get('SPACE_HOST', '').lower())


class FileConfiguration:
    def __init__(self, settings):
        self.settings, self.path = settings, settings.config_path
        self.bucket = HFBucket(settings.config_bucket, settings.hf_token) if settings.config_bucket else DirectoryBucket(self.path.parent)
        self.name = 'config.toml' if settings.config_bucket else self.path.name
        self.text = ''
        self.document = None
        self.failure = False

    def read_object(self, name):
        try:
            return self.bucket.read(name)
        except Exception:
            self.failure = True
            raise PersistenceError() from None

    def read(self):
        content = self.read_object(self.name)
        if content is None:
            self.failure = True
            raise PersistenceError()
        return content.decode()

    def open(self):
        text = self.read()
        document = read_toml(text)
        validate_full(document, self.path, self.settings.space_host)
        self.text, self.document = text, document

    def prepare(self, text):
        from .model_config import ModelConfig
        document = read_toml(text)
        if set(document) == {'llm', 'image', 'tts'}:
            # 保留旧 API 客户端的模型配置写入契约。
            parsed = ModelConfig(replace(self.settings, config_path=None)).parse(text)
            document = copy.deepcopy(self.document)
            mapped = from_legacy(self.settings, parsed)
            for name in ('llm', 'image', 'tts'):
                document[name] = mapped[name]
        for section, key in SECRET_FIELDS:
            if isinstance(document.get(section), dict) and document[section].get(key) == MASK:
                document[section][key] = self.document[section][key]
        validate_full(document, self.path, self.settings.space_host)
        return document

    def save(self, text, expected, confirm_binding):
        from .model_config import revision
        if self.failure:
            raise PersistenceError()
        if expected != revision(self.text) or self.read() != self.text:
            raise HTTPException(409, '配置已变化，请重新加载后再保存')
        document = self.prepare(text)
        changed = any((document[name]['base_url'], document[name]['credential_ref']) !=
                      (self.document[name]['base_url'], self.document[name]['credential_ref']) for name in ('llm', 'image'))
        if changed and not confirm_binding:
            raise HTTPException(409, '网关地址或密钥条目发生变化，请确认该网关有权接收选定密钥及文稿')
        content = encode_full(document)
        try:
            self.bucket.put('config.previous.toml', self.text.encode())
            self.bucket.put(self.name, content.encode())
            if self.read() != content:
                raise PersistenceError()
        except Exception:
            self.failure = True
            raise PersistenceError() from None
        self.text, self.document = content, document

    def info(self):
        from .model_config import revision
        safe = redacted(self.document)
        restart = []
        effective = validate_full(self.document, self.path, self.settings.space_host)
        for field in ('data', 'backup_dir', 'config_bucket', 'data_bucket', 'hf_token', 'token', 'cookie_secure',
                      'local_only', 'allowed_hosts', 'webhook', *LIMITS):
            if getattr(effective, field) != getattr(self.settings, field):
                restart.append(field)
        return {'text': encode_full(safe), 'document': safe, 'revision': revision(self.text),
                'secrets': {name: bool(value) for name, value in credentials(self.document).items()},
                'has_previous': self.read_object('config.previous.toml') is not None,
                'ephemeral': bool(self.settings.space_host and not self.settings.config_bucket),
                'storage': 'config bucket' if self.settings.config_bucket else '独立配置文件',
                'external_change': self.read() != self.text, 'restart_required': restart, 'complete_config': True}
