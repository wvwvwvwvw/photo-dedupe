"""小说章节级重合度自检：造两版同一本书 + 一本无关书，验证重合章数。"""
from __future__ import annotations

import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "_test"))

from media import novel as N  # noqa: E402

W = os.path.join(BASE, "_test", "_novel")


def build() -> None:
    import make_novels  # 复用造样本的脚本
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(W, exist_ok=True)
    make_novels.write(os.path.join(W, "星河彼岸 全本.txt"), 60, 100)
    make_novels.write(os.path.join(W, "星河彼岸 精校版.txt"), 60, 100, edit=True, extra=2)
    make_novels.write(os.path.join(W, "完全不同.txt"), 40, 5000)


def main() -> int:
    build()
    a = os.path.join(W, "星河彼岸 全本.txt")
    b = os.path.join(W, "星河彼岸 精校版.txt")
    c = os.path.join(W, "完全不同.txt")
    same = N.chapter_overlap(a, b)
    diff = N.chapter_overlap(a, c)
    total = max(same.get("a", 0), same.get("b", 0))
    ok = (same.get("matched", 0) >= 0.9 * total) and diff.get("matched", 0) == 0
    print(f"[{'OK ' if ok else 'FAIL'}] 改字版重合 {same.get('matched')}/{total} "
          f"({round((same.get('ratio') or 0) * 100)}%) · 无关书重合 {diff.get('matched')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
