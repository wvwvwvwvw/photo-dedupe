"""视频/音频分组的"链式合并"回归测试。

把打分函数换成固定矩阵，构造 A~B~C~D 的链：
    A-B 0.95   B-C 0.95   C-D 0.95
    A-C 0.60   A-D 0.40   B-D 0.60
老实现（连通分量）会并成一组，还会显示 0.60 / 0.40 这种低于阈值的相似度；
新实现（星形聚类）必须拆成两组，且每个成员与代表的相似度都 ≥ 阈值。
"""

from __future__ import annotations

import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import grouping as mg  # noqa: E402
from media import audio as A  # noqa: E402
from media import features as F  # noqa: E402
from media import video as V  # noqa: E402

MATRIX = np.array([
    [1.00, 0.95, 0.60, 0.40],
    [0.95, 1.00, 0.95, 0.60],
    [0.60, 0.95, 1.00, 0.95],
    [0.40, 0.60, 0.95, 1.00],
], dtype=np.float32)

THRESHOLD = 0.88
LABELS = ["A", "B", "C", "D"]


def _report(tag: str, groups: list[dict], want_groups: int) -> bool:
    ok = True
    print(f"[{tag}] 阈值 {THRESHOLD}：{len(groups)} 组")
    for g in groups:
        print("    组：" + LABELS[g["rep"] - 1] + " ← " + ", ".join(
            f"{LABELS[m['id'] - 1]}({m['sim']:.2f})" for m in g["members"]))
        for m in g["members"]:
            if m["sim"] < THRESHOLD:
                print(f"    ✗ {LABELS[m['id'] - 1]} 相似度 {m['sim']} 低于阈值")
                ok = False
    if len(groups) != want_groups:
        print(f"  ✗ 期望 {want_groups} 组，实际 {len(groups)} 组")
        ok = False
    return ok


def main() -> int:
    ok = True

    # ---------------- 视频
    keep_v = (V.decode_sig, V.decode_ph, F.mean_vec, F.frame_score, F.min_hamming)
    try:
        V.decode_sig = lambda blob, dim=0: np.array([[float(blob[0])]], np.float32)
        V.decode_ph = lambda blob: []
        # 均值预筛要能通过：给一个固定单位向量（真实实现里是归一化向量）
        F.mean_vec = lambda e: np.array([1.0], np.float32)
        F.min_hamming = lambda a, b: -1
        F.frame_score = lambda a, b: float(MATRIX[int(a[0, 0])][int(b[0, 0])])
        items = [{"id": i + 1, "sig": bytes([i]), "aux": None, "dur": 30.0,
                  "size": 1000 - i, "meta": {"dim": 1}} for i in range(4)]
        ok &= _report("视频", mg.group_video(items, threshold=THRESHOLD)["groups"], 2)
    finally:
        V.decode_sig, V.decode_ph, F.mean_vec, F.frame_score, F.min_hamming = keep_v

    # ---------------- 音频
    keep_a = (A.decode_sig, A.decode_times, A.decode_bounds, A.similarity)
    try:
        # 首位放序号，其余补同一个常数（抽样预筛要求两两有足够多共同哈希）
        A.decode_sig = lambda blob: np.array([blob[0]] + [7] * 199, dtype=np.uint32)
        A.decode_times = lambda blob: np.zeros(200, dtype=np.uint32)
        A.decode_bounds = lambda blob, n=0: np.array([0, 200], dtype=np.uint32)
        A.similarity = lambda a, b: float(MATRIX[int(a[0][0])][int(b[0][0])])
        items = [{"id": i + 1, "sig": bytes([i]), "aux": b"", "aux2": b"",
                  "dur": 300.0, "size": 1000 - i, "meta": {"bitrate": 320}} for i in range(4)]
        ok &= _report("音频", mg.group_audio(items, threshold=THRESHOLD)["groups"], 2)
    finally:
        A.decode_sig, A.decode_times, A.decode_bounds, A.similarity = keep_a

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
