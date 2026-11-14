"""音频指纹（自研，思路同 Shazam）：频谱峰值配对 → 32 位哈希 → 偏移投票比对。

不依赖 chromaprint/外部库，只需要 ffmpeg 解码 + numpy。
"""

from __future__ import annotations

import numpy as np

from . import config, ffmpeg

SR = config.AUDIO_SR
N_FFT = 1024
HOP = 512
FPS = SR / HOP            # 15.625 帧/秒
BINS = N_FFT // 2 + 1

NEIGHBOR_T = 5            # 峰值邻域（时间方向）
NEIGHBOR_F = 9            # 峰值邻域（频率方向）
PEAKS_PER_FRAME = 5       # 每帧最多保留几个峰
PAIRS_PER_PEAK = 3        # 每个锚点最多配对几个
DT_MIN, DT_MAX = 1, 24    # 配对时间差（帧）
DF_MAX = 64               # 配对频率差（bin）

SEG_FRAMES = 512          # 每段最多 512 帧 = 32.8 秒，塞进 16 位时间的高位


def _maxfilt(a: np.ndarray, k: int, axis: int) -> np.ndarray:
    r = a.copy()
    for d in range(1, k // 2 + 1):
        r = np.maximum(r, np.roll(a, d, axis=axis))
        r = np.maximum(r, np.roll(a, -d, axis=axis))
    return r


def _spectrogram(x: np.ndarray) -> np.ndarray:
    if len(x) < N_FFT:
        return np.zeros((0, BINS), dtype=np.float32)
    n = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    win = np.hanning(N_FFT).astype(np.float32)
    frames = x[idx] * win
    spec = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    return np.log1p(spec)


def _peaks(spec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (帧号, 频率bin)，按帧内强度取前 K。"""
    if spec.size == 0:
        return np.empty(0, np.int32), np.empty(0, np.int32)
    local_max = (_maxfilt(spec, NEIGHBOR_T, axis=0) == spec) & (_maxfilt(spec, NEIGHBOR_F, axis=1) == spec)
    thr = np.percentile(spec, 80)
    cand = local_max & (spec > max(thr, 1e-3))
    tf, ff = np.nonzero(cand)
    if len(tf) == 0:
        return np.empty(0, np.int32), np.empty(0, np.int32)
    mag = spec[tf, ff]
    order = np.lexsort((-mag, tf))
    tf, ff = tf[order], ff[order]
    # 每帧保留前 K 个
    uniq, start, counts = np.unique(tf, return_index=True, return_counts=True)
    keep = np.zeros(len(tf), dtype=bool)
    take = np.minimum(counts, PEAKS_PER_FRAME)
    pos = start.copy()
    for k in range(int(take.max()) if len(take) else 0):
        sel = pos + k
        ok = k < take
        keep[sel[ok]] = True
    return tf[keep].astype(np.int32), ff[keep].astype(np.int32)


def _hashes(tf: np.ndarray, ff: np.ndarray):
    """锚点 + 后续峰值 → (uint32 哈希, uint16 时间)。

    哈希只由「两个峰的频率」决定，**不带时间差**：两个文件的 STFT 帧栅格可能差半帧，
    带 dt 会让大量哈希对不上；时间对齐交给后面的偏移投票解决。
    """
    if len(tf) < 2:
        return np.empty(0, np.uint32), np.empty(0, np.uint16)
    order = np.argsort(tf, kind="stable")
    tf, ff = tf[order], ff[order]
    out_h, out_t = [], []
    n = len(tf)
    for i in range(n):
        t1, f1 = int(tf[i]), int(ff[i])
        paired = 0
        for j in range(i + 1, min(n, i + 64)):
            t2, f2 = int(tf[j]), int(ff[j])
            dt = t2 - t1
            if dt < DT_MIN:
                continue
            if dt > DT_MAX:
                break
            df = f2 - f1
            if abs(df) > DF_MAX:
                continue
            h = ((f1 & 0x1FF) << 9) | (f2 & 0x1FF)
            out_h.append(h)
            out_t.append(t1)
            paired += 1
            if paired >= PAIRS_PER_PEAK:
                break
    if not out_h:
        return np.empty(0, np.uint32), np.empty(0, np.uint16)
    return np.asarray(out_h, dtype=np.uint32), np.asarray(out_t, dtype=np.uint16)


def fingerprint_pcm(pcm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """一段 PCM → (哈希, 相对时间帧)。"""
    if pcm is None or len(pcm) < SR // 2:
        return np.empty(0, np.uint32), np.empty(0, np.uint16)
    x = pcm.astype(np.float32) / 32768.0
    spec = _spectrogram(x)
    tf, ff = _peaks(spec[:SEG_FRAMES] if len(spec) > SEG_FRAMES else spec)
    return _hashes(tf, ff)


def sample_windows(duration: float) -> list[tuple[float, float]]:
    """抽一段连续的音频（够长、又不至于太慢）。短音频整段。

    用"一段连续"而不是多段：比对时按时间偏移投票，剪掉开头/加片头也能对上。
    """
    if duration <= 0:
        return [(0.0, config.AUDIO_WINDOW)]
    win = min(float(config.AUDIO_WINDOW), max(8.0, duration * 0.9))
    start = min(max(0.0, duration * 0.1), max(0.0, duration - win))
    return [(start, win)]


def analyze(path: str, info: dict | None = None) -> dict:
    """解码抽样片段并算指纹。每段的哈希分开存（段时间从 0 开始），比对时逐段对齐。"""
    pr = info or ffmpeg.probe(path)
    dur = float(pr.get("duration") or 0)
    astream = pr.get("astream", 0)
    hs, ts, bounds = [], [], [0]
    for start, win in sample_windows(dur):
        pcm = ffmpeg.pcm_mono(path, SR, start, win, astream)
        if pcm is None:
            continue
        h, t = fingerprint_pcm(pcm)
        if not len(h):
            continue
        hs.append(h)
        ts.append(t)
        bounds.append(bounds[-1] + len(h))
    if not hs:
        raise RuntimeError("解不出音频（可能是纯视频或损坏）")
    h = np.concatenate(hs)
    t = np.concatenate(ts)
    return {
        "duration": round(dur, 3),
        "duration_txt": ffmpeg.fmt_duration(dur),
        "codec": pr.get("acodec") or "",
        "bitrate": int(pr.get("abitrate") or pr.get("bitrate") or 0),
        "sr": int(pr.get("asr") or 0),
        "channels": int(pr.get("channels") or 0),
        "hashes": int(len(h)),
        "segments": len(bounds) - 1,
        "sig": h.tobytes(),
        "times": t.tobytes(),
        "bounds": np.asarray(bounds, dtype=np.uint32).tobytes(),
    }


def decode_sig(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.uint32) if blob else np.empty(0, np.uint32)


def decode_times(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.uint16) if blob else np.empty(0, np.uint16)


def decode_bounds(blob: bytes, n: int = 0) -> np.ndarray:
    if not blob:
        return np.asarray([0, n], dtype=np.int64)
    return np.frombuffer(blob, dtype=np.uint32).astype(np.int64)


def _seg_similarity(ha, ta, hb, tb, cap: int = 8) -> float:
    """按时间偏移投票算匹配度。

    哈希只有「频率对」，同一组合会在不同时间反复出现，所以不能去重后比：
    要保留每次出现，把 所有(A时间,B时间) 组合 的差值 都投票，取最高票。
    """
    if len(ha) < 8 or len(hb) < 8:
        return 0.0
    oa = np.argsort(ha, kind="stable")
    ha_s, ta_s = ha[oa], ta[oa].astype(np.int32)
    ob = np.argsort(hb, kind="stable")
    hb_s, tb_s = hb[ob], tb[ob].astype(np.int32)

    common = np.intersect1d(ha_s, hb_s, assume_unique=True)
    if len(common) < 4:
        return 0.0
    la = np.searchsorted(ha_s, common, "left")
    ra = np.searchsorted(ha_s, common, "right")
    lb = np.searchsorted(hb_s, common, "left")
    rb = np.searchsorted(hb_s, common, "right")

    chunks = []
    budget = 400000
    for i in range(len(common)):
        a_i = ta_s[la[i]:ra[i]][:cap]
        b_i = tb_s[lb[i]:rb[i]][:cap]
        if a_i.size == 0 or b_i.size == 0:
            continue
        d = a_i[:, None] - b_i[None, :]
        chunks.append(d.ravel())
        budget -= d.size
        if budget <= 0:
            break
    if not chunks:
        return 0.0
    d = np.concatenate(chunks)
    vals, counts = np.unique(d, return_counts=True)
    best = 0
    # 偏移允许 ±1 帧抖动
    acc = counts.astype(np.int64).copy()
    idx_plus = np.searchsorted(vals, vals + 1)
    idx_minus = np.searchsorted(vals, vals - 1)
    okp = (idx_plus < len(vals)) & (vals[np.clip(idx_plus, 0, len(vals) - 1)] == vals + 1)
    okm = (idx_minus < len(vals)) & (vals[np.clip(idx_minus, 0, len(vals) - 1)] == vals - 1)
    acc[okp] += counts[idx_plus[okp]]
    acc[okm] += counts[idx_minus[okm]]
    best = int(acc.max()) if len(acc) else 0
    denom = max(1, min(len(ha), len(hb)))
    return min(1.0, best / float(denom))


def similarity(a: tuple, b: tuple) -> float:
    """两组指纹的最佳匹配度：逐段（每个抽样窗口）比对后取最大值。

    a/b = (hashes, times, bounds)
    """
    ha, ta, ba = a
    hb, tb, bb = b
    best = 0.0
    for i in range(len(ba) - 1):
        sa, ea = ba[i], ba[i + 1]
        if ea <= sa:
            continue
        for j in range(len(bb) - 1):
            sb, eb = bb[j], bb[j + 1]
            if eb <= sb:
                continue
            s = _seg_similarity(ha[sa:ea], ta[sa:ea], hb[sb:eb], tb[sb:eb])
            if s > best:
                best = s
    return best
