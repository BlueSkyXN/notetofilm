import fcntl
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def script_digest(script):
    return hashlib.sha256(script.encode()).hexdigest()


class Store:
    def __init__(self, data):
        self.data = data
        self.path = data / 'tasks.db'
        self.lock = None

    def open(self):
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = (self.data / '.service.lock').open('a+')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('此数据目录已被另一进程使用；只允许一个应用进程')
        try:
            self.migrate()
            self.recover()
        except Exception:
            self.close()
            raise

    def close(self):
        if self.lock is not None:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self):
        with self.connect(write=True) as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 4:
                raise RuntimeError('数据库版本高于当前程序，请勿降级启动')
            conn.execute('CREATE TABLE IF NOT EXISTS tasks ('
                         'id INTEGER PRIMARY KEY AUTOINCREMENT, script TEXT NOT NULL, '
                         'status TEXT NOT NULL, error TEXT, created_at TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS scenes ('
                         'id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id), '
                         'idx INTEGER NOT NULL, narration TEXT NOT NULL, '
                         'img_ok INTEGER NOT NULL DEFAULT 0, aud_ok INTEGER NOT NULL DEFAULT 0, '
                         'img_placeholder INTEGER NOT NULL DEFAULT 0)')
            fields = {
                'tasks': {
                    'script_hash': "TEXT NOT NULL DEFAULT ''", 'plan_complete': 'INTEGER NOT NULL DEFAULT 0',
                    'expected_scenes': 'INTEGER NOT NULL DEFAULT 0', 'operation_id': 'TEXT',
                    'operation_kind': 'TEXT', 'operation_scene': 'INTEGER', 'video_path': 'TEXT',
                    'plan_revision': 'INTEGER NOT NULL DEFAULT 1',
                    'image_requests': 'INTEGER NOT NULL DEFAULT 0', 'archived': 'INTEGER NOT NULL DEFAULT 0',
                    'owner': "TEXT NOT NULL DEFAULT 'admin'", 'model_config': "TEXT NOT NULL DEFAULT ''",
                },
                'scenes': {
                    'img_placeholder': 'INTEGER NOT NULL DEFAULT 0',
                    'img_state': "TEXT NOT NULL DEFAULT 'pending'", 'aud_state': "TEXT NOT NULL DEFAULT 'pending'",
                    'img_revision': 'INTEGER NOT NULL DEFAULT 0', 'aud_revision': 'INTEGER NOT NULL DEFAULT 0',
                    'img_approved': 'INTEGER NOT NULL DEFAULT 0', 'aud_approved': 'INTEGER NOT NULL DEFAULT 0',
                    'image_path': 'TEXT', 'audio_path': 'TEXT', 'img_error': 'TEXT', 'aud_error': 'TEXT',
                },
            }
            for table, additions in fields.items():
                existing = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
                for name, definition in additions.items():
                    if name not in existing:
                        conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            duplicates = conn.execute('SELECT task_id,idx FROM scenes GROUP BY task_id,idx HAVING COUNT(*)>1').fetchone()
            if duplicates:
                raise RuntimeError('旧库存在重复分镜；请备份并人工处理，程序不会自动删除')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS scene_identity ON scenes(task_id,idx)')
            conn.execute('CREATE INDEX IF NOT EXISTS task_script ON tasks(script_hash,status)')
            if version == 0:
                for row in conn.execute('SELECT id,script FROM tasks').fetchall():
                    conn.execute('UPDATE tasks SET script_hash=? WHERE id=?', (script_digest(row['script']), row['id']))
                # Legacy rows never recorded the full plan; generated prefixes cannot prove completeness.
                conn.execute("UPDATE tasks SET status='interrupted',error=? WHERE status!='done'",
                             ('旧版任务未保存完整分镜计划；恢复将重新规划，旧文件保留。',))
            conn.execute('CREATE INDEX IF NOT EXISTS task_owner ON tasks(owner,id)')
            conn.execute('CREATE TABLE IF NOT EXISTS daily_usage ('
                         'day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(day,kind))')
            conn.execute('CREATE TABLE IF NOT EXISTS image_requests ('
                         'owner TEXT NOT NULL, request_key TEXT NOT NULL, parameter_hash TEXT NOT NULL, '
                         'task_id INTEGER NOT NULL REFERENCES tasks(id), operation_id TEXT NOT NULL, '
                         'created_at TEXT NOT NULL, PRIMARY KEY(owner,request_key))')
            conn.execute('PRAGMA user_version=4')

    def recover(self):
        with self.connect(write=True) as conn:
            conn.execute("UPDATE scenes SET img_state='unknown',img_ok=0,img_approved=0,img_error=? WHERE img_state='running'",
                         ('服务中断，生图结果未知；需人工确认后重新请求，可能重复计费。',))
            conn.execute("UPDATE scenes SET aud_state='failed',aud_ok=0,aud_approved=0,aud_error=? WHERE aud_state='running'",
                         ('服务中断，请重新生成配音。',))
            conn.execute("UPDATE tasks SET status='interrupted',error=? WHERE operation_id IS NOT NULL "
                         "OR status IN ('submitted','generating','rendering')",
                         ('服务中断，已有成功素材保留，请核对后恢复。',))
            conn.execute('UPDATE tasks SET operation_id=NULL,operation_kind=NULL,operation_scene=NULL')
