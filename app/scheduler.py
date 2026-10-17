"""进程内素材调度器：全服务并发限制、任务内派发与收尾。

所有素材生成入口（完整生成、预演、恢复、单镜重做）共用同一个
限流入口：先取得全局图/音槽位，再取得任务在途槽位；槽位持有至
结果发布或失败收尾，任何异常路径都释放。持久化故障或停机后
全服务停止新认领（手册 W4）。
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock

LOG = logging.getLogger('notetofilm.scheduler')

POLL_SECONDS = 0.05


class Scheduler:
    def __init__(self, config, stopping=None, storage_failed=None):
        self.config = config
        self.stopping = stopping
        self.storage_failed = storage_failed or (lambda: False)
        self.image_slots = BoundedSemaphore(max(1, config.image_concurrency))
        self.audio_slots = BoundedSemaphore(max(1, config.audio_concurrency))
        self._task_lock = Lock()
        self._task_slots = {}
        self.executor = ThreadPoolExecutor(
            max_workers=max(config.image_concurrency + config.audio_concurrency, config.task_inflight),
            thread_name_prefix='notetofilm-asset')

    def close(self):
        self.executor.shutdown(wait=True)

    def _task_slots_for(self, tid):
        with self._task_lock:
            if tid not in self._task_slots:
                self._task_slots[tid] = BoundedSemaphore(max(1, self.config.task_inflight))
            return self._task_slots[tid]

    def should_stop(self):
        return bool((self.stopping is not None and self.stopping.is_set()) or self.storage_failed())

    def try_acquire(self, tid, kind):
        """非阻塞获取槽位：拿到返回释放句柄；暂无槽位或应停止返回 None。

        应停止与暂无槽位用 should_stop() 区分，协调者据此决定重试还是收尾。
        """
        if self.should_stop():
            return None
        global_slot = self.image_slots if kind == 'img' else self.audio_slots
        task_slot = self._task_slots_for(tid)
        if not global_slot.acquire(blocking=False):
            return None
        if not task_slot.acquire(blocking=False):
            global_slot.release()
            return None
        released = [False]
        lock = Lock()

        def release():
            with lock:
                if released[0]:
                    return
                released[0] = True
                task_slot.release()
                global_slot.release()

        return release

    def acquire(self, tid, kind, should_stop=None):
        """取得全局与任务槽位；返回持有的释放句柄，停止时返回 None。

        取槽顺序固定为先全局后任务，拿不到任务槽就释放全局槽重试，
        避免两个方向交叉持锁造成死锁。
        """
        while True:
            if self.should_stop() or (should_stop is not None and should_stop()):
                return None
            release = self.try_acquire(tid, kind)
            if release is not None:
                return release
            # 槽位紧张时短暂让出，避免忙等占满 CPU。
            import time
            time.sleep(POLL_SECONDS)

    def submit(self, work):
        return self.executor.submit(work)
