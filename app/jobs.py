"""素材生成作业：记录、条件认领、状态转换与输入快照。

作业表把每次素材生成变成可追溯、可恢复的持久记录（schema v10）。
所有状态变更都在调用方的写事务中完成，快照确认后才调用供应商（I1）。
输入快照只含必要非秘密字段，不序列化凭据（I12）。
"""
import json

from .db import now, script_digest

TERMINAL_STATES = ('succeeded', 'failed', 'unknown', 'cancelled')


def input_snapshot(kind, scene, settings, references=None):
    """组装非秘密输入快照与摘要。

    只记录模型、非秘密生成参数、配置引用、提示内容和实际参考 ID／摘要；
    不序列化完整配置、请求头或凭据（I12）。
    """
    document = {'kind': kind, 'scene_id': scene['id'], 'plan_revision': None,
                'prompt': scene['visual_prompt'] if kind == 'img' else scene['narration']}
    if kind == 'img':
        document['model'] = settings.image_model
        document['size'] = settings.image_size
        document['quality'] = settings.image_quality
        document['style'] = settings.style
        document['reference_protocol'] = settings.image_reference_protocol
    else:
        document['voice'] = settings.voice
    if references:
        document['references'] = references
    return json.dumps(document, ensure_ascii=False, sort_keys=True), script_digest(
        json.dumps(document, ensure_ascii=False, sort_keys=True))


def create_jobs(conn, tid, operation_id, plan_revision, entries):
    """预建 queued 作业；同一操作内每镜每类素材最多一条。

    entries: [{scene_id, scene_index, kind, depends_on(作业 id 列表)}]。
    重复创建同一 (operation, scene, kind) 返回已有作业 id。
    """
    existing = {(row['scene_id'], row['kind']): row['id'] for row in
                conn.execute('SELECT id,scene_id,kind FROM jobs WHERE operation_id=?', (operation_id,))}
    result = []
    for entry in entries:
        key = (entry['scene_id'], entry['kind'])
        if key in existing:
            result.append(existing[key])
            continue
        cursor = conn.execute(
            'INSERT INTO jobs(task_id,operation_id,scene_id,scene_index,plan_revision,kind,depends_on,'
            'input_json,input_digest,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (tid, operation_id, entry['scene_id'], entry['scene_index'], plan_revision, entry['kind'],
             json.dumps(entry.get('depends_on') or [], ensure_ascii=False), '{}', '',
             'queued', now()))
        result.append(cursor.lastrowid)
    return result


def job_row(conn, job_id):
    row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    return dict(row) if row else None


def jobs_for_operation(conn, operation_id):
    return [dict(row) for row in
            conn.execute('SELECT * FROM jobs WHERE operation_id=? ORDER BY id', (operation_id,))]


def claim(conn, job_id, input_json='', input_digest='', expected_material_revision=None):
    """条件认领：只有 queued 作业才能进入 running；返回是否成功。

    认领必须与额度计数、镜头状态在同一个写事务中完成，快照确认后才
    调用供应商（I1）。重复认领（两执行者、同一作业）只有一个成功。
    """
    cursor = conn.execute(
        "UPDATE jobs SET state='running',input_json=?,input_digest=?,expected_material_revision=?,"
        'started_at=? WHERE id=? AND state=?',
        (input_json, input_digest, expected_material_revision, now(), job_id, 'queued'))
    return cursor.rowcount == 1


def finish(conn, job_id, state, result=None, error=None):
    """写终态。成功结果要等媒体校验、版本发布和持久化确认后才能写入。

    result: 成功后的素材 ID、输出版本和 SHA-256。调用方先校验操作所有权。
    """
    if state not in TERMINAL_STATES:
        raise ValueError(f'非法作业终态: {state}')
    conn.execute('UPDATE jobs SET state=?,result_json=?,error=?,finished_at=? WHERE id=?',
                 (state, json.dumps(result, ensure_ascii=False) if result else None, error, now(), job_id))


def cancel_queued(conn, operation_id, error='操作收尾，剩余队列终止。'):
    """操作失败、暂停、关闭或重启后终止旧队列；不冒充成功。"""
    cursor = conn.execute(
        "UPDATE jobs SET state='cancelled',error=?,finished_at=? WHERE operation_id=? AND state='queued'",
        (error, now(), operation_id))
    return cursor.rowcount


def sanitize_error(message, secrets=()):
    """错误文本落库前脱敏；不持久化含凭据的供应商响应。"""
    text = str(message)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[redacted]')
    return text[:2000]
