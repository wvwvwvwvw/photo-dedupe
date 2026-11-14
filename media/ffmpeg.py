"""ffmpeg 封装：探测元信息、按时间点抽帧、抽缩略图、解码 PCM。

只用子进程调用，不落任何素材文件（抽帧写临时目录，用完即删）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import numpy as np

from . import config

_FFMPEG = None

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_STREAM_RE = re.compile(r"Stream #(\d+):(\d+)(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*(Video|Audio):\s*([^,\r\n]+)([^\r\n]*)")
_RES_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_KBPS_RE = re.compile(r"(\d+)\s*kb/s")
_HZ_RE = re.compile(r"(\d+)\s*Hz")
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")

# 封面图/附加图片流不是正片
_COVER_CODECS = {"mjpeg", "png", "jpeg", "bmp", "gif", "webp"}


def exe() -> str:
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    env = os.environ.get("MEDIADUP_FFMPEG")
    cand = [env] if env else []
    cand.append(config.FFMPEG)
    for p in cand:
        if p and os.path.isfile(p):
            _FFMPEG = p
            return p
    raise RuntimeError("找不到 ffmpeg（应在 vendor\\ffmpeg.exe）")


def available() -> bool:
    try:
        exe()
        return True
    except Exception:
        return False


def version() -> str:
    try:
        p = subprocess.run([exe(), "-version"], capture_output=True, timeout=30)
        line = p.stdout.decode("utf-8", errors="replace").splitlines()
        return (line[0].strip() if line else "?")[:80]
    except Exception:
        return "?"


def probe(path: str) -> dict:
    """读容器信息。返回 dict，失败抛 RuntimeError。"""
    if not os.path.isfile(path):
        raise RuntimeError("文件不存在")
    try:
        p = subprocess.run([exe(), "-hide_banner", "-nostdin", "-i", path],
                           capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("探测超时")
    err = p.stderr.decode("utf-8", errors="replace")

    info = {"duration": 0.0, "width": 0, "height": 0, "vcodec": "", "acodec": "",
            "has_video": False, "has_audio": False, "vstream": 0, "astream": 0,
            "abitrate": 0, "asr": 0, "channels": 0, "bitrate": 0}

    m = _DUR_RE.search(err)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = _BITRATE_RE.search(err)
    if m:
        info["bitrate"] = int(m.group(1))

    videos, audios = [], []
    for line in err.splitlines():
        m = _STREAM_RE.search(line)
        if not m:
            continue
        idx = int(m.group(2))
        kind = m.group(3)
        codec = m.group(4).strip().split(" ")[0].lower()
        tail = m.group(5) or ""
        if kind == "Video":
            videos.append((idx, codec, tail, line))
        else:
            audios.append((idx, codec, tail, line))

    videos = [v for v in videos if v[1] not in _COVER_CODECS] or videos
    if videos:
        idx, codec, tail, line = videos[0]
        info.update(has_video=True, vstream=idx, vcodec=codec)
        mr = _RES_RE.search(line)
        if mr:
            info["width"], info["height"] = int(mr.group(1)), int(mr.group(2))
    if audios:
        idx, codec, tail, line = audios[0]
        info.update(has_audio=True, astream=idx, acodec=codec)
        mh = _HZ_RE.search(line)
        if mh:
            info["asr"] = int(mh.group(1))
        if "stereo" in line:
            info["channels"] = 2
        elif "mono" in line:
            info["channels"] = 1
        mk = _KBPS_RE.search(line)
        if mk:
            info["abitrate"] = int(mk.group(1))

    if not info["has_video"] and not info["has_audio"]:
        raise RuntimeError("没有可识别的音视频流")
    return info


def _frame_vf(size: int) -> str:
    # 等比缩放 + 补黑边成正方形，保留完整画面（不同宽高比也能比）
    return (f"scale={size}:{size}:force_original_aspect_ratio=decrease,"
            f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:black")


def frames_at(path: str, times: list[float], size: int = 224, vstream: int = 0) -> list[np.ndarray]:
    """一个进程内多次 seek 抽帧，返回 uint8 HxWx3 列表（失败的点会被跳过）。"""
    times = [max(0.0, float(t)) for t in times if t is not None and t >= 0]
    if not times:
        return []
    tmp = tempfile.mkdtemp(prefix="mdup_")
    try:
        cmd = [exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "4"]
        for t in times:
            cmd += ["-ss", f"{t:.3f}", "-i", path]
        for i in range(len(times)):
            out = os.path.join(tmp, f"f{i:02d}.jpg")
            cmd += ["-map", f"{i}:{vstream}", "-frames:v", "1",
                    "-vf", _frame_vf(size), "-q:v", "3", "-an", "-sn", "-y", out]
        try:
            subprocess.run(cmd, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            pass
        from PIL import Image
        out_frames = []
        for i in range(len(times)):
            fp = os.path.join(tmp, f"f{i:02d}.jpg")
            if os.path.isfile(fp) and os.path.getsize(fp) > 200:
                try:
                    with Image.open(fp) as im:
                        out_frames.append(np.asarray(im.convert("RGB"), dtype=np.uint8))
                except Exception:
                    continue
        return out_frames
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def thumbnail(path: str, size: int = 320, t: float | None = None, vstream: int = 0) -> bytes | None:
    """抽一帧当缩略图（JPEG bytes）。"""
    if t is None:
        t = 1.0
        try:
            dur = probe(path).get("duration") or 0
            t = max(0.5, dur * 0.15) if dur > 1 else 0.0
        except Exception:
            pass
    cmd = [exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "4",
           "-ss", f"{t:.3f}", "-i", path, "-map", f"0:{vstream}", "-frames:v", "1",
           "-vf", f"scale={size}:-2", "-q:v", "5", "-an", "-sn",
           "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    return p.stdout if p.stdout and len(p.stdout) > 200 else None


def pcm_mono(path: str, sr: int, start: float, seconds: float, astream: int = 0) -> np.ndarray | None:
    """解一段音频成单声道 int16。"""
    cmd = [exe(), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.2, seconds):.3f}",
           "-i", path, "-map", f"0:{astream}", "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None
    if not p.stdout or len(p.stdout) < sr:      # 少于 0.5 秒当失败
        return None
    return np.frombuffer(p.stdout, dtype="<i2")


def waveform(path: str, buckets: int = 900, sr: int = 4000,
             max_seconds: float = 1800.0, astream: int = 0) -> tuple[list[int], float] | None:
    """整段音频的波形包络：把 PCM 压成 buckets 个 (min,max) 对，给画布用。

    低采样率（默认 4 kHz）+ 取每桶极值，既能看出音量起伏，又不用解整段原始 PCM。
    返回 (peaks, 实际秒数)，peaks 是交错排列的 [min0,max0,min1,max1,…]，取值 -128~127。
    """
    # 视频文件里 0:0 是画面，必须显式指定"第几条音轨"（0:a:N）；
    # 纯音频文件两种写法都行，所以先试 0:a:N，失败再退回 0:N。
    maps = [f"0:a:{astream}", f"0:{astream}"]
    raw = b""
    for mp in maps:
        cmd = [exe(), "-hide_banner", "-nostdin", "-loglevel", "error",
               "-t", f"{max(1.0, max_seconds):.3f}", "-i", path,
               "-map", mp, "-vn", "-sn", "-dn",
               "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1"]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            continue
        if p.stdout and len(p.stdout) >= sr:
            raw = p.stdout
            break
    if not raw:
        return None
    p = type("R", (), {"stdout": raw})()
    data = np.frombuffer(p.stdout, dtype="<i2").astype(np.float32)
    n = len(data)
    secs = n / float(sr)
    buckets = max(16, min(int(buckets), 4000))
    # 用 reshape 直接算每桶极值（最后不足一桶的丢掉）
    per = n // buckets
    if per <= 0:
        return None
    chunk = data[:per * buckets].reshape(buckets, per)
    lo = chunk.min(axis=1) / 32768.0
    hi = chunk.max(axis=1) / 32768.0
    peaks = np.empty(buckets * 2, dtype=np.int16)
    peaks[0::2] = np.clip(lo * 127.0, -127, 127).astype(np.int16)
    peaks[1::2] = np.clip(hi * 127.0, -127, 127).astype(np.int16)
    return peaks.tolist(), round(secs, 2)


def fmt_duration(sec: float) -> str:
    sec = int(sec or 0)
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
