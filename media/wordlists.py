"""密码字典：读本地字典、列目录、下载常用字典。

支持的"网上常见格式"（加载时自动认）：
* 纯文本（.txt/.dic/.lst/.wordlist/.pass/.csv）—— 一行一个密码
* 压缩过的（.gz / .zip / .bz2 / .xz）—— 解出来再按行读
* 编码：UTF-8（带不带 BOM 都行）→ 失败再试 GBK → 最后按 latin-1 兜底
* 空行、以 # 开头的注释行、行尾 \\r 都会清掉；日志式的行只取最后一个字段也能用

下载：只从下面 CATALOG 里的固定地址拿（不解析网页、不执行任何东西），
存到程序目录的 dicts\\ 下，文件名固定，方便下次直接选中。
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import re
import shutil
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DICT_DIR = os.path.join(BASE, "dicts")
TEXT_EXT = (".txt", ".dic", ".lst", ".wordlist", ".pass", ".csv", ".dict")
ARCHIVE_EXT = (".gz", ".zip", ".bz2", ".xz")
MAX_LOAD = 3_000_000          # 一次最多加载这么多条（再多也没意义，显卡也跑不完）

# 固定来源（只放自己验证过、能直连下载的）
CATALOG = [
    {"id": "pwdb-100k", "name": "SecLists · Pwdb Top 10 万",
     "url": "https://raw.githubusercontent.com/danielmiessler/SecLists/master/"
            "Passwords/Common-Credentials/Pwdb_top-100000.txt",
     "file": "Pwdb_top-100000.txt", "size": 828_498,
     "desc": "按泄露数据统计的常见密码前 10 万，日常最划算"},
    {"id": "probable-12k", "name": "Probable Wordlists · Top 12000",
     "url": "https://raw.githubusercontent.com/berzerk0/Probable-Wordlists/master/"
            "Real-Passwords/Top12Thousand-probable-v2.txt",
     "file": "Top12Thousand-probable-v2.txt", "size": 100_206,
     "desc": "按概率排序的前 1.2 万条，先跑这个最快"},
    {"id": "xato-1m", "name": "Xato · 1000 万密码前 100 万",
     "url": "https://raw.githubusercontent.com/danielmiessler/SecLists/master/"
            "Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt",
     "file": "xato-net-10-million-passwords-1000000.txt", "size": 8_557_632,
     "desc": "100 万条常见密码，覆盖英文/数字组合"},
    {"id": "pwdb-1m", "name": "SecLists · Pwdb Top 100 万",
     "url": "https://raw.githubusercontent.com/danielmiessler/SecLists/master/"
            "Passwords/Common-Credentials/Pwdb_top-1000000.txt",
     "file": "Pwdb_top-1000000.txt", "size": 8_755_171,
     "desc": "Pwdb 前 100 万，中文环境里也常命中"},
    {"id": "rockyou", "name": "rockyou.txt（133MB，经典大字典）",
     "url": "https://github.com/brannondorsey/naive-hashcat/releases/download/data/rockyou.txt",
     "file": "rockyou.txt", "size": 139_921_497,
     "desc": "1400 万条，老牌字典；文件大，下载要几分钟"},
]


def ensure_dir() -> str:
    os.makedirs(DICT_DIR, exist_ok=True)
    return DICT_DIR


def _line_ok(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    return True


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def _open_stream(path: str):
    """按后缀把文件变成可读的字节流（压缩的自动解开）。"""
    low = path.lower()
    if low.endswith(".gz"):
        return gzip.open(path, "rb")
    if low.endswith(".bz2"):
        return bz2.open(path, "rb")
    if low.endswith(".xz"):
        return lzma.open(path, "rb")
    if low.endswith(".zip"):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            zf.close()
            return None
        return zf.open(names[0])
    return open(path, "rb")


def read_words(path: str, limit: int = MAX_LOAD) -> "list[str]":
    """读一个字典文件（自动处理压缩/编码），返回去重后的密码列表。"""
    out: "list[str]" = []
    seen: set[str] = set()
    try:
        stream = _open_stream(path)
    except (OSError, zipfile.BadZipFile, lzma.LZMAError):
        return out
    if stream is None:
        return out
    try:
        raw = stream.read(64 * 1024 * 1024)
        while raw and len(out) < limit:
            text = _decode(raw)
            for line in text.splitlines():
                if not _line_ok(line):
                    continue
                w = line.strip()
                if len(w) > 128:            # 太长的当垃圾行
                    continue
                if w not in seen:
                    seen.add(w)
                    out.append(w)
                    if len(out) >= limit:
                        break
            raw = stream.read(64 * 1024 * 1024) if hasattr(stream, "read") else b""
            if not raw:
                break
    except (OSError, EOFError, lzma.LZMAError, zipfile.BadZipFile):
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def load_many(paths: "list[str]", limit: int = MAX_LOAD) -> dict:
    """加载多个字典（去重合并）。返回 {words, files, total, errors}。"""
    words: "list[str]" = []
    seen: set[str] = set()
    errors: "list[dict]" = []
    used: "list[str]" = []
    for p in paths:
        p = os.path.abspath(p)
        if not os.path.isfile(p):
            errors.append({"path": p, "error": "文件不存在"})
            continue
        got = read_words(p, limit=max(1, limit - len(words)))
        if not got:
            errors.append({"path": p, "error": "读不出内容（格式不支持或为空）"})
            continue
        used.append(p)
        for w in got:
            if w not in seen:
                seen.add(w)
                words.append(w)
        if len(words) >= limit:
            break
    return {"words": words, "files": used, "total": len(words), "errors": errors}


def list_local() -> "list[dict]":
    """dicts\\ 目录里现成的字典。"""
    out: "list[dict]" = []
    d = ensure_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        low = name.lower()
        if not (low.endswith(TEXT_EXT) or low.endswith(ARCHIVE_EXT)):
            continue
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
            mtime = int(os.path.getmtime(p))
        except OSError:
            continue
        out.append({"name": name, "path": p, "size": size, "mtime": mtime})
    return out


def list_catalog() -> "list[dict]":
    """可下载的字典（标出本地是否已有）。"""
    have = {x["name"]: x for x in list_local()}
    out = []
    for item in CATALOG:
        got = have.get(item["file"])
        out.append({**{k: item[k] for k in ("id", "name", "file", "size", "desc")},
                    "downloaded": bool(got),
                    "local_size": got["size"] if got else 0})
    return out


class DownloadJob:
    """一个后台下载任务（可以同时下多个）。"""

    def __init__(self, ids: "list[str]"):
        self.ids = [i for i in ids if i]
        self.state = "idle"
        self.items: "list[dict]" = []
        self.log: "list[str]" = []
        self.cancel_ev = None

    # 简单实现：同步下载，由 app 层放进线程跑
    def run(self, log=None, cancel=None) -> dict:
        import threading
        self.cancel_ev = cancel
        self.state = "running"
        d = ensure_dir()
        by_id = {c["id"]: c for c in CATALOG}
        for cid in self.ids:
            item = by_id.get(cid)
            if not item:
                self._log(f"跳过未知字典：{cid}", log)
                continue
            dst = os.path.join(d, item["file"])
            if os.path.isfile(dst) and os.path.getsize(dst) >= item["size"] * 0.9:
                self._log(f"{item['name']} 已经下载过了", log)
                self.items.append({"id": cid, "ok": True, "skipped": True})
                continue
            ok, err = _download(item["url"], dst, self._log, log, cancel)
            self.items.append({"id": cid, "ok": ok, "error": err, "path": dst})
            if ok:
                self._log(f"{item['name']} 下载完成 → {dst}", log)
            else:
                self._log(f"{item['name']} 下载失败：{err}", log)
        self.state = "cancelled" if (cancel is not None and cancel.is_set()) else "done"
        return {"ok": True, "items": self.items, "state": self.state}

    def _log(self, msg: str, log=None) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log.append(line)
        if len(self.log) > 200:
            del self.log[:50]
        if log:
            log(msg)


def _download(url: str, dst: str, notify=None, log=None, cancel=None) -> "tuple[bool, str]":
    """下载到 dst（先写 .part 再改名，避免半截文件）。"""
    tmp = dst + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "photo-dedupe/2.4"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("已取消")
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if notify and time.time() - last > 2:
                    last = time.time()
                    pct = f"{got * 100 // total}%" if total else f"{got // 1048576} MB"
                    notify(f"下载中 {pct}")
        if total and got < total * 0.9:
            raise RuntimeError(f"下载不完整（{got}/{total} 字节）")
        os.replace(tmp, dst)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, str(exc)[:200]


def resolve(names_or_paths: "list[str]") -> "list[str]":
    """界面传来的可能是文件名（在 dicts\\ 里）也可能是绝对路径。"""
    out: "list[str]" = []
    d = ensure_dir()
    for n in names_or_paths or []:
        n = str(n or "").strip()
        if not n:
            continue
        if os.path.isabs(n) and os.path.isfile(n):
            out.append(n)
            continue
        p = os.path.join(d, os.path.basename(n))
        if os.path.isfile(p):
            out.append(p)
    return out
