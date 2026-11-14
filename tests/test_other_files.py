"""没参与查重的文件（太小 / 动图 / 打不开）必须有去处。

踩过的坑：扫描结束会 `prune_missing(kind, root, seen)` 把"没见过"的记录删掉，
而跳过的文件本来就不在 seen 里 —— 结果刚记下的 skipped 行立刻被清掉，
界面上"其他文件"永远是 0。这个测试专门盯这个。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import scan as S  # noqa: E402
from media.store import MediaStore  # noqa: E402


def make_files(d: str) -> tuple[str, str]:
    tiny = os.path.join(d, "tiny.mp4")
    with open(tiny, "wb") as f:
        f.write(b"\x00" * 4096)                 # 4KB，低于 64KB 门槛
    gif = os.path.join(d, "anim.gif")
    with open(gif, "wb") as f:
        f.write(b"GIF89a" + b"\x00" * 2048)
    return tiny, gif


def run_scan(store, root: str) -> None:
    job = S.MediaScanJob(store, "video", [{"path": root, "recursive": 1}], workers=1)
    job.start()
    job.join(timeout=120)


def main() -> int:
    ok = True
    tmp = tempfile.mkdtemp(prefix="pd_other_")
    db = os.path.join(tmp, "library.db")
    media = os.path.join(tmp, "videos")
    os.makedirs(media, exist_ok=True)
    tiny, gif = make_files(media)

    st = MediaStore(db)
    try:
        # ① iter_files 会收集"跳过"的
        small: list = []
        files = list(S.iter_files(media, "video", True, set(), False, small))
        names = sorted(os.path.basename(p) for p, _ in small)
        good = sorted(["tiny.mp4", "anim.gif"]) == names and not files
        ok &= good
        print(f"[{'OK ' if good else 'FAIL'}] iter_files 收集到 {names}（待分析 {len(files)} 个）")

        # ② 扫一遍 → 两个都进"其他文件"
        st.add_root("video", media, True)
        run_scan(st, media)
        rows = st.other_items("video")
        got = sorted(r["name"] for r in rows)
        good = got == ["anim.gif", "tiny.mp4"]
        ok &= good
        print(f"[{'OK ' if good else 'FAIL'}] 扫描后其他文件：{got}")

        # ③ 再扫一遍 → 不能被 prune 删掉（这就是那个 bug）
        run_scan(st, media)
        rows2 = st.other_items("video")
        good2 = len(rows2) == 2
        ok &= good2
        print(f"[{'OK ' if good2 else 'FAIL'}] 二次扫描后仍在：{sorted(r['name'] for r in rows2)}")

        # ④ 原因写得清楚
        reasons = {r["name"]: r["reason"] for r in rows2}
        good3 = "动图" in reasons.get("anim.gif", "") and "64 KB" in reasons.get("tiny.mp4", "")
        ok &= good3
        print(f"[{'OK ' if good3 else 'FAIL'}] 原因字段：{reasons}")
    finally:
        st.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
