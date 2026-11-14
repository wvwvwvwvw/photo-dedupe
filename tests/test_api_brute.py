"""通过 HTTP 接口跑一遍「暴力破解」子页的完整流程（等同于界面上点开始破解）。

用法：先起服务（例如 --port 8799），再
      runtime\\python.exe _test\\test_api_brute.py 8799
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "_test"))

import pyzipper as pz  # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "8799"
URL = f"http://127.0.0.1:{PORT}"
SRC = os.path.join(BASE, "_test", "_brute_src")
PWD = "10293847"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(URL + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    os.makedirs(SRC, exist_ok=True)
    zpath = os.path.join(SRC, "secret.zip")
    if not os.path.exists(zpath):
        with pz.AESZipFile(zpath, "w", encryption=pz.WZ_AES,
                           encryption_kwargs={"nbits": 256}) as zf:
            zf.setpassword(PWD.encode())
            zf.writestr("doc.txt", b"secret payload " * 60)
    print("hashcat:", get("/api/brute/hashcat").get("devices"))

    r = post("/api/brute/start", {
        "src": SRC, "passwords": [],
        "brute": {"workers": 4, "max_len": 8, "prefix": "", "suffix": ""},
        "recursive": True,
    })
    print("start:", r)
    if not r.get("ok"):
        return 1

    t0 = time.time()
    last_log = 0
    while True:
        s = get("/api/brute/status")
        log = s.get("log") or []
        if len(log) > last_log:
            for ln in log[last_log:]:
                print("   ·", ln)
            last_log = len(log)
        if not s.get("running"):
            break
        if time.time() - t0 > 300:
            print("超时")
            return 1
        time.sleep(0.8)

    print("results:", s.get("results"))
    ok = any((x.get("pwd") == PWD) for x in (s.get("results") or []))
    print(f"耗时 {time.time() - t0:.1f}s · 密码{'命中' if ok else '没找到'} · "
          f"成功 {s.get('found')} 失败 {s.get('failed')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
