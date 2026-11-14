"""把文件夹打成压缩包：只在内存里拼，最后一次性写盘。

支持：
* **格式**：zip（通用）；加密可选「不加密 / ZipCrypto（传统，兼容性最好）/ AES-256（更安全）」
* **多层压缩**：勾上后，子文件夹各自先打成一个 zip，再被上一层 zip 套进去（由深到浅）
* **拆分**：按大小切成 name.z01、name.z02 … name.zip（和 WinRAR/7-Zip 的分卷 zip 一致，
  本程序自己的解压页也能直接解）

中间产物都是 `io.BytesIO`，最后才落盘一次；拆分也在内存里切完再按块写。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import pyzipper
    HAS_AES = True
except Exception:  # noqa: BLE001
    HAS_AES = False

# 这些格式本身已经压缩过，再 deflate 一遍既慢又几乎不省空间 → 直接存储
STORE_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v",
    ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".flac",
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".lz4", ".cab",
    ".docx", ".xlsx", ".pptx", ".odt", ".epub", ".apk", ".jar", ".whl",
)

WORKERS = max(1, min(16, (os.cpu_count() or 4)))

ENC_NONE = "none"
ENC_ZIPCRYPTO = "zipcrypto"
ENC_AES = "aes"
ENC_LABELS = {ENC_NONE: "不加密", ENC_ZIPCRYPTO: "ZipCrypto（传统）", ENC_AES: "AES-256"}

FMT_ZIP = "zip"
FMT_7Z = "7z"
FMT_TAR = "tar"
FMT_TGZ = "tar.gz"
FMT_TBZ2 = "tar.bz2"
FMT_TXZ = "tar.xz"
FORMATS = {
    FMT_ZIP: "zip（最通用）",
    FMT_7Z: "7z（压缩率高，需 7-Zip）",
    FMT_TAR: "tar（不压缩，只打包）",
    FMT_TGZ: "tar.gz",
    FMT_TBZ2: "tar.bz2",
    FMT_TXZ: "tar.xz",
}
TAR_MODES = {FMT_TAR: "w", FMT_TGZ: "w:gz", FMT_TBZ2: "w:bz2", FMT_TXZ: "w:xz"}
LEVELS = {0: "不压缩（打包最快）", 1: "快（压缩率低）", 6: "标准", 9: "更小（压缩率高，慢）"}

SEVENZIP_CACHE = [None]


SEVENZIP_NAMES = ("7za.exe", "7z.exe", "7zr.exe")


def find_7z() -> str:
    """找 7-Zip 命令行：自带的 vendor\7z → 7z.txt → PATH → 常见安装位置。"""
    if SEVENZIP_CACHE[0] is not None:
        return SEVENZIP_CACHE[0]
    cands: "list[str]" = []
    root = os.path.join(BASE, "vendor", "7z")
    for name in SEVENZIP_NAMES:
        cands.append(os.path.join(root, name))
    cfg = os.path.join(BASE, "7z.txt")
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip().strip('"')
                    if line and not line.startswith("#"):
                        cands.append(line)
        except OSError:
            pass
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "7-Zip", "7z.exe"))
    w = (shutil.which("7z") or shutil.which("7za") or shutil.which("7zr") or "")
    if w:
        cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            SEVENZIP_CACHE[0] = os.path.abspath(c)
            return SEVENZIP_CACHE[0]
    SEVENZIP_CACHE[0] = ""
    return ""


def formats_available() -> dict:
    avail = {}
    for k, label in FORMATS.items():
        if k == FMT_7Z and not find_7z():
            avail[k] = {"label": label, "ok": False, "why": "没找到 7z.exe"}
        else:
            avail[k] = {"label": label, "ok": True, "why": ""}
    return avail

MAX_DEPTH = 6                 # 多层压缩最多往下套这么多层
MAX_FILE = 2 * 1024 * 1024 * 1024


# ---------------------------------------------------------------- 基础：ZipCrypto 写
def _crc_table() -> "list[int]":
    tab = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        tab.append(c)
    return tab


_TAB = _crc_table()


def _update_keys(keys: "list[int]", ch: int) -> None:
    keys[0] = ((keys[0] >> 8) ^ _TAB[(keys[0] ^ ch) & 0xFF]) & 0xFFFFFFFF
    keys[1] = ((keys[1] + (keys[0] & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
    keys[2] = ((keys[2] >> 8) ^ _TAB[(keys[2] ^ ((keys[1] >> 24) & 0xFF)) & 0xFF]) & 0xFFFFFFFF


def _crypt_byte(keys: "list[int]") -> int:
    t = (keys[2] | 2) & 0xFFFF
    return ((t * (t ^ 1)) >> 8) & 0xFF


def _zipcrypto(pwd: bytes, plain: bytes, crc: int) -> bytes:
    """12 字节加密头 + 加密后的数据。"""
    keys = [0x12345678, 0x23456789, 0x34567890]
    for b in pwd:
        _update_keys(keys, b)
    header = bytearray(os.urandom(10))
    header.append((crc >> 16) & 0xFF)
    header.append((crc >> 24) & 0xFF)
    out = bytearray()
    for ch in bytes(header) + plain:
        out.append(ch ^ _crypt_byte(keys))
        _update_keys(keys, ch)
    return bytes(out)


# ---------------------------------------------------------------- 打一个 zip（内存）
def _pack_one_entry(job) -> tuple:
    """压缩一个条目（在线程池里跑）：返回 (name, flags, method, crc, body, usize)。"""
    name, data, pwd, enc, level, store = job
    raw = data if isinstance(data, (bytes, bytearray)) else open(data, "rb").read()
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    if store or level <= 0 or not raw:
        method, body = 0, raw
    else:
        co = zlib.compressobj(level, zlib.DEFLATED, -15)
        method, body = 8, co.compress(raw) + co.flush()
    flags = 0
    if pwd and enc == ENC_ZIPCRYPTO:
        body = _zipcrypto(pwd, body, crc)
        flags |= 0x0001
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        flags |= 0x0800
    return (name, flags, method, crc, body, len(raw))


def zip_bytes(entries: "list[dict]", *, password: str = "", enc: str = ENC_NONE,
              level: int = 6) -> bytes:
    """entries = [{name, data(bytes) | src(路径)}]，全部在内存里拼成 zip。

    * 多个条目会**多线程并行压缩**（zlib 压缩时释放 GIL）
    * 图片/视频/压缩包这类已经压过的内容直接存储，省 CPU 也基本不损失压缩率
    """
    pwd = (password or "").encode("utf-8")
    if enc == ENC_AES and pwd:
        if not HAS_AES:
            raise RuntimeError("AES 加密需要 pyzipper（runtime 里应该自带）")
        buf = io.BytesIO()
        with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES,
                                 compresslevel=level) as zf:
            zf.setpassword(pwd)
            for e in entries:
                data = e["data"] if "data" in e else open(e["src"], "rb").read()
                zf.writestr(e["name"], data)
        return buf.getvalue()

    jobs = []
    for e in entries:
        name = e["name"].replace("\\", "/")
        data = e["data"] if "data" in e else e["src"]
        size = len(data) if isinstance(data, (bytes, bytearray)) else os.path.getsize(data)
        store = name.lower().endswith(STORE_EXT) or size < 64
        jobs.append((name, data, pwd, enc, level, store))

    if len(jobs) <= 1:
        results = [_pack_one_entry(j) for j in jobs]
    else:
        workers = min(WORKERS, len(jobs))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_pack_one_entry, jobs))

    out = io.BytesIO()
    central = []
    dostime, dosdate = 0x6000, 0x5821          # 2024-01-01 12:00:00
    for (name, flags, method, crc, body, usize) in results:
        raw_name = name.encode("utf-8")
        offset = out.tell()
        out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method,
                              dostime, dosdate, crc, len(body), usize,
                              len(raw_name), 0))
        out.write(raw_name)
        out.write(body)
        central.append((raw_name, flags, method, crc, len(body), usize, offset))
    cd_start = out.tell()
    for (raw_name, flags, method, crc, csize, usize, offset) in central:
        out.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, method,
                              dostime, dosdate, crc, csize, usize, len(raw_name), 0, 0, 0, 0,
                              0, offset))
        out.write(raw_name)
    cd_size = out.tell() - cd_start
    out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central),
                          cd_size, cd_start, 0))
    return out.getvalue()


# ---------------------------------------------------------------- 拆包
def _read_entries(entries: "list[dict]") -> "list[tuple[str, bytes]]":
    out = []
    for e in entries:
        data = e["data"] if "data" in e else open(e["src"], "rb").read()
        out.append((e["name"].replace("\\", "/"), data))
    return out


def tar_bytes(entries: "list[dict]", *, fmt: str = FMT_TGZ) -> bytes:
    """tar / tar.gz / tar.bz2 / tar.xz —— 纯 Python，内存里拼。"""
    import tarfile
    buf = io.BytesIO()
    mode = TAR_MODES.get(fmt, "w:gz")
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in _read_entries(entries):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def seven_bytes(entries: "list[dict]", *, password: str = "", level: int = 6,
                hide_names: bool = True) -> bytes:
    """7z 格式：7z.exe 只能写文件，所以走临时文件，读完立刻删。"""
    exe = find_7z()
    if not exe:
        raise RuntimeError("没找到 7z.exe，7z 格式用不了")
    tmpdir = tempfile.mkdtemp(prefix="pd_pack_")
    try:
        stage = os.path.join(tmpdir, "in")
        os.makedirs(stage, exist_ok=True)
        for name, data in _read_entries(entries):
            fp = os.path.join(stage, *[x for x in name.split("/") if x and x != ".."])
            os.makedirs(os.path.dirname(fp) or stage, exist_ok=True)
            with open(fp, "wb") as f:
                f.write(data)
        out = os.path.join(tmpdir, "out.7z")
        args = [exe, "a", "-t7z", out, os.path.join(stage, "*"), "-y",
                f"-mx={max(0, min(9, level))}"]
        if password:
            args.append(f"-p{password}")
            if hide_names:
                args.append("-mhe=on")
        p = subprocess.run(args, capture_output=True, timeout=3600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0 or not os.path.exists(out):
            msg = (p.stderr or p.stdout or b"").decode("gbk", "replace")[:200]
            raise RuntimeError(f"7z 打包失败：{msg}")
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def archive_bytes(entries: "list[dict]", *, fmt: str = FMT_ZIP, enc: str = ENC_NONE,
                  password: str = "", level: int = 6) -> bytes:
    """按格式打成一个包的字节。"""
    if fmt == FMT_7Z:
        return seven_bytes(entries, password=password, level=level)
    if fmt in TAR_MODES:
        return tar_bytes(entries, fmt=fmt)
    return zip_bytes(entries, password=password, enc=enc, level=level)


def archive_suffix(fmt: str) -> str:
    return "." + (fmt or "zip")


def split_parts(total: int, part: int = 0, count: int = 0, *,
                min_size: int = 0, max_size: int = 0,
                min_parts: int = 0, max_parts: int = 0) -> "list[int]":
    """算出每卷的字节数。

    两条路子可以组合：
    * **按数量**：`count` 指定份数；`min_size` 表示"包小于这个就不拆"；
      `max_size` 表示"每卷超过这个就额外多切一份"（相当于每卷上限）
    * **按体积**：`part` 指定每卷字节数；`min_parts`/`max_parts` 把份数夹在区间里

    另外始终有个 1KB 下限：太小的包切了没法用。
    """
    if total <= 0:
        return [0]
    if total < 1024:
        return [total]
    if min_size and total < min_size:
        return [total]
    n = 0
    if count and count >= 2:
        n = int(count)
    if part and part > 0:
        want = -(-total // part)                 # ceil
        n = max(n, want) if n else want
    if max_size and max_size > 0 and total > max_size:
        n = max(n, -(-total // max_size))
    if min_parts and min_parts >= 2:
        n = max(n, int(min_parts))
    if max_parts and max_parts >= 2:
        n = min(n, int(max_parts)) if n else int(max_parts)
    if n <= 1:
        return [total]
    n = min(n, total)
    base = total // n
    rem = total % n
    return [base + (1 if i < rem else 0) for i in range(n)]


def write_split(path_without_ext: str, data: bytes, part: int = 0,
                log=None, suffix: str = ".zip", count: int = 0,
                style: str = "auto", min_size: int = 0, max_size: int = 0,
                min_parts: int = 0, max_parts: int = 0) -> "list[str]":
    """把 data 写成多卷（不拆就写 name<suffix>）。返回写出的文件列表。

    style：
    * "zip"  → name.z01、name.z02 … name<suffix>（WinRAR/7-Zip 的分卷 zip）
    * "001"  → name<suffix>.001、.002 …（7-Zip 的 7z/tar 分卷习惯）
    * "auto" → zip 用前一种，其它格式用后一种
    """
    parent = os.path.dirname(path_without_ext)
    if parent:
        os.makedirs(parent, exist_ok=True)
    sizes = split_parts(len(data), part, count, min_size=min_size,
                        max_size=max_size, min_parts=min_parts,
                        max_parts=max_parts)
    written = []
    off = 0
    suffix = suffix or ".zip"
    if style == "auto":
        style = "zip" if suffix == ".zip" else "001"
    if len(sizes) == 1:
        p = path_without_ext + suffix
        with open(p, "wb") as f:
            f.write(data)
        written.append(p)
    else:
        total = len(sizes)
        for i, n in enumerate(sizes, 1):
            if style == "001":
                p = f"{path_without_ext}{suffix}.{i:03d}"
            else:
                p = path_without_ext + (f".z{i:02d}" if i < total else suffix)
            with open(p, "wb") as f:
                f.write(data[off:off + n])
            written.append(p)
            off += n
    # 清掉同一名字下"过期的"分卷/整包：比如上次是 z01+zip，这次合并成一个 zip，
    # 旧的 z01 留着会让下次解压拼错包
    _cleanup_stale(path_without_ext, suffix, set(written), log)
    if log:
        log(f"    写出 {len(written)} 个文件，共 {len(data) / 1048576:.1f} MB")
    return written


def _cleanup_stale(base: str, suffix: str, keep: "set[str]", log=None) -> None:
    """删掉同名但这次没写的旧分卷/旧整包。"""
    parent = os.path.dirname(base) or "."
    name = os.path.basename(base)
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    stale = []
    for fn in entries:
        low = fn.lower()
        if not low.startswith(name.lower()):
            continue
        tail = fn[len(name):]
        full = os.path.join(parent, fn)
        if full in keep:
            continue
        if re.fullmatch(r"\.z\d\d", tail, re.I):                       # name.z01
            stale.append(full)
        elif suffix != ".zip" and re.fullmatch(
                re.escape(suffix) + r"\.\d{3}", tail, re.I):           # name.7z.001
            stale.append(full)
        elif tail.lower() == suffix.lower():                            # name.zip 本体
            stale.append(full)
    for fp in stale:
        try:
            os.remove(fp)
            if log:
                log(f"    清掉旧分卷 {os.path.basename(fp)}")
        except OSError:
            pass


# ---------------------------------------------------------------- 打一个文件夹
def _collect(dir_path: str, base: str, *, multi: bool, depth: int,
             enc: str, password: str, level: int, log=None,
             cancel=None) -> "list[dict]":
    """把 dir_path 下的内容变成 zip 的 entries；multi=True 时子文件夹先打成 zip。"""
    entries: "list[dict]" = []
    try:
        names = sorted(os.listdir(dir_path))
    except OSError:
        return entries
    for name in names:
        if cancel is not None and cancel.is_set():
            return entries
        full = os.path.join(dir_path, name)
        rel = f"{base}{name}" if not base else f"{base}/{name}"
        if os.path.isdir(full):
            if multi and depth < MAX_DEPTH:
                sub = _collect(full, "", multi=multi, depth=depth + 1,
                               enc=enc, password=password, level=level,
                               log=log, cancel=cancel)
                blob = zip_bytes(sub, password=password, enc=enc, level=level)
                entries.append({"name": rel + ".zip", "data": blob})
                if log:
                    log(f"      子树 {rel} → {rel}.zip（{len(blob) / 1024:.0f} KB，"
                        f"{len(sub)} 项）")
            else:
                entries.extend(_collect(full, rel, multi=multi, depth=depth + 1,
                                        enc=enc, password=password, level=level,
                                        log=log, cancel=cancel))
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > MAX_FILE:
            if log:
                log(f"      跳过超大文件 {rel}（{size / 1073741824:.1f} GB）")
            continue
        entries.append({"name": rel, "src": full})
    return entries


def pack_folder(folder: str, out_path_without_ext: str, *, enc: str = ENC_NONE,
                password: str = "", split_mb: float = 0, multi: bool = False,
                level: int = 6, log=None, cancel=None) -> dict:
    """把一个文件夹打成包（可选加密/多层/拆分），返回统计。"""
    t0 = time.time()
    entries = _collect(folder, "", multi=multi, depth=0, enc=enc, password=password,
                       level=level, log=log, cancel=cancel)
    if cancel is not None and cancel.is_set():
        return {"ok": False, "error": "已取消", "items": 0, "bytes": 0}
    if not entries:
        return {"ok": False, "error": "空文件夹", "items": 0, "bytes": 0}
    data = zip_bytes(entries, password=password, enc=enc, level=level)
    part = int(split_mb * 1024 * 1024) if split_mb and split_mb > 0 else 0
    files = write_split(out_path_without_ext, data, part, log=log)
    return {"ok": True, "items": len(entries), "bytes": len(data),
            "files": files, "seconds": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- 作业
class PackJob(threading.Thread):
    """把 src 下**第一层子文件夹**各打成一个包（文件按需也能直接打成一个总包）。"""

    daemon = True

    def __init__(self, src: str, dst: str, *, enc: str = ENC_NONE,
                 password: str = "", split_mb: float = 0,
                 multi: bool = False, level: int = 6, each_file: bool = False):
        super().__init__(name="pack-job")
        self.src = os.path.abspath(src)
        self.dst = os.path.abspath(dst)
        self.enc = enc if enc in ENC_LABELS else ENC_NONE
        self.password = password or ""
        self.split_mb = float(split_mb or 0)
        self.multi = bool(multi)
        self.level = max(0, min(9, int(level)))
        self.each_file = bool(each_file)
        self.cancel_ev = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._st = {"running": True, "state": "running", "total": 0, "done": 0,
                    "archives": 0, "bytes": 0, "parts": 0, "failed": 0,
                    "current": "", "message": "准备中…", "log": [], "paused": False,
                    "src": self.src, "dst": self.dst}

    # ---- 状态
    def log(self, msg: str) -> None:
        with self._lock:
            lst = self._st["log"]
            lst.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(lst) > 400:
                del lst[:120]

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
    def _targets(self) -> "list[tuple[str, str]]":
        """[(源路径, 输出名去扩展名)]"""
        if os.path.isfile(self.src):
            return [(self.src, os.path.splitext(os.path.basename(self.src))[0])]
        out: "list[tuple[str, str]]" = []
        try:
            names = sorted(os.listdir(self.src))
        except OSError:
            return out
        for name in names:
            full = os.path.join(self.src, name)
            if os.path.isdir(full):
                out.append((full, name))
            elif self.each_file:
                out.append((full, os.path.splitext(name)[0]))
        return out

    def _pack_one(self, path: str, name: str) -> None:
        target = os.path.join(self.dst, name)
        if os.path.isdir(path):
            r = pack_folder(path, target, enc=self.enc, password=self.password,
                            split_mb=self.split_mb, multi=self.multi,
                            level=self.level, log=self.log, cancel=self.cancel_ev)
        else:
            data = open(path, "rb").read()
            entries = [{"name": os.path.basename(path), "data": data}]
            blob = zip_bytes(entries, password=self.password, enc=self.enc,
                             level=self.level)
            part = int(self.split_mb * 1024 * 1024) if self.split_mb > 0 else 0
            r = {"ok": True, "items": 1, "bytes": len(blob),
                 "files": write_split(target, blob, part, log=self.log)}
        if not r.get("ok"):
            with self._lock:
                self._st["failed"] += 1
            self.log(f"  {name}：{r.get('error')}")
            return
        with self._lock:
            self._st["archives"] += 1
            self._st["bytes"] += int(r.get("bytes") or 0)
            self._st["parts"] += len(r.get("files") or [])
        self.log(f"  {name} → {len(r.get('files') or [])} 个文件，"
                 f"{r.get('bytes', 0) / 1048576:.1f} MB，{r.get('seconds', 0):.1f}s")

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
        if not os.path.exists(self.src):
            self._set(state="error", message=f"输入路径不存在：{self.src}")
            return
        try:
            os.makedirs(self.dst, exist_ok=True)
        except OSError as exc:
            self._set(state="error", message=f"建不了输出目录：{exc}")
            return
        targets = self._targets()
        self._set(total=len(targets))
        self.log(f"共 {len(targets)} 个待打包对象 → 输出到 {self.dst}")
        self.log(f"加密：{ENC_LABELS[self.enc]}" +
                 ("（有密码）" if self.password else "")
                 + f"；多层压缩：{'开' if self.multi else '关'}"
                 + (f"；拆分：{self.split_mb:g} MB" if self.split_mb else "；不拆分"))
        if not targets:
            self._set(message="没有子文件夹可打包")
            return
        for idx, (path, name) in enumerate(targets):
            if self.cancel_ev.is_set():
                self.log("已取消")
                return
            self._set(current=name, done=idx)
            self.log(f"→ {name}")
            self._pack_one(path, name)
        self._set(done=len(targets))
        self.log(f"完成：{self._st['archives']} 个包、{self._st['parts']} 个文件、"
                 f"{self._st['bytes'] / 1048576:.1f} MB，失败 {self._st['failed']} 个")
