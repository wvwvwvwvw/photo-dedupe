"""小说分析：抽文本 → 清洗 → 章节数 / 字数 / 正文 SimHash；书名相似度。

改进点：
1. 正文 SimHash（64 位）——读完文本本来就为了数数，顺手算个指纹，
   这样"同一本书改了文件名"也能认出来，"书名像但不是同一本"也能分开。
2. 章节按「章号」去重，支持中文数字；跳过开头目录块。
3. 清洗广告/网址行，字数更准。
"""

from __future__ import annotations

import difflib
import os
import re
import unicodedata
import zipfile
import zlib

import numpy as np

# ---------------------------------------------------------------- 章节
_CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}

CHAPTER_RE = re.compile(
    r"^\s*(?:正文\s*)?第\s*([0-9０-９零〇一二三四五六七八九十百千万两]{1,9})\s*([章节節回卷集部篇])",
    re.M)
CHAPTER_EN_RE = re.compile(r"^\s*(?:chapter|CHAPTER)\s+([0-9]{1,4})", re.M)
VOLUME_RE = re.compile(r"^\s*(?:卷|第)\s*([0-9０-９零〇一二三四五六七八九十百千万两]{1,9})\s*卷", re.M)
NUM_RE = re.compile(r"^\s*([0-9]{1,4})\s*[、.．]\s*\S", re.M)
SPECIAL_RE = re.compile(r"^\s*(序章|楔子|引子|尾声|后记|番外)", re.M)


def cn_to_int(s: str) -> int:
    s = unicodedata.normalize("NFKC", s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total, section, number = 0, 0, 0
    for ch in s:
        if ch in _CN_NUM:
            number = _CN_NUM[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if unit == 10000:
                total += (section + number) * unit
                section, number = 0, 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            continue
    return total + section + number


def count_chapters(text: str) -> int:
    """按章号去重计数；连续重复（目录）只算一次。"""
    if not text:
        return 0
    nums: list[tuple[str, int]] = []
    for pat, tag in ((CHAPTER_RE, "c"), (CHAPTER_EN_RE, "c"), (VOLUME_RE, "v"), (NUM_RE, "c")):
        for m in pat.finditer(text):
            nums.append((tag, cn_to_int(m.group(1))))
    specials = len(SPECIAL_RE.findall(text))

    # 章号可能在不同卷里重复出现（第1章...卷末...下一卷第1章），
    # 所以按「出现顺序里的最大值」来估，而不是简单的去重个数
    best = 0
    seen: set[int] = set()
    for _tag, n in nums:
        if n <= 0:
            continue
        key = n if n > best else len(seen) + 1
        if key not in seen:
            seen.add(key)
        best = max(best, n)
    # 目录块会让最大章号虚高，用出现次数兜底
    return int(max(best, len(seen), specials))


# ---------------------------------------------------------------- 章节切分 & 重合
_CHAP_LINE_RE = re.compile(
    r"^[ \t]*(?:正文[ \t]*)?第[ \t]*"
    r"([0-9０-９零〇一二三四五六七八九十百千万两]{1,9})[ \t]*[章节節回卷集部篇]"
    r"|^[ \t]*(?:chapter|CHAPTER)[ \t]+([0-9]{1,4})",
    re.M)

CHAP_MIN_BODY = 200          # 这么短的"章"多半是目录/占位，不算
CHAP_MAX = 4000              # 单本最多切这么多章
SHINGLE_SAMPLES = 24         # 每章取多少个 4 字滑窗哈希
CHAP_MATCH_MIN = 0.35        # 章级重合的 Jaccard 阈值


def split_chapters(text: str) -> "list[tuple[int, str, str]]":
    """把正文切成 [(章号, 标题, 正文)]；没有章节标记时整本当一章（章号 0）。"""
    if not text:
        return []
    marks = []
    for m in _CHAP_LINE_RE.finditer(text):
        num = cn_to_int(m.group(1) or m.group(2) or "0")
        title = (m.group(0) or "").strip()[:40]
        marks.append((m.start(), num, title))
        if len(marks) > CHAP_MAX:
            break
    if not marks:
        return [(0, "全文", text)]
    spans = []
    for i, (pos, num, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[pos:end]
        if len(body) >= CHAP_MIN_BODY:
            spans.append((num, title, body))
    return spans or [(0, "全文", text)]


def _chapter_shingles(body: str, k: int = 4, cap: int = 2000,
                      bottom: int = SHINGLE_SAMPLES) -> "list[int]":
    """bottom-k 指纹：取整章 4 字滑窗里哈希最小的 k 个。

    为什么不用等距抽样：两版书只要开头差几个字，等距抽样就全错位了（实测 0 命中）；
    bottom-k 只跟"哪些 4 字串出现过"有关，错位不影响。
    """
    t = re.sub(r"\s+", "", body)
    n = len(t) - k
    if n < 8:
        return []
    step = max(1, n // cap)
    hs = []
    for i in range(0, n, step):
        hs.append(zlib.crc32(t[i:i + k].encode("utf-8", "ignore")) & 0xFFFFFFFF)
    hs.sort()
    return hs[:bottom]


def _jaccard(a: "list[int]", b: "list[int]") -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    return inter / float(len(sa) + len(sb) - inter) if inter else 0.0


_SPAN_CACHE: "dict[tuple, tuple]" = {}


def _spans_of(path: str, limit: int = 1_200_000):
    """(章号, 标题, 滑窗哈希列表)，按 路径+修改时间 缓存。"""
    try:
        st = os.stat(path)
        key = (path, int(st.st_mtime), int(st.st_size))
    except OSError:
        return []
    got = _SPAN_CACHE.get(key)
    if got is not None:
        return got
    try:
        raw, _enc = read_text(path)
        text = clean_text(raw)[:limit * 2]
    except Exception:  # noqa: BLE001
        text = ""
    spans = [(num, title, _chapter_shingles(body))
             for num, title, body in split_chapters(text)]
    if len(_SPAN_CACHE) > 64:
        _SPAN_CACHE.clear()
    _SPAN_CACHE[key] = spans
    return spans


def chapter_overlap(path_a: str, path_b: str) -> dict:
    """两本书按章比对：重合多少章。返回 {a, b, matched, ratio} 或 {}（没法比）。"""
    sa = _spans_of(path_a)
    sb = _spans_of(path_b)
    if len(sa) < 2 or len(sb) < 2:          # 有一本没章节结构，比不了
        return {}
    idx_b = {num: fps for num, _t, fps in sb}
    used: set[int] = set()
    matched = 0
    samples = []
    for num, _title, fps in sa:
        best, best_num = 0.0, None
        for cand in (num - 1, num, num + 1):
            if cand in idx_b and cand not in used:
                j = _jaccard(fps, idx_b[cand])
                if j > best:
                    best, best_num = j, cand
        if best >= CHAP_MATCH_MIN and best_num is not None:
            used.add(best_num)
            matched += 1
            if len(samples) < 3:
                samples.append({"num": num, "jac": round(best, 2)})
    total = max(len(sa), len(sb))
    return {"a": len(sa), "b": len(sb), "matched": matched,
            "ratio": round(matched / float(total), 3) if total else 0.0,
            "samples": samples}


# ---------------------------------------------------------------- 清洗
_URL_RE = re.compile(r"(https?://|www\.)[^\s，。！？、）】]+", re.I)
_AD_LINE_RE = re.compile(
    r"(请记住|本站|域名|书友群|qq群|QQ群|最新网址|最新章节|无弹窗|手机版|手机阅读|"
    r"百度搜索|更多精彩|加入书架|投推荐票|月票|订阅|章节报错|一秒记住|免费阅读)", re.I)


CLEAN_CHARS = 900000       # 清洗/统计最多处理前 2MB


def clean_text(text: str) -> str:
    """去掉广告行、网址、重复空行，让字数和指纹都更可信。"""
    if not text:
        return ""
    text = text[:CLEAN_CHARS]
    out = []
    for line in text.splitlines():
        st = line.strip()
        if not st:
            continue
        if len(st) < 80 and _AD_LINE_RE.search(st):
            continue
        out.append(_URL_RE.sub(" ", st))
    return "\n".join(out)


# ---------------------------------------------------------------- SimHash
_BITS = 64


HASH_CHARS = 600000        # 只对前 1.2MB 正文算指纹（够用，且快）
SHINGLE_LIMIT = 6000        # 最多 6000 个 4 字滑窗


def _hash_array(text: str, k: int = 4, limit: int = SHINGLE_LIMIT) -> np.ndarray:
    t = re.sub(r"\s+", "", text[:HASH_CHARS])
    if len(t) < k:
        return np.empty(0, dtype=np.uint64)
    step = max(1, (len(t) - k) // limit)
    arr = np.frombuffer(t.encode("utf-8", "ignore"), dtype=np.uint8)
    idx = np.arange(0, max(1, len(t) - k), step, dtype=np.int64)
    if arr.size < k + 1:
        return np.empty(0, dtype=np.uint64)
    # 用 4 字节切片做 CRC32（numpy 里没有 CRC，退回 Python 但只算一次）
    out = np.empty(len(idx), dtype=np.uint64)
    for n, i in enumerate(idx):
        b = t[i:i + k]
        out[n] = zlib.crc32(b.encode("utf-8", "ignore"))
    return out


def simhash(text: str) -> int:
    """64 位正文指纹（向量化版）。内容相同 → 汉明距离极小；不同书 → 接近 32。"""
    hs = _hash_array(text)
    if hs.size == 0:
        return 0
    # 把每个 32 位哈希扩成 64 位（再用一次 CRC 补高位），然后按位统计
    hi = np.fromiter((zlib.crc32(bytes([n & 0xFF]) + hs[n].tobytes()) for n in range(hs.size)),
                     dtype=np.uint64, count=hs.size)
    full = (hi << np.uint64(32)) | hs.astype(np.uint64)
    bits = ((full[:, None] >> np.arange(_BITS, dtype=np.uint64)) & np.uint64(1)).astype(np.int8)
    vec = bits.sum(axis=0)
    out = 0
    for b in range(_BITS):
        if vec[b] > 0:
            out |= (1 << b)
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def content_similarity(a: int, b: int) -> float:
    if not a or not b:
        return 0.0
    return max(0.0, 1.0 - hamming(a, b) / float(_BITS))


# ---------------------------------------------------------------- 书名
_NOISE_WORDS = [
    "全本", "完结", "全集", "全文", "精校", "校对", "无删", "无删减", "番外", "完本",
    "txt", "TXT", "下载", "免费", "在线阅读", "小说", "合集", "打包", "最新章节",
    "作者", "著", "连载", "更新", "校对版", "精校版", "典藏版", "最终版", "修订版",
]
_BRACKETS = [("【", "】"), ("［", "］"), ("(", ")"), ("（", "）"), ("[", "]"), ("{", "}"), ("<", ">")]


_SITE_RE = re.compile(r"^[^\s@]{0,60}@")            # soushu2022.com@书名
_DOMAIN_RE = re.compile(r"[\w-]+\.(?:com|net|org|cc|cn|me|info|xyz|top|vip)\b", re.I)
_INDEX_RE = re.compile(r"^[\[【(（]\s*\d{1,4}\s*[\]】)）]\s*")   # 【002】书名


def normalize_name(name: str) -> str:
    s = os.path.splitext(os.path.basename(name))[0]
    s = _SITE_RE.sub("", s)
    s = _DOMAIN_RE.sub(" ", s)
    s = _INDEX_RE.sub("", s)
    for a, b in _BRACKETS:
        s = re.sub(re.escape(a) + r"[^" + re.escape(a) + re.escape(b) + r"]*" + re.escape(b), " ", s)
    s = s.replace("_", " ").replace("-", " ").replace(".", " ")
    # 长的先替换（"精校版" 要在 "精校" 之前干掉，否则会剩个"版"字）
    for w in sorted(_NOISE_WORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    s = re.sub(r"(?<=\s)版(?=\s|$)", " ", s)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s or os.path.splitext(os.path.basename(name))[0].lower()


def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sm = difflib.SequenceMatcher(None, a, b)
    if sm.quick_ratio() < 0.70:
        return round(sm.quick_ratio(), 4)
    ratio = sm.ratio()
    sa, sb = set(a.split()), set(b.split())
    if sa and sb:
        ratio = max(ratio, len(sa & sb) / len(sa | sb))
    return round(ratio, 4)


# ---------------------------------------------------------------- 文本抽取
def _decode(raw: bytes) -> tuple[str, str]:
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc
            except Exception:
                break
    for enc in ("utf-8", "gb18030", "big5", "utf-16-le"):
        try:
            text = raw.decode(enc)
            if text.count("\ufffd") <= max(2, len(text) // 2000):
                return text, enc
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|h\d|li|tr)>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'")):
        html = html.replace(a, b)
    return html


def _text_from_epub(path: str) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if n.lower().endswith((".html", ".htm", ".xhtml")))
        for n in names[:400]:
            try:
                txt, _ = _decode(z.read(n))
            except Exception:
                continue
            out.append(_strip_html(txt))
    return "\n".join(out)


def _text_from_docx(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        s, _ = _decode(z.read("word/document.xml"))
    s = s.replace("</w:p>", "\n")
    return re.sub(r"(?s)<[^>]+>", "", s)


def _text_from_rtf(path: str) -> str:
    s, _ = _decode(open(path, "rb").read())
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", s)
    return s.replace("{", " ").replace("}", " ")


def _text_from_pdf(path: str) -> str:
    raw = open(path, "rb").read()
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        try:
            data = zlib.decompress(m.group(1))
        except Exception:
            continue
        for tm in re.finditer(rb"\((.*?)\)\s*Tj|\[(.*?)\]\s*TJ", data, re.S):
            seg = tm.group(1) or tm.group(2) or b""
            out.append(re.sub(rb"\\(.)", rb"\1", seg).decode("utf-8", "replace"))
    return " ".join(out)


MAX_READ = 2 * 1024 * 1024     # 单本最多读 6MB，够算章节/字数/指纹


def _read_bytes(path: str) -> tuple[bytes, bool]:
    with open(path, "rb") as f:
        raw = f.read(MAX_READ)
        truncated = bool(f.read(1))
    return raw, truncated


def read_text(path: str) -> tuple[str, str]:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".epub":
            return _text_from_epub(path), "epub"
        if ext == ".docx":
            return _text_from_docx(path), "docx"
        if ext in (".html", ".htm"):
            raw, _trunc = _read_bytes(path)
            txt, enc = _decode(raw)
            return _strip_html(txt), enc
        if ext == ".rtf":
            raw, _trunc = _read_bytes(path)
            t, enc = _decode(raw)
            t = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), t)
            t = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", t)
            return t.replace("{", " ").replace("}", " "), "rtf"
        if ext == ".pdf":
            return _text_from_pdf(path), "pdf"
        raw, _trunc = _read_bytes(path)
        txt, enc = _decode(raw)
        return txt, enc
    except Exception:
        return "", "error"


# ---------------------------------------------------------------- 入口
def analyze(path: str) -> dict:
    try:
        size = os.path.getsize(path)
    except OSError:
        raise RuntimeError("文件读不到")
    raw, src = read_text(path)
    text = clean_text(raw)
    words = len(re.sub(r"\s+", "", text)) if text else 0
    chapters = count_chapters(text)
    sig = simhash(text) if words >= 5000 else 0
    return {
        "chapters": int(chapters),
        "words": int(words),
        "text_bytes": len(text.encode("utf-8")) if text else 0,
        "enc": src,
        "name_norm": normalize_name(path),
        "size": size,
        "sig": sig,
        "has_content": bool(sig),
        "garbled": bool(raw) and raw.count("\ufffd") > max(20, len(raw) // 200),
    }
