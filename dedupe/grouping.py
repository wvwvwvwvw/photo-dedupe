"""聚类入口：读索引 → 找候选 → 连通分量 → 分页出组 / 按规则选保留。

拆成两步，方便缓存：
* prepare()  算候选对和连通分量，并把分组结构一起缓存（只跟阈值/检索强度有关）；
* render()   统计、排序、分页、取图片详情（很快，切换排序/翻页不用重算）；
* select_members()  按"保留规则"挑出每组的赢家，返回其余应删的下标。
"""

from __future__ import annotations

import time

import numpy as np

from . import index as idx

KEEP_LABELS = {
    "quality": "画质最高",
    "newest": "最新修改",
    "resolution": "分辨率最高",
    "oldest": "最早修改",
    "folders": "按文件夹",
}


# dHash 二次校验的上限：真重复图几乎都 ≤16，pHash 8~10 位那段的误判大多在这里被挡住
DHASH_MAX = 16

# 16x16 灰度指纹的相关系数下限：实测真重复 ≥0.99，同人不同姿势 0.7~0.9
PATCH_CORR_MIN = 0.96


def _patch_corr(pm: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """逐条边算 16x16 灰度指纹的归一化相关系数（向量化）。"""
    a = pm[u].astype(np.float32)
    b = pm[v].astype(np.float32)
    a -= a.mean(axis=1, keepdims=True)
    b -= b.mean(axis=1, keepdims=True)
    na = np.sqrt((a * a).sum(axis=1)) + 1e-6
    nb = np.sqrt((b * b).sum(axis=1)) + 1e-6
    return (a * b).sum(axis=1) / (na * nb)


def _seed_order(data: dict, u, v, n: int):
    """谁先当种子：画质好的优先（大图/清晰），同分再看连边多的。

    种子先挑，后面的只能"加进来"、不能"抢人"，可避免两组互相拉扯。
    """
    try:
        q = _quality_key(data).astype(np.float64)
    except Exception:                                    # noqa: BLE001
        q = np.zeros(n, dtype=np.float64)
    deg = np.zeros(n, dtype=np.int32)
    if len(u):
        np.add.at(deg, u, 1)
        np.add.at(deg, v, 1)
    # 质量为主，度为辅（归一化后相加，避免量纲差）
    qn = (q - q.min()) / max(1e-9, q.max() - q.min())
    dn = deg / max(1, int(deg.max()) if len(deg) else 1)
    score = qn * 0.8 + dn * 0.2
    return np.argsort(-score, kind="stable")


def prepare(data: dict, threshold: int = 10, recall: str = "standard") -> dict:
    t0 = time.time()
    ids = data.get("ids")
    n = 0 if ids is None else len(ids)
    if n == 0:
        return {"n": 0, "labels": np.empty(0, np.int32), "edges": 0,
                "mode": "exact", "elapsed_ms": 0, "recall": recall,
                "order": np.empty(0, np.int64), "starts": np.empty(0, np.int64),
                "counts": np.empty(0, np.int64)}

    ph, phc = data["ph"], data["phc"]
    probes = {"fast": 0, "standard": 1, "full": 2}.get(recall, 1)
    exact = n <= idx.EXACT_LIMIT
    if exact:
        u, v, d = idx.edges_exact(ph, phc, threshold)
        mode = "exact"
    else:
        u, v, d = idx.edges_lsh(ph, phc, threshold, probes=probes, ar=data.get("ar"))
        mode = "lsh"

    # ① 用 dHash 再验一遍：pHash 在 8~10 位这段“看起来像”的很多是误判，
    #    而真重复图的 dHash 几乎都很小。pHash 很近（≤4）就免检，避免误杀。
    dh = data.get("dh")
    if dh is not None and len(u):
        dd = idx.hamming_pairs(dh, u, v)
        keep = (dd <= DHASH_MAX) | (d <= 4)
        u, v, d = u[keep], v[keep], d[keep]

    # ② 灰度指纹二次确认：哈希（pHash/dHash）只能筛出"候选"，
    #    真正像不像要看低频灰度结构。16x16 归一化后求相关系数，
    #    实测真重复 ≥0.99、同一个人不同姿势只有 0.7~0.9，用 0.96 卡很干净。
    pm = data.get("patch")
    if pm is not None and len(u) and pm.shape[1] == 256:
        has = data.get("has_patch")
        keep = np.ones(len(u), dtype=bool)
        if has is not None:
            keep &= has[u] & has[v]
        idx_keep = np.flatnonzero(keep)
        if len(idx_keep):
            corr = _patch_corr(pm, u[idx_keep], v[idx_keep])
            ok = corr >= PATCH_CORR_MIN
            drop = idx_keep[~ok]
            keep[drop] = False
        u, v, d = u[keep], v[keep], d[keep]

    # ③ 用“星形聚类”代替连通分量：每个成员都必须和**种子**相似，
    #    这样 A~B~C 不会把 A 和 C 串成一组（这正是“不同图片被分到一组”的根因）。
    labels = idx.cluster_star(n, u, v, seed_order=_seed_order(data, u, v, n))

    order = np.argsort(labels, kind="stable")
    sorted_lab = labels[order]
    starts = np.flatnonzero(np.r_[True, sorted_lab[1:] != sorted_lab[:-1]])
    counts = np.diff(np.r_[starts, n])
    return {
        "n": n, "labels": labels, "u": u, "v": v, "d": d,
        "edges": int(len(u)), "mode": mode, "recall": recall,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "order": order, "sorted_lab": sorted_lab,
        "starts": starts, "counts": counts,
    }


def _quality_key(data: dict) -> np.ndarray:
    """和打分一致的排序依据（组内归一化不改变顺序，所以直接用加权对数）。"""
    pix = data["pix"].astype(np.float64)
    sharp = np.maximum(data["sharp"].astype(np.float64), 0.0)
    size = data["size"].astype(np.float64)
    bpp = size / np.maximum(pix, 1.0)
    return (0.40 * np.log1p(pix) + 0.45 * np.log1p(sharp) + 0.15 * np.log1p(bpp))


def _keep_key(data: dict, keep: str) -> np.ndarray:
    n = len(data["ids"])
    if keep == "newest":
        return data["mtime"].astype(np.float64)
    if keep == "oldest":
        return -data["mtime"].astype(np.float64)
    if keep == "resolution":
        return data["pix"].astype(np.float64) * 1e6 + data["mtime"].astype(np.float64)
    return _quality_key(data)


def select_members(data: dict, cluster: dict, min_size: int = 2, keep: str = "quality",
                   prefer: np.ndarray | None = None) -> dict:
    """按规则为每组保留一张，返回其余（应该被删除的）图片下标与统计。"""
    n = cluster.get("n", 0)
    if not n:
        return {"idx": np.empty(0, np.int64), "groups": 0, "keep": 0, "bytes": 0}
    order = cluster["order"]
    starts_all = cluster["starts"]
    counts_all = cluster["counts"]
    keep_mask = counts_all >= min_size
    if not keep_mask.any():
        return {"idx": np.empty(0, np.int64), "groups": 0, "keep": 0, "bytes": 0}

    # 只保留"合格组"里的位置（不成组的单张要排除掉）
    gid_of_pos = np.repeat(np.arange(len(starts_all), dtype=np.int64), counts_all)
    in_keep = keep_mask[gid_of_pos]
    pos_sel = order[in_keep]
    gid_sel = gid_of_pos[in_keep]
    counts_sel = counts_all[keep_mask]

    key = _keep_key(data, keep)
    k1 = prefer[pos_sel].astype(np.int8) if prefer is not None else np.zeros(len(pos_sel), np.int8)
    rank_order = np.lexsort((key[pos_sel], k1, gid_sel))
    p2 = pos_sel[rank_order]

    span = np.repeat(np.cumsum(counts_sel) - counts_sel, counts_sel)
    within = np.arange(len(p2), dtype=np.int64) - span
    is_last = within == (counts_sel.repeat(counts_sel) - 1)
    survivors = p2[is_last]
    to_remove = p2[~is_last]
    sizes = data["size"]
    return {
        "idx": to_remove,
        "groups": int(len(counts_sel)),
        "keep": int(len(survivors)),
        "bytes": int(sizes[to_remove].sum()) if len(to_remove) else 0,
    }


def render(store, data: dict, cluster: dict, min_size: int = 2, sort: str = "reclaim",
           limit: int = 60, offset: int = 0, gpu_info: dict | None = None) -> dict:
    t0 = time.time()
    n = cluster.get("n", 0)
    if not n:
        return {
            "groups": [],
            "stats": {"images": 0, "groups": 0, "duplicates": 0, "reclaim": 0, "bytes": 0},
            "page": {"offset": offset, "limit": limit, "total_groups": 0, "has_more": False},
        }

    ids = data["ids"]
    sizes = data["size"]
    labels = cluster["labels"]
    u, v, d = cluster["u"], cluster["v"], cluster["d"]
    order = cluster["order"]
    starts_all = cluster["starts"]
    counts_all = cluster["counts"]

    keep_mask = counts_all >= min_size
    starts = starts_all[keep_mask]
    counts = counts_all[keep_mask]
    roots = cluster["sorted_lab"][starts]
    gcount = len(starts)

    size_sorted = sizes[order].astype(np.int64)
    if gcount:
        total_bytes = np.add.reduceat(size_sorted, starts)
        max_bytes = np.maximum.reduceat(size_sorted, starts)
        reclaim = total_bytes - max_bytes
    else:
        total_bytes = reclaim = np.empty(0, np.int64)

    gmin = np.full(gcount, 99, dtype=np.int16)
    gmax = np.zeros(gcount, dtype=np.int16)
    if len(u) and gcount:
        gidx = np.clip(np.searchsorted(roots, labels[u]), 0, gcount - 1)
        np.minimum.at(gmin, gidx, d)
        np.maximum.at(gmax, gidx, d)
    sim_max = np.where(gmin > 64, 100.0, 100.0 * (1.0 - gmin / 64.0))
    sim_min = np.where(gmax <= 0, 100.0, 100.0 * (1.0 - gmax / 64.0))

    if sort == "count":
        key = counts.astype(np.float64) * 1e12 + reclaim
    elif sort == "sim":
        key = sim_max * 1e12 + counts
    elif sort == "id":
        key = -starts.astype(np.float64)
    else:
        key = reclaim.astype(np.float64) * 1e6 + counts
    rank = np.argsort(-key, kind="stable")

    page_idx = rank[offset:offset + limit]
    has_more = offset + limit < gcount

    page_ids, spans = [], []
    for gi in page_idx:
        s = int(starts[gi])
        c = int(counts[gi])
        spans.append((s, c))
        page_ids.append(ids[order[s:s + c]])
    flat = np.concatenate(page_ids) if page_ids else np.empty(0, np.int64)
    rows = store.get_many([int(x) for x in flat.tolist()])
    # 展示用的记录里不要二进制指纹（JSON 传不出去，也没必要）
    by_id = {}
    for r in rows:
        rec = dict(r)
        rec.pop("patch", None)
        by_id[r["id"]] = rec

    groups = []
    no = offset + 1
    for k, gi in enumerate(page_idx):
        s, c = spans[k]
        members = []
        for pos in range(s, s + c):
            iid = int(ids[order[pos]])
            r = by_id.get(iid)
            if r:
                members.append(r)
        if not members:
            continue
        idx.score_group(members)

        def _rank(im):
            try:
                trimmed = any(float(v) > 0.02 for v in str(im.get("trim") or "").split(","))
            except Exception:
                trimmed = False
            return (0 if trimmed else 1, im.get("score", 0), im.get("size", 0), im.get("mtime", 0))

        members.sort(key=_rank, reverse=True)
        best = members[0]
        for im in members:
            dd = idx.hamming_hex(best["phash"], im["phash"])
            im["dist"] = dd
            im["sim"] = round(100.0 * (1.0 - dd / 64.0), 1)
            im["same_as_best"] = bool(
                im is not best and best.get("sha256") and im.get("sha256")
                and best["sha256"] == im["sha256"]
            )
            im["best"] = im is best
        groups.append({
            "key": f"g{int(roots[gi])}",
            "no": no,
            "count": len(members),
            "exact": sum(1 for i in members[1:] if i["same_as_best"]),
            "reclaim": int(reclaim[gi]),
            "best_size": int(best.get("size") or 0),
            "max_sim": round(float(sim_max[gi]), 1),
            "min_sim": round(float(sim_min[gi]), 1),
            "images": members,
        })
        no += 1

    stats = {
        "images": int(n),
        "groups": int(gcount),
        "duplicates": int(counts.sum() - gcount) if gcount else 0,
        "reclaim": int(reclaim.sum()) if gcount else 0,
        "bytes": int(sizes.sum()),
        "elapsed_ms": int(cluster.get("elapsed_ms", 0)),
        "render_ms": int((time.time() - t0) * 1000),
        "mode": cluster.get("mode", "exact"),
        "edges": cluster.get("edges", 0),
        "recall": cluster.get("recall", "exact"),
    }
    return {"groups": groups, "stats": stats,
            "page": {"offset": offset, "limit": limit,
                     "total_groups": int(gcount), "has_more": bool(has_more)},
            "engine": gpu_info or {}}


def build_groups(store, data: dict, threshold: int = 10, min_size: int = 2,
                 sort: str = "reclaim", limit: int = 60, offset: int = 0,
                 recall: str = "standard", gpu_info: dict | None = None) -> dict:
    cluster = prepare(data, threshold, recall)
    return render(store, data, cluster, min_size, sort, limit, offset, gpu_info)
