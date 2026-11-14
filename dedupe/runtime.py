"""启动期的小工具：如果本机是 CPU 版 Python，但配了 gpu_python.txt，就切到 GPU 环境。

app.py 和 bench.py 都用它，保证"测速"和"实际扫描"跑在同一个解释器上。
"""

from __future__ import annotations

import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def gpu_python_path() -> str:
    cfg = os.path.join(BASE, "gpu_python.txt")
    if not os.path.exists(cfg):
        return ""
    try:
        with open(cfg, "r", encoding="utf-8-sig") as f:      # 容忍记事本写的 BOM
            for line in f:
                line = line.strip().strip('"').strip("\ufeff")
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return ""


def has_torch() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def reexec_with_gpu_python(extra_args: list[str] | None = None) -> bool:
    """成功切换返回 True（调用方应立刻结束），失败/不需要返回 False。"""
    if os.environ.get("DEDUPE_NO_REEXEC") == "1":
        return False
    target = gpu_python_path()
    if not target:
        return False
    if not os.path.exists(target):
        print(f"  [i] gpu_python.txt 指向的 GPU 环境不存在，已忽略（用内置 Python）")
        return False
    if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(sys.executable)):
        return False
    if has_torch():
        return False
    try:
        probe = subprocess.run([target, "-c", "import torch, PIL, numpy"],
                               capture_output=True, timeout=180)
        if probe.returncode != 0:
            print("  [i] gpu_python.txt 指向的 Python 不可用，继续用当前环境")
            return False
    except Exception:
        return False
    print(f"  [i] 使用可选 GPU 环境：{target}")
    print("      （只在配置了这个文件时启用；换电脑后路径不存在会自动忽略）")
    sys.stdout.flush()
    args = [os.path.abspath(sys.argv[0])] + (extra_args if extra_args is not None else sys.argv[1:])
    try:
        os.execv(target, [target] + args)
    except Exception as exc:
        print(f"  [!] 切换失败：{exc}")
    return False
