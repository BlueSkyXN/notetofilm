import fcntl
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock

from .persistence import PersistenceError


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def script_digest(script):
    return hashlib.sha256(script.encode()).hexdigest()


class Store:
    def __init__(self, data, snapshots=None):
        self.data = data
        self.path = data / 'tasks.db'
        self.lock = None
        self.snapshots = snapshots
        self.mutation_lock = RLock()
        self.failure = False

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
            if self.snapshots:
                self.snapshots.restore(self.data)
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
        # 持久化确认之前不让其他读写观察新状态；失败后保留本地文件并停止新增操作。
        with self.mutation_lock:
            if self.failure:
                raise PersistenceError()
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
            if write and self.snapshots:
                try:
                    self.snapshots.checkpoint(self.data, self.path)
                except Exception:
                    self.failure = True
                    raise PersistenceError() from None

    def migrate(self):
        with self.connect(write=True) as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 10:
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
                    'material_revision_floor': 'INTEGER NOT NULL DEFAULT 0',
                    'aspect_ratio': "TEXT NOT NULL DEFAULT '16:9'",
                    'image_fit': "TEXT NOT NULL DEFAULT 'contain'",
                    'purpose': "TEXT NOT NULL DEFAULT ''",
                    'audience': "TEXT NOT NULL DEFAULT ''",
                    'focus': "TEXT NOT NULL DEFAULT ''",
                    'target_duration_seconds': 'INTEGER NOT NULL DEFAULT 0',
                    'pause_requested': 'INTEGER NOT NULL DEFAULT 0',
                    'title': "TEXT NOT NULL DEFAULT ''",
                    'copied_from': 'INTEGER',
                    'operation_started_at': 'TEXT', 'progress_stage': 'TEXT',
                    'progress_scene': 'INTEGER', 'progress_started_at': 'TEXT',
                },
                'scenes': {
                    'img_placeholder': 'INTEGER NOT NULL DEFAULT 0',
                    'img_state': "TEXT NOT NULL DEFAULT 'pending'", 'aud_state': "TEXT NOT NULL DEFAULT 'pending'",
                    'img_revision': 'INTEGER NOT NULL DEFAULT 0', 'aud_revision': 'INTEGER NOT NULL DEFAULT 0',
                    'img_approved': 'INTEGER NOT NULL DEFAULT 0', 'aud_approved': 'INTEGER NOT NULL DEFAULT 0',
                    'image_path': 'TEXT', 'audio_path': 'TEXT', 'img_error': 'TEXT', 'aud_error': 'TEXT',
                    'audio_duration_seconds': 'REAL',
                    'visual_prompt': "TEXT NOT NULL DEFAULT ''",
                    'image_width': 'INTEGER', 'image_height': 'INTEGER',
                    'image_spec_json': 'TEXT', 'image_asset_id': 'INTEGER',
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
            conn.execute('CREATE TABLE IF NOT EXISTS video_versions ('
                         'id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id), '
                         'video_path TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS image_assets ('
                         'id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id), '
                         "source TEXT NOT NULL CHECK(source IN ('upload','generated')), "
                         'path TEXT NOT NULL, sha256 TEXT NOT NULL, mime TEXT NOT NULL, '
                         'width INTEGER NOT NULL, height INTEGER NOT NULL, byte_size INTEGER NOT NULL, '
                         'origin_scene_id INTEGER, context_json TEXT NOT NULL DEFAULT \'{}\', created_at TEXT NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS image_asset_task ON image_assets(task_id,id)')
            if version < 6:
                conn.execute("UPDATE scenes SET visual_prompt=narration WHERE visual_prompt=''")
            # v10：素材生成作业表。历史标识不对当前 scenes 建外键——save_plan 会
            # 先删后插镜头行，历史作业与付费台账必须保留（I12：输入快照不含凭据）。
            conn.execute('CREATE TABLE IF NOT EXISTS jobs ('
                         'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                         'task_id INTEGER NOT NULL REFERENCES tasks(id), '
                         'operation_id TEXT NOT NULL, '
                         'scene_id INTEGER NOT NULL, '
                         'scene_index INTEGER NOT NULL, '
                         'plan_revision INTEGER NOT NULL, '
                         "kind TEXT NOT NULL CHECK(kind IN ('img','aud')), "
                         "depends_on TEXT NOT NULL DEFAULT '[]', "
                         "input_json TEXT NOT NULL DEFAULT '{}', "
                         "input_digest TEXT NOT NULL DEFAULT '', "
                         'expected_material_revision INTEGER, '
                         'result_json TEXT, '
                         "state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','unknown','cancelled')), "
                         'error TEXT, '
                         'created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS job_identity ON jobs(operation_id,scene_id,kind)')
            conn.execute('CREATE INDEX IF NOT EXISTS job_task ON jobs(task_id,id)')
            conn.execute('PRAGMA user_version=10')

    def recover(self):
        with self.connect(write=True) as conn:
            conn.execute("UPDATE scenes SET img_state='unknown',img_ok=0,img_approved=0,img_error=? WHERE img_state='running'",
                         ('服务中断，生图结果未知；需人工确认后重新请求，可能重复计费。',))
            conn.execute("UPDATE scenes SET aud_state='failed',aud_ok=0,aud_approved=0,aud_error=? WHERE aud_state='running'",
                         ('服务中断，请重新生成配音。',))
            # 作业恢复与镜头恢复同一规则：生图 unknown、配音 failed；未派发的
            # 队列终止。重启不自动继续旧队列，由人工恢复创建新操作。
            conn.execute("UPDATE jobs SET state='unknown',error=?,finished_at=? "
                         "WHERE state='running' AND kind='img'",
                         ('服务中断，生图结果未知；需人工确认后重新请求，可能重复计费。', now()))
            conn.execute("UPDATE jobs SET state='failed',error=?,finished_at=? "
                         "WHERE state='running' AND kind='aud'",
                         ('服务中断，请重新生成配音。', now()))
            conn.execute("UPDATE jobs SET state='cancelled',error=?,finished_at=? WHERE state='queued'",
                         ('服务重启，旧队列终止；恢复会创建新操作。', now()))
            conn.execute("UPDATE tasks SET status='interrupted',error=? WHERE operation_id IS NOT NULL "
                         "OR status IN ('submitted','generating','rendering')",
                         ('服务中断，已有成功素材保留，请核对后恢复。',))
            # 崩溃可能落在 request_pause 与 finish_paused 之间；残留标志会让
            # 下一次恢复立即"暂停"，重启时清零。
            conn.execute('UPDATE tasks SET operation_id=NULL,operation_kind=NULL,operation_scene=NULL,pause_requested=0')
            conn.execute('UPDATE tasks SET operation_started_at=NULL,progress_stage=NULL,progress_scene=NULL,progress_started_at=NULL')
