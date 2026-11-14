"""图片查重 - 感知哈希与画质评估。

只依赖 Pillow + numpy，不联网、不上传。
哈希设计：
  pHash (DCT 低频)  —— 对缩放、再压缩、轻微调色稳健，是主判据；
  dHash (梯度)      —— 对结构边缘敏感，用于二次校验，压掉 pHash 的误报；
  aHash (均值)      —— 辅助，仅在需要时展示。
"""

from __future__ import annotations

import hashlib
import os

import numpy as np
from PIL import Image, ImageFile, ImageOps

from .formats import CAPS, SUPPORTED_EXTS

ImageFile.LOAD_TRUNCATED_IMAGES = True
# 允许超大图，避免 "DecompressionBomb" 直接报错
Image.MAX_IMAGE_PIXELS = 500_000_000

IMG_EXTS = SUPPORTED_EXTS
HEIF_OK = CAPS.get("heif", False)


def digest_bytes(buf: bytes) -> str:
    """内容摘要：大小 + 首尾各 256KB（小文件就是全文）。

    比全量 sha256 快几十倍，判断"是不是同一份文件"完全够用。
    """
    h = hashlib.sha256()
    h.update(str(len(buf)).encode("ascii"))
    if len(buf) <= 512 * 1024:
        h.update(buf)
    else:
        h.update(buf[:256 * 1024])
        h.update(buf[-256 * 1024:])
    return h.hexdigest()


def file_digest(path: str) -> str:
    size = os.path.getsize(path)
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    with open(path, "rb") as f:
        if size <= 512 * 1024:
            h.update(f.read())
        else:
            h.update(f.read(256 * 1024))
            f.seek(size - 256 * 1024)
            h.update(f.read(256 * 1024))
    return h.hexdigest()

# ---------------------------------------------------------------- 位运算工具

def bits_to_u64(bits: np.ndarray) -> int:
    """64 个 0/1 -> uint64 整数（用 big-endian 打包，稳定且便于排序）。"""
    packed = np.packbits(np.asarray(bits, dtype=np.uint8))
    if packed.size < 8:
        packed = np.concatenate([packed, np.zeros(8 - packed.size, np.uint8)])
    return int.from_bytes(packed[:8].tobytes(), "big")


_DCT_CACHE: dict[int, np.ndarray] = {}


def _dct_matrix(n: int = 32) -> np.ndarray:
    """DCT-II 变换矩阵（常数缩放不影响中位数阈值）。"""
    m = _DCT_CACHE.get(n)
    if m is None:
        idx = np.arange(n, dtype=np.float64)
        m = np.cos(np.pi * (2.0 * idx[None, :] + 1.0) * idx[:, None] / (2.0 * n))
        _DCT_CACHE[n] = m.astype(np.float32)
    return _DCT_CACHE[n]


# ---------------------------------------------------------------- 三种哈希

def phash_bits(gray: Image.Image) -> np.ndarray:
    """pHash: 缩到 32x32 灰度 -> DCT -> 左上 8x8 低频 -> 与中位数比较。"""
    small = gray.resize((32, 32), Image.Resampling.BOX)
    a = np.asarray(small, dtype=np.float32)
    c = _dct_matrix(32)
    d = c @ a @ c.T
    block = d[:8, :8]
    thr = float(np.median(block))
    return (block > thr).reshape(-1)


def dhash_bits(gray: Image.Image) -> np.ndarray:
    """dHash: 9x8 灰度，比较左右相邻像素。"""
    small = gray.resize((9, 8), Image.Resampling.BOX)
    a = np.asarray(small, dtype=np.int16)
    return (a[:, 1:] > a[:, :-1]).reshape(-1)


def ahash_bits(gray: Image.Image) -> np.ndarray:
    """aHash: 8x8 灰度，与均值比较。"""
    small = gray.resize((8, 8), Image.Resampling.BOX)
    a = np.asarray(small, dtype=np.float32)
    return (a > a.mean()).reshape(-1)


# ---------------------------------------------------------------- 画质指标

def sharpness(gray: Image.Image) -> float:
    """拉普拉斯方差，作为清晰度指标。

    先内缩 2% 去掉边缘（边框、暗角），长边超过 512 再缩下来，
    避免纯靠分辨率取胜；数值越大越锐利（虚焦、强压缩的图会明显偏低）。
    """
    g = gray
    ix, iy = int(g.width * 0.02), int(g.height * 0.02)
    if g.width - 2 * ix >= 16 and g.height - 2 * iy >= 16:
        g = g.crop((ix, iy, g.width - ix, g.height - iy))
    g = g.copy()
    if max(g.size) > 512:
        g.thumbnail((512, 512), Image.Resampling.BOX)
    if g.width < 3 or g.height < 3:
        return 0.0
    a = np.asarray(g, dtype=np.float32)
    lap = (
        a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
        - 4.0 * a[1:-1, 1:-1]
    )
    return float(lap.var())


def file_sha256(path: str, chunk: int = 1 << 20) -> str:
    """全量 sha256（默认不用，仅备用；日常用 file_digest 快得多）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- 边框检测

def detect_content_box(gray_small: np.ndarray, tol: float = 3.0,
                       max_frac: float = 0.15) -> tuple[float, float, float, float]:
    """检测纯色边框（白边/黑边/截图留边），返回要裁掉的 (左, 上, 右, 下) 比例。

    判据两条同时成立才算留边：
      1) 从边缘往里连续若干行/列几乎恒定，且颜色等于最外侧像素；
      2) 这串恒定区之后紧接一个明显的亮度跳跃（真正的画面从这里开始）。
    这样渐变天空、大面积纯色背景都不会被误裁。
    """
    h, w = gray_small.shape
    if h < 8 or w < 8:
        return (0.0, 0.0, 0.0, 0.0)

    def run_len(means: np.ndarray, stds: np.ndarray, limit: int) -> int:
        ref = float(means[0])
        # 留边通常是大片纯白/纯黑（截图、排版、扫描）；天空、墙面之类的
        # 大面积纯色不算边框，否则会把正常照片裁坏。
        if not (ref >= 235.0 or ref <= 18.0):
            return 0
        n = 0
        # 留边区域的颜色应当和最外沿一致（JPEG 噪声下允许 ±4 级）
        while n < limit and abs(float(means[n]) - ref) < 4.0 and stds[n] < 6.0:
            n += 1
        if n == 0 or n >= limit:      # 整条都均匀：多半是纯色图/大色块，不裁
            return 0
        # 边界可能被缩放/抗锯齿糊了几行，往后多看几行找突变
        win = means[n:n + 8]
        if win.size == 0 or float(np.max(np.abs(win - ref))) < 15.0:
            return 0                      # 没有明显边界，是渐变，不裁
        if n < 2:
            return 0
        return n

    lim_y, lim_x = max(1, int(h * max_frac)), max(1, int(w * max_frac))
    row_mean, row_std = gray_small.mean(axis=1), gray_small.std(axis=1)
    col_mean, col_std = gray_small.mean(axis=0), gray_small.std(axis=0)

    top = run_len(row_mean, row_std, lim_y)
    bottom = run_len(row_mean[::-1], row_std[::-1], lim_y)
    left = run_len(col_mean, col_std, lim_x)
    right = run_len(col_mean[::-1], col_std[::-1], lim_x)

    if top + bottom > h * 0.4:
        top = bottom = 0
    if left + right > w * 0.4:
        left = right = 0
    return (left / w, top / h, right / w, bottom / h)


# ---------------------------------------------------------------- 单张分析

def analyze_image(path: str) -> dict:
    """读取一张图，返回哈希、尺寸、清晰度、主色等（CPU 版，Pillow 解码）。"""
    st = os.stat(path)
    with Image.open(path) as im:
        fmt = (im.format or os.path.splitext(path)[1].lstrip(".")).upper()
        w, h = im.size          # 原始尺寸要在 draft 之前记下来
        im.seek(0)  # 动图只取第一帧
        # JPEG 可以让解码器直接按 1/2、1/4、1/8 出图：反正最后只要 512 长边，
        # 这一步能省掉 90% 以上的解码量（大图尤其明显）。
        try:
            if fmt == "JPEG":
                im.draft("RGB", (512, 512))
        except Exception:
            pass
        im = ImageOps.exif_transpose(im) or im
        # 统一到 RGB（CMYK/16bit/调色板图都走这条路径）
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        # 只做一次全分辨率重采样：长边降到 512。
        # 之后所有指标（含留边检测、pHash、dHash、清晰度）都基于这张小图，
        # 既快，又能和 GPU 的 area 插值对齐。
        base = im.copy()
        base.thumbnail((512, 512), Image.Resampling.BOX)
        bw, bh = base.size
        base_gray = base.convert("L")

        fl, ft, fr, fb = detect_content_box(np.asarray(base_gray, dtype=np.float32))
        trimmed = (fl + fr + ft + fb) > 0.01
        # 缩放/抗锯齿会在边界留下几像素过渡带，往里多裁一点
        pad = max(2, int(round(min(bw, bh) * 0.004))) if trimmed else 0
        x0 = int(round(bw * fl)) + (pad if fl > 0 else 0)
        y0 = int(round(bh * ft)) + (pad if ft > 0 else 0)
        x1 = bw - int(round(bw * fr)) - (pad if fr > 0 else 0)
        y1 = bh - int(round(bh * fb)) - (pad if fb > 0 else 0)
        if trimmed and x1 - x0 >= 16 and y1 - y0 >= 16:
            base = base.crop((x0, y0, x1, y1))
            gray = base_gray.crop((x0, y0, x1, y1))
            cw = max(1, int(round(w * (1.0 - fl - fr))))
            ch = max(1, int(round(h * (1.0 - ft - fb))))
        else:
            fl = ft = fr = fb = 0.0
            gray = base_gray
            cw, ch = w, h

        sw, sh = base.size
        ph = bits_to_u64(phash_bits(gray))
        dh = bits_to_u64(dhash_bits(gray))
        ah = bits_to_u64(ahash_bits(gray))
        # 中心裁掉 6% 边框后再算一次 pHash：容忍裁边、加白边/黑边、截图留边
        mx, my = int(sw * 0.06), int(sh * 0.06)
        if sw - 2 * mx >= 8 and sh - 2 * my >= 8:
            ph_c = bits_to_u64(phash_bits(gray.crop((mx, my, sw - mx, sh - my))))
        else:
            ph_c = ph
        sharp = sharpness(gray)
        # 16x16 灰度指纹：给聚类做"第二阶段确认"用（哈希只能筛出候选，
        # 真正判断像不像要看更低频的灰度结构），256 字节/张
        patch = gray.resize((16, 16), Image.Resampling.BOX).tobytes()
        # 主色（1x1 BOX 下采样 = 平均色）
        mean = np.asarray(base.resize((1, 1), Image.Resampling.BOX), dtype=np.uint8).reshape(3)

    rec = {
        "path": path,
        "name": os.path.basename(path),
        "size": int(st.st_size),
        "mtime": float(st.st_mtime),
        "w": int(w),
        "h": int(h),
        "fmt": str(fmt),
        "pix": int(cw * ch),
        "phash": f"{ph:016x}",
        "phash_c": f"{ph_c:016x}",
        "trim": f"{fl:.3f},{ft:.3f},{fr:.3f},{fb:.3f}",
        "dhash": f"{dh:016x}",
        "ahash": f"{ah:016x}",
        "sharp": round(float(sharp), 3),
        "mr": int(mean[0]), "mg": int(mean[1]), "mb": int(mean[2]),
        "sha256": file_digest(path),
        "patch": patch,
    }
    return rec
