"""从文件名里抠密码。

网上分享的加密文件常把密码写进文件名，例如：

    [合集修订版]《噪音旅行箱》限制版密码：hg;mxj+P_s;A09S.pdf
    XXX 解压密码 abc123.rar
    YYY_密码=888888.zip
    提取码：k9m2 (百度网盘那种)

这里按几个固定模式抠出候选密码，交给破解流程先试（比密码本还快）。
"""

from __future__ import annotations

import os
import re

# 「密码」「解压密码」「提取码」「password / passwd / pwd」后面跟分隔符和一段非空白
_KEY = r"(?:解压密码|压缩密码|密码|提取码|password|passwd|pwd)"
_SEP = r"[:：=＝\s]*"
_VALUE = r"[\"'“”‘’]?([^\s/\\<>|*?\"'“”‘’，。；;、）)\]】]+)"

_PATTERNS = [
    re.compile(_KEY + _SEP + _VALUE, re.I),
]

# 文件名里常见但明显不是密码的词
_BAD = {"密码", "解压密码", "提取码", "password", "pwd", "压缩包", "rar", "zip", "7z",
        "pdf", "docx", "xlsx", "pptx", "txt", "文件", "下载", "合集", "修订版", "限制版"}


def _clean(tok: str) -> str:
    tok = tok.strip().strip("\"'“”‘’")
    tok = tok.strip("，。；;、）)]】")
    return tok


def candidates(path_or_name: str, limit: int = 6) -> "list[str]":
    """从文件名/路径里猜密码（按可信度排序，去重，最多 limit 个）。"""
    name = os.path.basename(str(path_or_name or ""))
    stem, _ext = os.path.splitext(name)
    out: "list[str]" = []
    seen: set[str] = set()

    def add(tok: str) -> None:
        tok = _clean(tok)
        if not tok or len(tok) > 64 or tok in seen:
            return
        if tok.lower() in _BAD:
            return
        seen.add(tok)
        out.append(tok)

    # 先抓"从密码标记一直到名字结尾"的整段（最常见、且往往就是完整密码）
    m = re.search(_KEY + _SEP + r"(.+)$", stem)
    if m:
        add(m.group(1))
        # 空格分段的第一段也试一下
        parts = m.group(1).split()
        if parts:
            add(parts[0])
    for pat in _PATTERNS:
        for mm in pat.finditer(stem):
            add(mm.group(1))
    return out[:limit]
