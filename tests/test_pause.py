"""破解暂停/继续自检：跑大批量穷举 → 暂停 → 继续 → 仍然能找到密码。

用 AES 包（13600，有真实计算量）让破解跑到 8 秒左右，中途暂停 2.5 秒。
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "_test"))

from media import hashcat as HC  # noqa: E402
from media import unzip as UZ  # noqa: E402

W = os.path.join(BASE, "_test", "_pause")
PWD = "87654321"


def main() -> int:
    if not HC.status()["available"]:
        print("没有可用的 hashcat/显卡，跳过")
        return 0
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(W, exist_ok=True)
    import pyzipper
    zp = os.path.join(W, "aes.zip")
    with pyzipper.AESZipFile(zp, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(PWD.encode())
        zf.writestr("a.txt", b"pause resume payload " * 60)
    data = open(zp, "rb").read()
    UZ.FOUND_FILE = os.path.join(W, "found.txt")
    HC.CACHE = os.path.join(W, "cracked.json")
    HC._PW_CACHE = None

    pause = threading.Event()
    logs: list[str] = []
    marks: dict[str, float] = {}
    t0 = time.time()

    def logger(msg: str) -> None:
        logs.append(f"{time.time() - t0:5.1f}s {msg}")
        if "已暂停（进度已保存" in msg:
            marks["paused"] = round(time.time() - t0, 1)
        if "继续破解" in msg:
            marks["resumed"] = round(time.time() - t0, 1)

    def toggler() -> None:
        time.sleep(3.0)
        pause.set()
        time.sleep(2.5)
        pause.clear()

    threading.Thread(target=toggler, daemon=True).start()
    pw, _enc = UZ.find_password(
        data, [], {"auto": True, "timeout": 300,
                   "elements": [{"type": "num", "from": 0, "to": 99999999, "width": 8}]},
        logger, "aes.zip", None, None, pause=pause)
    ok = bool(pw) and pw.decode() == PWD and "paused" in marks and "resumed" in marks
    print(f"[{'OK ' if ok else 'FAIL'}] 暂停@{marks.get('paused')}s "
          f"继续@{marks.get('resumed')}s → {pw.decode() if pw else None}")
    for ln in logs[-5:]:
        print("     ", ln)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
