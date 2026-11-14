"""CPU + 显卡混合引擎：两边同时干活，谁快谁多干。

思路很简单——**一个共享队列，两个消费者**：

* GPU 只吃 JPEG（nvJPEG 的强项），一次一张地啃；
* CPU 线程池先吃非 JPEG（PNG/WebP/GIF…），吃完就**顺手偷 JPEG**；

因为两边都从同一个队列里取活，快的自然拿得多，
不需要事先猜"分几成给显卡"，负载就自动均摊了（work stealing）。
扫描器每批给几百上千个路径，队列刚好够两边吃。
"""

from __future__ import annotations

import math
import os
import queue
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from .formats import is_jpeg
from .hashing import analyze_image


class HybridEngine:
    kind = "hybrid"

    def __init__(self, cpu_workers: int | None = None, batch: int = 8,
                 device: str = "auto", load: int = 100):
        from .gpupipe import GpuPipeline

        self.pipe = GpuPipeline(device=device, batch=batch)
        total = os.cpu_count() or 4
        # 留 1~2 个核给 GPU 主循环和系统，别把 CPU 吃满导致显卡喂不饱
        default_cpu = max(2, min(8, total - 2))
        base = int(cpu_workers or default_cpu)
        self.load = max(5, min(100, int(load))) if str(load).isdigit() or isinstance(load, int) else 100
        # 占用百分比直接体现在 CPU 线程数上：20% → 线程数打两折（显卡那边靠休眠控速）
        self.cpu_workers = max(1, int(math.ceil(base * self.load / 100.0)))
        self.pool = ThreadPoolExecutor(max_workers=self.cpu_workers,
                                       thread_name_prefix="hybrid-cpu")
        suffix = "" if self.load >= 100 else f"，限速 {self.load}%"
        self.name = f"CPU ×{self.cpu_workers} + GPU {self.pipe.name}{suffix}"
        self.stats = {"gpu": 0, "cpu": 0}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 内部分派
    def _bump(self, who: str) -> None:
        with self._lock:
            self.stats[who] = self.stats.get(who, 0) + 1

    def _run_one(self, path: str) -> dict:
        try:
            return analyze_image(path)
        except Exception as exc:  # noqa: BLE001
            return {"path": path, "error": str(exc)}

    def analyze_batch(self, paths):
        paths = list(paths)
        n = len(paths)
        out: "list[dict | None]" = [None] * n
        if n == 0:
            return []

        # ① 先按类型分两堆（读 3 字节判断 JPEG，代价可忽略）
        q_jpeg: "queue.Queue" = queue.Queue()
        q_other: "queue.Queue" = queue.Queue()
        for i, p in enumerate(paths):
            try:
                jpeg = is_jpeg(p)
            except Exception:  # noqa: BLE001
                jpeg = False
            (q_jpeg if jpeg else q_other).put((i, p))

        def gpu_worker() -> None:
            pipe = self.pipe
            while True:
                try:
                    i, p = q_jpeg.get_nowait()
                except queue.Empty:
                    return
                t0 = time.time()
                try:
                    out[i] = pipe.analyze_one(p)
                except Exception:  # noqa: BLE001
                    out[i] = self._run_one(p)      # GPU 失败就本线程用 CPU 算
                self._bump("gpu")
                pace(t0)

        def cpu_worker() -> None:
            while True:
                item = None
                # 先吃自己的（非 JPEG），吃完了偷 JPEG
                for q in (q_other, q_jpeg):
                    try:
                        item = q.get_nowait()
                        break
                    except queue.Empty:
                        continue
                if item is None:
                    return
                i, p = item
                if out[i] is None:                 # 可能已被 GPU 抢走
                    out[i] = self._run_one(p)
                    self._bump("cpu")

        def pace(t0: float) -> None:
            """按占用百分比休眠：干 t 秒 → 歇 t*(100-load)/load 秒。"""
            if self.load >= 100:
                return
            work = time.time() - t0
            if work > 0:
                time.sleep(work * (100 - self.load) / self.load)

        g = threading.Thread(target=gpu_worker, name="hybrid-gpu", daemon=True)
        g.start()
        futs = [self.pool.submit(cpu_worker) for _ in range(self.cpu_workers)]
        for f in futs:
            try:
                f.result()
            except Exception:  # noqa: BLE001
                pass
        g.join(timeout=600)

        # 兜底：极端情况下（线程异常）别把 None 交给上层
        for i in range(n):
            if out[i] is None:
                out[i] = self._run_one(paths[i])
        return out

    # ---------------------------------------------------------------- 收尾
    def close(self) -> None:
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
