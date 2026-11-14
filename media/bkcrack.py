"""bkcrack 接入：ZipCrypto 的"已知明文攻击"（Biham-Kocher）。

原理：传统 zip 加密（ZipCrypto）是流密码，只要知道密文对应的 **12 字节以上明文**，
就能反推出内部密钥（key0/key1/key2），然后整包解密——不需要知道原密码，
比暴力快几个数量级。这是 ARCHPR 那类商业工具"保证一小时内恢复"的核心。

* 二进制随包携带（vendor\\bkcrack，zlib 许可，见 vendor\\bkcrack\\license.txt）
* 也可以用 bkcrack.txt 指向别处的 bkcrack.exe
* 目标条目是 deflate 压缩时，用户给的"明文"要当成**压缩后**的字节用，
  这里会先用 zlib 以几种常见级别压一遍再试（store 的条目直接用原文）
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import zipfile
import zlib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(BASE, "bkcrack.txt")
MIN_PLAIN = 12                      # bkcrack 至少要 12 字节已知明文
MAX_PLAIN = 1024                    # 给多了反而慢，截断到 1KB

_CACHE: dict | None = None
_LOCK = threading.Lock()
_KEYS_RE = re.compile(r"Keys:\s*\n?\s*([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})")
_KEYS_RE2 = re.compile(r"Keys:\s*([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})")
_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?) %")
_PASS_RE = re.compile(r"Password:\s*(\S+)")


def find_exe() -> str:
    """按 bkcrack.txt → vendor\\bkcrack\\**\\bkcrack.exe 的顺序找。"""
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8-sig") as f:
                for line in f:
                    p = line.strip().strip('"')
                    if p and not p.startswith("#") and os.path.isfile(p):
                        return os.path.abspath(p)
        except OSError:
            pass
    root = os.path.join(BASE, "vendor", "bkcrack")
    if os.path.isdir(root):
        for dirpath, _dirs, files in os.walk(root):
            if "bkcrack.exe" in files:
                return os.path.join(dirpath, "bkcrack.exe")
    return ""


def status(refresh: bool = False) -> dict:
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not refresh:
            return _CACHE
        exe = find_exe()
        info = {"available": False, "exe": exe, "version": "", "error": ""}
        if not exe:
            info["error"] = "没有找到 bkcrack.exe（把 bkcrack.txt 指向它，或放到 vendor\\bkcrack）"
            _CACHE = info
            return info
        rc, out, err = _run(exe, ["--version"], timeout=30)
        text = (out or "") + (err or "")
        m = re.search(r"bkcrack\s+v?([\d.]+)", text)
        if m:
            info["version"] = m.group(1)
        info["available"] = rc == 0 or bool(info["version"])
        if not info["available"]:
            info["error"] = (text.strip().splitlines() or ["bkcrack 起不来"])[-1][:200]
        _CACHE = info
        return info


def _run(exe: str, args: list[str], timeout: int = 600,
         cancel=None, on_line=None) -> tuple[int, str, str]:
    """跑 bkcrack；支持取消和逐行回调（进度）。"""
    cwd = os.path.dirname(exe)
    try:
        p = subprocess.Popen([exe] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)
    lines: list[str] = []
    import time
    t0 = time.time()
    while True:
        if cancel is not None and cancel.is_set():
            try:
                p.kill()
            except OSError:
                pass
            break
        if time.time() - t0 > timeout:
            try:
                p.kill()
            except OSError:
                pass
            break
        ln = p.stdout.readline()          # type: ignore[union-attr]
        if not ln:
            break
        lines.append(ln.rstrip("\r\n"))
        if on_line is not None:
            try:
                on_line(ln.rstrip("\r\n"))
            except Exception:  # noqa: BLE001
                pass
    try:
        rc = p.wait(timeout=10)
    except Exception:  # noqa: BLE001
        rc = -1
    return rc, "\n".join(lines), ""


def zipcrypto_entries(data: bytes) -> "list[dict]":
    """返回可做明文攻击的条目（传统加密的 store/deflate）。"""
    out: list[dict] = []
    try:
        zf = zipfile.ZipFile(__import__("io").BytesIO(data))
        infos = zf.infolist()
        zf.close()
    except Exception:  # noqa: BLE001
        return out
    for info in infos:
        if not (info.flag_bits & 0x1):
            continue
        if info.compress_type not in (0, 8):
            continue
        if info.file_size <= 0:
            continue
        out.append({"name": info.filename, "method": info.compress_type,
                    "size": int(info.file_size), "csize": int(info.compress_size)})
    return out


def _plain_variants(plain: bytes, method: int) -> "list[tuple[str, bytes]]":
    """把用户给的明文变成"密文对应的明文"：store 用原文，deflate 先压缩。"""
    plain = plain[:MAX_PLAIN]
    out = [("原文", plain)]
    if method == 8:
        for lvl in (9, 6, 1):
            try:
                co = zlib.compressobj(lvl, zlib.DEFLATED, -15)
                comp = (co.compress(plain) + co.flush())[:MAX_PLAIN]
            except zlib.error:
                continue
            if comp and all(comp != b for _n, b in out):
                out.append((f"zlib-{lvl}", comp))
    return out


def attack(zip_path: str, entry: str, plain: bytes, offset: int = 0,
           timeout: int = 1800, log=None, cancel=None) -> dict:
    """对单个条目做已知明文攻击。返回 {ok, keys, variant, seconds} 或 {ok: False, error}。"""
    st = status()
    if not st["available"]:
        return {"ok": False, "error": st["error"] or "bkcrack 不可用"}
    if len(plain) < MIN_PLAIN:
        return {"ok": False, "error": f"已知明文只有 {len(plain)} 字节，至少要 {MIN_PLAIN} 字节"}
    data = b""
    try:
        with open(zip_path, "rb") as f:
            data = f.read(8 * 1024 * 1024)
    except OSError as exc:
        return {"ok": False, "error": f"读不了压缩包：{exc}"}
    meth = 0
    for e in zipcrypto_entries(data):
        if e["name"] == entry:
            meth = e["method"]
            break
    import time
    for label, blob in _plain_variants(plain, meth):
        tmp = tempfile.mkdtemp(prefix="bkplain_")
        try:
            pf = os.path.join(tmp, "plain.bin")
            with open(pf, "wb") as f:
                f.write(blob)
            args = ["-C", zip_path, "-c", entry, "-p", pf]
            if offset:
                args += ["-o", str(int(offset))]
            t0 = time.time()
            last = {"pct": -1}

            def on_line(ln: str) -> None:
                m = _PROGRESS_RE.search(ln)
                if m and log:
                    pct = float(m.group(1))
                    if pct - last["pct"] >= 10:
                        last["pct"] = pct
                        log(f"  明文攻击 {pct:.0f}%（{label}）")

            rc, out, err = _run(st["exe"], args, timeout=timeout, cancel=cancel,
                                on_line=on_line)
            m = _KEYS_RE.search(out) or _KEYS_RE2.search(out)
            if m:
                keys = [m.group(1).lower(), m.group(2).lower(), m.group(3).lower()]
                if log:
                    log(f"  找到密钥：{' '.join(keys)}（{label}，{time.time() - t0:.1f}s）")
                return {"ok": True, "keys": keys, "variant": label,
                        "seconds": round(time.time() - t0, 1)}
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": False, "error": "这个条目没试出来（明文不匹配，或明文太短/偏移不对）"}


def decrypt(zip_path: str, keys: "list[str]", out_path: str, timeout: int = 600) -> dict:
    """用密钥生成一个去掉密码的新 zip（所有条目）。"""
    st = status()
    if not st["available"]:
        return {"ok": False, "error": st["error"] or "bkcrack 不可用"}
    args = ["-C", zip_path, "-k"] + list(keys) + ["-D", out_path]
    rc, out, err = _run(st["exe"], args, timeout=timeout)
    ok = rc == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    return {"ok": ok, "rc": rc,
            "error": "" if ok else ((err or out).strip().splitlines() or ["解密失败"])[-1][:200]}


def recover_password(keys: "list[str]", max_len: int = 8, charset: str = "?d",
                     timeout: int = 120) -> str:
    """用密钥反推原密码（n^(l-6) 复杂度，短密码很快）。"""
    st = status()
    if not st["available"]:
        return ""
    args = ["-k"] + list(keys) + ["-r", str(int(max_len)), charset]
    rc, out, err = _run(st["exe"], args, timeout=timeout)
    m = _PASS_RE.search(out or "")
    return m.group(1) if m else ""
