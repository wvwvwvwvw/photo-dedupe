"""图像哈希的向量化内核（numpy）。

大图库的候选检索在 index.py，这里只放最基础的距离计算，
保证 CPU / GPU 两条分析管线共用同一套判据。
"""

from __future__ import annotations

import numpy as np

_LUT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


_HAS_BITCOUNT = hasattr(np, "bitwise_count")


def _pc(x: np.ndarray) -> np.ndarray:
    """按位 1 的个数。numpy 2.x 的 bitwise_count 比查表快约 5 倍。"""
    if _HAS_BITCOUNT:
        return np.bitwise_count(x).astype(np.int32)
    x = np.ascontiguousarray(x)
    shp = x.shape
    b = x.view(np.uint8).reshape(-1, 8)
    return _LUT[b].sum(axis=1, dtype=np.int32).reshape(shp)


def min4_numpy(a_ph, a_phc, b_ph, b_phc) -> np.ndarray:
    """两组哈希之间"四个变体取最小"的汉明距离矩阵（numpy 版）。"""
    d = _pc(a_ph[:, None] ^ b_ph[None, :])
    np.minimum(d, _pc(a_phc[:, None] ^ b_phc[None, :]), out=d)
    np.minimum(d, _pc(a_ph[:, None] ^ b_phc[None, :]), out=d)
    np.minimum(d, _pc(a_phc[:, None] ^ b_ph[None, :]), out=d)
    return d
