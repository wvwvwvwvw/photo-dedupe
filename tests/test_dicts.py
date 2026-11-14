"""字典功能自检：读写常见格式 + 加载多个 + 目录清单。"""
from __future__ import annotations

import gzip
import os
import shutil
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import wordlists as WL  # noqa: E402


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="dict_test_")
    ok = True
    try:
        # ① 纯文本（注释行/空行/中文/去重）
        p1 = os.path.join(tmp, "a.txt")
        with open(p1, "w", encoding="utf-8") as f:
            f.write("# 注释\n123456\npassword\n\n123456\n中文密码\n")
        w1 = WL.read_words(p1)
        ok &= w1 == ["123456", "password", "中文密码"]
        # ② gzip
        p2 = os.path.join(tmp, "b.txt.gz")
        with gzip.open(p2, "wt", encoding="utf-8") as f:
            f.write("abc\n123456\ndef\n")
        w2 = WL.read_words(p2)
        ok &= w2 == ["abc", "123456", "def"]
        # ③ zip
        p3 = os.path.join(tmp, "c.zip")
        with zipfile.ZipFile(p3, "w") as zf:
            zf.writestr("words.txt", "xyz\n123456\n")
        w3 = WL.read_words(p3)
        ok &= w3 == ["xyz", "123456"]
        # ④ GBK 编码
        p4 = os.path.join(tmp, "d.txt")
        with open(p4, "wb") as f:
            f.write("中文密码\n测试密码\n".encode("gbk"))
        w4 = WL.read_words(p4)
        ok &= w4 == ["中文密码", "测试密码"]
        # ⑤ 多文件合并去重
        r = WL.load_many([p1, p2, p3])
        ok &= r["total"] == 6 and r["words"][0] == "123456"
        # ⑥ 目录清单（真实 dicts/）
        cat = WL.list_catalog()
        ok &= len(cat) >= 5 and all({"id", "name", "size"} <= set(c) for c in cat)
        print(f"[{'OK ' if ok else 'FAIL'}] 纯文本/压缩/GBK 读取正常，"
              f"合并 {r['total']} 条，清单 {len(cat)} 项")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

