"""灰度指纹补算的时机与开销。

用户反馈"还没开始扫描就占 CPU"——原因是老库在**启动**时就把补指纹的线程拉满了。
现在的规则：
* 程序启动**不**自动补指纹
* 只有真正要看分组时（`groups()`）才在后台补，而且 4 线程 + 限速
* 补完自动停，`/api/patch/status` 能查进度
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app as A  # noqa: E402
from dedupe.store import Store  # noqa: E402


def seed(db: str, paths: list[str]) -> int:
    st = Store(db)
    try:
        recs = []
        for i, p in enumerate(paths):
            recs.append({
                "path": p, "root": os.path.dirname(p), "name": os.path.basename(p),
                "size": os.path.getsize(p), "mtime": os.path.getmtime(p),
                "w": 800, "h": 600, "fmt": "JPEG", "pix": 480000,
                "phash": f"{0x1111 + i:016x}", "phash_c": f"{0x1111 + i:016x}",
                "trim": "0,0,0,0", "dhash": f"{0x2222 + i:016x}", "ahash": f"{0x3333 + i:016x}",
                "sharp": 10.0, "mr": 120, "mg": 120, "mb": 120, "sha256": "b" * 64,
                "patch": None,          # 故意留空，模拟老库
            })
        st.upsert_many(recs)
        st._conn.execute("INSERT OR REPLACE INTO roots(path, recursive, added, last_scan, status) "
                         "VALUES(?,1,?,0,'ok')", (os.path.dirname(paths[0]), time.time()))
        st._conn.commit()
        return len(recs)
    finally:
        st.close()


def main() -> int:
    ok = True
    tmp = tempfile.mkdtemp(prefix="pd_patch_")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir, exist_ok=True)
    db = os.path.join(data_dir, "library.db")

    # 从现有库里挑几张真实图片（要能读到，补指纹才有意义）
    src = Store(os.path.join(BASE, "data", "library.db"))
    try:
        rows = src._conn.execute(
            "SELECT path FROM images WHERE status='ok' ORDER BY RANDOM() LIMIT 8").fetchall()
    finally:
        src.close()
    paths = [r["path"] for r in rows if os.path.isfile(r["path"])]
    if not paths:
        print("跳过：库里的图片路径都读不到")
        return 0
    n = seed(db, paths)
    print(f"准备了 {n} 条缺指纹的记录")

    a = A.App(data_dir, engine_mode="cpu")
    try:
        # ① 启动不该开始补
        time.sleep(0.4)
        early = a._patch_thread is not None and a._patch_thread.is_alive()
        ok &= not early
        print(f"[{'OK ' if not early else 'FAIL'}] 启动后没有自动补指纹（running={a.patch_status()['running']}）")

        # ② 请求分组时才补
        a.groups(root=None, threshold=10, min_size=2, sort="count", limit=5, offset=0)
        t0 = time.time()
        while time.time() - t0 < 60:
            st = a.patch_status()
            if not st["running"]:
                break
            time.sleep(0.3)

        con = sqlite3.connect(db)
        left = con.execute("SELECT COUNT(*) FROM images WHERE status='ok' AND patch IS NULL").fetchone()[0]
        con.close()
        good = left == 0 and not a.patch_status()["running"]
        ok &= good
        print(f"[{'OK ' if good else 'FAIL'}] 看分组后补完并自动停：剩余缺失 {left}")

        # ③ 再请求分组不会重复启动
        a.groups(root=None, threshold=10, min_size=2, sort="count", limit=5, offset=0)
        time.sleep(0.3)
        st2 = a.patch_status()
        good2 = not st2["running"] and st2["done"] >= 0
        ok &= good2
        print(f"[{'OK ' if good2 else 'FAIL'}] 不会重复补：running={st2['running']}")
    finally:
        try:
            a.store.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
