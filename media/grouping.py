"""三类媒体的分组：视频（画面特征）、音频（指纹）、小说（书名相似度）。"""

from __future__ import annotations

import numpy as np

from . import audio as A
from . import config, features, novel as N, video as V


class UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        p = self.p
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _clusters(uf: UnionFind, n: int) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for i in range(n):
        out.setdefault(uf.find(i), []).append(i)
    return {k: v for k, v in out.items() if len(v) >= 2}


# ---------------------------------------------------------------- 视频
def group_video(items: list[dict], threshold: float = config.VIDEO_THRESHOLD,
                **_kw) -> dict:
    vals = [it for it in items if it.get("sig")]
    n = len(vals)
    if n < 2:
        return {"groups": [], "pairs": 0, "stats": {"considered": n}}
    dims = [it["meta"].get("dim") or 0 for it in vals]
    dim = max(dims) if dims else 0
    embs, means = [], []
    for it in vals:
        e = V.decode_sig(it["sig"], dim)
        embs.append(e)
        means.append(features.mean_vec(e))
    m = np.vstack(means).astype(np.float32)

    # 先把"够像"的边算出来（和以前一样：时长、pHash、画面分三重筛）
    score: dict[tuple[int, int], float] = {}
    pairs = 0
    for i in range(n):
        sims = m[i] @ m[i + 1:].T
        cand = np.nonzero(sims > config.VIDEO_PRE)[0] + i + 1
        for j in cand:
            pairs += 1
            if abs((vals[i].get("dur") or 0) - (vals[j].get("dur") or 0)) > max(
                    3.0, 0.03 * (vals[i].get("dur") or 1)):
                continue
            hm = features.min_hamming(V.decode_ph(vals[i]["aux"]), V.decode_ph(vals[j]["aux"]))
            if hm >= 0 and hm > config.VIDEO_PHASH_MAX:
                continue
            s = features.frame_score(embs[i], embs[j])
            if s >= threshold:
                score[(i, j)] = s
                score[(j, i)] = s

    # 星形聚类：每个成员必须**直接**和代表（最长/最大的那个）够像，
    # 不能像以前那样 A~B、B~C 就把 A 和 C 串进同一组（连通分量会滚雪球）。
    order = sorted(range(n), key=lambda k: ((vals[k].get("dur") or 0), vals[k].get("size") or 0),
                   reverse=True)
    members_of: dict[int, list[int]] = {}
    taken = [False] * n
    for seed in order:
        if taken[seed]:
            continue
        taken[seed] = True
        group = []
        for other in order:
            if taken[other]:
                continue
            s = score.get((seed, other))
            if s is not None and s >= threshold:
                taken[other] = True
                group.append(other)
        members_of[seed] = group
    groups = []
    for rep, members in members_of.items():
        if not members:
            continue
        entries = []
        for k in members:
            entries.append({"id": vals[k]["id"], "sim": round(score.get((rep, k), 0.0), 4)})
        entries.sort(key=lambda e: -e["sim"])
        groups.append({"rep": vals[rep]["id"], "members": entries})
    groups.sort(key=lambda g: -len(g["members"]))
    return {"groups": groups, "pairs": pairs, "stats": {"considered": n}}


# ---------------------------------------------------------------- 音频
def group_audio(items: list[dict], threshold: float = config.AUDIO_THRESHOLD,
                **_kw) -> dict:
    vals = [it for it in items if it.get("sig")]
    n = len(vals)
    if n < 2:
        return {"groups": [], "pairs": 0, "stats": {"considered": n}}
    sigs = []
    for it in vals:
        sigs.append((A.decode_sig(it["sig"]), A.decode_times(it["aux"]),
                     A.decode_bounds(it["aux2"], len(A.decode_sig(it["sig"])))))
    durs = [float(it.get("dur") or 0) for it in vals]
    sampled = [s[0][::10] for s in sigs]

    # 先算"够像"的边（时长相近 + 指纹抽样预筛 + 逐段比对）
    score: dict[tuple[int, int], float] = {}
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            d1, d2 = durs[i], durs[j]
            if d1 > 0 and d2 > 0 and abs(d1 - d2) > max(3.0, 0.05 * max(d1, d2)):
                continue
            pairs += 1
            # 先用抽样的哈希估个重合度，太低直接跳过
            if len(sampled[i]) and len(sampled[j]):
                common = np.intersect1d(sampled[i], sampled[j], assume_unique=True)
                if len(common) < max(3, int(0.02 * min(len(sampled[i]), len(sampled[j])))):
                    continue
            sim = A.similarity(sigs[i], sigs[j])
            if sim >= threshold:
                score[(i, j)] = sim
                score[(j, i)] = sim

    # 星形聚类：成员必须直接和代表（码率/体积最大的那个）够像。
    # 之前用连通分量，A~B、B~C 就会把 A 和 C 串进一组，界面上还会显示
    # 一条低于阈值的相似度——和图片查重当年的"完全不同也被分到一组"同因。
    order = sorted(range(n), key=lambda k: ((vals[k]["meta"].get("bitrate") or 0),
                                            vals[k]["size"] or 0), reverse=True)
    members_of: dict[int, list[int]] = {}
    taken = [False] * n
    for seed in order:
        if taken[seed]:
            continue
        taken[seed] = True
        group = []
        for other in order:
            if taken[other]:
                continue
            sim = score.get((seed, other))
            if sim is not None and sim >= threshold:
                taken[other] = True
                group.append(other)
        members_of[seed] = group

    groups = []
    for rep, members in members_of.items():
        if not members:
            continue
        entries = []
        for k in members:
            entries.append({"id": vals[k]["id"], "sim": round(score.get((rep, k), 0.0), 4)})
        entries.sort(key=lambda e: -e["sim"])
        groups.append({"rep": vals[rep]["id"], "members": entries})
    groups.sort(key=lambda g: -len(g["members"]))
    return {"groups": groups, "pairs": pairs, "stats": {"considered": n}}


# ---------------------------------------------------------------- 小说
def group_novel(items: list[dict], threshold: float = config.NOVEL_NAME_THRESHOLD,
                content_threshold: float = 0.88, content: bool = True) -> dict:
    """按**书名相似度**成组；正文指纹只用来标记组内谁和谁真的重复。"""
    vals = list(items)
    n = len(vals)
    if n < 2:
        return {"groups": [], "pairs": 0, "stats": {"considered": n}}
    names = [it["meta"].get("name_norm") or N.normalize_name(it["path"]) for it in vals]
    sigs = [int.from_bytes(it["sig"], "little") if it.get("sig") else 0 for it in vals]
    words = [int(it["meta"].get("words") or 0) for it in vals]
    lens = [len(x) for x in names]

    # 书名分桶 → 只比同桶，避免 O(N²)
    cand: set[tuple[int, int]] = set()
    buckets: dict[str, list[int]] = {}
    for i, nm in enumerate(names):
        buckets.setdefault(nm[:2] if len(nm) >= 2 else nm, []).append(i)
    for ids in buckets.values():
        if len(ids) > 5000:
            continue
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                cand.add((ids[a], ids[b]))

    # 书名相似度只当"候选"，真正成组要求每个成员都直接贴着代表（星形聚类）。
    # 以前用连通分量，会出现 A 像 B、B 像 C，于是 A 和 C 也被塞进一组，
    # 界面上还会显示 0.66 这种低于阈值的相似度。
    ns: dict[tuple[int, int], float] = {}
    pairs = 0
    for i, j in cand:
        if abs(lens[i] - lens[j]) > 8:
            continue
        pairs += 1
        v = N.name_similarity(names[i], names[j])
        if v >= threshold:
            ns[(i, j)] = v
            ns[(j, i)] = v

    order = sorted(range(n), key=lambda k: (vals[k]["meta"].get("chapters") or 0,
                                            vals[k]["size"] or 0), reverse=True)
    taken = [False] * n
    members_of: dict[int, list[int]] = {}
    for seed in order:
        if taken[seed]:
            continue
        taken[seed] = True
        group = []
        for other in order:
            if taken[other]:
                continue
            v = ns.get((seed, other))
            if v is not None and v >= threshold:
                taken[other] = True
                group.append(other)
        members_of[seed] = group

    groups = []
    for rep, members in members_of.items():
        if not members:
            continue
        entries = []
        for k in members:
            if k == rep:
                continue
            ns = N.name_similarity(names[rep], names[k])
            cs = (N.content_similarity(sigs[rep], sigs[k])
                  if (content and sigs[rep] and sigs[k]) else 0.0)
            wr = (abs(words[rep] - words[k]) / float(max(words[rep], words[k], 1))
                  if (words[rep] and words[k]) else 1.0)
            same = bool(content and cs >= content_threshold and wr <= 0.20)
            entries.append({"id": vals[k]["id"], "sim": round(ns, 4),
                            "content_sim": round(cs * 100, 1), "by_content": same,
                            "has_sig": bool(sigs[k])})
        # 正文确认重复的排前面
        entries.sort(key=lambda e: (not e["by_content"], -e["content_sim"], -e["sim"]))
        groups.append({"rep": vals[rep]["id"], "members": entries})
    groups.sort(key=lambda g: -len(g["members"]))
    return {"groups": groups, "pairs": pairs, "stats": {"considered": n}}


def group_quick(kind: str, items: list[dict], min_size: int = 2) -> dict:
    """扫描过程中的"先看一眼"：只按能 O(N) 判断的条件成组。

    - 小说：规范化书名完全相同
    - 视频/音频：文件大小完全相同（字节级重复的必要条件）
    """
    n = len(items)
    keyf = None
    if kind == "novel":
        def keyf(it):
            return it["meta"].get("name_norm") or N.normalize_name(it["path"])
    else:
        def keyf(it):
            return int(it.get("size") or 0)

    buckets: dict = {}
    for it in items:
        k = keyf(it)
        if k in (0, "", None):
            continue
        buckets.setdefault(k, []).append(it)

    groups = []
    for members in buckets.values():
        if len(members) < min_size:
            continue
        rep = max(members, key=lambda x: (x["meta"].get("chapters") or 0, x.get("size") or 0))
        entries = []
        for it in members:
            if it["id"] == rep["id"]:
                continue
            ns = 1.0 if kind == "novel" else 0.0
            entries.append({"id": it["id"], "sim": ns, "content_sim": 0.0,
                            "by_content": False})
        groups.append({"rep": rep["id"], "members": entries})
    groups.sort(key=lambda g: -len(g["members"]))
    return {"groups": groups, "pairs": 0, "stats": {"considered": n, "quick": True}}


def build(kind: str, items: list[dict], **kw) -> dict:
    if kw.pop("quick", False):
        return group_quick(kind, items, int(kw.pop("min_size", 2) or 2))
    kw.pop("min_size", None)
    if kind == "video":
        return group_video(items, **kw)
    if kind == "audio":
        return group_audio(items, **kw)
    if kind == "novel":
        return group_novel(items, **kw)
    raise ValueError(f"未知类型 {kind}")
