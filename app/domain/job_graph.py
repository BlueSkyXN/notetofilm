"""作业依赖图：就绪集合与派发顺序的纯计算。

无 I/O：输入是作业记录的普通字段，输出是派发决策依据。
调度器负责槽位与执行，本模块只回答"哪些作业的依赖已满足"。
"""
import json


def _depends_on(job):
    try:
        value = json.loads(job.get('depends_on') or '[]')
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def ready_job_ids(jobs):
    """依赖全部成功且自身仍排队的作业集合。

    依赖不在给定作业列表中的（例如上游在更早操作完成）视为已满足；
    依赖失败或被取消的作业不算就绪，由协调者终止。
    """
    states = {job['id']: job['state'] for job in jobs}
    ready = set()
    for job in jobs:
        if job['state'] != 'queued':
            continue
        if all(dep not in states or states[dep] == 'succeeded' for dep in _depends_on(job)):
            ready.add(job['id'])
    return ready


def execution_order(jobs):
    """按镜头顺序与类型给出建议派发顺序（音频先于图片，稳定排序）。

    顺序只影响公平性，不影响正确性；依赖检查总在派发时进行。
    """
    return [job['id'] for job in sorted(
        (job for job in jobs if job['state'] == 'queued'),
        key=lambda job: (job['scene_index'], 0 if job['kind'] == 'aud' else 1))]
