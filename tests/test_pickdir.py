"""选择框自检：现代对话框脚本 + 后端解析分支。"""
from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app as A  # noqa: E402

W = os.path.join(BASE, "_test", "_pick_check")


def main() -> int:
    ok = True

    # ① 现代对话框脚本存在、能被 PowerShell 编译（PROBE 模式不弹窗）
    script = A.PICK_PS1
    hit = os.path.isfile(script)
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 选择框脚本存在 → {script}")

    import subprocess
    env = dict(os.environ)
    env["PD_PICK_PROBE"] = "1"
    p = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy",
                        "Bypass", "-File", script], capture_output=True, text=True,
                       env=env, timeout=180)
    out = (p.stdout or "").strip()
    hit = "PROBE_OK" in out
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 现代对话框能构造（{out or p.stderr[:60]}）")

    # ② 后端解析：拿一个假脚本冒充对话框，验证 OK/CANCEL/FALLBACK 三种分支
    os.makedirs(W, exist_ok=True)
    fake = os.path.join(W, "fake_pick.ps1")
    real = A.PICK_PS1
    try:
        for body, expect in (("Write-Output 'OK|C:\\Windows'", "ok"),
                             ("Write-Output 'CANCEL'", "cancelled"),
                             ("Write-Output 'FALLBACK|boom'", "fallback")):
            with open(fake, "w", encoding="utf-8") as f:
                f.write(body)
            A.PICK_PS1 = fake
            r = A.native_pick_folder("测试", "", "")
            if expect == "ok":
                good = r.get("ok") and r.get("path") == "C:\\Windows"
            elif expect == "cancelled":
                good = (not r.get("ok")) and r.get("cancelled")
            else:
                good = not r.get("ok")   # 会走兜底对话框，这里只要求不抛异常
            ok &= bool(good)
            print(f"[{'OK ' if good else 'FAIL'}] 分支 {expect}: {r}")
    finally:
        A.PICK_PS1 = real

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
