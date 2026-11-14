"""压缩包文件名乱码自检：写包要打 UTF-8 标记，读包要能修复 GBK 老包的名字。"""
from __future__ import annotations

import io
import os
import struct
import sys
import zipfile
import zlib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import pack  # noqa: E402
from media import unzip as UZ  # noqa: E402

W = os.path.join(BASE, "_test", "_moji_check")


class FakeInfo:
    def __init__(self, name: str, flags: int = 0):
        self.filename = name
        self.flag_bits = flags


def gbk_zip_bytes(name_cn: str, payload: bytes = b"data") -> bytes:
    """造一个"GBK 存名字、不打 UTF-8 标记"的老式 zip。"""
    raw_name = name_cn.encode("gbk")
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    out = io.BytesIO()
    off = 0
    out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0x6000, 0x5821,
                          crc, len(payload), len(payload), len(raw_name), 0))
    out.write(raw_name)
    out.write(payload)
    cd_start = out.tell()
    out.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0,
                          0x6000, 0x5821, crc, len(payload), len(payload),
                          len(raw_name), 0, 0, 0, 0, 0, off))
    out.write(raw_name)
    out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1,
                          out.tell() - cd_start, cd_start, 0))
    return out.getvalue()


def main() -> int:
    ok = True
    os.makedirs(W, exist_ok=True)

    # ① 写：非 ASCII 名字带 UTF-8 标记（0x0800），纯 ASCII 不带
    blob = pack.zip_bytes([
        {"name": "#暂停约稿…十六夜晓晨-约稿短篇/第一章.txt", "data": b"hi"},
        {"name": "readme.txt", "data": b"ok"},
    ])
    infos = zipfile.ZipFile(io.BytesIO(blob)).infolist()
    cn = next(i for i in infos if not i.filename.startswith("readme"))
    asc = next(i for i in infos if i.filename.startswith("readme"))
    hit = bool(cn.flag_bits & 0x800) and not (asc.flag_bits & 0x800) \
        and cn.filename.startswith("#暂停约稿")
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 写包：中文名 flag=0x{cn.flag_bits:04x}、"
          f"英文名 flag=0x{asc.flag_bits:04x}")

    # ② 读：GBK 名字的老包（Python 会按 cp437 解成乱码）
    path = os.path.join(W, "gbk.zip")
    with open(path, "wb") as f:
        f.write(gbk_zip_bytes("#暂停约稿测试.txt"))
    info = zipfile.ZipFile(path).infolist()[0]
    fixed = UZ.fix_zip_name(info)
    hit = fixed == "#暂停约稿测试.txt"
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 读老包：{info.filename!r} → {fixed!r}")

    # ③ 二次乱码（UTF-8 字节被当成 cp437 读出来的那种）
    double = "#暂停约稿…十六夜晓晨-约稿短篇10136880.zip"
    mojibake = double.encode("utf-8").decode("cp437")
    fixed2 = UZ.fix_zip_name(FakeInfo(mojibake))
    hit = fixed2 == double
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 修二次乱码：{mojibake[:24]!r}… → {fixed2[:30]!r}…")

    # ④ 不误伤：带标记的名字、纯英文名字都不动
    hit = (UZ.fix_zip_name(FakeInfo(mojibake, 0x800)) == mojibake
           and UZ.fix_zip_name(FakeInfo("readme.txt")) == "readme.txt")
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 不误伤：已带标记/纯英文保持原样")

    # ⑤ 端到端：自己写的包再读回来，名字一致
    with open(os.path.join(W, "mine.zip"), "wb") as f:
        f.write(blob)
    zf = zipfile.ZipFile(os.path.join(W, "mine.zip"))
    names = [UZ.fix_zip_name(i) for i in zf.infolist()]
    hit = "#暂停约稿…十六夜晓晨-约稿短篇/第一章.txt" in names
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 端到端：{names}")
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
