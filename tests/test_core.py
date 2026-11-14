"""核心链路自检：分析目录 -> 入库 -> 聚类 -> 打印分组结果。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dedupe.grouping import build_groups  # noqa: E402
from dedupe.hashing import analyze_image  # noqa: E402
from dedupe.scanner import iter_image_files  # noqa: E402
from dedupe.store import Store  # noqa: E402

IMGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imgs")


def main():
    t0 = time.time()
    rows = []
    for p in iter_image_files(IMGS):
        r = analyze_image(p)
        r["root"] = IMGS
        rows.append(r)
    print(f"分析 {len(rows)} 张，用时 {time.time() - t0:.2f}s")

    db = os.path.join(tempfile.mkdtemp(), "t.db")
    store = Store(db)
    store.upsert_many(rows)
    data = store.load_index()

    for th in (6, 10, 14):
        res = build_groups(store, data, threshold=th, min_size=2, limit=50)
        gs = res["groups"]
        st = res["stats"]
        print(f"\n阈值 {th} [{st['mode']}]: {st['groups']} 组, 重复 {st['duplicates']} 张, "
              f"可释放 {st['reclaim'] / 1048576:.1f}MB, 耗时 {st['elapsed_ms']}ms")
        for g in gs[:3]:
            names = [i["name"] for i in g["images"]]
            print(f"  组{g['no']} n={g['count']} sim={g['min_sim']}~{g['max_sim']} "
                  f"best={names[0]} -> {names[1:]}")

    res = build_groups(store, data, threshold=10, min_size=2, limit=50)
    big = max(res["groups"], key=lambda g: g["count"])
    names = [i["name"] for i in big["images"]]
    assert big["count"] >= 8, names
    assert not any(n.startswith("C_") for n in names), names
    # 分页（现在精度高，合成样本可能只剩 1 组：只有在多于 1 组时才要求 has_more）
    total_groups = res["stats"]["groups"]
    page = build_groups(store, data, threshold=10, min_size=2, limit=1)
    assert page["page"]["total_groups"] == total_groups
    if total_groups > 1:
        assert page["page"]["has_more"] is True
        assert len(page["groups"]) == 1
    else:
        assert page["page"]["has_more"] is False
    store.close()
    print("\n✅ 自检通过")


if __name__ == "__main__":
    main()
