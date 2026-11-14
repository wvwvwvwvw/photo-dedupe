"""多语言（i18n.js）的静态检查。

不是测浏览器渲染，而是保证"关键文案都翻到了"：
导航、页面标题、主要按钮、筛选标签这些中文短语，en / ja 两本字典里必须有。
少一条就报出来，避免切换语言后大片还是中文。
"""

from __future__ import annotations

import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 必须翻译的关键短语（界面骨架）
MUST = [
    "图片", "视频", "音频", "小说", "广告过滤", "繁简 / 编码", "打包", "解压", "暴力破解",
    "设置", "存档", "开始扫描", "暂停", "继续", "停止", "浏览", "添加", "含子文件夹",
    "张图片", "组重复", "张多余", "可释放", "相似度阈值", "排序", "每组至少", "范围",
    "检索强度", "自动选择", "导出清单", "其他文件", "清空选择", "移到文件夹", "移到回收站",
    "保留", "预览", "对比", "取消", "删除", "保存当前结果", "我的存档",
    "载入", "合并", "覆盖", "导出", "系统播放器", "打开所在文件夹", "跟随系统",
]


def dict_of(src: str, name: str) -> set[str]:
    m = re.search(rf"const {name} = \{{(.*?)\n  \}};", src, re.S)
    if not m:
        raise SystemExit(f"找不到 {name} 字典")
    return set(re.findall(r'^\s*"([^"]+)"\s*:', m.group(1), re.M))


def main() -> int:
    js = open(os.path.join(BASE, "web", "i18n.js"), encoding="utf-8").read()
    en = dict_of(js, "EN")
    ja = dict_of(js, "JA")
    ok = True
    print(f"字典条目：en {len(en)} 条 · ja {len(ja)} 条")

    for lang, d in (("en", en), ("ja", ja)):
        miss = [k for k in MUST if k not in d]
        good = not miss
        ok &= good
        print(f"[{'OK ' if good else 'FAIL'}] {lang} 覆盖关键短语"
              + (f"，缺 {miss[:6]}" if miss else ""))

    # 结构检查：必须有 t/apply/setLang，并且启动时会读 localStorage
    for need in ("I18N.setLang", "I18N.apply", "I18N.t", "pd.lang", "MutationObserver"):
        good = need in js
        ok &= good
        print(f"[{'OK ' if good else 'FAIL'}] 包含 {need}")

    # 页面里必须有语言选择器，且默认是"跟随系统"
    html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    good = 'id="langSel"' in html and "跟随系统" in html
    ok &= good
    print(f"[{'OK ' if good else 'FAIL'}] index.html 有 #langSel 且含「跟随系统」选项")
    good2 = html.index('id="langSel"') < html.index('<nav class="nav"')
    ok &= good2
    print(f"[{'OK ' if good2 else 'FAIL'}] 语言选择器在导航上方（醒目）")
    good3 = "/static/i18n.js" in html
    ok &= good3
    print(f"[{'OK ' if good3 else 'FAIL'}] 页面引入了 i18n.js")

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
