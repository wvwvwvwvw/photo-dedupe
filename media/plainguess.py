"""已知明文攻击的"自动猜文件头"。

同一个格式的文件头是确定字节，只要加密条目是**不压缩（stored）**存的，
就能拿文件头当已知明文去反推密钥——用户什么都不用填。

只放"足够确定、且长度够 12 字节"的签名：bkcrack 至少要 12 字节已知明文，
其中至少 8 字节连续；签名太短（GIF 只有 6 字节、BMP 只有 2 字节）就凑不够，
硬猜只会白跑一次攻击，所以干脆不放。
"""

from __future__ import annotations

# 每项：(标签, 适用后缀, 文件头字节)
SIGNATURES = [
    ("PNG", {".png", ".apng"},
     b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),

    ("XML", {".xml", ".svg", ".xhtml", ".plist", ".rss", ".atom"},
     b'<?xml version="1.0"'),

    ("SQLite", {".db", ".sqlite", ".sqlite3", ".db3"},
     b"SQLite format 3\x00"),

    ("PDF 1.4", {".pdf"},
     b"%PDF-1.4\n%\xe2\xe3\xcf\xd3"),
    ("PDF 1.5", {".pdf"},
     b"%PDF-1.5\n%\xe2\xe3\xcf\xd3"),
    ("PDF 1.7", {".pdf"},
     b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"),
    ("PDF 1.6", {".pdf"},
     b"%PDF-1.6\n%\xe2\xe3\xcf\xd3"),

    ("HTML", {".html", ".htm", ".xhtml"},
     b"<!DOCTYPE html>"),

    ("JPEG/JFIF", {".jpg", ".jpeg", ".jpe"},
     b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"),
    ("JPEG/Exif", {".jpg", ".jpeg", ".jpe"},
     b"\xff\xd8\xff\xe1\x00\x00Exif\x00\x00"),

    ("ZIP", {".zip", ".jar", ".apk", ".docx", ".xlsx", ".pptx", ".epub"},
     b"PK\x03\x04\x14\x00\x06\x00"),

    ("gzip", {".gz", ".tgz"},
     b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"),

    ("7z", {".7z"},
     b"7z\xbc\xaf\x27\x1c\x00\x04"),

    ("RAR", {".rar"},
     b"Rar!\x1a\x07\x00"),

    ("RTF", {".rtf"},
     b"{\\rtf1\\ansi"),

    ("MP4", {".mp4", ".m4a", ".m4v", ".mov"},
     b"\x00\x00\x00\x18ftypmp42"),
]

# 后缀认不出来时，按这个顺序试（越常见、越长越靠前）
FALLBACK = ["PNG", "PDF 1.4", "XML", "ZIP", "SQLite"]
MAX_TRIES = 6          # 一次最多试几个猜测，免得每个都白跑几十秒


def guess_for(name: str) -> "list[tuple[str, bytes]]":
    """按文件后缀给候选文件头（最可能的排前面）。"""
    low = (name or "").lower()
    ext = ""
    i = low.rfind(".")
    if i >= 0:
        ext = low[i:]
    out: "list[tuple[str, bytes]]" = []
    for label, exts, sig in SIGNATURES:
        if ext and ext in exts:
            out.append((label, sig))
    return out


def common_guesses() -> "list[tuple[str, bytes]]":
    by_label = {label: sig for label, _exts, sig in SIGNATURES}
    return [(lab, by_label[lab]) for lab in FALLBACK if lab in by_label]


def candidates(entries: "list[dict]") -> "list[dict]":
    """给一组 zip 条目挑出"值得猜"的候选。

    只挑 stored（压缩法 0）的传统加密条目：deflate 条目的密文对应的是
    **压缩后**的字节，光知道原文的头是猜不出来的。
    每个候选 = {entry, label, plain}
    """
    out: "list[dict]" = []
    for e in entries:
        if int(e.get("method", -1)) != 0:
            continue
        if int(e.get("size") or 0) < 12:
            continue
        name = e.get("name") or ""
        for label, sig in (guess_for(name) or common_guesses()):
            if len(sig) < 12:
                continue
            if int(e.get("size") or 0) < len(sig):
                continue
            out.append({"entry": name, "label": label, "plain": sig})
            break                     # 一个条目先只试最像的那一个
    # 后缀认得出来的排在前面，长的签名优先（更快）
    out.sort(key=lambda c: (len(c["plain"]), c["entry"]))
    return out[:MAX_TRIES]
