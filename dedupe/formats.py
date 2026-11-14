"""图片格式支持：自动发现 Pillow 能解的格式，并按需挂载 HEIC / JXL / RAW 插件。

不硬编码扩展名，而是读 Pillow 的注册表 —— 装了新插件就自动多支持一种格式。
"""

from __future__ import annotations

import os

from PIL import Image

# 需要外部程序、容易触发误报的格式，排除掉
_EXCLUDE = {".eps", ".ps", ".pdf", ".pcl", ".plt"}

# 可选插件：装了就注册，没装就跳过
_OPTIONAL = []


def _try_register(module: str, label: str) -> bool:
    try:
        if module == "pillow_heif":
            import pillow_heif  # type: ignore
            pillow_heif.register_heif_opener()
            _OPTIONAL.append(label)
            return True
        if module == "pillow_jxl":
            import pillow_jxl  # type: ignore  # noqa: F401
            _OPTIONAL.append(label)
            return True
        if module == "rawpy":
            import rawpy  # type: ignore  # noqa: F401
            _OPTIONAL.append(label)
            return True
    except Exception:
        pass
    return False


def _build() -> tuple[set[str], dict]:
    supported: set[str] = set()
    try:
        supported.update(Image.registered_extensions().keys())
    except Exception:
        supported.update({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
    supported -= _EXCLUDE

    caps = {
        "heif": _try_register("pillow_heif", "HEIC/HEIF"),
        "jxl": _try_register("pillow_jxl", "JPEG XL"),
        "raw": _try_register("rawpy", "RAW"),
    }
    if caps["heif"]:
        supported |= {".heic", ".heif"}
    if caps["jxl"]:
        supported |= {".jxl"}
    if caps["raw"]:
        supported |= {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
                      ".srw", ".pef", ".3fr"}
    # 一些常见别名
    supported |= {".jfif", ".jpe", ".tiff", ".apng", ".mpo"}
    return supported, caps


SUPPORTED_EXTS, CAPS = _build()

# CPU 能解、但 GPU(nvJPEG) 不能的，走 CPU 分支
JPEG_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif"}


_COMMON = [
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".jxl", ".psd", ".ico", ".jp2", ".qoi", ".mpo", ".dds",
    ".pcx", ".tga", ".dng", ".cr2", ".nef", ".arw", ".raf", ".jfif",
]


def capabilities() -> dict:
    exts = sorted(SUPPORTED_EXTS)
    common = [e for e in _COMMON if e in SUPPORTED_EXTS]
    other = [e for e in exts if e not in common]
    return {
        "count": len(exts),
        "extensions": exts,
        "common": common,
        "other": other,
        "optional": list(_OPTIONAL),
        "heif": CAPS.get("heif", False),
        "jxl": CAPS.get("jxl", False),
        "raw": CAPS.get("raw", False),
    }


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTS


def is_jpeg(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in JPEG_EXTS
