"""视频分析：抽帧 → 画面特征 + pHash，产出可入库的记录。"""

from __future__ import annotations

import os

import numpy as np

from . import config, features, ffmpeg


def sample_times(duration: float, count: int) -> list[float]:
    """在时间轴上均匀取 count 个点（避开片头片尾各 0.3 秒）。"""
    n = max(2, int(count))
    if duration <= 1.0:
        return [0.0]
    hi = max(0.3, duration - 0.3)
    return [min(max((i + 0.5) * duration / n, 0.3), hi) for i in range(n)]


def analyze(path: str, frames: int = config.VIDEO_FRAMES) -> dict:
    """分析单个视频。失败抛异常（调用方记 error）。"""
    pr = ffmpeg.probe(path)
    if not pr.get("has_video"):
        raise RuntimeError("没有视频轨")
    dur = float(pr.get("duration") or 0)
    times = sample_times(dur, frames)
    imgs = ffmpeg.frames_at(path, times, config.VIDEO_SIZE, pr.get("vstream", 0))
    if not imgs:
        raise RuntimeError("抽不出画面（可能是纯音频容器或损坏）")

    emb = features.Embedder.get().embed(imgs)
    ph = [features.phash(f) for f in imgs]
    thumb = ffmpeg.thumbnail(path, 320, None if dur > 1 else 0.0, pr.get("vstream", 0))

    return {
        "duration": round(dur, 3),
        "duration_txt": ffmpeg.fmt_duration(dur),
        "w": int(pr.get("width") or 0),
        "h": int(pr.get("height") or 0),
        "vcodec": pr.get("vcodec") or "",
        "acodec": pr.get("acodec") or "",
        "has_audio": bool(pr.get("has_audio")),
        "frames": int(emb.shape[0]),
        "dim": int(emb.shape[1]),
        "sig": emb.tobytes(),                       # float16 [F, D]
        "ph": np.asarray(ph, dtype=np.uint64).tobytes(),
        "thumb": thumb or b"",
    }


def decode_sig(blob: bytes, dim: int = 0) -> np.ndarray | None:
    if not blob:
        return None
    arr = np.frombuffer(blob, dtype=np.float16)
    if dim and arr.size % dim == 0:
        return arr.reshape(-1, dim).astype(np.float32)
    return arr.reshape(1, -1).astype(np.float32)


def decode_ph(blob: bytes) -> list[int]:
    if not blob:
        return []
    return [int(x) for x in np.frombuffer(blob, dtype=np.uint64)]
