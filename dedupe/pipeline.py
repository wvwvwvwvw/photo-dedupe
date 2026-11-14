"""计算引擎：GPU 优先，CPU 兜底。

对外只有一个接口 `analyze_batch(paths) -> [record]`：
* GPU 引擎：JPEG 走 nvJPEG 解码 + 全程显存计算；
* CPU 引擎：Pillow 解码 + 多线程 numpy；
* 单张出错不影响整批（返回 `{"path":.., "error":..}`），由扫描任务记账。
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .gpupipe import GpuPipeline, _probe_torch
from . import gpupipe as gpupipe
from .hybrid import HybridEngine
from .hashing import analyze_image


class CpuEngine:
    kind = "cpu"

    def __init__(self, workers: int | None = None, load: int = 100):
        self.workers = workers or min(8, os.cpu_count() or 4)
        self.load = _clamp_load(load)
        suffix = "" if self.load >= 100 else f"，限速 {self.load}%"
        self.name = f"CPU ×{self.workers}{suffix}"
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hash")

    def analyze_batch(self, paths):
        futs = [self.pool.submit(analyze_image, p) for p in paths]
        out = []
        for p, f in zip(paths, futs):
            try:
                out.append(f.result())
            except Exception as exc:
                out.append({"path": p, "error": str(exc)})
        return out

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


class GpuEngine:
    kind = "gpu"

    def __init__(self, batch: int = 8, device: str = "cuda", cpu_workers: int = 4,
                 load: int = 100):
        self.pipe = GpuPipeline(device=device, batch=batch)
        self.load = _clamp_load(load)
        self.name = f"GPU {self.pipe.name}"
        self.batch = batch
        # 非 JPEG（PNG/WebP/HEIC…）GPU 解不了，交给线程池并行，别拖慢 GPU 主循环
        self._pool = ThreadPoolExecutor(max_workers=max(2, cpu_workers),
                                        thread_name_prefix="gpu-fallback")

    def _throttle(self, t0: float) -> None:
        """按占用百分比限速：干 t 秒活就歇 t*(100-load)/load 秒。"""
        if self.load >= 100:
            return
        work = time.time() - t0
        if work > 0:
            time.sleep(work * (100 - self.load) / self.load)

    def analyze_batch(self, paths):
        from .formats import is_jpeg
        t_start = time.time()
        out: list = [None] * len(paths)
        futs = []
        for i, p in enumerate(paths):
            if is_jpeg(p):
                try:
                    out[i] = self.pipe.analyze_one(p)
                except Exception as exc:
                    out[i] = {"path": p, "error": str(exc)}
            else:
                futs.append((i, p, self._pool.submit(analyze_image, p)))
        for i, p, f in futs:
            try:
                out[i] = f.result()
            except Exception as exc:
                out[i] = {"path": p, "error": str(exc)}
        return out

    def close(self):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass


class EngineInfo:
    def __init__(self):
        self.lock = threading.Lock()
        self.kind = "cpu"
        self.name = ""
        self.reason = "尚未初始化"
        self.mode = "auto"
        self.gpu_probe = {"ok": False, "reason": "未检测"}
        self.verify = None          # 与 CPU 结果的一致性抽样
        self.speed = None           # 实测吞吐（张/秒）

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "kind": self.kind, "name": self.name, "mode": self.mode,
                "reason": self.reason, "gpu": self.gpu_probe,
                "verify": self.verify, "speed": self.speed,
                "backend": gpu_backend(), "dml": dml_available(),
            }


INFO = EngineInfo()


def probe_async():
    """后台探测 GPU（导入 torch 可能要几秒，不阻塞启动）。"""
    ok, desc = _probe_torch()
    with INFO.lock:
        INFO.gpu_probe = {"ok": ok, "reason": desc}
        if ok and INFO.kind == "cpu":
            INFO.kind = "gpu"
            INFO.name = f"GPU {desc}"
            INFO.reason = "GPU 已就绪，首次扫描时启用"
        elif not ok:
            INFO.reason = f"未启用 GPU（{desc}），使用 CPU"


def gpu_backend() -> str:
    """当前用哪个后端：cuda / dml / cpu（探测后才有意义）。"""
    return getattr(gpupipe, "GPU_DEVICE", "cpu")


def gpu_backend_name() -> str:
    return getattr(gpupipe, "GPU_NAME", "")


def dml_available() -> bool:
    """装了 torch_directml 并且能拿到设备吗（AMD/Intel 的备用方案）。"""
    try:
        return gpupipe._dml_module() is not None
    except Exception:                                  # noqa: BLE001
        return False


def gpu_available() -> tuple[bool, str]:
    ok, desc = _probe_torch()
    return ok, desc


def _cpu_workers_ok(workers: int | None) -> bool:
    if workers is not None and int(workers) <= 0:
        return False
    return True


def _hybrid_cpu_workers(workers: int | None, load: int = 100) -> int:
    """混合模式给 CPU 分多少线程：留 1~2 个核喂显卡，默认 8 但不超过核数-2。"""
    total = os.cpu_count() or 4
    cap = max(2, total - 2)
    if workers:
        base = max(2, min(int(workers), cap))
    else:
        base = max(2, min(8, cap))
    return max(1, int(round(base * _clamp_load(load) / 100.0)))


def _clamp_load(load) -> int:
    try:
        v = int(load)
    except (TypeError, ValueError):
        return 100
    return max(5, min(100, v))


def _cpu_workers_for(workers: int | None, load: int = 100) -> int:
    """按占用百分比算 CPU 线程数。"""
    total = os.cpu_count() or 4
    if workers:
        base = int(workers)
    else:
        base = max(2, min(8, total - 2))
    return max(1, min(total, int(round(base * _clamp_load(load) / 100.0)))) or 1


def make_engine(mode: str = "auto", workers: int | None = None,
                sample_paths: list[str] | None = None,
                load: int = 100) -> CpuEngine | GpuEngine:
    """mode: auto（有 GPU 就用，但实测 CPU 明显更快时选 CPU）/ gpu（强制）/ cpu。"""
    mode = (mode or "auto").lower()
    ok, desc = gpu_available()
    with INFO.lock:
        INFO.gpu_probe = {"ok": ok, "reason": desc}
        INFO.mode = mode

    if mode == "cpu":
        eng = CpuEngine(_cpu_workers_for(workers, load), load=load)
        INFO.kind, INFO.name = "cpu", eng.name
        INFO.reason = "使用 CPU（来自设置或命令行 --engine cpu）"
        return eng

    if not ok:
        eng = CpuEngine(_cpu_workers_for(workers, load), load=load)
        INFO.kind, INFO.name = "cpu", eng.name
        INFO.reason = f"未启用 GPU（{desc}），使用 CPU"
        return eng

    try:
        gpu_eng = GpuEngine(batch=8, device="auto", load=load)
    except Exception as exc:
        eng = CpuEngine(_cpu_workers_for(workers, load), load=load)
        INFO.kind, INFO.name = "cpu", eng.name
        INFO.reason = f"GPU 初始化失败（{exc}），已回退 CPU"
        return eng

    # 强制 GPU：不再比较，直接返回（仍然做一致性校验）
    if mode == "gpu":
        if sample_paths:
            v = verify_engine(gpu_eng, sample_paths)
            INFO.verify = v
            if _hash_mismatch(v):
                INFO.reason = f"注意：GPU 与 CPU 结果差异 {v['max_dist']} 位（已强制使用 GPU）"
            else:
                INFO.reason = "GPU 加速已启用（手动指定）"
        INFO.kind, INFO.name = "gpu", gpu_eng.name
        return gpu_eng

    # auto：GPU 和 CPU 都可用 → 混合引擎（两边同时跑，work stealing 自动均摊）
    if _cpu_workers_ok(workers):
        try:
            hy = HybridEngine(cpu_workers=_hybrid_cpu_workers(workers, load),
                              batch=8, device="auto", load=load)
            v = verify_engine(hy, sample_paths) if sample_paths else None
            INFO.verify = v
            if v is not None and _hash_mismatch(v):
                hy.close()
                INFO.reason = f"混合引擎与 CPU 结果差异过大（{v.get('max_dist')} 位），改用纯 CPU"
                eng = CpuEngine(_cpu_workers_for(workers, load), load=load)
                INFO.kind, INFO.name = "cpu", eng.name
                return eng
            INFO.kind, INFO.name = "hybrid", hy.name
            INFO.reason = "CPU + 显卡混合（两边同时算，自动按速度分活）"
            return hy
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            eng = CpuEngine(workers)
            INFO.kind, INFO.name = "cpu", eng.name
            INFO.reason = f"混合引擎初始化失败（{type(exc).__name__}: {exc}），用纯 CPU"
            return eng

    # auto：只有 GPU（CPU 线程数被设成 0）
    sample = [p for p in (sample_paths or []) if p][:12]
    if len(sample) < 2:
        INFO.kind, INFO.name = "gpu", gpu_eng.name
        INFO.reason = "GPU 加速已启用（无样本可对比）"
        return gpu_eng

    cpu_eng = CpuEngine(workers)
    try:
        gpu_eng.analyze_batch(sample[:2])      # 预热，避开首次编译
        t0 = time.time()
        gpu_eng.analyze_batch(sample)
        t_gpu = time.time() - t0
        t0 = time.time()
        cpu_eng.analyze_batch(sample)
        t_cpu = time.time() - t0
    except Exception as exc:
        cpu_eng.close()
        INFO.reason = f"GPU 基准测试失败（{exc}），改用 CPU"
        eng = CpuEngine(workers)
        INFO.kind, INFO.name = "cpu", eng.name
        return eng

    s_gpu = len(sample) / max(1e-6, t_gpu)
    s_cpu = len(sample) / max(1e-6, t_cpu)
    cpu_eng.close()
    with INFO.lock:
        INFO.speed = {"gpu": round(s_gpu, 1), "cpu": round(s_cpu, 1)}

    v = verify_engine(gpu_eng, sample)
    INFO.verify = v
    if _hash_mismatch(v):
        INFO.kind, INFO.name = "cpu", "CPU"
        INFO.reason = f"GPU 与 CPU 结果差异过大（{v['max_dist']} 位），已回退 CPU"
        return CpuEngine(workers)

    # 实测谁快用谁：GPU 只有在明显更快（≥1.05×）时才用，否则用 CPU
    # （4K JPEG 走 PIL draft 只解 1/8，nvJPEG 只能全分辨率解，很多图上 CPU 反而更快）
    if s_gpu >= s_cpu * 1.05:
        INFO.kind, INFO.name = "gpu", gpu_eng.name
        INFO.reason = (f"GPU 加速已启用（实测 {s_gpu:.0f} 张/秒，"
                       f"CPU {s_cpu:.0f} 张/秒）")
        return gpu_eng
    gpu_eng.close()
    eng = CpuEngine(workers)
    INFO.kind, INFO.name = "cpu", eng.name
    INFO.reason = (f"用 CPU（实测 {s_cpu:.0f} 张/秒，GPU 只有 {s_gpu:.0f} 张/秒；"
                   "大尺寸 JPEG 走 draft 缩略解码更快，加 --engine gpu 可强制显卡）")
    return eng


def verify_engine(eng, sample_paths: list[str], limit: int = 6) -> dict:
    """比较 GPU/混合 与纯 CPU 在同一批图上的哈希差异（分 dHash / pHash 统计）。"""
    paths = [p for p in sample_paths if p][:limit]
    if not paths:
        return {"checked": 0, "max_dist": 0, "mean_dist": 0,
                "max_dhash": 0, "max_phash": 0}
    try:
        eng.analyze_batch([paths[0]])  # 预热，别把首次编译算进耗时
    except Exception:
        pass
    dists: "list[int]" = []
    d_dists: "list[int]" = []
    p_dists: "list[int]" = []
    t_gpu = t_cpu = 0.0
    for path in paths:
        t0 = time.time()
        a = eng.analyze_batch([path])[0]
        t_gpu += time.time() - t0
        t1 = time.time()
        b = analyze_image(path)
        t_cpu += time.time() - t1
        if "error" in a or "error" in b:
            continue
        if a.get("dhash") and b.get("dhash"):
            dd = bin(int(a["dhash"], 16) ^ int(b["dhash"], 16)).count("1")
            d_dists.append(dd)
            dists.append(dd)
        if a.get("phash") and b.get("phash"):
            dp = bin(int(a["phash"], 16) ^ int(b["phash"], 16)).count("1")
            p_dists.append(dp)
            dists.append(dp)
    return {
        "checked": len(paths),
        "max_dist": max(dists) if dists else 0,
        "mean_dist": round(sum(dists) / len(dists), 2) if dists else 0,
        "max_dhash": max(d_dists) if d_dists else 0,
        "max_phash": max(p_dists) if p_dists else 0,
        "gpu_ms": round(t_gpu / max(1, len(paths)) * 1000),
        "cpu_ms": round(t_cpu / max(1, len(paths)) * 1000),
    }


def _hash_mismatch(v: dict) -> bool:
    """GPU/混合 与 CPU 的哈希差太多 → 说明引擎路径有问题（不是精度问题）。"""
    return (v.get("max_dhash", 0) > 12 or v.get("max_phash", 0) > 12
            or v.get("mean_dist", 0) > 4)


def measure(paths: list[str], eng, limit: int = 8) -> float:
    """粗测吞吐（张/秒），用于界面显示。"""
    paths = paths[:limit]
    if not paths:
        return 0.0
    t0 = time.time()
    eng.analyze_batch(paths)
    dt = max(1e-6, time.time() - t0)
    return round(len(paths) / dt, 1)
