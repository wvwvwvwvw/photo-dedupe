"""DirectML 备用方案：没有 NVIDIA 时能否自动切到 DirectML。

真机上没装 torch-directml 也能跑这个测试——它用一个假的 `torch_directml`
模块注入 sys.modules，验证：
* 选路顺序 CUDA → DirectML → CPU
* 引擎名字/后端字段会标出 DirectML
* JPEG 走"CPU 解码 + 搬进设备"，分析结果字段和 CPU 版一致
"""

from __future__ import annotations

import os
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import torch  # noqa: E402

from dedupe import gpupipe as G  # noqa: E402
from dedupe import pipeline as P  # noqa: E402
from dedupe.store import Store  # noqa: E402


def fake_dml() -> types.ModuleType:
    m = types.ModuleType("torch_directml")

    def device(index: int = 0):
        return torch.device("cpu")          # 测试机没有 DX12 设备，用 CPU 顶替

    def device_name(index: int = 0) -> str:
        return "测试用 DirectML 设备"

    m.device = device
    m.device_name = device_name
    return m


def main() -> int:
    ok = True
    saved = sys.modules.get("torch_directml")
    cuda_was = torch.cuda.is_available
    try:
        sys.modules["torch_directml"] = fake_dml()
        torch.cuda.is_available = lambda: False        # 模拟 AMD / Intel 机器

        ok_gpu, desc = P.gpu_available()
        dml_ok = P.dml_available()
        backend = P.gpu_backend()
        hit = ok_gpu and dml_ok and backend == "dml"
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 探测：{desc} back={backend} dml={dml_ok}")

        st = Store(os.path.join(BASE, "data", "library.db"))
        rows = st._conn.execute(
            "SELECT path FROM images WHERE status='ok' ORDER BY RANDOM() LIMIT 8").fetchall()
        st.close()
        paths = [r["path"] for r in rows if os.path.isfile(r["path"])]

        eng = P.make_engine("auto", workers=4, sample_paths=paths[:3])
        info = P.INFO.as_dict()
        named = "DirectML" in (info.get("name") or "")
        ok &= named and info.get("backend") == "dml"
        print(f"[{'OK ' if named else 'FAIL'}] 引擎：{info.get('name')} · backend={info.get('backend')}")

        outs = eng.analyze_batch(paths[:6])
        errs = [r for r in outs if r and "error" in r]
        ok &= not errs
        print(f"[{'OK ' if not errs else 'FAIL'}] 分析 {len(paths[:6])} 张，错误 {len(errs)}"
              + (f"：{errs[0]['error'][:60]}" if errs else ""))

        # 字段要和 CPU 版一致
        if outs and not errs:
            need = {"path", "phash", "phash_c", "dhash", "ahash", "w", "h", "patch"}
            miss = sorted(k for k in need if k not in outs[0])
            ok &= not miss
            print(f"[{'OK ' if not miss else 'FAIL'}] 结果字段齐全"
                  + (f"，缺 {miss}" if miss else ""))
        eng.close()

        # 显式 device="cpu" 也要能用
        pipe = G.GpuPipeline(device="cpu", batch=2)
        ok &= pipe.backend == "cpu"
        print(f"[{'OK ' if pipe.backend == 'cpu' else 'FAIL'}] 显式 CPU 设备：{pipe.name}")
    finally:
        torch.cuda.is_available = cuda_was
        if saved is not None:
            sys.modules["torch_directml"] = saved
        else:
            sys.modules.pop("torch_directml", None)
        G.GPU_DEVICE, G.GPU_NAME = ("cuda" if torch.cuda.is_available() else "cpu"), ""

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
