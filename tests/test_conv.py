"""繁简/编码转换自检：编码识别、繁→简、批量转换。"""
from __future__ import annotations

import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import conv  # noqa: E402

W = os.path.join(BASE, "_test", "_conv_check")


def main() -> int:
    ok = True
    st = conv.dict_status()
    ok &= st["available"] and st["chars"] > 3000
    print(f"[{'OK ' if st['available'] else 'FAIL'}] 字典：{st['chars']} 字 / {st['phrases']} 词")

    # ① 编码识别
    cases = [
        ("utf-8", "简体中文测试".encode("utf-8"), "utf-8"),
        ("gbk", "简体中文测试".encode("gbk"), "gb18030"),
        ("big5", "繁體中文測試".encode("big5"), "big5hkscs"),
        ("utf-16-le", "简体中文".encode("utf-16-le"), "utf-16-le"),
        ("utf-8-sig", b"\xef\xbb\xbf" + "中文".encode(), "utf-8-sig"),
        ("ascii", b"hello world", "utf-8"),
    ]
    for name, raw, want in cases:
        got, _bom = conv.detect(raw)
        hit = got == want
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 编码识别 {name:<10} → {got}（期望 {want}）")

    # ② 繁→简
    pairs = [
        ("這裡是繁體中文", "这里是繁体中文"),
        ("龍鳳臺灣認識頭髮乾杯", "龙凤台湾认识头发干杯"),
        ("他說：「這個問題很難。」", "他说：「这个问题很难。」"),
    ]
    for src, want in pairs:
        got = conv.t2s(src)
        hit = got == want
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] {src} → {got}")

    # ③ 批量转换（GBK 繁体 → UTF-8 简体，含子目录）
    shutil.rmtree(W, ignore_errors=True)
    src_dir = os.path.join(W, "src")
    os.makedirs(os.path.join(src_dir, "子目录"), exist_ok=True)
    open(os.path.join(src_dir, "a.txt"), "wb").write(
        "繁體小說第一章。\r\n第二行：軟體與硬體。".encode("gbk"))
    open(os.path.join(src_dir, "子目录", "b.txt"), "wb").write(
        "繁體子目錄文件。".encode("utf-8"))
    open(os.path.join(src_dir, "c.bin"), "wb").write(b"\x00\x01\x02")   # 不该被处理
    job = conv.ConvJob(src_dir, os.path.join(W, "out"), t2s_on=True,
                       out_enc="utf-8", newline="lf", recursive=True)
    job.start()
    t0 = time.time()
    while job.is_alive() and time.time() - t0 < 60:
        time.sleep(0.2)
    snap = job.snapshot()
    out_a = os.path.join(W, "out", "a.txt")
    out_b = os.path.join(W, "out", "子目录", "b.txt")
    text_a = open(out_a, encoding="utf-8").read() if os.path.exists(out_a) else ""
    text_b = open(out_b, encoding="utf-8").read() if os.path.exists(out_b) else ""
    hit = (snap["files"] == 2 and "繁体小说第一章。" in text_a
           and "\r" not in text_a and "繁体子目录文件。" in text_b
           and not os.path.exists(os.path.join(W, "out", "c.bin")))
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 批量转换：{snap['files']} 个文件，a.txt={text_a!r}")
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
