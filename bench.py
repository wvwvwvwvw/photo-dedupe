#!/usr/bin/env python3
"""算力测速：在本机实测 GPU / CPU（不同线程数）的吞吐，把推荐配置写进 settings.json。

用法：
    python bench.py            # 只测速、只打印
    python bench.py --write    # 测完写进 settings.json（下次扫描按这个跑）
    python bench.py --samples 24

换电脑、换显卡、换硬盘之后跑一次就行，不需要每次扫描都测。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from dedupe.runtime import reexec_with_gpu_python  # noqa: E402


def load_settings() -> dict:
    from app import DEFAULT_SETTINGS, SETTINGS_PATH
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            data.update({k: v for k, v in got.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return data


def save_settings(data: dict):
    from app import SETTINGS_PATH
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def collect_samples(n: int) -> tuple[list[str], str]:
    """优先用索引库里的真实照片，其次用演示图集。"""
    db = os.path.join(BASE, "data", "library.db")
    picked: list[str] = []
    if os.path.exists(db):
        try:
            from dedupe.store import Store
            st = Store(db)
            rows = st._conn.execute(
                "SELECT path FROM images WHERE status='ok' ORDER BY RANDOM() LIMIT ?",
                (n * 3,)).fetchall()
            st.close()
            for r in rows:
                if os.path.exists(r["path"]):
                    picked.append(r["path"])
                if len(picked) >= n:
                    break
        except Exception:
            picked = []
    if len(picked) >= 4:
        return picked, "索引库里的真实照片"
    demo = os.path.join(BASE, "_test", "imgs")
    if os.path.isdir(demo):
        files = [os.path.join(demo, f) for f in sorted(os.listdir(demo))
                 if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png", ".webp")]
        if files:
            return (files * 3)[:max(n, 12)], "演示图集（扫描过后会用你自己的照片）"
    return [], ""


def run_cpu(samples: list[str], workers: int) -> float:
    from dedupe.pipeline import CpuEngine
    eng = CpuEngine(workers)
    try:
        eng.analyze_batch(samples[:2])       # 预热
        t0 = time.time()
        eng.analyze_batch(samples)
        return len(samples) / max(1e-6, time.time() - t0)
    finally:
        eng.close()


def run_gpu(samples: list[str]) -> tuple[float, str]:
    from dedupe.pipeline import GpuEngine
    eng = GpuEngine()
    try:
        eng.analyze_batch(samples[:2])
        t0 = time.time()
        eng.analyze_batch(samples)
        return len(samples) / max(1e-6, time.time() - t0), eng.name
    finally:
        eng.close()


def run_hybrid(samples: list[str], cpu_workers: int) -> tuple[float, str]:
    from dedupe.hybrid import HybridEngine
    eng = HybridEngine(cpu_workers=cpu_workers)
    try:
        eng.analyze_batch(samples[:2])
        best = 0.0
        for _ in range(2):
            t0 = time.time()
            eng.analyze_batch(samples)
            best = max(best, len(samples) / max(1e-6, time.time() - t0))
        return best, eng.name
    finally:
        eng.close()


def main():
    ap = argparse.ArgumentParser(description="本机算力测速")
    ap.add_argument("--samples", type=int, default=24, help="每种配置测多少张")
    ap.add_argument("--write", action="store_true", help="把推荐配置写进 settings.json")
    ap.add_argument("--no-reexec", action="store_true", help="不要自动切换到 GPU Python")
    args = ap.parse_args()

    if not args.no_reexec:
        reexec_with_gpu_python()

    from dedupe.pipeline import gpu_available
    ok, desc = gpu_available()
    ncpu = os.cpu_count() or 4

    samples, source = collect_samples(args.samples)
    if not samples:
        print("没有可用的测试图片：先扫描一个文件夹，或把一些图片放进 _test/imgs。")
        return 1
    print("=" * 62)
    print("  本机算力测速")
    print("=" * 62)
    print(f"  样本：{len(samples)} 张（{source}）")
    print(f"  CPU：{ncpu} 逻辑核心    显卡：{('可用 · ' + desc) if ok else ('不可用 · ' + desc)}")
    print()

    cpu_results = []
    cands = sorted({w for w in (1, 2, 4, 6, 8, 12, 16, 24, 32) if w <= max(4, ncpu * 2)})
    for w in cands:
        try:
            best = 0.0
            for _ in range(2):              # 跑两轮取最好，抵消抖动
                best = max(best, run_cpu(samples, w))
            cpu_results.append((w, best))
            print(f"  CPU {w:>2} 线程            {best:8.1f} 张/秒   ({1000 / best:6.1f} ms/张)")
        except Exception as exc:
            print(f"  CPU {w:>2} 线程            失败：{exc}")
    if not cpu_results:
        print("  CPU 测速失败")
        return 1
    best_workers, best_cpu = max(cpu_results, key=lambda x: x[1])

    gpu_rate, gpu_name = 0.0, ""
    if ok:
        try:
            from dedupe.pipeline import GpuEngine
            eng = GpuEngine()
            gpu_name = eng.name
            try:
                eng.analyze_batch(samples[:2])          # 预热
                best = 0.0
                for _ in range(2):
                    t0 = time.time()
                    eng.analyze_batch(samples)
                    best = max(best, len(samples) / max(1e-6, time.time() - t0))
                gpu_rate = best
            finally:
                eng.close()
            print(f"  {gpu_name[:22]:<22} {gpu_rate:8.1f} 张/秒   ({1000 / gpu_rate:6.1f} ms/张)")
        except Exception as exc:
            print(f"  GPU                    失败：{exc}")

    hy_rate, hy_name = 0.0, ""
    if ok and best_workers >= 2:
        try:
            hy_rate, hy_name = run_hybrid(samples, min(8, best_workers))
            print(f"  {('混合 ' + hy_name)[:22]:<22} {hy_rate:8.1f} 张/秒   ({1000 / hy_rate:6.1f} ms/张)")
        except Exception as exc:
            print(f"  混合引擎                失败：{exc}")

    print()
    rates = {"hybrid": hy_rate, "gpu": gpu_rate, "cpu": best_cpu}
    best_kind = max(rates, key=lambda k: rates[k])
    if best_kind == "hybrid":
        rec_engine = "hybrid"
        rec_text = f"CPU+GPU 混合（{hy_rate:.0f} 张/秒，比纯 CPU 快 {hy_rate / best_cpu:.2f} 倍）"
    elif best_kind == "gpu":
        rec_engine, rec_text = "gpu", f"GPU（比 CPU 快 {gpu_rate / best_cpu:.2f} 倍）"
    elif gpu_rate > 0:
        rec_engine, rec_text = "cpu", f"CPU（比 GPU 快 {best_cpu / gpu_rate:.2f} 倍）"
    else:
        rec_engine, rec_text = "cpu", "CPU（本机没有可用显卡）"
    print(f"  推荐：{rec_text}")
    print(f"        engine={rec_engine}, workers={best_workers}")
    print()

    if args.write:
        s = load_settings()
        s["engine"] = "auto" if rec_engine == "hybrid" else rec_engine
        s["workers"] = best_workers
        s["bench"] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "samples": len(samples),
            "source": source,
            "cpu": {str(w): round(r, 1) for w, r in cpu_results},
            "gpu": round(gpu_rate, 1) if gpu_rate else 0,
            "gpu_name": gpu_name,
            "recommended": rec_engine,
            "recommended_workers": best_workers,
        }
        save_settings(s)
        print(f"  已写入 settings.json：engine={rec_engine}, workers={best_workers}")
        print("  下次开始扫描时生效。")
    else:
        print("  （加 --write 可以把推荐值写进 settings.json）")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
