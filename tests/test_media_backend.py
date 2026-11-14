# 媒体后端联调：扫描 → 入库 → 分组
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import grouping, scan  # noqa: E402
from media.store import MediaStore  # noqa: E402

SAMPLES = os.path.join(BASE, "_test", "媒体样本")
DB = os.path.join(BASE, "_test", "媒体样本", "media_test.db")
if os.path.exists(DB):
    os.remove(DB)
for ext in ("-wal", "-shm"):
    p = DB + ext
    if os.path.exists(p):
        os.remove(p)

# ---- 准备小说样本 ----
NOV = os.path.join(SAMPLES, "小说")
os.makedirs(NOV, exist_ok=True)


def write(path, chapters, words=800):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(1, chapters + 1):
            f.write(f"第{i}章 测试标题{i}\n")
            f.write(("正文内容。" * (words // 5)) + "\n\n")


write(os.path.join(NOV, "《星河彼岸》全本.txt"), 120)
write(os.path.join(NOV, "星河彼岸(精校版).TXT"), 118)
write(os.path.join(NOV, "星河彼岸 番外.txt"), 20)
write(os.path.join(NOV, "完全不同的一本书.txt"), 300)

store = MediaStore(DB)

tests = [
    ("video", SAMPLES),
    ("audio", SAMPLES),
    ("novel", NOV),
]

for kind, root in tests:
    print(f"\n===== {kind} =====")
    store.add_root(kind, root, True)
    roots = store.list_roots(kind)
    job = scan.MediaScanJob(store, kind, roots, workers=3, flush_rows=2, flush_secs=1.0)
    t0 = time.time()
    job.run()          # 同步跑完
    st = job.snapshot()
    print(f"扫描：total={st['total']} 新分析={st['hashed']} 跳过={st['reused']} "
          f"失败={st['errors']} 落库={st['writes']} 用时={time.time()-t0:.1f}s 状态={st['state']}")
    items = store.all_items(kind)
    print(f"入库 {len(items)} 条")
    for it in items[:6]:
        meta = it["meta"]
        extra = (f"{meta.get('duration_txt') or ''} {it['w']}x{it['h']}" if kind == "video"
                 else (f"{meta.get('duration_txt') or ''} {meta.get('bitrate')}k" if kind == "audio"
                       else f"{meta.get('chapters')}章 {meta.get('words')}字"))
        print(f"   {it['name'][:34]:36s} {extra}")
    r = grouping.build(kind, items)
    print(f"分组：{len(r['groups'])} 组，比较 {r['pairs']} 对")
    by_id = {it["id"]: it for it in items}
    for g in r["groups"][:4]:
        print(f"   代表 {by_id[g['rep']]['name'][:30]}")
        for m in g["members"]:
            print(f"      - {by_id[m['id']]['name'][:30]:32s} 相似 {m['sim']:.3f}")

print("\n=== 统计 ===")
for kind in ("video", "audio", "novel"):
    s = store.stats(kind)
    print(f"  {kind}: {s['items']} 条 {s['bytes']/1024/1024:.1f}MB 失败 {s['errors']}")
store.close()
