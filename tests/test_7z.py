"""RAR / 7z 解压自检：造样本 → 用 UnzipJob 解 → 核对文件。

用法：runtime\\python.exe _test\\test_7z.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import unzip as UZ  # noqa: E402

W = os.path.join(BASE, "_test", "_7z")
OUT = os.path.join(BASE, "_test", "_7z_out")


def build():
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(W, exist_ok=True)
    exe = UZ.find_7z()
    if not exe:
        print("没找到 7z.exe，跳过（放一个到 vendor\\7z 或写 7z.txt）")
        return None
    open(os.path.join(W, "a.txt"), "w", encoding="utf-8").write("hello 7z")
    open(os.path.join(W, "b.txt"), "w", encoding="utf-8").write("line " * 50)
    subprocess.run([exe, "a", os.path.join(W, "plain.7z"),
                    os.path.join(W, "a.txt"), os.path.join(W, "b.txt"), "-y"],
                   capture_output=True)
    subprocess.run([exe, "a", os.path.join(W, "aes.7z"),
                    os.path.join(W, "a.txt"), os.path.join(W, "b.txt"), "-p7788", "-y"],
                   capture_output=True)
    inner = os.path.join(W, "inner.zip")
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("deep.txt", "nested " * 5)
    subprocess.run([exe, "a", os.path.join(W, "nested.7z"), inner, "-y"], capture_output=True)
    return exe


def main() -> int:
    if build() is None:
        return 0
    shutil.rmtree(OUT, ignore_errors=True)
    UZ.FOUND_FILE = os.path.join(OUT, "found.txt")
    os.makedirs(OUT, exist_ok=True)
    job = UZ.UnzipJob(W, OUT, ["7788"], None, True, auto_brute=False)
    job.start()
    job.join(timeout=300)
    st = job.snapshot()
    got = []
    for dp, _d, fs in os.walk(OUT):
        for f in fs:
            got.append(os.path.relpath(os.path.join(dp, f), OUT).replace("\\", "/"))
    want = {"aes/a.txt", "aes/b.txt", "plain/a.txt", "plain/b.txt",
            "inner/deep.txt", "nested/inner/deep.txt"}
    ok = st.get("state") == "done" and want <= set(got)
    print(f"[{'OK ' if ok else 'FAIL'}] 7z/RAR 解压：{len(got)} 个文件（期望含 {len(want)} 个）")
    for f in sorted(got):
        print("     ", f)
    for ln in (st.get("log") or [])[-4:]:
        print("   ·", ln)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
