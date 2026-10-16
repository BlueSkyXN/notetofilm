"""单实例持久化：先保存不可变文件，最后发布完整快照清单。"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .errors import UserFacingError


class PersistenceError(UserFacingError, RuntimeError):
    def __init__(self):
        super().__init__('storage_unavailable', '持久化存储写入或读取失败，已暂停新的修改和生成。请检查存储配置与网络后重启恢复。')


def checksum(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def safe_path(root, name):
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or '..' in relative.parts or '\\' in name:
        raise ValueError('存储路径无效')
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('存储路径越界')
    return target


def atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class DirectoryBucket:
    """本地持久目录，也用于在离线测试中复现快照协议。"""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, name):
        path = safe_path(self.root, name)
        return path.read_bytes() if path.is_file() else None

    def download(self, name, destination):
        source = safe_path(self.root, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def put(self, name, source):
        content = source if isinstance(source, bytes) else Path(source).read_bytes()
        atomic_bytes(safe_path(self.root, name), content)

    def put_many(self, entries):
        for name, source in entries:
            self.put(name, source)

    def download_many(self, entries):
        for name, destination in entries:
            self.download(name, destination)


class HFBucket:
    def __init__(self, name, token, prefix=''):
        from huggingface_hub import HfApi
        from huggingface_hub.utils import disable_progress_bars
        disable_progress_bars()
        self.name, self.token = name, token
        self.prefix = prefix.strip('/') + '/' if prefix else ''
        self.api = HfApi(token=token)
        try:
            if not self.api.bucket_info(name).private:
                raise ValueError('持久化 bucket 必须为私有')
        except Exception:
            raise PersistenceError() from None

    def read(self, name):
        from huggingface_hub.errors import EntryNotFoundError
        try:
            with tempfile.TemporaryDirectory(prefix='notetofilm-bucket-') as folder:
                path = Path(folder) / 'object'
                self.api.download_bucket_files(self.name, [(self.prefix + name, path)], raise_on_missing_files=True)
                return path.read_bytes()
        except EntryNotFoundError:
            # bucket 本身在构造时已验证；再次确认权限，避免把不可达误判为空桶。
            try:
                self.api.bucket_info(self.name)
            except Exception:
                raise PersistenceError() from None
            return None
        except Exception:
            raise PersistenceError() from None

    def download(self, name, destination):
        self.download_many([(name, destination)])

    def download_many(self, entries):
        try:
            for _, destination in entries:
                destination.parent.mkdir(parents=True, exist_ok=True)
            self.api.download_bucket_files(self.name, [(self.prefix + name, destination) for name, destination in entries],
                                           raise_on_missing_files=True)
        except Exception:
            raise PersistenceError() from None

    def put(self, name, source):
        self.put_many([(name, source)])

    def put_many(self, entries):
        try:
            self.api.batch_bucket_files(self.name, add=[(source, self.prefix + name) for name, source in entries])
        except Exception:
            raise PersistenceError() from None


class Snapshots:
    def __init__(self, bucket):
        self.bucket = bucket
        self.known = set()
        self.generation = None
        self.file_digests = {}

    def restore(self, root):
        try:
            raw = self.bucket.read('latest.json')
            if raw is None:
                return
            manifest = json.loads(raw)
            if manifest['schema_version'] != 1 or not isinstance(manifest['files'], dict):
                raise ValueError('不支持的快照')
            files = manifest['files']
            if 'tasks.db' not in files:
                raise ValueError('快照缺少数据库')
            with tempfile.TemporaryDirectory(prefix='.restore-', dir=root) as folder:
                staging = Path(folder)
                downloads = []
                for name, sha in files.items():
                    if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                        raise ValueError('摘要无效')
                    target = safe_path(staging, name)
                    downloads.append(('objects/' + sha, target))
                self.bucket.download_many(downloads)
                for name, sha in files.items():
                    target = safe_path(staging, name)
                    if checksum(target) != sha:
                        raise ValueError('快照文件摘要不符')
                with sqlite3.connect(staging / 'tasks.db') as conn:
                    if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('数据库快照无效')
                for name in files:
                    safe_path(root, name)
                existing = root / 'tasks.db'
                if existing.is_file() and checksum(existing) != files['tasks.db']:
                    retained = root / '.recovery-backups' / uuid.uuid4().hex
                    retained.mkdir(parents=True)
                    with sqlite3.connect(existing) as source, sqlite3.connect(retained / 'tasks.db') as destination:
                        source.backup(destination)
                # 全部下载和校验成功后才替换；旧文件与未引用历史文件保留。
                for name in sorted(files, key=lambda name: name == 'tasks.db'):
                    target = safe_path(root, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(safe_path(staging, name), target)
                for suffix in ('-journal', '-wal', '-shm'):
                    (root / ('tasks.db' + suffix)).unlink(missing_ok=True)
            self.known = set(files.values())
            self.generation = manifest['generation']
        except Exception:
            raise PersistenceError() from None

    def checkpoint(self, root, database):
        try:
            with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=root) as folder:
                backup = Path(folder) / 'tasks.db'
                with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
                    source.backup(destination)
                files = {'tasks.db': backup}
                with sqlite3.connect(backup) as conn:
                    conn.row_factory = sqlite3.Row
                    # 保留旧版图音；只有经过校验并原子改名的媒体才会使用这些文件名。
                    for row in conn.execute('SELECT id FROM tasks'):
                        assets = root / str(row['id']) / 'assets'
                        for path in assets.glob('*/*'):
                            if path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.mp3') and '.part' not in path.name:
                                files[path.relative_to(root).as_posix()] = path
                    # 数据库引用兼容旧版本路径；FFmpeg 临时工作目录不进入快照。
                    for row in conn.execute('SELECT task_id,path FROM image_assets'):
                        files[f"{row['task_id']}/{row['path']}"] = root / str(row['task_id']) / row['path']
                    for row in conn.execute('SELECT task_id,image_path,audio_path FROM scenes'):
                        for column in ('image_path', 'audio_path'):
                            if row[column]:
                                files[f"{row['task_id']}/{row[column]}"] = root / str(row['task_id']) / row[column]
                    videos = list(conn.execute('SELECT id AS task_id,video_path FROM tasks WHERE video_path IS NOT NULL'))
                    videos += list(conn.execute('SELECT task_id,video_path FROM video_versions'))
                    for row in videos:
                        path = Path(str(row['task_id'])) / row['video_path']
                        files[path.as_posix()] = root / path
                        if path.parent.name:
                            for name in ('complete.json', 'frozen.json', 'layout.json', 'scenes.json', 'video.build.json'):
                                sibling = path.parent / name
                                if (root / sibling).is_file():
                                    files[sibling.as_posix()] = root / sibling
                hashes, pending = {}, {}
                for name, path in files.items():
                    if name != 'tasks.db':
                        path = safe_path(root, name)
                    if not path.is_file():
                        # 丢失素材由现有恢复流程识别，不能捏造一个可恢复的文件。
                        continue
                    stat = path.stat()
                    identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    cached = self.file_digests.get(name) if name != 'tasks.db' else None
                    sha = cached[1] if cached and cached[0] == identity else checksum(path)
                    if name != 'tasks.db':
                        self.file_digests[name] = (identity, sha)
                    hashes[name] = sha
                    if sha not in self.known:
                        pending['objects/' + sha] = path
                generation = uuid.uuid4().hex
                content = json.dumps({'schema_version': 1, 'generation': generation, 'files': hashes},
                                     ensure_ascii=False, sort_keys=True).encode()
                pending['snapshots/' + generation + '.json'] = content
                # 此批次非事务，但只写不可变内容。任一上传失败时不发布 latest。
                self.bucket.put_many(list(pending.items()))
                self.bucket.put('latest.json', content)
                if self.bucket.read('latest.json') != content:
                    raise ValueError('快照发布未确认')
                self.known.update(hashes.values())
                self.generation = generation
        except Exception:
            raise PersistenceError() from None
