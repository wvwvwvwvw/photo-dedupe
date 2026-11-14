"""压缩/解压性能路径自检：并行压缩、已压缩格式直存、并行解压与并行写盘。"""
from __future__ import annotations

import io
import os
import shutil
import sys
import time
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import pack  # noqa: E402
from media import unzip as UZ  # noqa: E402

W = os.path.join(BASE, "_test", "_speed_check")


def main() -> int:
    ok = True
    shutil.rmtree(W, ignore_errors=True)
    src = os.path.join(W, "src")
    os.makedirs(src, exist_ok=True)

    # 200 个文本文件（可压缩）+ 20 个"已压缩"的假 jpg（不可压缩）
    text = ("测试文本，用来验证并行压缩的正确性。中文内容要能原样还原。" * 40).encode("utf-8")
    for i in range(200):
        with open(os.path.join(src, f"文本{i:03d}.txt"), "wb") as f:
            f.write(text)
    blob = os.urandom(200_000)
    for i in range(20):
        with open(os.path.join(src, f"图片{i:02d}.jpg"), "wb") as f:
            f.write(blob)

    entries = [{"name": f, "src": os.path.join(src, f)} for f in sorted(os.listdir(src))]
    t0 = time.time()
    data = pack.zip_bytes(entries, level=6)
    dt = time.time() - t0
    total = sum(os.path.getsize(os.path.join(src, f)) for f in os.listdir(src))

    hit = len(data) > 0
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 并行压缩 {len(entries)} 个文件 "
          f"{total / 1048576:.1f} MB → {len(data) / 1048576:.1f} MB，{dt:.2f}s"
          f"（{total / 1048576 / max(dt, 1e-6):.0f} MB/s）")

    # 条目完整、名字正确（含中文）、内容一致
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        hit = len(names) == len(entries) and "文本000.txt" in names
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 条目数 {len(names)}，中文名保留")

        txt = zf.read("文本000.txt")
        hit = txt == text
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 文本内容一致")

        info = zf.getinfo("图片00.jpg")
        hit = info.compress_type == 0          # 已压缩格式应直存
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] jpg 走直存（method={info.compress_type}）")

    # 并行解压 + 并行写盘
    zpath = os.path.join(W, "all.zip")
    with open(zpath, "wb") as f:
        f.write(data)
    out = os.path.join(W, "out")
    t0 = time.time()
    UZ.FOUND_FILE = os.path.join(W, "found.txt")
    try:
        os.remove(UZ.FOUND_FILE)
    except OSError:
        pass
    srcdir = os.path.join(W, "ziponly")
    os.makedirs(srcdir, exist_ok=True)
    shutil.copy2(zpath, os.path.join(srcdir, "all.zip"))
    job = UZ.UnzipJob(srcdir, out, ["x"], None, True, auto_brute=False)
    job.start()
    job.join(timeout=300)
    dt = time.time() - t0
    n = sum(len(fs) for _dp, _d, fs in os.walk(out))
    hit = n == len(entries)
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 解压回来 {n} 个文件，{dt:.2f}s")

    p1 = os.path.join(out, "all", "文本000.txt")
    p2 = os.path.join(out, "文本000.txt")
    path = p1 if os.path.exists(p1) else p2
    hit = os.path.exists(path) and open(path, "rb").read() == text
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 解压内容一致（{os.path.relpath(path, out) if os.path.exists(path) else '没找到'}）")

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
