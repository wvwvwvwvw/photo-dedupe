"""分桶检索（LSH）自检：召回率、耗时、内存。

造 N 张随机哈希，再埋入若干"只差 k 位"的重复对，
看分桶检索能不能把它们找出来、速度如何。
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dedupe import index as idx  # noqa: E402


def make_data(n=100000, planted=2000, seed=7, tmax=12):
    rng = np.random.default_rng(seed)
    ph = rng.integers(0, np.iinfo(np.uint64).max, n, dtype=np.uint64)
    ph = np.array(ph, dtype=np.uint64)
    phc = ph.copy()
    # 宽高比：真实照片多在 0.5~2.5 之间
    ar = np.exp(rng.uniform(np.log(0.5), np.log(2.5), n)).astype(np.float32)
    # 埋入重复对：随机翻转 4..tmax 位（克隆沿用原图的宽高比）
    base = rng.choice(n, planted, replace=False)
    dist = rng.integers(4, tmax + 1, planted)
    new = []
    for i, b in enumerate(base):
        v = int(ph[b])
        for bit in rng.choice(64, int(dist[i]), replace=False):
            v ^= (1 << int(bit))
        new.append(np.uint64(v))
    ph = np.concatenate([ph, np.array(new, dtype=np.uint64)])
    phc = np.concatenate([phc, np.array(new, dtype=np.uint64)])
    ar = np.concatenate([ar, ar[base]])
    pairs = []
    for i, b in enumerate(base):
        a, c = int(b), n + i
        pairs.append((min(a, c), max(a, c)))
    return ph, phc, ar, pairs, np.array(dist)


def recall_of(u, v, pairs):
    got = set()
    for a, b in zip(u.tolist(), v.tolist()):
        got.add((min(a, b), max(a, b)))
    want = set(pairs)
    hit = len(got & want)
    return hit / max(1, len(want)), len(got - want)


def main():
    n, planted = 100000, 2000
    ph, phc, ar, pairs, dist = make_data(n, planted)
    total = len(ph)
    inside = [p for p, d in zip(pairs, dist) if d <= 10]
    print(f"数据：{total} 张（埋了 {planted} 对，其中 {len(inside)} 对在阈值 10 内）")

    for probes in (0, 1):
        t0 = time.time()
        u, v, d = idx.edges_lsh(ph, phc, 10, probes=probes, ar=ar)
        dt = time.time() - t0
        rec, extra = recall_of(u, v, inside)
        print(f"  逐条 probes={probes}: 命中 {len(u):>6} 对, 召回 {rec*100:5.1f}%, "
              f"多余 {extra:>4}, 耗时 {dt:5.1f}s ({total/dt:,.0f} 张/秒)")

    # 桶变大后走"整块 + 宽高比分片"实现
    n2 = 500000
    ph2, phc2, ar2, pairs2, dist2 = make_data(n2, 3000, seed=11)
    inside2 = [p for p, d in zip(pairs2, dist2) if d <= 10]
    t0 = time.time()
    u2, v2, d2 = idx._edges_blocks(ph2, phc2, 10, ar=ar2)
    dt = time.time() - t0
    rec2, extra2 = recall_of(u2, v2, inside2)
    print(f"  整块 {n2:,} 张: 命中 {len(u2):>7} 对, 召回 {rec2*100:5.1f}%, "
          f"多余 {extra2:>5}, 耗时 {dt:5.1f}s ({len(ph2)/dt:,.0f} 张/秒)")

    # 小规模下和全量比较对拍
    sub = 6000
    t0 = time.time()
    ue, ve, de = idx.edges_exact(ph[:sub], phc[:sub], 10)
    t_exact = time.time() - t0
    t0 = time.time()
    ul, vl, dl = idx.edges_lsh(ph[:sub], phc[:sub], 10, probes=1, ar=ar[:sub])
    t_lsh = time.time() - t0
    se = {(int(a), int(b)) for a, b in zip(ue, ve)}
    sl = {(int(a), int(b)) for a, b in zip(ul, vl)}
    print(f"\n{ sub } 张对照：全量 {len(se)} 对 ({t_exact:.1f}s) / 分桶 {len(sl)} 对 ({t_lsh:.1f}s)，"
          f"完全一致={se == sl}")

    lab = idx.components(sub, ul, vl)
    print(f"连通分量：{len(set(lab.tolist()))} 个")


if __name__ == "__main__":
    main()
