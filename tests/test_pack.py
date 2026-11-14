"""快速打包自检：三种加密 × 多层压缩 × 拆分，并用自家解压引擎回读。"""
from __future__ import annotations

import io
import os
import shutil
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import pack  # noqa: E402
from media import unzip as UZ  # noqa: E402

W = os.path.join(BASE, "_test", "_pack_check")


def build() -> None:
    shutil.rmtree(W, ignore_errors=True)
    d = os.path.join(W, "src", "A", "B")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(W, "src", "A", "C"), exist_ok=True)
    open(os.path.join(W, "src", "A", "a1.txt"), "w", encoding="utf-8").write("A 文件\n" * 30)
    open(os.path.join(d, "b1.txt"), "w", encoding="utf-8").write("B 文件\n" * 10)
    open(os.path.join(W, "src", "A", "C", "c1.txt"), "w", encoding="utf-8").write("C 文件\n" * 10)
    big = os.path.join(W, "src", "BIG")
    os.makedirs(big, exist_ok=True)
    with open(os.path.join(big, "blob.bin"), "wb") as f:
        f.write(os.urandom(300_000))               # 不可压缩，保证能拆出多卷


def read_zip(path: str, pwd: str = ""):
    if pwd:
        import pyzipper
        with pyzipper.AESZipFile(path) as zf:
            zf.setpassword(pwd.encode())
            return zf.namelist(), {n: zf.read(n) for n in zf.namelist()}
    with zipfile.ZipFile(path) as zf:
        return zf.namelist(), {n: zf.read(n) for n in zf.namelist()}


def main() -> int:
    build()
    ok = True
    for enc, pwd, label in ((pack.ENC_NONE, "", "不加密"),
                            (pack.ENC_ZIPCRYPTO, "pw123", "ZipCrypto"),
                            (pack.ENC_AES, "pw123", "AES-256")):
        out = os.path.join(W, "out_" + enc)
        r = pack.pack_folder(os.path.join(W, "src", "A"), os.path.join(out, "A"),
                             enc=enc, password=pwd, multi=True)
        hit = bool(r.get("ok"))
        names, inner = [], {}
        if hit:
            if enc == pack.ENC_ZIPCRYPTO:
                with zipfile.ZipFile(r["files"][0]) as zf:
                    zf.setpassword(pwd.encode())
                    names = zf.namelist()
                    inner = {n: zf.read(n) for n in names if n.endswith(".zip")}
            else:
                names, inner = read_zip(r["files"][0], pwd)
            # 多层：B/C 应该是 zip
            hit = ("a1.txt" in names and "B.zip" in names and "C.zip" in names)
            if hit:
                sub = zipfile.ZipFile(io.BytesIO(inner["B.zip"]))
                hit = sub.namelist() == ["b1.txt"]
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] {label:<10} 条目 {names}")

    # 拆分 + 自家解压回读
    out = os.path.join(W, "out_split")
    r = pack.pack_folder(os.path.join(W, "src", "BIG"), os.path.join(out, "BIG"),
                         enc=pack.ENC_ZIPCRYPTO, password="pw123", split_mb=0.1)
    parts = [os.path.basename(p) for p in r.get("files") or []]
    split_ok = len(parts) >= 2 and parts[0].endswith(".z01") and parts[-1].endswith(".zip")
    ext = os.path.join(W, "extract")
    UZ.FOUND_FILE = os.path.join(W, "found.txt")
    try:
        os.remove(UZ.FOUND_FILE)
    except OSError:
        pass
    job = UZ.UnzipJob(out, ext, ["pw123"], None, True, auto_brute=False)
    job.start()
    job.join(timeout=120)
    got = []
    for dp, _d, fs in os.walk(ext):
        for f in fs:
            got.append((os.path.relpath(os.path.join(dp, f), ext), os.path.getsize(os.path.join(dp, f))))
    read_ok = any(n.endswith("blob.bin") and sz == 300_000 for n, sz in got)
    ok &= split_ok and read_ok
    print(f"[{'OK ' if split_ok and read_ok else 'FAIL'}] 拆分 {parts} → 解回 {got}")
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
