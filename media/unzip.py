"""解压：多层/分卷压缩在内存里逐层展开，只把最终文件写到输出目录。

支持（标准库）：zip、tar/tar.gz/tgz/tar.bz2/tar.xz、gz、bz2、xz
支持（外部 7z.exe，有就用、没有就提示）：7z、rar（含 AES 的 7z）
分卷：name.zip.001/.002… 或 name.z01/z02…（先拼接再解）
密码：先空密码 → 密码本 → 已破解密码本 → 多要素穷举
"""

from __future__ import annotations

import bz2
import gzip
import io
import itertools
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
import zlib

try:                                   # pyzipper 支持 AES(压缩法 99)，标准库不支持
    import pyzipper as _pyzipper
    ZIP_READER = _pyzipper.AESZipFile
    ZIP_BAD = (zipfile.BadZipFile,)
except Exception:                      # 没装就退回标准库
    ZIP_READER = zipfile.ZipFile
    ZIP_BAD = (zipfile.BadZipFile,)

ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz",
               ".txz", ".gz", ".bz2", ".xz", ".7z", ".rar")
MAX_INFLATE = 4 * 1024 * 1024 * 1024      # 单层解压后的上限 4GB（防炸弹）
MAX_DEPTH = 6
MAX_ENTRY = 512 * 1024 * 1024             # 单个文件上限


def is_archive(name: str) -> bool:
    n = name.lower()
    if n.endswith(ARCHIVE_EXT):
        # .gz/.bz2/.xz 作为单文件压缩也算
        return True
    return False


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEVENZIP_CONFIG = os.path.join(BASE, "7z.txt")
_7Z_CACHE: "str | None" = None
_7Z_LOCK = threading.Lock()
MAGIC_7Z = b"7z\xbc\xaf\x27\x1c"
MAGIC_RAR = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")


def find_7z() -> str:
    """找 7z.exe：7z.txt → vendor\\7z → PATH → 常见安装位置。"""
    global _7Z_CACHE
    with _7Z_LOCK:
        if _7Z_CACHE is not None:
            return _7Z_CACHE
        cands: list[str] = []
        if os.path.exists(SEVENZIP_CONFIG):
            try:
                with open(SEVENZIP_CONFIG, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip().strip('"')
                        if line and not line.startswith("#"):
                            cands.append(line)
            except OSError:
                pass
        root = os.path.join(BASE, "vendor", "7z")
        for name in ("7za.exe", "7z.exe", "7zr.exe"):
            fp = os.path.join(root, name)
            if os.path.isfile(fp):
                cands.append(fp)
        which = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
        if which:
            cands.append(which)
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "7-Zip", "7z.exe"))
        for c in cands:
            if c and os.path.isfile(c):
                _7Z_CACHE = os.path.abspath(c)
                return _7Z_CACHE
        _7Z_CACHE = ""
        return _7Z_CACHE


# 按文件头认格式（Bandizip 那种：后缀不对也能解）
_MAGICS = (
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07\x01\x00", "rar"), (b"Rar!\x1a\x07\x00", "rar"),
    (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"), (b"\x04\x22\x4d\x18", "lz4"),
    (b"\x1f\x9d", "compress"),
    (b"MSCF", "cab"), (b"ITSF", "chm"), (b"070701", "cpio"), (b"070702", "cpio"),
    (b"!<arch>", "ar"), (b"\xed\xab\xee\xdb", "rpm"), (b"\x60\xea", "arj"),
    (b"MSWIM\x00\x00\x00", "wim"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "msi"),
    (b"hsqs", "squashfs"), (b"sqsh", "squashfs"), (b"shsq", "squashfs"),
    (b"qshs", "squashfs"),
    (b"conectix", "vhd"), (b"vhdxfile", "vhdx"),
    (b"\x5d\x00\x00\x00\x01", "lzma"),
)

NATIVE_FMT = {"zip", "tar", "gzip", "bzip2", "xz"}
SEVENZIP_FMT = {"7z", "rar", "cab", "chm", "cpio", "ar", "rpm", "arj", "lzh",
                "wim", "msi", "squashfs", "vhd", "vhdx", "zstd", "lz4",
                "compress", "lzma", "iso", "udf", "nsis"}
EXT_FOR_FMT = {"zip": ".zip", "7z": ".7z", "rar": ".rar", "cab": ".cab",
               "chm": ".chm", "cpio": ".cpio", "ar": ".deb", "rpm": ".rpm",
               "arj": ".arj", "lzh": ".lzh", "wim": ".wim", "msi": ".msi",
               "squashfs": ".squashfs", "vhd": ".vhd", "vhdx": ".vhdx",
               "zstd": ".zst", "lz4": ".lz4", "compress": ".Z", "lzma": ".lzma",
               "iso": ".iso", "udf": ".udf", "nsis": ".exe", "tar": ".tar",
               "gzip": ".gz", "bzip2": ".bz2", "xz": ".xz"}


def sniff_archive(data: bytes) -> str:
    """看文件头认格式（zip / 7z / rar / cab / iso / tar …），认不出返回空串。"""
    if not data or len(data) < 4:
        return ""
    for magic, name in _MAGICS:
        if data.startswith(magic):
            return name
    if len(data) > 5 and data[2:5] == b"-lh":          # LZH
        return "lzh"
    if len(data) > 262 and data[257:262] == b"ustar":  # tar（ustar 标记在 257）
        return "tar"
    if len(data) > 32774 and data[32769:32774] in (b"CD001", b"BEA01"):
        return "iso"
    return ""


_EXT_FMT = {
    ".zip": "zip", ".7z": "7z", ".rar": "rar", ".tar": "tar",
    ".tgz": "gzip", ".gz": "gzip", ".bz2": "bzip2", ".xz": "xz",
    ".cab": "cab", ".iso": "iso", ".chm": "chm", ".cpio": "cpio",
    ".rpm": "rpm", ".arj": "arj", ".lzh": "lzh", ".lha": "lzh",
    ".wim": "wim", ".msi": "msi", ".zst": "zstd", ".lz4": "lz4",
    ".vhd": "vhd", ".vhdx": "vhdx", ".deb": "ar", ".ar": "ar",
    ".tbz2": "bzip2", ".txz": "xz", ".z": "compress",
}


def fmt_from_name(name: str) -> str:
    low = name.lower()
    if low.endswith(".tar.gz"):
        return "tar"
    if low.endswith(".tar.bz2"):
        return "tar"
    if low.endswith(".tar.xz"):
        return "tar"
    return _EXT_FMT.get(os.path.splitext(low)[1], "")


DOC_EXT = (".pdf", ".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm")


def looks_like_crackable(path: str) -> bool:
    """破解页要扫的文件：压缩包，以及 PDF / Office 文档。"""
    if looks_like_archive(path):
        return True
    return path.lower().endswith(DOC_EXT)


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def fix_zip_name(info) -> str:
    """修 zip 条目名。

    Windows 上的很多老压缩包用 GBK 存文件名、又没打 UTF-8 标记，
    Python 会按 cp437 解成乱码（形如 "µÜéσü£"）。这里把名字还原回
    cp437 原始字节，再按 UTF-8 / GB18030 / BIG5 试解；只有"解出来是中文、
    原来不是"时才替换，避免误伤真的西文文件名。
    """
    name = getattr(info, "filename", "") or ""
    if not name or (getattr(info, "flag_bits", 0) & 0x800):
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "gb18030", "big5hkscs"):
        try:
            cand = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if cand != name and _CJK.search(cand) and not _CJK.search(name):
            return cand
    return name


def looks_like_archive(path: str) -> bool:
    """后缀像压缩包，或者文件头认得出是压缩包（后缀写错也能识别）。"""
    if is_archive(path):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return bool(sniff_archive(head))


def _run7z(args: list[str], timeout: int = 300) -> tuple[int, str]:
    exe = find_7z()
    if not exe:
        return -1, "没找到 7z.exe"
    try:
        p = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout,
                           errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-3000:]
    except subprocess.TimeoutExpired:
        return -9, "7z 超时"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


RAR_CONFIG = os.path.join(BASE, "rar.txt")
_RAR_CACHE = [None]


def find_rar() -> str:
    """找 UnRAR.exe / Rar.exe：rar.txt → 常见安装位置 → PATH。"""
    if _RAR_CACHE[0] is not None:
        return _RAR_CACHE[0]
    cands = []
    if os.path.exists(RAR_CONFIG):
        try:
            with open(RAR_CONFIG, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip().strip('"')
                    if line and not line.startswith("#"):
                        cands.append(line)
        except OSError:
            pass
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "WinRAR", "UnRAR.exe"))
            cands.append(os.path.join(base, "WinRAR", "Rar.exe"))
    cands.append(shutil.which("unrar") or "")
    for c in cands:
        if c and os.path.isfile(c):
            _RAR_CACHE[0] = os.path.abspath(c)
            return _RAR_CACHE[0]
    _RAR_CACHE[0] = ""
    return ""


def rar_try(path: str, pwd: str) -> bool:
    """用 UnRAR.exe / Rar.exe 试一个密码（rc=0 表示对）。"""
    exe = find_rar()
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "t", path, f"-p{pwd}"], capture_output=True,
                           text=True, timeout=120, errors="replace",
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def sevenzip_try(path: str, pwd: str) -> bool:
    """试一个密码：RAR 优先用 WinRAR 的 unrar，其它用 7z.exe。"""
    if path.lower().endswith(".rar") and find_rar():
        return rar_try(path, pwd)
    args = ["t", path, f"-p{pwd}"]
    rc, _out = _run7z(args, timeout=120)
    return rc == 0


def _strip_archive_ext(name: str) -> str:
    n = name
    for ext in sorted(ARCHIVE_EXT, key=len, reverse=True):
        if n.lower().endswith(ext):
            return n[: -len(ext)]
    return os.path.splitext(n)[0]


class Piece:
    """内存里的一个节点：文件或目录。"""

    __slots__ = ("path", "data", "is_dir")

    def __init__(self, path: str, data: bytes | None = None, is_dir: bool = False):
        self.path = path.replace("\\", "/").lstrip("/")
        self.data = data
        self.is_dir = is_dir


# ------------------------------------------------------------------ 解密
FOUND_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "passwords_found.txt")


def save_found(pwd: str, source: str = "") -> None:
    """把破解出来的密码单独记一本（和用户自己的密码本分开）。"""
    try:
        os.makedirs(os.path.dirname(FOUND_FILE), exist_ok=True)
        existing = set()
        if os.path.exists(FOUND_FILE):
            with open(FOUND_FILE, "r", encoding="utf-8") as f:
                existing = {ln.split("\t")[0] for ln in f}
        if pwd in existing:
            return
        with open(FOUND_FILE, "a", encoding="utf-8") as f:
            f.write(f"{pwd}\t{source}\t{time.strftime('%Y-%m-%d %H:%M')}\n")
    except OSError:
        pass


def load_found() -> list[dict]:
    out = []
    try:
        with open(FOUND_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                parts = ln.rstrip("\n").split("\t")
                if parts and parts[0]:
                    out.append({"pwd": parts[0],
                                "src": parts[1] if len(parts) > 1 else "",
                                "at": parts[2] if len(parts) > 2 else ""})
    except OSError:
        pass
    return out[-500:][::-1]


# ---------- 多要素穷举 ----------
DIGITS = "0123456789"
LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SYMBOLS = "!@#$%^&*()-_=+"
_ALPHABET = {"digits": DIGITS, "lower": LOWER, "upper": UPPER,
             "letters": LOWER + UPPER, "symbols": SYMBOLS,
             "alnum": DIGITS + LOWER,
             "mix": DIGITS + LOWER + UPPER + SYMBOLS}

GPU_MIN = 150_000          # 候选少于此数走 CPU（hashcat 启动就要 3 秒左右）


def _hc():
    """延迟导入，避免没装/没配 hashcat 时整个解压模块都受影响。"""
    from media import hashcat
    return hashcat


def element_count(el: dict) -> int:
    """不展开也能知道这一"要素"有多少种取值。"""
    kind = str(el.get("type") or "literal")
    if kind == "num":
        lo = int(el.get("from") or 0)
        hi = int(el.get("to") or 0)
        return max(0, hi - lo + 1)
    if kind == "charset":
        chars = _ALPHABET.get(str(el.get("charset") or "digits"), DIGITS)
        lo = max(1, min(12, int(el.get("len_min") or el.get("len") or 1)))
        hi = max(lo, min(12, int(el.get("len_max") or lo)))
        return sum(len(chars) ** n for n in range(lo, hi + 1))
    return 1


def element_values(el: dict, limit: int = 2_000_000) -> list[str]:
    """把一"要素"展开成取值列表（超过 limit 就截断，防止 10 位数字把内存吃光）。"""
    kind = str(el.get("type") or "literal")
    if kind == "literal":
        return [str(el.get("value") or "")]
    if kind == "num":
        lo = int(el.get("from") or 0)
        hi = int(el.get("to") or 0)
        width = int(el.get("width") or 0)
        vals = [str(i) for i in range(lo, min(hi, lo + limit - 1) + 1)]
        if width:
            vals = [v.zfill(width) for v in vals]
        return vals
    if kind == "charset":
        chars = _ALPHABET.get(str(el.get("charset") or "digits"), DIGITS)
        lo = max(1, min(12, int(el.get("len_min") or el.get("len") or 1)))
        hi = max(lo, min(12, int(el.get("len_max") or lo)))
        out: list[str] = []
        for n in range(lo, hi + 1):
            part = [""]
            for _ in range(n):
                part = [p + c for p in part for c in chars]
                if len(part) + len(out) >= limit:
                    return (out + part)[:limit]
            out += part
            if len(out) >= limit:
                return out[:limit]
        return out
    return [str(el.get("value") or "")]


def _expand_masks(parts: list[str], rest: list[tuple], count: int,
                  sets: "list[str]") -> "list[tuple[str, int]]":
    """把"固定段 + 长度可变的段"展开成一组完整掩码。

    段类型：("lit", 文本) / ("var_digits", lo, hi) / ("var_set", 字符集序号, lo, hi)
    """
    if not rest:
        return [("".join(parts), count)]
    head, tail = rest[0], rest[1:]
    if head[0] == "lit":
        return _expand_masks(parts + [head[1]], tail, count, sets)
    out: "list[tuple[str, int]]" = []
    if head[0] == "var_digits":
        _, lo, hi = head
        for length in range(lo, hi + 1):
            out += _expand_masks(parts + ["?d" * length], tail,
                                 count * 10 ** length, sets)
        return out
    _, idx, lo, hi = head                       # var_set：自定义字符集
    size = len(sets[idx - 1]) if 0 < idx <= len(sets) else 1
    for length in range(lo, hi + 1):
        out += _expand_masks(parts + [f"?{idx}" * length], tail,
                             count * size ** length, sets)
    return out


def build_mask(elements: list[dict]) -> "dict | None":
    """要素列表 → hashcat 掩码；表示不了就返回 None（交给字典模式 -a 0）。

    返回 {masks, charsets, count}：
    * 字面量            → 原样写进掩码（`?` 转义成 `??`）
    * 数字 0 ~ 10^n-1   → 写了 width 用 `?d`×n（补零）；没写 width 表示"不补零"，
                          拆成 1~n 位的一组掩码（hashcat 掩码文件一行一个）
    * 字符集 × n 位      → 自定义字符集 `?1` × n（最多 4 组）
    区间不整齐 / 非 ASCII 字面量 → 返回 None。
    """
    segs: "list[tuple]" = []            # ("lit", 文本) 或 ("var", 最大位数)
    sets: list[str] = []
    count = 1
    for el in elements:
        kind = str(el.get("type") or "literal")
        if kind == "literal":
            txt = str(el.get("value") or "")
            if not txt:
                continue
            if not txt.isascii():
                return None                       # 非 ASCII 走字典，免得命令行编码出错
            segs.append(("lit", txt.replace("?", "??")))
            continue
        if kind == "num":
            lo = int(el.get("from") or 0)
            hi = int(el.get("to") or 0)
            width = int(el.get("width") or 0)
            if lo == hi:
                segs.append(("lit", str(lo)))
                continue
            n = max(1, width or len(str(hi)))
            if lo != 0 or hi != 10 ** n - 1:
                return None
            if width:
                segs.append(("lit", "?d" * n))
                count *= 10 ** n
            else:
                segs.append(("var_digits", 1, n))   # 不补零：1~n 位都试
            continue
        if kind == "charset":
            chars = _ALPHABET.get(str(el.get("charset") or "digits"), DIGITS)
            lo = max(1, min(12, int(el.get("len_min") or el.get("len") or 1)))
            hi = max(lo, min(12, int(el.get("len_max") or lo)))
            ranged = (lo != hi) or el.get("len_min") is not None
            if chars == DIGITS:
                if ranged and not el.get("width"):
                    segs.append(("var_digits", lo, hi))   # 数字：?d 逐位长
                else:
                    segs.append(("lit", "?d" * hi))
                    count *= 10 ** hi
                continue
            if chars not in sets:
                if len(sets) >= 4:
                    return None
                sets.append(chars)
            idx = sets.index(chars) + 1
            if ranged:
                segs.append(("var_set", idx, lo, hi))
            else:
                segs.append(("lit", f"?{idx}" * hi))
                count *= len(chars) ** hi
            continue
        return None
    if not segs:
        return None
    pairs = _expand_masks([], segs, count, sets)
    if not pairs or len(pairs) > 256:
        return None
    return {"masks": [m for m, _c in pairs], "charsets": sets,
            "count": sum(c for _m, c in pairs)}


def brute_list(elements: list[dict], limit: int = 2_000_000) -> "list[str]":
    """多要素笛卡尔积（前缀 + 数字 + 后缀 + 字符集…），有上限。"""
    total = 1
    for el in elements:
        total *= max(1, element_count(el))
        if total > limit * 4:
            break
    out = [""]
    for el in elements:
        vals = element_values(el, limit)
        if not vals:
            continue
        nxt = []
        for pre in out:
            for v in vals:
                nxt.append(pre + v)
                if len(nxt) >= limit:
                    break
            if len(nxt) >= limit:
                break
        out = nxt
        if len(out) >= limit:
            break
    return out


def iter_candidates(elements: list[dict], start: int = 0):
    """惰性产出候选（顺序和 brute_list 一样，但不建大列表）。

    start > 0 时从第 start 个候选继续（GPU 崩掉后断点续跑用），数字类形状是 O(1) 跳过的。
    """
    kinds = [str(el.get("type") or "literal") for el in elements]
    start = max(0, int(start or 0))
    # ① 纯数字
    if len(elements) == 1 and kinds[0] == "num":
        el = elements[0]
        lo = int(el.get("from") or 0) + start
        hi = int(el.get("to") or 0)
        width = int(el.get("width") or 0)
        for i in range(lo, hi + 1):
            val = str(i)
            yield val.zfill(width) if width else val
        return
    # ② 前缀字面量 + 数字 + 后缀字面量
    if 1 <= len(elements) <= 3 and "num" in kinds and all(k in ("literal", "num") for k in kinds) \
            and kinds.count("num") == 1:
        pre = ""
        suf = ""
        num = None
        for el, kind in zip(elements, kinds):
            if kind == "num":
                num = el
                continue
            txt = str(el.get("value") or "")
            if num is None:
                pre += txt
            else:
                suf += txt
        if num is not None:
            lo = int(num.get("from") or 0) + start
            hi = int(num.get("to") or 0)
            width = int(num.get("width") or 0)
            for i in range(lo, hi + 1):
                val = str(i)
                if width:
                    val = val.zfill(width)
                yield pre + val + suf
            return
    # ③ 其他：各要素展开后做笛卡尔积
    lists = [element_values(el) for el in elements] or [[""]]
    seq = (("".join(c) for c in itertools.product(*lists)))
    for cand in itertools.islice(seq, start, None):
        yield cand



# ---------- 并行试解 ----------
def _worker(args):
    """子进程：用一段候选去试 zip。参数是可 pickle 的普通类型。"""
    import io as _io
    data, first_name, cands = args
    try:
        import pyzipper as _pz
        opener = _pz.AESZipFile
    except Exception:
        import zipfile as _zfx
        opener = _zfx.ZipFile
    try:
        zf = opener(_io.BytesIO(data))
    except Exception:
        return None
    for pw in cands:
        try:
            zf.read(first_name, pwd=pw.encode("utf-8"))
            zf.close()
            return pw
        except Exception:
            continue
    zf.close()
    return None


def _try_batch(data: bytes, first_name: str, cands: "list[str]", workers: int,
               log=None) -> "str | None":
    """一批候选：多进程并行试，失败退回串行。"""
    if not cands:
        return None
    if workers <= 1 or len(cands) < 2000:
        for pw in cands:
            zf = _try_zip(data, pw.encode("utf-8"))
            if zf is not None:
                zf.close()
                return pw
        return None
    try:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        chunk = max(1, len(cands) // (workers * 4))
        chunks = [cands[i:i + chunk] for i in range(0, len(cands), chunk)]
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            futs = [ex.submit(_worker, (data, first_name, c)) for c in chunks]
            errs = 0
            for fut in futs:
                try:
                    hit = fut.result()
                except Exception as exc:  # noqa: BLE001
                    errs += 1
                    if errs == 1 and log:
                        log(f"并行出错（{exc}），改串行兜底")
                    continue
                if hit:
                    for f in futs:
                        f.cancel()
                    return hit
            if errs >= len(futs):
                # 子进程全挂了（偶发的 spawn 失败）：回到串行，结果不能丢
                for pw in cands:
                    zf = _try_zip(data, pw.encode("utf-8"))
                    if zf is not None:
                        zf.close()
                        return pw
                return None
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"并行不可用（{exc}），退回单进程")
        for pw in cands:
            zf = _try_zip(data, pw.encode("utf-8"))
            if zf is not None:
                zf.close()
                return pw
    return None


def _doc_worker(payload):
    """子进程：一批候选密码，按文档校验（PDF/Office）。"""
    data, cands = payload
    from media import docs as _docs
    try:
        prepared = _docs.prepared(data)
    except Exception:  # noqa: BLE001
        return None
    chk = prepared.get("check")
    if chk is None:
        return None
    for pw in cands:
        try:
            if chk(pw.encode("utf-8")):
                return pw
        except Exception:  # noqa: BLE001
            continue
    return None


def _parallel_try_doc(data: bytes, cands: "list[str]", workers: int,
                      log=None, pause=None, cancel=None) -> "str | None":
    """文档类（PDF/Office）的 CPU 穷举：一次解析、多进程校验。"""
    if not cands:
        return None
    workers = max(1, min(int(workers or 1), 16))
    total = len(cands)
    step = max(1000, min(40000, total // 20 or 1000))
    for off in range(0, total, step):
        if cancel is not None and cancel.is_set():
            return None
        if pause is not None and pause.is_set():
            if _hc().run_paused_wait(pause, cancel, log):
                return None
        part = cands[off:off + step]
        if workers <= 1 or len(part) < 3000:
            hit = _doc_worker((data, part))
        else:
            try:
                import multiprocessing as mp
                from concurrent.futures import ProcessPoolExecutor
                chunk = max(1, len(part) // (workers * 2))
                chunks = [part[i:i + chunk] for i in range(0, len(part), chunk)]
                ctx = mp.get_context("spawn")
                hit = None
                with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
                    for r in ex.map(_doc_worker, [(data, c) for c in chunks]):
                        if r:
                            hit = r
                            break
            except Exception:  # noqa: BLE001
                hit = _doc_worker((data, part))
        if hit:
            return hit
        if log and off and (off // step) % 5 == 0:
            log(f"文档已试 {min(off + step, total):,}/{total:,}")
    return None


def _parallel_try(data: bytes, first_name: str, cands: "list[str]",
                  workers: int, log=None, pause=None, cancel=None) -> "str | None":
    """CPU 穷举：按批跑，批与批之间响应暂停/取消。"""
    if not cands:
        return None
    workers = max(1, min(int(workers or 1), 16))
    total = len(cands)
    step = max(2000, min(60000, total // 20 or 2000))
    for off in range(0, total, step):
        if cancel is not None and cancel.is_set():
            return None
        if pause is not None and pause.is_set():
            if _hc().run_paused_wait(pause, cancel, log):
                return None
        hit = _try_batch(data, first_name, cands[off:off + step], workers, log)
        if hit:
            return hit
        if log and off and (off // step) % 5 == 0:
            log(f"CPU 已试 {min(off + step, total):,}/{total:,}")
    return None

def _zip_pick(zf: zipfile.ZipFile) -> str | None:
    for info in zf.infolist():
        if info.flag_bits & 0x1:
            return info.filename
    return None


def _try_zip(data: bytes, pwd: bytes | None) -> zipfile.ZipFile | None:
    try:
        zf = ZIP_READER(io.BytesIO(data))
        name = _zip_pick(zf)
        if name is None:
            return zf
        if pwd is None:
            zf.close()
            return None
        zf.read(name, pwd=pwd)
        return zf
    except Exception:
        return None


LEET_SINGLE = {"a": "@", "e": "3", "i": "1", "o": "0", "s": "$",
               "t": "7", "g": "9", "b": "8", "l": "1", "z": "2"}
DICT_CAP = 1_000_000          # 密码本变形后的条数上限（再大就太占内存）
GPU_DICT_MIN = 20_000         # 密码本条数超过这个就直接交给显卡


def dict_variant_count(n: int, rules: dict | None) -> int:
    """估算变形后的条数（不真的展开，给界面预估用）。"""
    rules = rules or {}
    mult = 1
    if rules.get("case"):
        mult *= 4                       # 原样 / 小写 / 大写 / 首字母大写
    digits = min(4, int(rules.get("digits") or 0))
    if digits > 0:
        mult *= 10 ** digits
    if rules.get("leet"):
        mult *= 3                       # 全替换 + 单字母替换（估）
    return n * mult


def expand_dict_rules(words: list[str], rules: dict | None = None,
                      cap: int = DICT_CAP) -> list[str]:
    """按规则扩充密码本：原样 → 大小写 → 追加数字 → leet 替换，顺序由常用到冷门。"""
    rules = rules or {}
    base = [str(w) for w in (words or []) if str(w)]
    if not base or not (rules.get("case") or int(rules.get("digits") or 0)
                        or rules.get("leet")):
        return base
    out: list[str] = []
    seen: set[str] = set()

    def push(w: str) -> None:
        if w and w not in seen and len(out) < cap:
            seen.add(w)
            out.append(w)

    for w in base:
        push(w)
    if rules.get("case"):
        for w in base:
            push(w.lower())
            push(w.upper())
            if len(w) > 1:
                push(w[0].upper() + w[1:].lower())
    digits = min(4, int(rules.get("digits") or 0))
    if digits > 0:
        for w in list(out):
            for i in range(10 ** digits):
                push(w + str(i))
    if rules.get("leet"):
        for w in list(out):
            full = "".join(LEET_SINGLE.get(c.lower(), c) for c in w)
            push(full)
            for pos, ch in enumerate(w):
                rep = LEET_SINGLE.get(ch.lower())
                if rep:
                    push(w[:pos] + rep + w[pos + 1:])
    return out


def _deadline(brute: dict | None) -> float:
    """每个包最多跑多久（0 = 不限）。"""
    try:
        m = float((brute or {}).get("max_minutes") or 0)
    except (TypeError, ValueError):
        m = 0.0
    return (time.time() + m * 60) if m > 0 else 0.0


def _stop_reason(cancel, deadline: float) -> str:
    if cancel is not None and cancel.is_set():
        return "cancel"
    if deadline and time.time() > deadline:
        return "timeout"
    return ""


CHARSET_NAMES = {"digits": "数字", "lower": "小写字母", "upper": "大写字母",
                 "letters": "大小写字母", "alnum": "数字+小写", "symbols": "符号",
                 "mix": "混合字符"}


def _elements_label(elements: list[dict]) -> str:
    parts = []
    for el in elements:
        kind = str(el.get("type") or "literal")
        if kind == "literal":
            txt = str(el.get("value") or "")
            if txt:
                parts.append(f"“{txt}”")
        elif kind == "num":
            lo = int(el.get("from") or 0)
            hi = int(el.get("to") or 0)
            if lo == 0:
                parts.append(f"数字 {len(str(max(1, hi)))} 位")
            else:
                parts.append(f"数字 {lo}-{hi}")
        elif kind == "charset":
            name = CHARSET_NAMES.get(str(el.get("charset") or "digits"), "字符")
            lo = int(el.get("len_min") or el.get("len") or 1)
            hi = int(el.get("len_max") or lo)
            parts.append(f"{name} {lo}~{hi} 位" if lo != hi else f"{name} {lo} 位")
    return " + ".join(parts) or "组合要素"


def plan_attack(brute: dict | None, dict_count: int = 0, mode: int = 0) -> dict:
    """给界面做预估：变形后密码本多少条、穷举空间多大、大概要跑多久。"""
    brute = brute or {}
    dv = dict_variant_count(int(dict_count or 0), brute.get("rules") or {})
    els = brute.get("elements") or []
    count = 0
    masks = 0
    if els:
        mi = build_mask(els)
        if mi:
            count, masks = int(mi["count"]), len(mi["masks"])
        else:
            total = 1
            for el in els:
                total *= max(1, element_count(el))
            count = int(total)
    hc = _hc()
    gpu = bool(hc.exe_ok())
    sp_gpu = float(hc.speed_hint(mode)) if gpu else 0.0
    sp_cpu = 12000.0
    total = dv + count
    eta_gpu = (1.5 + dv / 2e6 + count / sp_gpu) if (gpu and sp_gpu) else 0.0
    return {"ok": True, "dict_count": int(dict_count or 0), "dict_variants": dv,
            "brute_count": count, "masks": masks, "total": total,
            "label": _elements_label(els) if els else "",
            "gpu": gpu, "speed_gpu": sp_gpu, "speed_cpu": sp_cpu,
            "eta_gpu": round(eta_gpu, 1), "eta_cpu": round(total / sp_cpu, 1)}


SEVENZIP_BRUTE_CAP = 50_000     # 7z/RAR 每个候选要起一次 7z 进程，给个上限
SEVENZIP_WORKERS = 8


def _find_password_7z(data: bytes, dict_words: list[str], brute: dict | None,
                      log=None, source: str = "", cancel=None,
                      info: dict | None = None, pause=None) -> tuple[bytes | None, bool]:
    """7z / RAR 的密码尝试：空密码 → 密码本（含变形）→ 已破解密码本 → 小空间穷举。

    RAR/7z 没有现成的 GPU 校验（hashcat 的 11600/12500/13000 要另外抽哈希），
    所以这里靠 7z.exe 逐个试：每个候选起一次进程，约 50~100ms。
    """
    info = info if info is not None else {}
    if not find_7z() and not find_rar():
        if log:
            log("这是 7z/RAR 包，需要 7-Zip 或 WinRAR：装一个，或把 exe 路径写进 7z.txt / rar.txt")
        info["engine"] = "缺少 7-Zip / WinRAR"
        return None, True
    started = time.time()
    deadline = _deadline(brute)
    tmp = tempfile.mkdtemp(prefix="pd7z_")
    ext = EXT_FOR_FMT.get(sniff_archive(data), ".7z")
    src = os.path.join(tmp, "in" + ext)
    try:
        with open(src, "wb") as f:
            f.write(data)
        if sevenzip_try(src, ""):
            info.update(engine="没有密码", seconds=round(time.time() - started, 2))
            return b"", False
        if log:
            log("检测到密码保护（7z/RAR）…")
        words = expand_dict_rules(dict_words, (brute or {}).get("rules") or {})
        found_list = [f["pwd"] for f in load_found() if f.get("pwd")]

        # 7z 有现成的 hashcat 模式（11600）：有显卡就先试字典、再试掩码
        # （RAR 没有现成哈希格式，这里跳过，免得白跑一次）
        _is_7z = sniff_archive(data) == "7z"
        hc = _hc()
        els_now = (brute or {}).get("elements") or []
        if _is_7z and hc.exe_ok():
            tries: "list[tuple[str, dict]]" = []
            cands = list(dict.fromkeys([*words, *found_list]))
            if cands:
                tries.append((f"字典 {len(cands):,} 条", {"candidates": cands}))
            mm = build_mask(els_now) if els_now else None
            if mm:
                tries.append((f"掩码 {mm['count']:,} 个", {
                    "masks": mm["masks"],
                    "charsets": [mm["charsets"][k] for k in sorted(mm["charsets"])],
                }))
            for label, kw in tries:
                why = _stop_reason(cancel, deadline)
                if why:
                    if log:
                        log("已取消" if why == "cancel" else "到时间上限了，先停")
                    return None, True
                try:
                    r = hc.crack_7z(data, timeout=int((brute or {}).get("timeout") or 1800),
                                    runtime=int(max(1, deadline - time.time())) if deadline else 0,
                                    log=log, cancel=cancel, pause=pause, **kw)
                except Exception as exc:  # noqa: BLE001
                    r = {"ok": False, "error": str(exc)}
                if r.get("ok"):
                    pw = r["password"]
                    if log:
                        log(f"显卡破解成功：{pw}（{label}，{r.get('seconds', 0):.1f}s）")
                    save_found(pw, source)
                    info.update(engine="7z 显卡（11600）",
                                seconds=round(time.time() - started, 2))
                    return pw.encode("utf-8"), True
                if log:
                    log(f"  {label} 没成功（{r.get('error') or '跑完没命中'}）")
            if log:
                log("显卡这条路没结果，改用 7z.exe 逐个试")
        for label, arr in (("密码本", words), ("已破解密码本", found_list)):
            if not arr:
                continue
            for i, pw in enumerate(arr):
                if i % 10 == 0:
                    why = _stop_reason(cancel, deadline)
                    if why:
                        if log:
                            log("已取消" if why == "cancel" else "到时间上限了，先停")
                        return None, True
                if sevenzip_try(src, pw):
                    if log:
                        log(f"{label}命中：{pw}（第 {i + 1} 个）")
                    save_found(pw, source)
                    info.update(engine=f"{label}（7z）", seconds=round(time.time() - started, 2),
                                tries=i + 1)
                    return pw.encode("utf-8"), True
        els = (brute or {}).get("elements") or []
        if els and not (brute or {}).get("no_brute"):
            cap = min(SEVENZIP_BRUTE_CAP,
                      int((brute or {}).get("limit") or SEVENZIP_BRUTE_CAP))
            if log:
                log(f"7z/RAR 只能逐个起 7z 试，最多试 {cap:,} 个候选")
            hit = _try_many_7z(src, iter_candidates(els), cap, log, cancel, deadline)
            if hit:
                if log:
                    log(f"暴力成功：{hit}")
                save_found(hit, source)
                info.update(engine="穷举（7z）", seconds=round(time.time() - started, 2))
                return hit.encode("utf-8"), True
            if log:
                log("穷举完成，没找到")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    info.update(seconds=round(time.time() - started, 2))
    return None, True


def _try_many_7z(path: str, cands, cap: int, log=None, cancel=None, deadline: float = 0.0):
    """多线程试密码（7z 是独立进程，可以并行）。命中就返回密码。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pool = ThreadPoolExecutor(max_workers=SEVENZIP_WORKERS)
    futures = {}
    tried = 0
    try:
        it = iter(cands)
        for w in range(SEVENZIP_WORKERS * 4):
            try:
                cand = next(it)
            except StopIteration:
                break
            futures[pool.submit(sevenzip_try, path, cand)] = cand
        while futures:
            if _stop_reason(cancel, deadline):
                break
            done = next(as_completed(list(futures.keys())), None)
            if done is None:
                break
            cand = futures.pop(done)
            tried += 1
            try:
                ok = done.result()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                for f in futures:
                    f.cancel()
                return cand
            if tried >= cap:
                break
            try:
                nxt = next(it)
            except StopIteration:
                continue
            futures[pool.submit(sevenzip_try, path, nxt)] = nxt
            if log and tried and tried % 2000 == 0:
                log(f"  已试 {tried:,} 个…")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return None


def _find_password_doc(data: bytes, dinf: dict, dict_words: list[str],
                       brute: dict | None, log=None, source: str = "", cancel=None,
                       pause=None, info: dict | None = None) -> tuple[bytes | None, bool]:
    """PDF / Office 的密码尝试：密码本 → 穷举（CPU，一次解析多次校验）。"""
    info = info if info is not None else {}
    from media import docs as _docs
    kind = dinf.get("kind") or "文档"
    started = time.time()
    deadline = _deadline(brute)
    prepared = _docs.prepared(data)
    chk = prepared.get("check")
    if chk is None:
        if log:
            log(f"{kind}：这个加密方式还不会校验（{dinf.get('detail') or ''}）")
        info["engine"] = kind
        return None, True
    if log:
        log(f"检测到 {kind}（{dinf.get('detail') or ''}），开始试密码…")

    def finish(pw: bytes, engine: str):
        if log:
            log(f"{engine}命中：{pw.decode('utf-8', 'replace')}")
        save_found(pw.decode("utf-8", "replace"), source)
        info.update(engine=engine, seconds=round(time.time() - started, 2))
        return pw, True

    # 空密码
    if chk(b""):
        return finish(b"", "没有密码")
    words = expand_dict_rules(dict_words, (brute or {}).get("rules") or {})
    found_list = [f["pwd"] for f in load_found() if f.get("pwd")]
    for label, arr in (("密码本", words), ("已破解密码本", found_list)):
        for i, pw in enumerate(arr or []):
            if i % 200 == 0:
                why = _stop_reason(cancel, deadline)
                if why:
                    if log:
                        log("已取消" if why == "cancel" else "到时间上限了，先停")
                    return None, True
            try:
                if chk(pw.encode("utf-8")):
                    return finish(pw.encode("utf-8"), label)
            except Exception:  # noqa: BLE001
                continue
    els = (brute or {}).get("elements") or []
    if els:
        cap = int((brute or {}).get("cpu_cap") or 2_000_000)
        space = brute_list(els, cap)
        if space:
            if log:
                log(f"穷举空间：{len(space):,} 个候选（CPU 校验）")
            hit = _parallel_try_doc(data, space, int((brute or {}).get("workers") or 4),
                                    log, pause=pause, cancel=cancel)
            if hit:
                return finish(hit.encode("utf-8"), "穷举")
            if log:
                log("穷举完成，没找到")
    info.update(seconds=round(time.time() - started, 2))
    return None, True


def find_password(data: bytes, dict_words: list[str], brute: dict | None,
                  log=None, source: str = "", cancel=None,
                  info: dict | None = None, pause=None) -> tuple[bytes | None, bool]:
    """返回 (密码, 是否加密)。空密码 → 密码本(含变形) → 已破解密码本 → GPU/CPU 穷举。

    brute 可用的键（都能省略）：
      rules: {case, digits, leet}     密码本变形
      max_minutes: 0                  每个包最多跑多久
      elements / max_len / prefix / suffix   旧的"要素"写法
      workers / limit / timeout
    命中时把方式、耗时、试了多少写进 info（给界面显示）。
    """
    info = info if info is not None else {}
    # 文件名里常直接写着密码（「密码：xxx」「解压密码 xxx」），先试这些
    try:
        from media import namepass as _np
        name_pw = _np.candidates(source)
    except Exception:  # noqa: BLE001
        name_pw = []
    if name_pw:
        dict_words = list(dict.fromkeys([*name_pw, *(dict_words or [])]))
        if log:
            log(f"文件名里像是写着密码：{'、'.join(name_pw)}，先试这个")
    fmt = sniff_archive(data)
    if fmt in ("", "msi", "ole"):
        try:
            from media import docs as _docs
            dinf = _docs.info(data)
        except Exception:  # noqa: BLE001
            dinf = {}
        if dinf.get("encrypted"):
            return _find_password_doc(data, dinf, dict_words, brute, log, source,
                                      cancel, pause, info)
        if dinf.get("format") in ("pdf", "ole"):
            # 普通 PDF / 普通 OLE：本来就没密码，别当成"有密码但没试出来"
            info.update(engine="没有密码", seconds=0.0)
            return b"", False
    if fmt in ("7z", "rar"):
        return _find_password_7z(data, dict_words, brute, log, source, cancel, info, pause)
    started = time.time()
    deadline = _deadline(brute)

    def finish(pw: bytes, engine: str, tries: int = 0):
        info.update(engine=engine, seconds=round(time.time() - started, 2), tries=tries)
        return pw, True

    zf = _try_zip(data, None)
    if zf is not None:
        zf.close()
        info.update(engine="没有密码", seconds=round(time.time() - started, 2))
        return b"", False
    if log:
        log("检测到密码保护，开始尝试…")

    # ---------------- ① 密码本（可带变形） ----------------
    words = expand_dict_rules(dict_words, (brute or {}).get("rules") or {})
    if words and len(words) != len(dict_words):
        if log:
            log(f"密码本变形：{len(dict_words):,} → {len(words):,} 条")
    if words:
        hc = _hc()
        if len(words) >= GPU_DICT_MIN and hc.exe_ok():
            try:
                r = hc.crack_zip(data, candidates=words, count=len(words),
                                 timeout=int((brute or {}).get("timeout") or 1800),
                                 runtime=int(max(1, deadline - time.time())) if deadline else 0,
                                 log=log, cancel=cancel, pause=pause)
            except Exception as exc:  # noqa: BLE001
                r = {"ok": False, "error": str(exc)}
            if r.get("ok"):
                if log:
                    log(f"密码本命中：{r['password']}")
                return finish(r["password"].encode("utf-8"), "密码本（GPU）", len(words))
        for i, pw in enumerate(words):
            if i % 2000 == 0:
                why = _stop_reason(cancel, deadline)
                if why:
                    if log:
                        log("已取消" if why == "cancel" else "到时间上限了，先停")
                    return None, True
            zf = _try_zip(data, pw.encode("utf-8"))
            if zf is not None:
                zf.close()
                if log:
                    log(f"密码本命中：{pw}（第 {i + 1} 个）")
                return finish(pw.encode("utf-8"), "密码本", i + 1)

    # ---------------- ② 已破解密码本 ----------------
    found = [f["pwd"] for f in load_found() if f.get("pwd")]
    for i, pw in enumerate(found):
        zf = _try_zip(data, pw.encode("utf-8"))
        if zf is not None:
            zf.close()
            if log:
                log(f"已破解密码本命中：{pw}（第 {i + 1} 个）")
            return finish(pw.encode("utf-8"), "已破解密码本", i + 1)

    if brute is not None and not brute.get("auto") and not dict_words \
            and not brute.get("elements") and not int(brute.get("max_len") or 0):
        return None, True

    # ---------------- ③ 穷举（GPU 掩码优先，CPU 兜底） ----------------
    els = (brute or {}).get("elements") or []
    if not els and brute and int(brute.get("max_len") or 0) > 0:
        els = [{"type": "num", "from": 0, "to": 10 ** int(brute["max_len"]) - 1,
                "width": 0}]
        if brute.get("prefix"):
            els.insert(0, {"type": "literal", "value": brute["prefix"]})
        if brute.get("suffix"):
            els.append({"type": "literal", "value": brute["suffix"]})
    if els:
        cap = int((brute or {}).get("limit") or 2_000_000)
        mask_info = build_mask(els)
        space: "list[str] | None" = None
        if mask_info is not None:
            m_masks = mask_info["masks"]
            m_sets = mask_info["charsets"]
            count = mask_info["count"]
        else:
            m_masks, m_sets = None, []
            space = brute_list(els, cap)
            count = len(space)
        if log:
            log(f"穷举空间：{count:,} 个候选（{len(els)} 个要素）")
        t0 = time.time()
        hit = None
        if count >= GPU_MIN and _hc().exe_ok():
            try:
                r = _hc().crack_zip(
                    data, masks=m_masks, charsets=m_sets,
                    candidates=None if m_masks else space,
                    iterator_factory=((lambda start=0: iter_candidates(els, start))
                                      if els else None),
                    count=count,
                    timeout=int((brute or {}).get("timeout") or 1800),
                    runtime=int(max(1, deadline - time.time())) if deadline else 0,
                    log=log, cancel=cancel, pause=pause)
            except Exception as exc:  # noqa: BLE001
                r = {"ok": False, "error": str(exc)}
            if r.get("ok"):
                hit = r["password"]
                info["engine"] = "GPU 穷举"
            elif cancel is not None and cancel.is_set():
                if log:
                    log("已取消")
                return None, True
            elif log:
                log(f"GPU 未命中（{r.get('error') or '候选已跑完'}），改走 CPU")
        if hit is None:
            if space is None:
                if count <= cap:
                    space = brute_list(els, cap)
                elif log:
                    log(f"候选 {count:,} 个超出 CPU 上限 {cap:,}，跳过 CPU 穷举")
            if space:
                why = _stop_reason(cancel, deadline)
                if why:
                    if log:
                        log("已取消" if why == "cancel" else "到时间上限了，先停")
                    return None, True
                first = _zip_pick(ZIP_READER(io.BytesIO(data)))
                hit = _parallel_try(data, first, space,
                                    int((brute or {}).get("workers") or 4), log,
                                    pause=pause, cancel=cancel)
                if hit:
                    info["engine"] = "CPU 穷举"
        if hit:
            if log:
                log(f"暴力成功：{hit}（{count:,} 个候选，{time.time()-t0:.1f}s）")
            save_found(hit, source)
            return finish(hit.encode("utf-8"), info.get("engine") or "穷举", count)
        if log:
            log(f"穷举完成，没找到（{time.time()-t0:.1f}s）")
    return None, True


def _expand_7z(data: bytes, name: str, dict_words, brute, pieces, depth: int,
               base: str, log=None, cancel=None, fmt: str = "") -> None:
    """7z / RAR / cab / iso 等：先试密码，再解到临时目录，读进内存继续递归。"""
    if not find_7z():
        if log:
            log(f"跳过 {name}：需要 7-Zip（程序自带 vendor\\7z\\7za.exe；"
                "RAR 还要装 WinRAR 或完整版 7-Zip）")
        return
    if fmt == "rar" and "7za" in os.path.basename(find_7z()).lower() and not find_rar():
        if log:
            log(f"跳过 {name}：自带的 7za 解不了 RAR，请装 WinRAR 或完整版 7-Zip")
        return
    pw, encrypted = _find_password_7z(data, dict_words, brute, log, name, cancel)
    if encrypted and pw is None:
        return
    pwd = pw.decode("utf-8", "replace") if pw else ""
    tmp = tempfile.mkdtemp(prefix="pd7zx_")
    try:
        ext = EXT_FOR_FMT.get(fmt or sniff_archive(data), ".7z")
        src = os.path.join(tmp, "in" + ext)
        with open(src, "wb") as f:
            f.write(data)
        out = os.path.join(tmp, "out")
        rc, text = _run7z(["x", src, f"-o{out}", f"-p{pwd}", "-y"], timeout=600)
        if rc != 0:
            if log:
                tail = [ln for ln in text.strip().splitlines() if ln.strip()][-1:] or [""]
                log(f"7z 解不开 {name}（rc={rc}）{tail[0][:90]}")
            return
        total = 0
        count = 0
        for dirpath, _dirs, files in os.walk(out):
            for fn in files:
                fp = os.path.join(dirpath, fn)
                rel = os.path.relpath(fp, out).replace("\\", "/")
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    continue
                if size > MAX_ENTRY:
                    if log:
                        log(f"跳过超大文件 {rel}（{size / 1e6:.0f}MB）")
                    continue
                total += size
                if total > MAX_INFLATE:
                    if log:
                        log(f"{name} 解出来超过 4GB，后面的跳过")
                    return
                try:
                    with open(fp, "rb") as f:
                        blob = f.read()
                except OSError:
                    continue
                count += 1
                _emit(blob, rel, base, dict_words, brute, pieces, depth, log, cancel)
        if log:
            log(f"  7z 解开 {count} 个文件（{total / 1e6:.1f}MB）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 并行解压
ZIP_WORKERS = max(1, min(16, (os.cpu_count() or 4)))
PAR_MIN_SIZE = 512 * 1024          # 小于这个就不折腾并行


def _read_one_zip_entry(data: bytes, info, pw, ) -> "bytes | None":
    try:
        with ZIP_READER(io.BytesIO(data)) as zf:
            return zf.read(info, pwd=pw if pw else None)
    except Exception:  # noqa: BLE001
        return None


def _read_entries_parallel(data: bytes, infos, pw, cancel) -> list:
    """并行解出一个 zip 里的所有条目；返回与 infos 对齐的 bytes/None 列表。"""
    n = len(infos)
    out: "list[bytes | None]" = [None] * n
    if n == 0:
        return out
    todo = []
    for i, info in enumerate(infos):
        if info.is_dir() or info.filename.endswith("/"):
            continue
        if info.file_size > MAX_ENTRY:
            continue
        todo.append(i)
    total = sum(infos[i].file_size for i in todo)
    workers = min(ZIP_WORKERS, len(todo))
    if workers <= 1 or total < PAR_MIN_SIZE:
        for i in todo:
            if cancel is not None and cancel.is_set():
                break
            out[i] = _read_one_zip_entry(data, infos[i], pw)
        return out
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_read_one_zip_entry, data, infos[i], pw): i for i in todo}
        for fut in futs:
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception:  # noqa: BLE001
                out[i] = None
    return out


# ------------------------------------------------------------------ 解包
def expand(data: bytes, name: str, dict_words: list[str], brute: dict | None,
           pieces: list[Piece], depth: int = 0, base: str = "", log=None,
           cancel=None) -> None:
    """把 data 解开，产物追加到 pieces（内存），嵌套的继续递归。

    格式按**文件头**认（和后缀无关）：zip/tar/gz/bz2/xz 走内置实现，
    7z/rar/cab/iso/chm/cpio/rpm/arj/lzh/wim/msi/zst/vhd… 交给 7z.exe。
    """
    if depth > MAX_DEPTH:
        if log:
            log(f"嵌套超过 {MAX_DEPTH} 层，停止：{name}")
        return
    low = name.lower()
    fmt = sniff_archive(data)
    if fmt and log and depth <= 1:
        ext = os.path.splitext(low)[1]
        if ext and fmt_from_name(low) != fmt:
            log(f"  按文件头认作 {fmt}（后缀 {ext}）：{name}")

    if fmt == "zip":
        pw, encrypted = find_password(data, dict_words, brute, log, name, cancel)
        if encrypted and pw is None:
            return
        try:
            zf = ZIP_READER(io.BytesIO(data))
        except zipfile.BadZipFile:
            if log:
                log(f"不是有效的 zip：{name}")
            return
        with zf:
            infos = zf.infolist()
            blobs = _read_entries_parallel(data, infos, pw, cancel)
            for info, blob in zip(infos, blobs):
                rel = fix_zip_name(info).replace("\\", "/")
                if rel.endswith("/") or info.is_dir():
                    pieces.append(Piece(base + rel, is_dir=True))
                    continue
                if blob is None:
                    if log:
                        log(f"解不出 {rel}")
                    continue
                _emit(blob, rel, base, dict_words, brute, pieces, depth, log, cancel)
        return

    if fmt == "tar":
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"打不开 tar：{name}（{exc}）")
            return
        with tf:
            for m in tf.getmembers():
                rel = m.name.replace("\\", "/").lstrip("./")
                if not rel:
                    continue
                if m.isdir():
                    pieces.append(Piece(base + rel, is_dir=True))
                    continue
                if not m.isfile() or m.size > MAX_ENTRY:
                    continue
                try:
                    f = tf.extractfile(m)
                    blob = f.read() if f else b""
                except Exception:
                    continue
                _emit(blob, rel, base, dict_words, brute, pieces, depth, log, cancel)
        return

    if fmt in ("gzip", "bzip2", "xz"):
        try:
            if fmt == "gzip":
                blob = gzip.decompress(data)
            elif fmt == "bzip2":
                blob = bz2.decompress(data)
            else:
                blob = lzma.decompress(data)
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"解压失败 {name}：{exc}")
            return
        inner = _strip_archive_ext(os.path.basename(name))
        if sniff_archive(blob[:4096]) == "tar":
            # x.tar.gz / x.tgz / 改名成 .txt 的 tar.gz：tar 的内容直接放当前层，
            # 不要再套一层目录，否则会变成 x/x/...
            expand(blob, inner + ".tar", dict_words, brute, pieces,
                   depth + 1, base, log, cancel)
        else:
            _emit(blob, inner, base, dict_words, brute, pieces, depth, log, cancel)
        return

    if fmt in ("msi", "ole"):
        try:
            from media import docs as _docs
            dinf = _docs.info(data)
        except Exception:  # noqa: BLE001
            dinf = {}
        if dinf.get("encrypted"):
            if log:
                log(f"{name} 是加密的 {dinf.get('kind')}，解压不了；去「暴力破解」页试密码")
            return

    if fmt in SEVENZIP_FMT:
        _expand_7z(data, name, dict_words, brute, pieces, depth, base, log, cancel, fmt)
        return

    if log:
        if fmt:
            log(f"这个格式还不会解（{fmt}）：{name}")
        elif is_archive(low):
            log(f"文件头认不出是压缩包（可能损坏或后缀不对）：{name}")


def _emit(blob: bytes, rel: str, base: str, dict_words, brute, pieces, depth, log,
          cancel=None):
    """一个成员解开后：是压缩包就继续在内存里解，不是就落地。"""
    if len(blob) > MAX_INFLATE:
        if log:
            log(f"跳过超大产物 {rel}")
        return
    if (is_archive(rel) or sniff_archive(blob[:4096])) and len(blob) > 64:
        inner_dir = _strip_archive_ext(rel)
        if log:
            log(f"  嵌套：{rel} → 继续解（{len(blob)/1e6:.1f}MB）")
        expand(blob, rel, dict_words, brute, pieces,
               depth + 1, base + inner_dir + "/", log, cancel)
        return
    pieces.append(Piece(base + rel, data=blob))


# ------------------------------------------------------------------ 作业
class UnzipJob(threading.Thread):
    daemon = True

    def __init__(self, src: str, dst: str, passwords: list[str],
                 brute: dict | None = None, recursive: bool = True,
                 split_dir: bool = True, auto_brute: bool = True):
        super().__init__(name="unzip-job")
        self.src = os.path.abspath(src)
        self.dst = os.path.abspath(dst)
        self.passwords = passwords or []
        self.brute = dict(brute or {})
        self.brute["auto"] = bool(auto_brute)
        self.recursive = bool(recursive)
        self.split_dir = bool(split_dir)      # 每个包单独一个子目录
        self.cancel_ev = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._pwd_cache: dict[str, bytes] = {}
        self._st = {
            "running": True, "state": "running", "total": 0, "done": 0,
            "archives": 0, "files": 0, "bytes": 0, "nested": 0,
            "failed": 0, "cracked": 0, "current": "", "message": "准备中…",
            "src": self.src, "dst": self.dst, "log": [], "elapsed": 0,
        }

    def log(self, msg: str):
        with self._lock:
            lst = self._st["log"]
            lst.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(lst) > 400:
                del lst[:100]

    def cancel(self):
        self.cancel_ev.set()

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._st)
            s["log"] = list(self._st["log"])
        s["elapsed"] = round(time.time() - self._t0, 1)
        return s

    def _set(self, **kw):
        with self._lock:
            self._st.update(kw)

    def _list(self) -> list[str]:
        out = []
        for dp, dirs, files in os.walk(self.src):
            dirs[:] = [d for d in dirs if not d.startswith("$")]
            for fn in files:
                low = fn.lower()
                fp = os.path.join(dp, fn)
                if low.endswith(".001") and is_archive(low[:-4]):
                    out.append(fp)                            # name.zip.001 分卷
                elif low.endswith(".z01"):
                    out.append(fp)                            # name.z01 分卷（第一个）
                elif low.endswith((".z02", ".z03", ".z04", ".z05", ".z06",
                                   ".z07", ".z08", ".z09", ".002", ".003")):
                    continue                                  # 分卷的后续部分不单独处理
                elif looks_like_archive(fp):
                    out.append(fp)
            if not self.recursive:
                break
        return out

    def _collect_split(self, path: str) -> tuple[bytes, str] | None:
        """name.zip.001/.002… 或 name.z01/z02… 先拼成整体。"""
        low = path.lower()
        if low.endswith(".001"):
            base = path[:-4]
            parts, i = [], 1
            while True:
                p = f"{base}.{i:03d}"
                if not os.path.exists(p):
                    break
                with open(p, "rb") as f:
                    parts.append(f.read())
                i += 1
            if parts:
                self.log(f"拼接分卷：{os.path.basename(path)} 共 {len(parts)} 个")
                return b"".join(parts), base
        if low.endswith(".z01"):
            base = path[:-4]
            parts, i = [], 1
            while True:
                p = f"{base}.z{i:02d}"
                if not os.path.exists(p):
                    break
                with open(p, "rb") as f:
                    parts.append(f.read())
                i += 1
            last = base + ".zip"                    # 最后一块是 .zip
            if os.path.exists(last):
                with open(last, "rb") as f:
                    parts.append(f.read())
            if parts:
                self.log(f"拼接分卷：{os.path.basename(path)} 共 {len(parts)} 个（含 .zip 尾块）")
                return b"".join(parts), base
        return None

    def _write(self, pieces: list[Piece], target: str):
        """把解出来的内容写盘。多个文件会并行写（目录先建好，避免并发抢建）。"""
        dirs = set()
        jobs = []
        for pc in pieces:
            rel = [p for p in pc.path.split("/") if p not in ("", ".", "..")]
            if not rel:
                continue
            fp = os.path.join(target, *rel)
            if pc.is_dir:
                dirs.add(fp)
                continue
            dirs.add(os.path.dirname(fp) or target)
            jobs.append((fp, pc.data or b""))
        for d in sorted(dirs):                     # 先一次性建好目录
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
        if len(jobs) <= 1 or sum(len(d) for _f, d in jobs) < 1024 * 1024:
            for fp, data in jobs:
                self._write_one(fp, data)
            return
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(16, (os.cpu_count() or 4), len(jobs)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda j: self._write_one(j[0], j[1]), jobs))

    def _write_one(self, fp: str, data: bytes) -> None:
        try:
            with open(fp, "wb") as f:
                f.write(data)
        except OSError as exc:  # noqa: BLE001
            self.log(f"  写不进去 {fp}：{exc}")
            return
        with self._lock:
            self._st["files"] += 1
            self._st["bytes"] += len(data)

    def run(self):
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", message=f"出错：{exc}")
            self.log(f"出错：{exc}")
        finally:
            self._set(running=False)
            if self._st.get("state") != "error":
                self._set(state="cancelled" if self.cancel_ev.is_set() else "done")

    def _run(self):
        if not os.path.isdir(self.src):
            self._set(state="error", message="输入文件夹不存在")
            return
        if os.path.normcase(self.dst).startswith(os.path.normcase(self.src) + os.sep):
            self._set(state="error", message="输出文件夹不能放在输入文件夹里面")
            return
        os.makedirs(self.dst, exist_ok=True)
        files = self._list()
        self._set(total=len(files), message="开始解压…")
        self.log(f"发现 {len(files)} 个压缩包")

        for idx, p in enumerate(files):
            if self.cancel_ev.is_set():
                return
            self._set(current=p, done=idx)
            base_name = _strip_archive_ext(os.path.basename(p))
            if base_name.lower().endswith(".tar"):
                base_name = base_name[:-4]
            target = os.path.join(self.dst, base_name) if self.split_dir else self.dst
            self.log(f"→ {os.path.basename(p)}")
            data = None
            expand_name = p
            whole = self._collect_split(p)
            if whole:
                data, _base = whole
                expand_name = _base          # 用「分卷包.zip」这个名字去解，否则认不出格式
                base_name = _strip_archive_ext(os.path.basename(_base))
                target = os.path.join(self.dst, base_name) if self.split_dir else self.dst
            else:
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                except OSError as exc:
                    self.log(f"  读不了：{exc}")
                    self._set(failed=self._st["failed"] + 1)
                    continue
            pieces: list[Piece] = []
            before = len(self._st.get("log") or [])
            expand(data, expand_name, self.passwords, self.brute, pieces, 0, "",
                   self.log, self.cancel_ev)
            with self._lock:
                self._st["archives"] += 1
            if not pieces:
                self.log("  没有产出（可能密码不对或格式不支持）")
                continue
            self._write(pieces, target)
            self.log(f"  写出 {len(pieces)} 项 → {target}")
            del pieces
        self._set(done=len(files), message="完成")
        self.log(f"完成：{self._st['archives']} 个包 / {self._st['files']} 个文件 / "
                 f"{self._st['bytes']/1e6:.1f}MB")


# ------------------------------------------------------------------ 独立破解
class BruteJob(threading.Thread):
    """只破解、不解压：扫压缩包 → 试密码本 → 穷举 → 记录结果。"""
    daemon = True

    def __init__(self, src: str, passwords: list[str], brute: dict | None,
                 recursive: bool = True):
        super().__init__(name="brute-job")
        self.src = os.path.abspath(src)
        self.passwords = passwords or []
        self.brute = brute or {}
        self.recursive = bool(recursive)
        self.cancel_ev = threading.Event()
        self.pause_ev = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._st = {"running": True, "state": "running", "total": 0, "done": 0,
                    "found": 0, "failed": 0, "current": "", "message": "准备中…",
                    "paused": False, "results": [], "log": [], "src": self.src,
                    "elapsed": 0}

    def log(self, msg: str):
        with self._lock:
            lst = self._st["log"]
            lst.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(lst) > 400:
                del lst[:100]

    def cancel(self):
        self.cancel_ev.set()
        self.pause_ev.clear()

    def pause(self):
        self.pause_ev.set()
        with self._lock:
            self._st["paused"] = True

    def resume(self):
        self.pause_ev.clear()
        with self._lock:
            self._st["paused"] = False

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._st)
            d["log"] = list(self._st["log"])
            d["results"] = list(self._st["results"])
        d["elapsed"] = round(time.time() - self._t0, 1)
        return d

    def _set(self, **kw):
        with self._lock:
            self._st.update(kw)

    def _list(self) -> list[str]:
        if os.path.isfile(self.src):
            return [self.src]
        out = []
        for dp, dirs, files in os.walk(self.src):
            dirs[:] = [d for d in dirs if not d.startswith("$")]
            for fn in files:
                fp = os.path.join(dp, fn)
                low = fn.lower()
                if low.endswith((".z01", ".z02", ".002", ".003")):
                    continue
                if looks_like_crackable(fp):
                    out.append(fp)
            if not self.recursive:
                break
        return out

    def run(self):
        try:
            files = self._list()
            self._set(total=len(files), message="开始破解…")
            self.log(f"发现 {len(files)} 个压缩包")
            for i, path in enumerate(files):
                if self.cancel_ev.is_set():
                    break
                self._set(current=path, done=i)
                self.log(f"→ {os.path.basename(path)}")
                try:
                    if path.lower().endswith(".001"):
                        base = path[:-4]
                        parts, k = [], 1
                        while os.path.exists(f"{base}.{k:03d}"):
                            with open(f"{base}.{k:03d}", "rb") as f:
                                parts.append(f.read())
                            k += 1
                        data = b"".join(parts)
                        name = base
                    else:
                        with open(path, "rb") as f:
                            data = f.read()
                        name = path
                    meta: dict = {}
                    pw, encrypted = find_password(data, self.passwords, self.brute,
                                                  self.log, name, self.cancel_ev, meta,
                                                  pause=self.pause_ev)
                    if not encrypted:
                        self.log("  没有加密，跳过")
                    elif pw:
                        with self._lock:
                            self._st["found"] += 1
                            self._st["results"].append({
                                "name": os.path.basename(path), "path": path,
                                "pwd": pw.decode("utf-8", "replace"), "ok": True,
                                "engine": meta.get("engine") or "",
                                "seconds": meta.get("seconds") or 0})
                        self.log(f"  破解成功：{pw.decode('utf-8', 'replace')}")
                    else:
                        with self._lock:
                            self._st["results"].append({
                                "name": os.path.basename(path), "path": path,
                                "pwd": "", "ok": False})
                        self.log("  没找到密码")
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._st["failed"] += 1
                    self.log(f"  出错：{exc}")
            self._set(done=self._st["total"], message="完成")
            self.log(f"完成：成功 {self._st['found']} 个，失败 {self._st['failed']} 个")
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", message=f"出错：{exc}")
            self.log(f"出错：{exc}")
        finally:
            self._set(running=False)
            if self._st.get("state") != "error":
                self._set(state="cancelled" if self.cancel_ev.is_set() else "done")
