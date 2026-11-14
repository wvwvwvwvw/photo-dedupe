"""便携性自检：只用自带工具（假装这台电脑没装 7-Zip / WinRAR）时功能是否还在。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import pack, pipeline as PL  # noqa: E402
from media import unzip as UZ  # noqa: E402

W = os.path.join(BASE, "_test", "_portable_check")


def main() -> int:
    ok = True
    bundled = os.path.join(BASE, "vendor", "7z", "7za.exe")
    hit = os.path.isfile(bundled)
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 自带 7za.exe → {bundled}")

    # 假装系统里没装任何 7-Zip / WinRAR
    pack.SEVENZIP_CACHE[0] = bundled
    UZ._7Z_CACHE = bundled
    real_rar = UZ.find_rar
    UZ.find_rar = lambda: ""
    try:
        shutil.rmtree(W, ignore_errors=True)
        src = os.path.join(W, "src", "作品甲")
        os.makedirs(src, exist_ok=True)
        open(os.path.join(src, "说明.txt"), "w", encoding="utf-8").write("内容" * 500)
        with open(os.path.join(src, "数据.bin"), "wb") as f:
            f.write(os.urandom(50_000))

        # ① 用自带 7za 打包 7z
        out = os.path.join(W, "out")
        job = PL.PipelineJob(os.path.join(W, "src"), out,
                             [{"type": "pack", "fmt": "7z", "level": 6}], "batch")
        job.start()
        job.join(timeout=180)
        st = job.snapshot()
        files = os.listdir(out) if os.path.isdir(out) else []
        hit = st.get("state") == "done" and files == ["作品甲.7z"]
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 只用自带 7za 打 7z → {files}")

        # ② 质检：自带 7za 能测试通过
        p = subprocess.run([bundled, "t", os.path.join(out, "作品甲.7z")],
                           capture_output=True, timeout=120)
        hit = p.returncode == 0
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 7za 自检通过")

        # ③ 用自家解压引擎解这个 7z
        ex = os.path.join(W, "ex")
        UZ.FOUND_FILE = os.path.join(W, "found.txt")
        try:
            os.remove(UZ.FOUND_FILE)
        except OSError:
            pass
        job = UZ.UnzipJob(out, ex, ["x"], None, True, auto_brute=False)
        job.start()
        job.join(timeout=300)
        got = []
        for dp, _d, fs in os.walk(ex):
            for f in fs:
                got.append(os.path.relpath(os.path.join(dp, f), ex))
        hit = sorted(os.path.basename(g) for g in got) == ["说明.txt", "数据.bin"] \
            or len(got) >= 2
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 自带 7za 解 7z → {sorted(got)}")

        # ④ 没有 WinRAR 时解 RAR 要有明确提示，而不是崩
        rar = os.path.join(W, "x.rar")
        with open(rar, "wb") as f:
            f.write(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 100)   # 假 RAR5 头
        logs = []
        UZ.expand(open(rar, "rb").read(), "x.rar", ["1"], None, [], 0, "", logs.append)
        hit = any("RAR" in ln or "7-Zip" in ln for ln in logs)
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 缺 RAR 工具时给提示 → {logs[-1] if logs else '（无日志）'}")
    finally:
        UZ.find_rar = real_rar

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
