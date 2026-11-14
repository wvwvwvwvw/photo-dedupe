"""小说广告过滤：把输入文件夹里的 txt 清洗后写到输出文件夹（只读源文件）。"""

from __future__ import annotations

import os
import re
import threading
import time

MAX_READ = 32 * 1024 * 1024

# 广告行特征（命中即整行删除）
_AD = re.compile(
    r"(请记住|记住本站|本站|域名|网址|书友群|QQ群|qq群|群号|最新网址|最新地址|"
    r"无弹窗|手机版|手机阅读|手机站|百度搜索|更多精彩|加入书架|投推荐票|"
    r"月票|打赏|章节报错|一秒记住|免费阅读|全文阅读|尽在|首发|www\.|http|"
    r"[（(]\s*本章完\s*[)）]|求收藏|求订阅)", re.I)
# 纯噪声行：只有符号/数字/网址
_NOISE = re.compile(r"^[\s\d\-=_*#·•—―\W]{0,40}$")
_BLANK = re.compile(r"^\s*$")


def strip_ads(text: str) -> tuple[str, int, int]:
    """返回 (清洗后文本, 删除的行数, 原始行数)。保留段落空行。"""
    if not text:
        return "", 0, 0
    lines = text.splitlines()
    out, removed = [], 0
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append("")
            continue
        short = len(s) <= 90
        if short and (_AD.search(s) or _NOISE.match(s)):
            removed += 1
            continue
        if short and re.fullmatch(r"[-=*_#~\s]{3,}", s):
            removed += 1
            continue
        out.append(s)
    # 合并 3 个以上连续空行
    merged, blanks = [], 0
    for ln in out:
        if ln == "":
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        merged.append(ln)
    return "\n".join(merged).strip() + "\n", removed, len(lines)


def _decode(raw: bytes) -> tuple[str, str]:
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc
            except Exception:
                break
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            t = raw.decode(enc)
            if t.count("\ufffd") <= max(2, len(t) // 2000):
                return t, enc
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


class CleanJob(threading.Thread):
    """把 src 下的 .txt 清洗后写到 dst（保持相对目录结构）。"""
    daemon = True

    def __init__(self, src: str, dst: str, recursive: bool = True):
        super().__init__(name="novel-clean")
        self.src = os.path.abspath(src)
        self.dst = os.path.abspath(dst)
        self.recursive = bool(recursive)
        self.cancel_ev = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._st = {
            "running": True, "state": "running", "total": 0, "done": 0,
            "written": 0, "skipped": 0, "errors": 0, "removed_lines": 0,
            "saved_bytes": 0, "current": "", "message": "准备中…",
            "src": self.src, "dst": self.dst, "elapsed": 0,
        }

    def cancel(self):
        self.cancel_ev.set()

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._st)
        s["elapsed"] = round(time.time() - self._t0, 1)
        return s

    def _set(self, **kw):
        with self._lock:
            self._st.update(kw)

    def _list(self) -> list[str]:
        out = []
        if self.recursive:
            for dp, dirs, files in os.walk(self.src):
                dirs[:] = [d for d in dirs if not d.startswith("$")]
                for fn in files:
                    if fn.lower().endswith(".txt"):
                        out.append(os.path.join(dp, fn))
        else:
            for fn in os.listdir(self.src):
                p = os.path.join(self.src, fn)
                if os.path.isfile(p) and fn.lower().endswith(".txt"):
                    out.append(p)
        return out

    def run(self):
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", running=False, message=f"出错：{exc}")
        finally:
            self._set(running=False)
            if self._st.get("state") != "error":
                self._set(state="cancelled" if self.cancel_ev.is_set() else "done")

    def _run(self):
        if not os.path.isdir(self.src):
            self._set(state="error", message="输入文件夹不存在")
            return
        if os.path.normcase(self.dst) == os.path.normcase(self.src):
            self._set(state="error", message="输出文件夹不能和输入相同（会覆盖原文件）")
            return
        if os.path.normcase(self.dst).startswith(os.path.normcase(self.src) + os.sep):
            self._set(state="error", message="输出文件夹不能放在输入文件夹里面")
            return
        files = self._list()
        self._set(total=len(files), message="开始清洗…")
        for i, p in enumerate(files):
            if self.cancel_ev.is_set():
                return
            self._set(current=p, done=i)
            try:
                with open(p, "rb") as f:
                    raw = f.read(MAX_READ)
                text, enc = _decode(raw)
                cleaned, removed, _total_lines = strip_ads(text)
                if not cleaned.strip():
                    with self._lock:
                        self._st["skipped"] += 1
                    continue
                rel = os.path.relpath(p, self.src)
                out_path = os.path.join(self.dst, rel)
                os.makedirs(os.path.dirname(out_path) or self.dst, exist_ok=True)
                data = cleaned.encode("utf-8")
                with open(out_path, "wb") as f:
                    f.write(data)
                with self._lock:
                    self._st["written"] += 1
                    self._st["removed_lines"] += removed
                    self._st["saved_bytes"] += max(0, len(raw) - len(data))
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._st["errors"] += 1
        self._set(done=len(files), message="完成")
