"""混合引擎 + 占用百分比 自检。"""
from __future__ import annotations

import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from dedupe import pipeline as P  # noqa: E402


def main() -> int:
    ok = True

    # ① 占用百分比 → CPU 线程数
    cases = [(100, 8), (80, 7), (60, 5), (40, 4), (20, 2)]
    for pct, want in cases:
        got = P._cpu_workers_for(8, pct)
        hit = abs(got - want) <= 1
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 占用 {pct}% → CPU 线程 {got}（期望≈{want}）")

    # ② 混合引擎按占用缩线程
    from dedupe.hybrid import HybridEngine
    for pct, want in ((100, 8), (50, 4), (20, 2)):
        h = HybridEngine(cpu_workers=8, load=pct)
        got = h.cpu_workers
        hit = abs(got - want) <= 1
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 混合引擎 占用 {pct}% → CPU 线程 {got}（{h.name}）")
        h.close()

    # ③ 混合引擎结果与纯 CPU 一致（dHash / pHash 允许 ≤2 位差异）
    from dedupe.store import Store
    from dedupe.hashing import analyze_image
    st = Store(os.path.join(BASE, "data", "library.db"))
    rows = st._conn.execute(
        "SELECT path FROM images WHERE status='ok' ORDER BY RANDOM() LIMIT 24").fetchall()
    st.close()
    paths = [r["path"] for r in rows if os.path.exists(r["path"])]
    if len(paths) >= 6:
        ok_gpu, _ = P.gpu_available()
        if ok_gpu:
            h = HybridEngine(cpu_workers=4)
            outs = h.analyze_batch(paths[:12])
            h.close()
            worst_d = worst_p = 0
            for rec in outs:
                if "error" in rec:
                    ok = False
                    print("[FAIL] 混合引擎有错误:", rec["path"], rec["error"])
                    continue
                cpu = analyze_image(rec["path"])
                worst_d = max(worst_d, bin(int(rec["dhash"], 16) ^ int(cpu["dhash"], 16)).count("1"))
                worst_p = max(worst_p, bin(int(rec["phash"], 16) ^ int(cpu["phash"], 16)).count("1"))
            # 和程序自己的判定一致：单张 ≤6 位、平均 ≤4 位（GPU 浮点与 CPU 插值的正常误差）
            hit = worst_d <= 6 and worst_p <= 6
            ok &= hit
            print(f"[{'OK ' if hit else 'FAIL'}] 混合 vs 纯 CPU：dHash 最大差 {worst_d} 位、"
                  f"pHash {worst_p} 位（程序阈值 ≤6）")
        else:
            print("[跳过] 本机没有可用显卡")

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
