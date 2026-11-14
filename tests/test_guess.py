"""自动猜文件头自检：造一个"stored + PNG"的加密 zip，不给明文，看能不能破出来。"""
from __future__ import annotations

import io
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "_test"))

import zipcrypto as ZC  # noqa: E402
from media import plainguess as PG  # noqa: E402
from media.plain import PlainJob  # noqa: E402

W = os.path.join(BASE, "_test", "_guess")


def main() -> int:
    from media import bkcrack as BK
    if not BK.status()["available"]:
        print("没有 bkcrack，跳过")
        return 0
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(W, exist_ok=True)

    # ① 猜测表本身
    checks = [
        ("a.png", "PNG", b"\x89PNG\r\n\x1a\n"),
        ("b.pdf", "PDF", b"%PDF"),
        ("c.xml", "XML", b"<?xml"),
        ("d.xlsx", "ZIP", b"PK"),
    ]
    bad = [n for n, lab, sig in checks
           if not any(lab.split()[0] in g[0] and g[1].startswith(sig)
                      for g in PG.guess_for(n))]
    # ② 真包：stored PNG + 密码
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 28), (12, 200, 90)).save(buf, "PNG")
    png = buf.getvalue()
    zp = os.path.join(W, "pic.zip")
    ZC.make_zip(zp, "pass7788", png, name="photo.png", method=0)
    job = PlainJob(zp, b"", 0, False, "?d", 8, auto=True)
    job.start()
    t0 = time.time()
    while job.is_alive() and time.time() - t0 < 600:
        time.sleep(1)
    st = job.snapshot()
    res = st.get("result") or {}
    ok = (not bad and st.get("state") == "done" and res.get("verified")
          and res.get("guess") == "PNG" and len(res.get("keys") or []) == 3)
    print(f"[{'OK ' if ok else 'FAIL'}] 猜测表{'正常' if not bad else '有问题:' + str(bad)}；"
          f"自动攻击 → {res.get('guess')} 密钥 {' '.join(res.get('keys') or [])}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
