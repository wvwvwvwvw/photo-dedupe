"""大规模聚类：分桶检索（LSH）+ 并查集。

小图库（<= 3 万张）仍然做全量两两比较，结果最准；
超过就走"分桶候选"：把 64 位 pHash 切成交错的 12 位段，同段值相同（或只差 1 位）
才可能是重复。这样 1000 万张也只有约 N × 1.5 万次比较，而不是 N²。

代价是概率性：阈值 10 时一次命中的召回率约 94%（可以调 probes 提高，
代价是候选数翻倍）。所有候选都会用完整的 64 位距离复核，不会误判。
"""

from __future__ import annotations

import math
from array import array

import numpy as np

from . import gpu as gpu_mod

EXACT_LIMIT = 30000          # 低于这个数量直接全量比较
BAND_BITS = 12               # 分桶用的位宽（4 段 12 位 = 48 位参与分桶）
BAND_COUNT = 5
BAND_OFFSETS = (0, 12, 24, 36, 48)   # 最后一段只取 4 位，做个兜底


_HAS_BITCOUNT = hasattr(np, "bitwise_count")


def _popcount(x: np.ndarray) -> np.ndarray:
    """按位 1 的个数。numpy 2.x 有 bitwise_count，比查表快约 5 倍。"""
    if _HAS_BITCOUNT:
        return np.bitwise_count(x).astype(np.int32)
    b = np.ascontiguousarray(x).view(np.uint8).reshape(-1, 8)
    return gpu_mod._LUT[b].sum(axis=1, dtype=np.int32)


def pair_dist(a_ph, a_phc, b_ph, b_phc) -> np.ndarray:
    """逐对计算两个哈希变体的最小汉明距离。"""
    d = np.minimum(_popcount(a_ph ^ b_ph), _popcount(a_phc ^ b_phc))
    d = np.minimum(d, _popcount(a_ph ^ b_phc))
    d = np.minimum(d, _popcount(a_phc ^ b_ph))
    return d


# ---------------------------------------------------------------- 小图库：全量

def edges_exact(ph, phc, threshold: int, block: int = 512):
    n = len(ph)
    t = np.uint64(threshold)
    u_list, v_list, d_list = [], [], []
    for s in range(0, n, block):
        e = min(n, s + block)
        a_ph, a_phc = ph[s:e], phc[s:e]
        best = gpu_mod.min4_numpy(a_ph, a_phc, ph, phc)
        rows = np.arange(e - s)[:, None]
        cols = np.arange(n)[None, :]
        cand = (best <= t) & (cols > (rows + s))
        if not cand.any():
            continue
        ii, jj = np.nonzero(cand)
        u_list.append((ii + s).astype(np.int64))
        v_list.append(jj.astype(np.int64))
        d_list.append(best[ii, jj].astype(np.int8))
    if not u_list:
        return (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int8))
    return np.concatenate(u_list), np.concatenate(v_list), np.concatenate(d_list)


# ---------------------------------------------------------------- 大图库：分桶

def _band_keys(ph: np.ndarray) -> np.ndarray:
    """(B, N) 的 uint16 段键。"""
    out = np.empty((BAND_COUNT, len(ph)), dtype=np.uint16)
    for i, off in enumerate(BAND_OFFSETS):
        out[i] = ((ph >> np.uint64(off)) & np.uint64((1 << BAND_BITS) - 1)).astype(np.uint16)
    return out


def _expand_ranges(starts: np.ndarray, lengths: np.ndarray):
    """把 [start, start+len) 的区间展开成下标数组（向量化）。"""
    total = int(lengths.sum())
    if total == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    idx = np.repeat(np.arange(len(lengths)), lengths)
    off = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(lengths) - lengths, lengths)
    return idx, starts[idx] + off


def edges_lsh(ph, phc, threshold: int, probes: int = 1, ar=None):
    """分桶候选检索。

    两种实现，按桶的平均大小自动选：
    * 桶小（图库不大）：逐条查询 + 邻桶探测，灵活、召回高；
    * 桶大（百万级以上）：先按宽高比分片，再按桶整块算密集矩阵。
    """
    n = len(ph)
    est_bucket = max(1, n // (1 << BAND_BITS))
    # 12 万以上、且不是"彻底"模式时走整块矩阵：快 3~17 倍，召回少约 1%
    # （"彻底"保留逐条查询，给需要极限召回的人用）
    if est_bucket >= 30 and probes < 2:
        return _edges_blocks(ph, phc, threshold, ar=ar)
    return _edges_queries(ph, phc, threshold, probes)


def _edges_blocks(ph, phc, threshold: int, ar=None, partitions: int = 2):
    """百万级：宽高比分片 → 桶内密集矩阵（numpy 一次算一片，效率最高）。"""
    n = len(ph)
    t = np.int32(threshold)
    u_parts, v_parts, d_parts = [], [], []

    if ar is None or len(ar) != n:
        shards = [np.arange(n, dtype=np.int64)]
    else:
        # 同一张照片的不同版本几乎总在同一档宽高比里；粗分档不会切碎重复组
        bucket = np.clip(np.floor(np.log2(np.maximum(ar, 1e-6)) * 2.0).astype(np.int32), -8, 8)
        order_s = np.argsort(bucket, kind="stable")
        cut = np.flatnonzero(bucket[order_s][1:] != bucket[order_s][:-1]) + 1
        bounds = np.r_[0, cut, n]
        shards = [order_s[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]

    def run(p_ph, p_phc, index_map):
        for part in range(partitions):
            if part == 0:
                keys = _band_keys(p_ph)
            else:
                rot = (p_ph >> np.uint64(7)) | (p_ph << np.uint64(57))
                keys = _band_keys(rot)
            for b in range(BAND_COUNT):
                key = keys[b]
                o = np.argsort(key, kind="stable")
                sk = key[o]
                cut = np.flatnonzero(sk[1:] != sk[:-1]) + 1
                bounds = np.r_[0, cut, len(sk)]
                for gi in range(len(bounds) - 1):
                    s, e = int(bounds[gi]), int(bounds[gi + 1])
                    m = e - s
                    if m < 2:
                        continue
                    ids = o[s:e]
                    bp, bpc = p_ph[ids], p_phc[ids]
                    dist = gpu_mod.min4_numpy(bp, bpc, bp, bpc)
                    ii, jj = np.nonzero(dist <= t)
                    keep = jj > ii
                    if not keep.any():
                        continue
                    ii, jj = ii[keep], jj[keep]
                    u_parts.append(index_map[ids[ii]])
                    v_parts.append(index_map[ids[jj]])
                    d_parts.append(dist[ii, jj].astype(np.int8))

    for sh in shards:
        if len(sh) < 2:
            continue
        run(ph[sh], phc[sh], sh)

    if not u_parts:
        return (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int8))
    u = np.concatenate(u_parts)
    v = np.concatenate(v_parts)
    d = np.concatenate(d_parts)
    lo = np.minimum(u, v)
    hi = np.maximum(u, v)
    order_k = np.lexsort((hi, lo))
    lo_s, hi_s, d_s = lo[order_k], hi[order_k], d[order_k]
    first = np.r_[True, (lo_s[1:] != lo_s[:-1]) | (hi_s[1:] != hi_s[:-1])]
    return lo_s[first], hi_s[first], d_s[first]


_CAND_CACHE: "dict[tuple, tuple]" = {}
# 缓存里最多保留到这个距离的候选：界面阈值范围是 2~20，超了才需要重建
CAND_CAP = 20


def _cand_key(ph, probes: int) -> tuple:
    import hashlib
    h = hashlib.blake2b(np.ascontiguousarray(ph).tobytes(), digest_size=8).digest()
    return (len(ph), h, int(probes))


def _edges_queries(ph, phc, threshold: int, probes: int = 1, block: int = 256):
    """中小图库：逐条查询（桶很小，整块计算反而浪费）。

    候选对只跟图库内容和探测层数有关，跟阈值无关——所以缓存起来：
    拖动阈值滑块时不用重新算桶，直接拿候选再按阈值筛，秒出。
    """
    n = len(ph)
    ck = _cand_key(ph, probes)
    cached = _CAND_CACHE.get(ck)
    if cached is not None and threshold <= CAND_CAP:
        cu, cv, cd = cached            # 距离已经算好，阈值只做一次比较
        good = cd <= threshold
        if not good.any():
            return (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int8))
        return cu[good], cv[good], cd[good]

    keys = _band_keys(ph)
    order = {}
    skeys = {}
    for b in range(BAND_COUNT):
        o = np.argsort(keys[b], kind="stable")
        order[b] = o
        skeys[b] = keys[b][o]

    # 每段的探测值：自身 + 翻转 1 位/2 位
    masks = [0]
    if probes >= 1:
        masks += [1 << i for i in range(BAND_BITS)]
    if probes >= 2:
        masks += [(1 << i) | (1 << j) for i in range(BAND_BITS) for j in range(i + 1, BAND_BITS)]

    u_parts, v_parts, d_parts = [], [], []
    total_u = 0
    for s in range(0, n, block):
        e = min(n, s + block)
        idx_all, cand_all = [], []
        for b in range(BAND_COUNT):
            q = keys[b][s:e]
            sk = skeys[b]
            for m in masks:
                qv = q ^ np.uint16(m)
                lo = np.searchsorted(sk, qv, side="left")
                hi = np.searchsorted(sk, qv, side="right")
                lengths = hi - lo
                if lengths.sum() == 0:
                    continue
                qi, ci = _expand_ranges(lo, lengths)
                idx_all.append(qi + s)
                cand_all.append(order[b][ci])
        if not idx_all:
            continue
        src = np.concatenate(idx_all)
        dst = np.concatenate(cand_all)
        keep = src < dst                      # 每对只留一次
        src, dst = src[keep], dst[keep]
        if src.size == 0:
            continue
        # 段键相同不代表真的近，用完整距离复核（顺手把距离留着缓存）
        dist = pair_dist(ph[src], phc[src], ph[dst], phc[dst])
        keep = dist <= max(threshold, CAND_CAP)     # 距离太远的对缓存也没意义
        if not keep.any():
            continue
        u_parts.append(src[keep])
        v_parts.append(dst[keep])
        d_parts.append(dist[keep].astype(np.int8))
    if not u_parts:
        empty = np.empty(0, np.int64)
        _CAND_CACHE[ck] = (empty, empty, np.empty(0, np.int8))
        return (empty, empty, np.empty(0, np.int8))
    u = np.concatenate(u_parts)
    v = np.concatenate(v_parts)
    d = np.concatenate(d_parts)
    # 同一对可能被多段命中，去重并保留最小距离
    key = u * np.int64(n) + v
    order_k = np.argsort(key, kind="stable")
    key_s = key[order_k]
    first = np.ones(len(key_s), dtype=bool)
    first[1:] = key_s[1:] != key_s[:-1]
    sel = order_k[first]
    u, v, d = u[sel], v[sel], d[sel]

    # 缓存（只留最近 2 个图库，别把内存撑爆）
    if len(_CAND_CACHE) >= 2:
        _CAND_CACHE.pop(next(iter(_CAND_CACHE)))
    _CAND_CACHE[ck] = (u, v, d)

    good = d <= threshold
    if not good.any():
        return (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0, np.int8))
    return u[good], v[good], d[good]


# ---------------------------------------------------------------- 连通分量

def hamming_pairs(hashes: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """逐条边算汉明距离（用于 dHash 二次校验）。"""
    if len(u) == 0:
        return np.empty(0, np.int32)
    return _popcount(hashes[u] ^ hashes[v]).astype(np.int32)


def cluster_star(n: int, u: np.ndarray, v: np.ndarray, seed_order=None) -> np.ndarray:
    """星形聚类：每个簇都要有一个种子，成员只和种子比较过。

    连通分量（并查集）会把 A~B~C 串成 {A,B,C}，即使 A 与 C 差很远；
    星形聚类要求"每个成员都挨着种子"，簇的直径被限制在 2×阈值 内，
    这正是“完全不同的图片被放进一组”的解药。
    """
    labels = np.full(n, -1, dtype=np.int32)
    if n <= 0:
        return labels
    adj: "list[list[int]]" = [[] for _ in range(n)]
    for a, b in zip(u.tolist(), v.tolist()):
        adj[a].append(b)
        adj[b].append(a)
    order = range(n) if seed_order is None else seed_order.tolist()
    nxt = 0
    for s in order:
        if labels[s] >= 0:
            continue
        labels[s] = nxt
        for nb in adj[s]:
            if labels[nb] < 0:
                labels[nb] = nxt
        nxt += 1
    rest = np.flatnonzero(labels < 0)
    if len(rest):
        labels[rest] = np.arange(nxt, nxt + len(rest), dtype=np.int32)
    return labels


def components(n: int, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """并查集求连通分量，返回每个点的根标签。"""
    if n <= 0:
        return np.empty(0, np.int32)
    parent = array("i", range(n))
    for a, b in zip(u.tolist(), v.tolist()):
        ra = a
        while parent[ra] != ra:
            parent[ra] = parent[parent[ra]]
            ra = parent[ra]
        rb = b
        while parent[rb] != rb:
            parent[rb] = parent[parent[rb]]
            rb = parent[rb]
        if ra != rb:
            parent[rb] = ra
    lab = np.frombuffer(parent.tobytes(), dtype=np.int32).copy()
    # 路径压缩（向量化指针跳跃）
    for _ in range(64):
        nxt = lab[lab]
        if np.array_equal(nxt, lab):
            break
        lab = nxt
    return lab


# ---------------------------------------------------------------- 打分

def _log_norm(vals: np.ndarray) -> np.ndarray:
    x = np.log1p(np.maximum(vals, 0).astype(np.float64))
    lo, hi = float(x.min()), float(x.max())
    if hi - lo < 1e-12:
        return np.full(len(x), 0.5)
    return (x - lo) / (hi - lo)


def score_group(imgs: list[dict]) -> None:
    """组内相对打分：分辨率 40% + 清晰度 45% + 压缩保真 15%。"""
    if not imgs:
        return
    res = _log_norm(np.array([float(i.get("pix") or 0) for i in imgs]))
    sharp = _log_norm(np.array([float(i.get("sharp") or 0) for i in imgs]))
    bpp = np.array([min(float(i.get("size") or 0) / max(1, int(i.get("pix") or 1)), 6.0) for i in imgs])
    comp = _log_norm(bpp)
    for k, im in enumerate(imgs):
        s = 0.40 * float(res[k]) + 0.45 * float(sharp[k]) + 0.15 * float(comp[k])
        # 带留边（白边/黑边）的版本略微降权，优先保留干净的原图
        try:
            if any(float(v) > 0.02 for v in str(im.get("trim") or "").split(",")):
                s *= 0.96
        except Exception:
            pass
        im["score"] = round(s * 100, 1)
        im["parts"] = {"res": round(float(res[k]) * 100, 1),
                       "sharp": round(float(sharp[k]) * 100, 1),
                       "comp": round(float(comp[k]) * 100, 1)}
        im["bpp"] = round(float(bpp[k]), 3)


def hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
