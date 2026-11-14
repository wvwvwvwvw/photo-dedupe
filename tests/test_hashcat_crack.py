"""端到端：find_password 的 GPU/CPU 分流。

用法：runtime\\python.exe _test\\test_hashcat_crack.py
"""
from __future__ import annotations

import os
import sys
import time
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "_test"))

import pyzipper as pz  # noqa: E402

from media import hashcat as HC  # noqa: E402
from media import unzip as UZ  # noqa: E402
import zipcrypto as ZC  # noqa: E402

WORK = os.path.join(BASE, "_test", "_aes")
PAD = (b"the quick brown fox jumps over the lazy dog\n" * 40)[:1000]
# 别污染真正的"已破解密码本"和破解缓存，测试用独立文件
UZ.FOUND_FILE = os.path.join(WORK, "found_test.txt")
if os.path.exists(UZ.FOUND_FILE):
    os.remove(UZ.FOUND_FILE)
HC.CACHE = os.path.join(WORK, "cracked_test.json")
HC._PW_CACHE = None
if os.path.exists(HC.CACHE):
    os.remove(HC.CACHE)


def make_zip(path: str, pwd: str, nbits: int, payload: bytes = PAD, name="a.txt"):
    if os.path.exists(path):
        os.remove(path)
    with pz.AESZipFile(path, "w", encryption=pz.WZ_AES,
                       encryption_kwargs={"nbits": nbits}) as zf:
        zf.setpassword(pwd.encode())
        zf.writestr(name, payload)


def elements_num(n: int) -> list[dict]:
    return [{"type": "num", "from": 0, "to": 10 ** n - 1, "width": 0}]


def run_case(title: str, path: str, pwd: str, brute: dict | None,
             words: list[str] | None = None) -> bool:
    data = open(path, "rb").read()
    logs: list[str] = []
    t0 = time.time()
    got, enc = UZ.find_password(data, words or [], brute, logs.append, os.path.basename(path))
    dt = time.time() - t0
    ok = (got is not None and got.decode() == pwd)
    print(f"[{'OK ' if ok else 'FAIL'}] {title:<34} {dt:6.2f}s  期望={pwd!r} 得到={got!r}")
    for ln in logs:
        print(f"        · {ln}")
    return ok


def main() -> int:
    os.makedirs(WORK, exist_ok=True)
    st = HC.status(refresh=True)
    print(f"hashcat: available={st['available']} version={st['version']} "
          f"devices={st['device_list']} preferred={HC._preferred_device()}")
    if not st["available"]:
        print("!! hashcat 不可用，下面只能验证 CPU 回退")
    print()

    # 掩码构造
    print("build_mask 抽查：")
    for els in ([{"type": "num", "from": 0, "to": 99999999, "width": 0}],
                [{"type": "literal", "value": "love"},
                 {"type": "num", "from": 0, "to": 9999, "width": 0},
                 {"type": "literal", "value": "!"}],
                [{"type": "charset", "charset": "lower", "len": 4}],
                [{"type": "charset", "charset": "alnum", "len": 2},
                 {"type": "num", "from": 0, "to": 999, "width": 0}]):
        print("   ", els, "->", UZ.build_mask(els))
    print()

    cases = []

    # 1) 小空间：4 位数字 → CPU
    p1 = os.path.join(WORK, "e4.zip")
    make_zip(p1, "7788", 256)
    cases.append(run_case("AES-256 4 位数字（CPU 路径）", p1, "7788",
                          {"auto": True, "elements": elements_num(4)}))

    # 2) 大空间：8 位数字 → GPU 掩码
    p2 = os.path.join(WORK, "g8.zip")
    make_zip(p2, "12345678", 256)
    cases.append(run_case("AES-256 8 位数字（GPU 掩码）", p2, "12345678",
                          {"auto": True, "elements": elements_num(8)}))

    # 3) 前缀 + 6 位数字 → GPU 掩码
    p3 = os.path.join(WORK, "p6.zip")
    make_zip(p3, "love2024", 128)
    cases.append(run_case("AES-128 前缀+6 位不补零（increment）", p3, "love2024",
                          {"auto": True, "elements": [
                              {"type": "literal", "value": "love"},
                              {"type": "num", "from": 0, "to": 999999, "width": 0}]}))

    # 3b) 前缀 + 6 位补零（width=4 之外的另一种写法）必须走固定掩码
    p3b = os.path.join(WORK, "p6w.zip")
    make_zip(p3b, "img000042", 256)
    cases.append(run_case("AES-256 前缀+6 位补零（固定掩码）", p3b, "img000042",
                          {"auto": True, "elements": [
                              {"type": "literal", "value": "img"},
                              {"type": "num", "from": 0, "to": 999999, "width": 6}]}))

    # 4) 字符集 4 位小写 → GPU 掩码（26^4 = 45.7 万）
    p4 = os.path.join(WORK, "c4.zip")
    make_zip(p4, "xqzk", 256)
    cases.append(run_case("AES-256 4 位小写字母（GPU 掩码）", p4, "xqzk",
                          {"auto": True, "elements": [
                              {"type": "charset", "charset": "lower", "len": 4}]}))

    # 4b) ZipCrypto（传统加密）→ hashcat 17225
    pz1 = os.path.join(WORK, "zc_deflate.zip")
    if os.path.exists(pz1):
        os.remove(pz1)
    ZC.make_zip(pz1, "654321", PAD, method=8)
    cases.append(run_case("ZipCrypto deflate 6 位（GPU 17225）", pz1, "654321",
                          {"auto": True, "elements": elements_num(6)}))

    pz2 = os.path.join(WORK, "zc_stored.zip")
    if os.path.exists(pz2):
        os.remove(pz2)
    ZC.make_zip(pz2, "123987", PAD, method=0)
    cases.append(run_case("ZipCrypto stored 6 位（GPU 17225）", pz2, "123987",
                          {"auto": True, "elements": elements_num(6)}))

    # 4c) ZipCrypto 条目太大 → 不该硬塞给 hashcat（单测判断逻辑）
    pz3 = os.path.join(WORK, "zc_big.zip")
    if os.path.exists(pz3):
        os.remove(pz3)
    ZC.make_zip(pz3, "4321", b"x" * 400_000, method=0)
    big = open(pz3, "rb").read()
    info_big = HC.pkzip_info(big)
    picked = HC._pick_hash(big)
    ok_big = (info_big["zipcrypto"] and info_big["too_big"]
              and picked[0] is None and "太大" in picked[2])
    print(f"[{'OK ' if ok_big else 'FAIL'}] {'ZipCrypto 超 300KB 拒绝走 GPU':<34}        "
          f"entries={info_big['entries']} ct={info_big['ct']} 说明={picked[2]}")
    cases.append(ok_big)
    cases.append(run_case("ZipCrypto 超限包 4 位（CPU 回退）", pz3, "4321",
                          {"auto": True, "elements": elements_num(4)}))

    # 5) 密码本命中（不该动 GPU）
    p5 = os.path.join(WORK, "d5.zip")
    make_zip(p5, "p@ss-2024", 256)
    cases.append(run_case("密码本命中", p5, "p@ss-2024",
                          {"auto": True, "elements": elements_num(6)},
                          ["123456", "p@ss-2024", "abcabc"]))

    # 6) 没有密码的包
    p6 = os.path.join(WORK, "plain.zip")
    if not os.path.exists(p6):
        with zipfile.ZipFile(p6, "w") as zf:
            zf.writestr("a.txt", PAD)
    data = open(p6, "rb").read()
    got, enc = UZ.find_password(data, [], {"auto": True, "elements": elements_num(4)},
                               print, "plain.zip")
    ok6 = (got == b"" and enc is False)
    print(f"[{'OK ' if ok6 else 'FAIL'}] {'无密码包直接返回':<34}        期望=b'' 得到={got!r}")
    cases.append(ok6)

    # 7) 5.7MB 单条目 + 4 位数字 → 小空间走 CPU（验证 AES 大包也能被 pyzipper 读）
    p7 = os.path.join(WORK, "big4.zip")
    make_zip(p7, "4321", 256, os.urandom(5_700_000), name="big.bin")
    cases.append(run_case("AES-256 5.7MB 单条目 4 位（CPU）", p7, "4321",
                          {"auto": True, "elements": elements_num(4)}))

    print()
    print(f"通过 {sum(1 for c in cases if c)}/{len(cases)}")
    return 0 if all(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
