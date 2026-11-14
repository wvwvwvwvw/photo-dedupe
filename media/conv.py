"""繁简转换 + 编码转换。

繁简用 OpenCC 的字典（vendor\\opencc，Apache-2.0）：
* TSPhrases.txt：词组优先（長度優先，效果明显好于逐字换）
* TSCharacters.txt：单字兜底

编码：先看 BOM，再依次试 UTF-8 / GB18030（兼容 GBK/GB2312）/ BIG5 / UTF-16（无 BOM 时按零字节分布猜），
最后 latin-1 兜底（不会抛异常，只是可能乱码——会记进日志）。
"""

from __future__ import annotations

import codecs
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DICT_DIR = os.path.join(BASE, "vendor", "opencc")

TEXT_EXT = (".txt", ".md", ".csv", ".srt", ".ass", ".ssa", ".html", ".htm", ".xml",
            ".json", ".log", ".ini", ".srt", ".lrc", ".nfo", ".tex", ".rst")
DEFAULT_MAX = 200 * 1024 * 1024        # 单个文件上限 200MB

# 输出编码
OUT_ENCODINGS = {
    "utf-8": ("UTF-8", "utf-8"),
    "utf-8-sig": ("UTF-8（带 BOM）", "utf-8-sig"),
    "gbk": ("GBK / GB18030", "gb18030"),
    "big5": ("BIG5（繁體）", "big5hkscs"),
    "utf-16-le": ("UTF-16 LE", "utf-16-le"),
}

_PHRASES: "dict[str, str] | None" = None
_CHARS: "dict[str, str] | None" = None
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 字典
def _load(path: str) -> "dict[str, str]":
    out: "dict[str, str]" = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                key, vals = parts[0], parts[1].split()
                if key and vals:
                    out[key] = vals[0]
    except OSError:
        pass
    return out


def _dicts() -> "tuple[dict[str, str], dict[str, str]]":
    global _PHRASES, _CHARS
    with _LOCK:
        if _PHRASES is None:
            _PHRASES = _load(os.path.join(DICT_DIR, "TSPhrases.txt"))
            _CHARS = _load(os.path.join(DICT_DIR, "TSCharacters.txt"))
    return _PHRASES or {}, _CHARS or {}


def dict_status() -> dict:
    ph, ch = _dicts()
    return {"phrases": len(ph), "chars": len(ch), "dir": DICT_DIR,
            "available": bool(ph or ch)}


# ---------------------------------------------------------------- 繁→简
def t2s(text: str) -> str:
    """繁体转简体：先按词组（长的优先）替换，再逐字兜底。"""
    if not text:
        return ""
    phrases, chars = _dicts()
    if not phrases and not chars:
        return text
    if phrases:
        # 最长匹配优先：按词长从长到短建正则
        keys = sorted(phrases, key=len, reverse=True)
        pat = re.compile("|".join(re.escape(k) for k in keys))
        text = pat.sub(lambda m: phrases[m.group(0)], text)
    if chars:
        text = "".join(chars.get(ch, ch) for ch in text)
    return text


# ---------------------------------------------------------------- 编码
_BOMS = [
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
]


def detect(raw: bytes) -> "tuple[str, bool]":
    """猜编码，返回 (编码名, 是否有 BOM)。"""
    if not raw:
        return "utf-8", False
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            return enc, True
    try:
        raw.decode("utf-8")
        return "utf-8", False
    except UnicodeDecodeError:
        pass
    # 无 BOM 的 UTF-16：中英文混排时会有大量零字节
    if len(raw) >= 8:
        head = raw[:8192]
        n = max(1, len(head) // 2)
        zeros_odd = head[1::2].count(0) / n
        zeros_even = head[0::2].count(0) / n
        if zeros_odd > 0.25 and zeros_even < 0.05:
            return "utf-16-le", False
        if zeros_even > 0.25 and zeros_odd < 0.05:
            return "utf-16-be", False
    # GBK / BIG5 / 无 BOM 的 UTF-16 字节范围互相重叠，只能靠"解出来像不像正常中文"来挑
    cands = ["gb18030", "big5hkscs"]
    if len(raw) % 2 == 0:
        cands += ["utf-16-le", "utf-16-be"]
    cands += ["shift_jis", "euc-kr"]
    best_enc, best_score = "", -1.0
    for enc in cands:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        sc = _plausible(text)
        if sc > best_score:
            best_enc, best_score = enc, sc
    if best_enc and best_score >= 0.55:
        return best_enc, False
    return "latin-1", False


_COMMON: "set[str] | None" = None


def _common_chars() -> "set[str]":
    """常见汉字集合（拿 OpenCC 字典里的字当白名单）+ 标点/ASCII。"""
    global _COMMON
    if _COMMON is not None:
        return _COMMON
    chars: "set[str]" = set()
    ph, ch = _dicts()
    for k in list(ph) + list(ch):
        chars.update(k)
    for v in ch.values():
        chars.update(v)
    for v in ph.values():
        chars.update(v)
    _COMMON = chars
    return chars


def _plausible(text: str) -> float:
    """0~1：这段文本"像不像正常中文"——常见汉字/ASCII/标点占比。"""
    if not text:
        return 0.0
    common = _common_chars()
    good = 0
    for ch in text:
        o = ord(ch)
        if o < 128:                       # ASCII
            good += 1
        elif 0x3000 <= o <= 0x303F:       # 中文标点
            good += 1
        elif 0xFF00 <= o <= 0xFFEF:       # 全角
            good += 1
        elif ch in common:                # 常用汉字
            good += 1
        elif 0x4E00 <= o <= 0x9FFF:       # 生僻汉字：算半个
            good += 0.5
        elif ch in "\r\n\t":
            good += 1
    return good / max(1, len(text))


def decode(raw: bytes) -> "tuple[str, str]":
    """解成文本，返回 (文本, 实际用的编码)。"""
    enc, _bom = detect(raw)
    try:
        return raw.decode(enc), enc
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", "replace"), "utf-8(替换)"

    
def normalize_newlines(text: str, mode: str = "keep") -> str:
    """mode: keep / lf / crlf"""
    if mode == "lf":
        return text.replace("\r\n", "\n").replace("\r", "\n")
    if mode == "crlf":
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return text


def convert_text(raw: bytes, *, t2s_on: bool = True, out_enc: str = "utf-8",
                 newline: str = "keep") -> dict:
    """把一个文件的字节转好，返回 {data, src_enc, changed}。"""
    text, enc = decode(raw)
    new = normalize_newlines(text, newline)
    if t2s_on:
        new = t2s(new)
    codec = OUT_ENCODINGS.get(out_enc, OUT_ENCODINGS["utf-8"])[1]
    try:
        data = new.encode(codec, "replace")
    except LookupError:
        data = new.encode("utf-8", "replace")
    return {"data": data, "src_enc": enc, "changed": bool(t2s_on and new != text)
             or new != text or data != raw}


# ---------------------------------------------------------------- 批处理作业
class ConvJob(threading.Thread):
    """把一个文件夹里的文本文件转成目标编码（可选繁→简），输出到另一个文件夹。"""

    daemon = True

    def __init__(self, src: str, dst: str, *, t2s_on: bool = True,
                 out_enc: str = "utf-8", newline: str = "keep",
                 recursive: bool = True, exts: "list[str] | None" = None,
                 overwrite: bool = False):
        super().__init__(name="conv-job")
        self.src = os.path.abspath(src)
        self.dst = os.path.abspath(dst)
        self.t2s_on = bool(t2s_on)
        self.out_enc = out_enc if out_enc in OUT_ENCODINGS else "utf-8"
        self.newline = newline if newline in ("keep", "lf", "crlf") else "keep"
        self.recursive = bool(recursive)
        self.exts = tuple(e.lower() for e in (exts or TEXT_EXT))
        self.overwrite = bool(overwrite)
        self.cancel_ev = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._st = {"running": True, "state": "running", "total": 0, "done": 0,
                    "files": 0, "changed": 0, "failed": 0, "current": "",
                    "message": "准备中…", "log": [], "paused": False,
                    "src": self.src, "dst": self.dst,
                    "out_enc": self.out_enc, "t2s": self.t2s_on}

    # ---- 状态
    def log(self, msg: str) -> None:
        with self._lock:
            lst = self._st["log"]
            lst.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(lst) > 500:
                del lst[:150]

    def cancel(self) -> None:
        self.cancel_ev.set()

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._st)
            s["log"] = list(self._st["log"])
        s["elapsed"] = round(time.time() - self._t0, 1)
        return s

    def _set(self, **kw) -> None:
        with self._lock:
            self._st.update(kw)

    # ---- 主流程
    def _list(self) -> "list[str]":
        if os.path.isfile(self.src):
            return [self.src]
        out = []
        for dp, dirs, files in os.walk(self.src):
            dirs[:] = [d for d in dirs if not d.startswith(("$", "."))]
            for fn in files:
                if fn.lower().endswith(self.exts):
                    out.append(os.path.join(dp, fn))
            if not self.recursive:
                break
        return out

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", message=f"出错：{exc}")
            self.log(f"出错：{exc}")
        finally:
            self._set(running=False)
            if self._st.get("state") != "error":
                self._set(state="cancelled" if self.cancel_ev.is_set() else "done")

    def _run(self) -> None:
        if not os.path.isdir(self.src) and not os.path.isfile(self.src):
            self._set(state="error", message=f"输入路径不存在：{self.src}")
            return
        files = self._list()
        self._set(total=len(files))
        st = dict_status()
        if not st["available"]:
            self.log("⚠ 没找到 OpenCC 字典（vendor\\opencc），繁简转换会被跳过")
        else:
            self.log(f"繁简字典：词组 {st['phrases']} 条、单字 {st['chars']} 条")
        self.log(f"共 {len(files)} 个文本文件 → 输出编码 "
                 f"{OUT_ENCODINGS[self.out_enc][0]}")
        if not files:
            self._set(message="没有可转换的文件")
            return
        for idx, path in enumerate(files):
            if self.cancel_ev.is_set():
                self.log("已取消")
                return
            rel = os.path.relpath(path, self.src if os.path.isdir(self.src)
                                  else os.path.dirname(self.src))
            self._set(current=rel, done=idx)
            try:
                size = os.path.getsize(path)
                if size > DEFAULT_MAX:
                    self.log(f"跳过（太大 {size/1048576:.0f}MB）：{rel}")
                    continue
                with open(path, "rb") as f:
                    raw = f.read()
                r = convert_text(raw, t2s_on=self.t2s_on, out_enc=self.out_enc,
                                 newline=self.newline)
                out_path = os.path.join(self.dst, rel)
                os.makedirs(os.path.dirname(out_path) or self.dst, exist_ok=True)
                if os.path.exists(out_path) and not self.overwrite:
                    stem, ext = os.path.splitext(out_path)
                    k = 1
                    while os.path.exists(f"{stem}.{k}{ext}"):
                        k += 1
                    out_path = f"{stem}.{k}{ext}"
                with open(out_path, "wb") as f:
                    f.write(r["data"])
                with self._lock:
                    self._st["files"] += 1
                    if r["changed"]:
                        self._st["changed"] += 1
                if idx % 50 == 0 or r["changed"]:
                    self.log(f"  {rel}（{r['src_enc']} → {self.out_enc}）")
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._st["failed"] += 1
                self.log(f"  {rel} 失败：{exc}")
        self._set(done=len(files))
        self.log(f"完成：{self._st['files']} 个文件，"
                 f"其中 {self._st['changed']} 个内容有变化，失败 {self._st['failed']} 个")
