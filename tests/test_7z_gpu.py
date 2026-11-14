"""7z 走显卡（hashcat 11600）自检：造 4 种压缩方式的加密包 → 抽哈希 → 破解。

用法：runtime\\python.exe _test\\test_7z_gpu.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import hashcat as HC  # noqa: E402
from media import sevenzip as SZ  # noqa: E402
from media import unzip as UZ  # noqa: E402

W = os.path.join(BASE, "_test", "_7zh_gpu")
PWD = "6011"


def build(exe: str) -> None:
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(W, exist_ok=True)
    src = os.path.join(W, "p.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write("7z gpu test payload " * 200)
    for name, meth in (("s.7z", "Copy"), ("l2.7z", "LZMA2"), ("d.7z", "Deflate")):
        subprocess.run([exe, "a", os.path.join(W, name), src, f"-p{PWD}",
                        f"-m0={meth}", "-y"], capture_output=True)


def main() -> int:
    exe = UZ.find_7z()
    st = HC.status()
    if not exe or not st["available"]:
        print("缺 7-Zip 或 hashcat，跳过")
        return 0
    build(exe)
    ok = 0
    total = 0
    for name in ("s.7z", "l2.7z", "d.7z"):
        p = os.path.join(W, name)
        data = open(p, "rb").read()
        line, err = HC.seven_hash(p)
        total += 1
        if not line:
            print(f"[FAIL] {name} 抽不出哈希：{err}")
            continue
        r = HC.crack_7z(data, candidates=["1111", PWD], timeout=300)
        good = bool(r.get("ok")) and r.get("password") == PWD
        ok += 1 if good else 0
        print(f"[{'OK ' if good else 'FAIL'}] {name} 哈希 {len(line)} 字符 → "
              f"{r.get('password') or r.get('error')}")
    print(f"{'通过' if ok == total else '失败'} {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
