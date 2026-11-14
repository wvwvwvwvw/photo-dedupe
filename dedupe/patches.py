"""补齐 16×16 灰度指纹（`images.patch`）。

指纹是"第二阶段确认"用的：pHash/dHash 只能筛出候选，真正像不像要看低频灰度结构。
老库（升级前扫的）没有这一列数据，需要补算；新扫描会自动写入，不用管这里。

调用方式：
* 程序启动时后台自动补（缺多少补多少，可被打断，下次接着补）
* 手动：`runtime\\python.exe app.py --fill-patches`
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps

BATCH = 2000


def make_patch(path: str) -> "bytes | None":
    """把一张图压成 16×16 灰度指纹（256 字节）。失败返回 None。"""
    try:
        with Image.open(path) as im:
            try:
                im.draft("L", (64, 64))        # JPEG 直接按 1/8 解码，快很多
            except Exception:                  # noqa: BLE001
                pass
            im.seek(0)
            im = ImageOps.exif_transpose(im) or im
            if im.mode != "L":
                im = im.convert("L")
            return im.resize((16, 16), Image.Resampling.BOX).tobytes()
    except Exception:                          # noqa: BLE001
        return None


def missing_count(store) -> int:
    try:
        row = store._conn.execute(
            "SELECT COUNT(*) FROM images WHERE status='ok' AND patch IS NULL").fetchone()
        return int(row[0])
    except Exception:                          # noqa: BLE001
        return 0


def fill_missing(store, *, limit: int = 0, workers: int = 8,
                 log=None, cancel=None, pace: float = 0.0,
                 on_progress=None) -> int:
    """补缺失的指纹，返回补了多少张。limit=0 表示有多少补多少。

    pace > 0 时每个批次后歇一会儿，把 CPU 占用压下来（后台任务别抢前台资源）。
    """
    import time as _t
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, path FROM images WHERE status='ok' AND patch IS NULL"
            + (" LIMIT %d" % int(limit) if limit else "")).fetchall()
    total = len(rows)
    if not total:
        return 0
    done = 0
    t0 = time.time()
    if log:
        log(f"补算指纹：{total:,} 张待补（{workers} 线程）")
    workers = max(1, min(int(workers), 16))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for s in range(0, total, BATCH):
            if cancel is not None and cancel.is_set():
                break
            chunk = rows[s:s + BATCH]
            patches = list(ex.map(lambda r: make_patch(r["path"]), chunk))
            payload = [(p, r["id"]) for r, p in zip(chunk, patches) if p]
            if payload:
                try:
                    # 后台线程和扫描线程共用一条连接：必须拿 store 的锁，
                    # 否则两边的事务会互相打断（"database is locked" / 半提交）
                    with store._lock:
                        store._conn.executemany("UPDATE images SET patch=? WHERE id=?", payload)
                        store._conn.commit()
                except Exception:              # noqa: BLE001
                    pass
            done += len(chunk)
            if on_progress is not None:
                try:
                    on_progress(done, total)
                except Exception:              # noqa: BLE001
                    pass
            if pace > 0:
                _t.sleep(pace)
            if log and (s // BATCH) % 5 == 0:
                el = time.time() - t0
                log(f"  指纹 {done:,}/{total:,}  {done / max(el, 1e-6):.0f} 张/秒")
    if log:
        log(f"补算完成：{done:,} 张，用时 {time.time() - t0:.0f}s")
    return done
