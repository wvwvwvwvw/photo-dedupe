"""音频/视频预览的波形图：后端取样 + 前端接口字段。

真实音频用 ffmpeg 现造（几段不同音量），验证：
* ffmpeg.waveform 能把整段压成 N 个 (min,max) 对
* 音量大的段峰值大、安静的段峰值小（波形能反映起伏）
* /api/media/waveform 返回的字段够前端画图（peaks/duration/buckets）
* 没有音轨的文件返回 ok=false 而不是抛异常
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app as A  # noqa: E402
from media import ffmpeg as mffmpeg  # noqa: E402


def make_audio(dst_dir: str) -> str:
    ff = mffmpeg.exe()
    parts = []
    for i, amp in enumerate((1.0, 0.25, 0.7)):
        p = os.path.join(dst_dir, f"seg{i}.wav")
        subprocess.run([ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                        "-af", f"volume={amp}", "-ar", "44100", "-ac", "2", p],
                       capture_output=True)
        parts.append(p)
    lst = os.path.join(dst_dir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    out = os.path.join(dst_dir, "wave.m4a")
    subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c:a", "aac", out],
                   capture_output=True)
    return out


def main() -> int:
    ok = True
    tmp = tempfile.mkdtemp(prefix="pd_wave_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir, exist_ok=True)
    audio = make_audio(tmp)

    # ① 后端：波形 + 音量差异
    got = mffmpeg.waveform(audio, buckets=90)
    good = bool(got) and len(got[0]) == 180
    ok &= good
    print(f"[{'OK ' if good else 'FAIL'}] waveform 返回 {len(got[0])//2 if got else 0} 桶，"
          f"时长 {got[1] if got else '-'}s")
    if good:
        peaks = got[0]
        seg = [max(peaks[i * 2:(i + 30) * 2]) for i in range(0, 90, 30)]
        ordered = seg[0] > seg[1] and seg[2] > seg[1]
        ok &= ordered
        print(f"[{'OK ' if ordered else 'FAIL'}] 三段音量：{seg}（应递减再回升）")

    # ② 接口
    A.APP = A.App(data_dir, engine_mode="cpu")
    try:
        st = A.APP.media
        st.add_root("audio", tmp, True)
        st.upsert_many("audio", [{
            "path": audio, "root": tmp, "name": os.path.basename(audio),
            "size": os.path.getsize(audio), "mtime": 0, "dur": 3.6, "w": 0, "h": 0,
            "sig": b"\x00", "aux": None, "aux2": None, "thumb": b"", "meta": {},
        }])
        mid = st._conn.execute("SELECT id FROM media_items WHERE kind='audio'").fetchone()["id"]

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/media/waveform?id={mid}&buckets=120", timeout=120) as r:
            data = json.loads(r.read())
        fields = all(k in data for k in ("ok", "peaks", "duration", "buckets"))
        ok &= bool(data.get("ok")) and fields and len(data["peaks"]) == 240
        print(f"[{'OK ' if ok else 'FAIL'}] 接口返回 ok={data.get('ok')} "
              f"桶={data.get('buckets')} 时长={data.get('duration')}")

        # ③ 不存在的 id → 干净的 ok=false
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/media/waveform?id=999999", timeout=30) as r:
                bad = json.loads(r.read())
            good3 = bad.get("ok") is False and "error" in bad
        except urllib.error.HTTPError as e:
            good3 = e.code in (404, 200)
        ok &= good3
        print(f"[{'OK ' if good3 else 'FAIL'}] 不存在的 id 返回 ok=false")

        # ④ 视频文件（音轨在 0:a:0，不能 map 0:0）
        vid = os.path.join(tmp, "clip.mp4")
        subprocess.run([mffmpeg.exe(), "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=8:duration=2",
                        "-f", "lavfi", "-i", "sine=frequency=500:duration=2",
                        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", vid],
                       capture_output=True)
        if os.path.isfile(vid):
            st.upsert_many("audio", [{
                "path": vid, "root": tmp, "name": "clip.mp4", "size": os.path.getsize(vid),
                "mtime": 0, "dur": 2.0, "w": 160, "h": 120, "sig": b"\x00",
                "aux": None, "aux2": None, "thumb": b"", "meta": {"from_video": True},
            }])
            mid3 = st._conn.execute("SELECT id FROM media_items WHERE name='clip.mp4'").fetchone()["id"]
            got3 = A.APP.media_waveform(mid3)
            good5 = bool(got3.get("ok")) and len(got3.get("peaks") or []) > 0
            ok &= good5
            print(f"[{'OK ' if good5 else 'FAIL'}] 视频音轨波形：ok={got3.get('ok')} "
                  f"桶={got3.get('buckets')} 时长={got3.get('duration')}")
        else:
            print("[跳过] 造不出测试视频")

        # ⑤ 没音轨的文件（拿个文本文件冒充）
        txt = os.path.join(tmp, "noaudio.m4a")
        with open(txt, "wb") as f:
            f.write(b"not audio" * 100)
        st.upsert_many("audio", [{
            "path": txt, "root": tmp, "name": "noaudio.m4a", "size": 900, "mtime": 0,
            "dur": 0, "w": 0, "h": 0, "sig": b"\x00", "aux": None, "aux2": None,
            "thumb": b"", "meta": {},
        }])
        mid2 = st._conn.execute("SELECT id FROM media_items WHERE name='noaudio.m4a'").fetchone()["id"]
        got2 = A.APP.media_waveform(mid2)
        good4 = got2.get("ok") is False
        ok &= good4
        print(f"[{'OK ' if good4 else 'FAIL'}] 没音轨：{got2.get('error')}")

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
