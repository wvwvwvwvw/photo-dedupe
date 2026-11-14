"""视频画帧的特征：MobileNetV2 卷积嵌入（ONNX，DirectML 显卡加速）+ 64 位 pHash。"""

from __future__ import annotations

import os
import threading

import numpy as np

from . import config

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_DCT = {}


def _dct_matrix(n: int) -> np.ndarray:
    if n not in _DCT:
        k = np.arange(n, dtype=np.float64)
        _DCT[n] = np.cos(np.pi / n * np.outer(k + 0.5, k))
    return _DCT[n]


def phash(frame: np.ndarray) -> int:
    """64 位感知哈希（32×32 DCT 低频 + 中位数阈值）。"""
    from PIL import Image
    g = np.asarray(Image.fromarray(frame).convert("L").resize((32, 32), Image.BOX),
                   dtype=np.float64)
    c = _dct_matrix(32)
    d = c @ g @ c.T
    block = d[:8, :8]
    bits = (block > float(np.median(block))).reshape(-1)
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def min_hamming(pa, pb) -> int:
    if not pa or not pb:
        return -1
    best = 99
    for x in pa:
        for y in pb:
            d = hamming(x, y)
            if d < best:
                best = d
                if best <= 2:
                    return best
    return best


class Embedder:
    """进程内单例：加载 ONNX 模型，批量算 1280 维画面特征。"""

    _inst = None
    _lock = threading.Lock()

    def __init__(self, backend: str = "auto"):
        import onnxruntime as ort
        self.ort = ort
        self.lock = threading.Lock()
        self.dim = 0
        self.device = "cpu"
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        provs = ort.get_available_providers()
        want = []
        if backend in ("auto", "gpu") and "DmlExecutionProvider" in provs:
            want.append("DmlExecutionProvider")
        if "CPUExecutionProvider" in provs:
            want.append("CPUExecutionProvider")
        self.sess = ort.InferenceSession(config.MODEL, so, providers=want or provs)
        self.providers = self.sess.get_providers()
        self.device = "dml" if "DmlExecutionProvider" in self.providers else "cpu"
        self.input_name = self.sess.get_inputs()[0].name
        outs = self.sess.get_outputs()
        target = None
        for o in outs:
            shape = [d for d in o.shape if isinstance(d, int) and d > 0]
            if shape and shape[-1] == 1280:
                target = o.name
                break
        if target is None and outs:
            best = max(outs, key=lambda o: max([d for d in o.shape if isinstance(d, int) and d > 0] or [0]))
            target = best.name
        self.out_name = target

    @classmethod
    def get(cls, backend: str = "auto") -> "Embedder":
        with cls._lock:
            if cls._inst is None:
                cls._inst = Embedder(backend)
            return cls._inst

    def embed(self, frames) -> np.ndarray | None:
        if frames is None or len(frames) == 0:
            return None
        x = np.stack([np.asarray(f) for f in frames]).astype(np.float32)
        x = (x / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
        x = np.transpose(x, (0, 3, 1, 2))
        with self.lock:
            (out,) = self.sess.run([self.out_name], {self.input_name: x})
        out = np.asarray(out, dtype=np.float32)
        if out.ndim > 2:
            out = out.reshape(out.shape[0], -1)
        self.dim = out.shape[1]
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return (out / norm).astype(np.float16)

    def label(self) -> str:
        kind = "GPU-DirectML" if self.device == "dml" else "CPU"
        return f"{kind} · {self.out_name or '?'} · {self.dim}维"


def frame_score(a: np.ndarray, b: np.ndarray) -> float:
    """两段帧特征的最优匹配分（0~1）。a/b: [F,D] 已 L2 归一化。"""
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    s = a @ b.T
    return float((s.max(axis=1).mean() + s.max(axis=0).mean()) * 0.5)


def mean_vec(emb: np.ndarray) -> np.ndarray:
    v = emb.astype(np.float32).mean(axis=0)
    n = np.linalg.norm(v)
    return v / n if n else v
