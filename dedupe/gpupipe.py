"""GPU 优先的分析管线（NVIDIA + torch/torchvision nvJPEG）。

设计：
* JPEG 走 GPU 解码（nvJPEG，实测比 PIL 单线程快 7 倍以上），
  灰度化、缩放、DCT、dHash、清晰度全部在显存里算，最后只把 64 位结果拿回 CPU；
* 其它格式（PNG/WebP/HEIC/PSD…）仍走 Pillow —— GPU 解码不了，硬搬反而更慢；
* 任何一步失败都自动退回该文件的 CPU 分析，不会中断扫描；
* 结果字段与 CPU 版完全一致，两种模式混着建索引也能正常聚类。
"""

from __future__ import annotations

import os

import numpy as np

from .hashing import (bits_to_u64, detect_content_box, digest_bytes, file_digest)

_ORIENT_FLIP_LR = 2
_ORIENT_ROT180 = 3
_ORIENT_FLIP_TB = 4
_ORIENT_TRANSPOSE = 5
_ORIENT_ROT270 = 6
_ORIENT_TRANSVERSE = 7
_ORIENT_ROT90 = 8


# 当前选中的 GPU 后端：cuda / dml / cpu
GPU_DEVICE = "cpu"
GPU_NAME = ""


def _dml_module():
    """torch_directml 存在且能拿到设备时返回模块，否则 None。"""
    try:
        import torch_directml as dml      # noqa: PLC0415
        dml.device()                      # 拿不到设备会抛错
        return dml
    except Exception:                     # noqa: BLE001
        return None


def _probe_torch():
    """检查能不能用显卡，返回 (ok, 描述)。

    顺序：CUDA（NVIDIA，能用 nvJPEG）→ DirectML（AMD / Intel / 任何 DX12 显卡）→ CPU。
    选中的后端写到模块级 GPU_DEVICE / GPU_NAME，GpuPipeline 用它建管线。
    """
    global GPU_DEVICE, GPU_NAME
    try:
        import torch  # noqa: F401
    except Exception:
        return False, "未安装 torch"
    try:
        import torchvision  # noqa: F401
    except Exception:
        return False, "未安装 torchvision（GPU 解码需要它）"
    try:
        import torch
        if torch.cuda.is_available():
            GPU_DEVICE = "cuda"
            GPU_NAME = torch.cuda.get_device_name(0)
            return True, GPU_NAME
    except Exception as exc:                      # noqa: BLE001
        pass
    # 退一步：DirectML（Windows 通用，AMD/Intel/NVIDIA 都行；没有 nvJPEG 加速）
    dml = _dml_module()
    if dml is not None:
        try:
            GPU_DEVICE = "dml"
            GPU_NAME = dml.device_name(0) or "DirectML"
            return True, f"{GPU_NAME}（DirectML）"
        except Exception:                         # noqa: BLE001
            pass
    GPU_DEVICE = "cpu"
    GPU_NAME = ""
    return False, "torch 检测不到 CUDA / DirectML 设备"


class GpuPipeline:
    kind = "gpu"

    def __init__(self, device: str = "auto", batch: int = 8):
        import torch
        import torchvision
        self.torch = torch
        self.tv = torchvision
        self.F = torch.nn.functional
        self.batch = max(1, int(batch))

        # device 解析：auto = 探测结果；显式 cuda/dml 时再校验一次
        want = (device or "auto").lower()
        if want == "auto":
            ok, desc = _probe_torch()
            if not ok:
                raise RuntimeError(desc)
            want = GPU_DEVICE
        if want == "dml":
            dml = _dml_module()
            if dml is None:
                raise RuntimeError("没有 torch_directml，用不了 DirectML")
            self.dev = dml.device()
            self.name = f"{dml.device_name(0) or 'DirectML'}（DirectML）"
            self.backend = "dml"
        elif want == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA 不可用")
            self.dev = torch.device("cuda")
            self.name = torch.cuda.get_device_name(0)
            self.backend = "cuda"
        else:
            self.dev = torch.device("cpu")
            self.name = "CPU"
            self.backend = "cpu"
        self.w = torch.tensor([0.299, 0.587, 0.114], dtype=torch.float32,
                              device=self.dev).view(3, 1, 1)
        idx = torch.arange(32, dtype=torch.float32, device=self.dev)
        dct = torch.cos(torch.pi * (2 * idx[None, :] + 1) * idx[:, None] / 64.0)
        self.dct = dct.contiguous()
        # 预热：让 CUDA kernel 先编译好，避免头几张特别慢
        self._warmup()

    # ------------------------------------------------------------ 内部
    def _warmup(self):
        t = self.torch
        x = t.rand(1, 1, 64, 64, device=self.dev)
        for sz in [(32, 32), (8, 9), (8, 8)]:
            self.F.interpolate(x, size=sz, mode="area")
        d = self.dct @ t.rand(32, 32, device=self.dev) @ self.dct.T
        _ = d.median()
        if getattr(self, "backend", "") == "cuda":
            t.cuda.synchronize()

    def _gray(self, img):
        return (img.to(self.torch.float32) * self.w).sum(0)

    def _orient(self, img, o: int):
        t = self.torch
        if o == _ORIENT_FLIP_LR:
            return t.flip(img, [2])
        if o == _ORIENT_ROT180:
            return t.flip(img, [1, 2])
        if o == _ORIENT_FLIP_TB:
            return t.flip(img, [1])
        if o == _ORIENT_TRANSPOSE:
            return img.transpose(1, 2)
        if o == _ORIENT_ROT270:
            return t.rot90(img, 3, [1, 2])
        if o == _ORIENT_TRANSVERSE:
            return t.flip(img.transpose(1, 2), [1, 2])
        if o == _ORIENT_ROT90:
            return t.rot90(img, 1, [1, 2])
        return img

    def _phash_bits(self, g):
        """g: 2D float32 灰度（已裁边）"""
        t = self.torch
        x = self.F.interpolate(g[None, None], size=(32, 32), mode="area")[0, 0]
        d = (self.dct @ x @ self.dct.T)[:8, :8].flatten()
        med = d.sort().values
        thr = (med[31] + med[32]) * 0.5
        return (d > thr)

    def _dhash_bits(self, g):
        # 注意：interpolate 的 size 是 (高, 宽)。dHash 要在**横向**比较相邻像素，
        # 所以是 8 行 × 9 列 → 每行 8 个比较 → 64 位。写成 (9, 8) 会变成 63 位且方向错。
        x = self.F.interpolate(g[None, None], size=(8, 9), mode="area")[0, 0]
        return (x[:, 1:] > x[:, :-1]).flatten()

    def _ahash_bits(self, g):
        x = self.F.interpolate(g[None, None], size=(8, 8), mode="area")[0, 0]
        return (x > x.mean()).flatten()

    def _sharpness(self, g):
        t = self.torch
        h, w = int(g.shape[0]), int(g.shape[1])
        iy, ix = int(h * 0.02), int(w * 0.02)
        if h - 2 * iy >= 16 and w - 2 * ix >= 16:
            g = g[iy:h - iy, ix:w - ix]
        g = g[None, None]
        h2, w2 = int(g.shape[2]), int(g.shape[3])
        scale = 512.0 / max(h2, w2)
        if scale < 1.0:
            g = self.F.interpolate(g, scale_factor=scale, mode="area")
        a = g[0, 0]
        lap = (a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:] - 4.0 * a[1:-1, 1:-1])
        return float(lap.var().item())

    def _trim(self, g):
        """返回 (l, t, r, b) 比例；用缩小后的探针判断，成本很低。"""
        t = self.torch
        h, w = int(g.shape[0]), int(g.shape[1])
        scale = 640.0 / max(h, w)
        if scale < 1.0:
            probe = self.F.interpolate(g[None, None], scale_factor=scale, mode="area")[0, 0]
        else:
            probe = g
        arr = probe.detach().to("cpu").numpy().astype(np.float32)
        return detect_content_box(arr)

    # ------------------------------------------------------------ 单张
    def _one(self, path: str, buf: bytes, fmt: str, orient: int) -> dict:
        t = self.torch
        if fmt == "JPEG":
            src = t.frombuffer(bytearray(buf), dtype=t.uint8)
            if self.backend == "cuda":
                img = self.tv.io.decode_jpeg(src, device=self.dev, mode="RGB")
            else:
                # DirectML 没有 nvJPEG：先 CPU 解码再搬进显存
                img = self.tv.io.decode_jpeg(src, device="cpu", mode="RGB").to(self.dev)
        else:  # 理论上不会走到这里
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            arr = np.asarray(im, dtype=np.uint8)
            img = t.from_numpy(arr).to(self.dev).permute(2, 0, 1)
            orient = 1

        if orient != 1:
            img = self._orient(img, orient)

        h0, w0 = int(img.shape[1]), int(img.shape[2])
        # 只做一次全分辨率重采样：长边降到 512，后续都在这张小图上算
        x = img.to(t.float32)
        scale = 512.0 / max(h0, w0)
        if scale < 1.0:
            x = self.F.interpolate(x[None], scale_factor=scale, mode="area")[0]
        g = (x * self.w).sum(0)
        sw, sh = int(x.shape[2]), int(x.shape[1])

        fl, ft, fr, fb = self._trim(g)
        trimmed = (fl + fr + ft + fb) > 0.01
        if trimmed:
            pad = max(2, int(round(min(sw, sh) * 0.004)))
            x0 = int(round(sw * fl)) + (pad if fl > 0 else 0)
            y0 = int(round(sh * ft)) + (pad if ft > 0 else 0)
            x1 = sw - int(round(sw * fr)) - (pad if fr > 0 else 0)
            y1 = sh - int(round(sh * fb)) - (pad if fb > 0 else 0)
            if x1 - x0 >= 16 and y1 - y0 >= 16:
                x = x[:, y0:y1, x0:x1]
                g = g[y0:y1, x0:x1]
                cw = max(1, int(round(w0 * (1.0 - fl - fr))))
                ch = max(1, int(round(h0 * (1.0 - ft - fb))))
            else:
                fl = ft = fr = fb = 0.0
                cw, ch = w0, h0
        else:
            fl = ft = fr = fb = 0.0
            cw, ch = w0, h0

        gw, gh = int(g.shape[1]), int(g.shape[0])
        ph_bits = self._phash_bits(g)
        mx, my = int(gw * 0.06), int(gh * 0.06)
        if gw - 2 * mx >= 8 and gh - 2 * my >= 8:
            ph_c_bits = self._phash_bits(g[my:gh - my, mx:gw - mx])
        else:
            ph_c_bits = ph_bits
        dh_bits = self._dhash_bits(g)
        ah_bits = self._ahash_bits(g)
        patch = self.F.interpolate(g[None, None], size=(16, 16), mode="area")[0, 0] \
            .clamp(0, 255).to("cpu").numpy().astype(np.uint8).tobytes()
        sharp = self._sharpness(g)
        mean = x.mean(dim=(1, 2)).detach().to("cpu").numpy()

        ph = bits_to_u64(ph_bits.to("cpu").numpy())
        phc = bits_to_u64(ph_c_bits.to("cpu").numpy())
        dh = bits_to_u64(dh_bits.to("cpu").numpy())
        ah = bits_to_u64(ah_bits.to("cpu").numpy())

        st = os.stat(path)
        return {
            "path": path,
            "name": os.path.basename(path),
            "size": int(st.st_size),
            "mtime": float(st.st_mtime),
            "w": int(w0),
            "h": int(h0),
            "fmt": fmt,
            "pix": int(cw * ch),
            "phash": f"{ph:016x}",
            "phash_c": f"{phc:016x}",
            "trim": f"{fl:.3f},{ft:.3f},{fr:.3f},{fb:.3f}",
            "dhash": f"{dh:016x}",
            "ahash": f"{ah:016x}",
            "sharp": round(float(sharp), 3),
            "mr": int(mean[0]), "mg": int(mean[1]), "mb": int(mean[2]),
            "sha256": digest_bytes(buf),
            "patch": patch,
        }

    # ------------------------------------------------------------ 批量
    def analyze_batch(self, paths: list[str]) -> list[dict]:
        out = []
        for p in paths:
            out.append(self.analyze_one(p))
        return out

    def analyze_one(self, path: str) -> dict:
        """单个文件：失败就退回 CPU，绝不抛异常给扫描任务。"""
        try:
            with open(path, "rb") as f:
                buf = f.read(4)
            if not buf:
                raise ValueError("空文件")
            # JPEG 用文件头认（FF D8 FF），省掉一次 PIL.open（4K 图上一张能省几毫秒）；
            # EXIF 方向交给 torchvision 的 apply_exif_orientation（默认开）
            if buf[:3] != b"\xff\xd8\xff":
                from .hashing import analyze_image
                return analyze_image(path)
            with open(path, "rb") as f:
                data = f.read()
            return self._one(path, data, "JPEG", 1)
        except Exception:
            from .hashing import analyze_image
            return analyze_image(path)
