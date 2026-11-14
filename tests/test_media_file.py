"""媒体预览接口 `/api/media/file` 的回归测试。

预览靠这个接口把真实文件流给浏览器，必须满足：
* 200 + 正确的 Content-Type（mp4 → video/mp4，m4a → audio/mp4 …）
* 支持 Range → 206 + Content-Range（否则进度条拖不动）
* 只认库里的 id，随便给个 id 就 404，不会把任意路径吐出去
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app as A  # noqa: E402


def main() -> int:
    ok = True
    tmp = tempfile.mkdtemp(prefix="pd_mfile_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir, exist_ok=True)

    # 造两个"媒体文件"（内容不重要，只看能不能流出去）
    mp4 = os.path.join(tmp, "测试视频.mp4")
    m4a = os.path.join(tmp, "测试音频.m4a")
    with open(mp4, "wb") as f:
        f.write(bytes(range(256)) * 400)       # 102400 字节
    with open(m4a, "wb") as f:
        f.write(b"AUDIO" * 5000)

    A.APP = A.App(data_dir, engine_mode="cpu")
    try:
        st = A.APP.media
        st.add_root("video", tmp, True)
        st.upsert_many("video", [{
            "path": mp4, "root": tmp, "name": os.path.basename(mp4), "size": os.path.getsize(mp4),
            "mtime": 0, "dur": 3.0, "w": 640, "h": 360, "sig": b"\x00", "aux": None,
            "aux2": None, "thumb": b"", "meta": {"dim": 1},
        }])
        st.upsert_many("audio", [{
            "path": m4a, "root": tmp, "name": os.path.basename(m4a), "size": os.path.getsize(m4a),
            "mtime": 0, "dur": 2.0, "w": 0, "h": 0, "sig": b"\x00", "aux": None,
            "aux2": None, "thumb": b"", "meta": {},
        }])
        vid = st._conn.execute("SELECT id FROM media_items WHERE kind='video'").fetchone()["id"]
        aud = st._conn.execute("SELECT id FROM media_items WHERE kind='audio'").fetchone()["id"]

        # ① App.media_file：MIME 正确
        got = A.APP.media_file(vid)
        ok &= bool(got) and got[1] == "video/mp4"
        print(f"[{'OK ' if got and got[1] == 'video/mp4' else 'FAIL'}] media_file(视频) → {got[1] if got else None}")
        got2 = A.APP.media_file(aud)
        ok &= bool(got2) and got2[1] == "audio/mp4"
        print(f"[{'OK ' if got2 and got2[1] == 'audio/mp4' else 'FAIL'}] media_file(音频) → {got2[1] if got2 else None}")

        # ② 起一个临时 HTTP 服务测真实响应
        A.APP.settings = dict(A.APP.settings)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        def fetch(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()

        status, hdr, body = fetch(f"/api/media/file?id={vid}")
        same = body == open(mp4, "rb").read()
        ok &= status == 200 and hdr.get("Content-Type") == "video/mp4" and same
        print(f"[{'OK ' if ok else 'FAIL'}] GET 整段：{status} {hdr.get('Content-Type')} "
              f"{len(body)} 字节 · 内容一致={same} · 支持Range={hdr.get('Accept-Ranges')}")

        status, hdr, body = fetch(f"/api/media/file?id={vid}", {"Range": "bytes=100-199"})
        cr = hdr.get("Content-Range")
        good = status == 206 and len(body) == 100 and cr == f"bytes 100-199/{os.path.getsize(mp4)}"
        ok &= good
        print(f"[{'OK ' if good else 'FAIL'}] Range 请求：{status} {cr} 读回 {len(body)} 字节")

        status, hdr, body = fetch(f"/api/media/file?id={aud}")
        ok &= hdr.get("Content-Type") == "audio/mp4"
        print(f"[{'OK ' if hdr.get('Content-Type') == 'audio/mp4' else 'FAIL'}] 音频：{status} {hdr.get('Content-Type')}")

        try:
            fetch("/api/media/file?id=999999")
            print("[FAIL] 不存在的 id 居然返回了内容")
            ok = False
        except urllib.error.HTTPError as e:
            ok &= e.code == 404
            print(f"[{'OK ' if e.code == 404 else 'FAIL'}] 不存在的 id → {e.code}")

        httpd.shutdown()
    finally:
        try:
            A.APP.store.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
