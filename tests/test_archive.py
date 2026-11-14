"""存档保存 / 覆盖 / 载入的回归测试。

重点盯两个曾经出过问题的地方：
1. `dump_images()` 不能带二进制字段（`patch` 这类 BLOB）——带进去 json 序列化
   会写到一半抛 TypeError，界面上就是"覆盖失败"，目录里留下半截 .tmp。
2. 写盘失败时必须把 .tmp 清掉，别留垃圾文件。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from dedupe import archive  # noqa: E402
from dedupe.store import Store  # noqa: E402


def _seed(st: Store, n: int = 3) -> None:
    recs = []
    for i in range(n):
        recs.append({
            "path": f"E:\\测试\\p{i}.jpg", "root": "E:\\测试", "name": f"p{i}.jpg",
            "size": 1000 + i, "mtime": 1700000000.0, "w": 800, "h": 600, "fmt": "JPEG",
            "pix": 480000, "phash": f"{0x1234 + i:016x}", "phash_c": f"{0x1234 + i:016x}",
            "trim": "0,0,0,0", "dhash": f"{0x5678 + i:016x}", "ahash": f"{0x9abc + i:016x}",
            "sharp": 12.5, "mr": 128, "mg": 128, "mb": 128, "sha256": "a" * 64,
            # 关键：带一个 BLOB，模拟灰度指纹
            "patch": bytes(range(256)),
        })
    st.upsert_many(recs)
    st._conn.execute("INSERT OR REPLACE INTO roots(path, recursive, added, last_scan, status) "
                     "VALUES(?,1,?,0,'ok')", ("E:\\测试", 1700000000.0))
    st._conn.commit()


def main() -> int:
    ok = True
    tmp = tempfile.mkdtemp(prefix="pd_arch_")
    db = os.path.join(tmp, "library.db")
    adir = os.path.join(tmp, "archives")
    st = Store(db)
    try:
        _seed(st)

        # ① dump_images 不含 bytes，能直接 json 序列化
        imgs = st.dump_images()
        has_blob = [k for k, v in imgs[0].items() if isinstance(v, (bytes, bytearray))]
        ok &= not has_blob
        print(f"[{'OK ' if not has_blob else 'FAIL'}] dump_images 无二进制字段"
              f"（字段 {len(imgs[0])} 个）")
        try:
            json.dumps(imgs)
            print("[OK ] dump_images 可以 JSON 序列化")
        except TypeError as exc:
            print("[FAIL] dump_images 序列化失败:", exc)
            ok = False

        # ② 保存 → 文件、索引都在，没有 .tmp
        meta = archive.save(st, adir, "测试存档", {"threshold": 10})
        p = os.path.join(adir, meta["id"] + ".json")
        ok &= os.path.isfile(p)
        ok &= not os.path.exists(p + ".tmp")
        print(f"[{'OK ' if os.path.isfile(p) else 'FAIL'}] 保存成功：{meta['id']} "
              f"{meta['images']} 张 {meta['size']} 字节")

        # ③ 覆盖（同 aid）→ 内容更新、索引不重复
        _seed(st, 4)          # 再加一张，模拟"扫描后又多了图"
        meta2 = archive.save(st, adir, "测试存档", {"threshold": 8}, aid=meta["id"])
        items = archive.list_all(adir)
        ok &= meta2["id"] == meta["id"] and len(items) == 1
        ok &= meta2["images"] == 4
        print(f"[{'OK ' if meta2['images'] == 4 and len(items) == 1 else 'FAIL'}] "
              f"覆盖成功：{meta2['images']} 张，索引里 {len(items)} 条")

        # ④ 读回来能塞回另一个库
        payload = archive.read(adir, meta["id"])
        ok &= bool(payload) and _payload_ok(payload)
        db2 = os.path.join(tmp, "restore.db")
        st2 = Store(db2)
        try:
            st2.replace_all(payload["images"], payload["roots"])
            n = st2.stats()["images"]
            ok &= n == 4
            print(f"[{'OK ' if n == 4 else 'FAIL'}] 载入成功：恢复 {n} 张")
        finally:
            st2.close()

        # ⑤ 写盘失败要清掉半截 .tmp
        orig_dump = json.dump

        def boom(*a, **kw):
            a[1].write('{"app":"partial"')     # 先写半截
            raise TypeError("模拟序列化失败")

        json.dump = boom
        bad = os.path.join(adir, "bad.json")
        try:
            try:
                archive.save(st, adir, "会失败", {}, aid="bad")
            except TypeError:
                pass
            left = os.path.exists(bad + ".tmp")
            ok &= not left
            print(f"[{'OK ' if not left else 'FAIL'}] 写盘失败后没有残留 .tmp")
        finally:
            json.dump = orig_dump
    finally:
        st.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("通过" if ok else "失败")
    return 0 if ok else 1


def _payload_ok(p: dict) -> bool:
    return isinstance(p, dict) and p.get("app") == archive.APP_TAG and isinstance(p.get("images"), list)


if __name__ == "__main__":
    raise SystemExit(main())
