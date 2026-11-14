"""hashcat 接入：便携路径解析 + GPU 设备探测 + WinZip AES 哈希提取 + 破解调用。

原则：
* 路径全部相对程序目录（可整个文件夹搬走），也可用 hashcat.txt 指定外部 hashcat
* 目标机器没有 OpenCL/显卡时**不报错**，返回 available=False，调用方退回 CPU 穷举
* hashcat 必须在自己的目录下运行（它按相对路径找 OpenCL/ 和 modules/），这里统一设置 cwd
* WinZip AES（压缩法 99）用 `-m 13600`：`$zip2$*type*mode*magic*salt*pv*clen*data*auth*$/zip2$`
  —— hashcat 在 `comp` 阶段会拿**整段密文**算 HMAC 跟 auth 比对（module_13600.c 里
  `sha1_hmac_update_global_swap(data_buf, data_len)`），所以 data 必须放完整密文，
  只放 salt/pv/auth、data 留空的话即使密码对也会判定不通过（上一版就卡在这里）。
  单条目密文上限 4MB（hex 后 8MB），超了这条路径就退回 CPU。
* ZipCrypto（传统 PKWARE 加密）用 `-m 17225`：`$pkzip2$1*cs*2*0*clen*ulen*crc*off*aoff*comp*dlen*pv*pv*data*$/pkzip2$`
  —— data_type=2 才会用 CRC 全量校验；`data_type=1`（只校验 12 字节头）在单文件时
  hashcat 走 `continue`、根本不会报命中（17225 kernel 第 680 行的注释写了），所以必须放整段密文。
  hashcat 的 MAX_DATA 是 320KB，这里取 300KB；压缩法只支持 stored(0)/deflate(8)。
* 用的是 vendor 里的 hashcat 8.0.0：6.2.6 的 pkzip kernel 在长任务里会 `CL_UNKNOWN_ERROR`
  （掩码键空间 >1M 必崩、管道 3000 万候选崩），8.0.0 重写了 PKZIP kernel（官方 13.8x 提速）已不再复现；
  `_stream_chunks()` 的管道分块实现保留下来，作为万一再遇到显卡层报错时的兜底。
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import struct
import subprocess
import threading
import time
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(BASE, "hashcat.txt")
WORK = os.path.join(BASE, "data", "hc")
CACHE = os.path.join(WORK, "cracked.json")

SALT_LEN = {1: 8, 2: 12, 3: 16}          # WinZip AES 强度 1/2/3 = 128/192/256 位
AES_BITS = {1: 128, 2: 192, 3: 256}
MAX_CT = 4 * 1024 * 1024                 # AES 单条目密文上限（hex 后 8MB，hashcat 一行）
MAX_PK_DATA = 300 * 1024                 # ZipCrypto 单条目上限（hashcat 17225 的 MAX_DATA 是 320KB）
MODE_AES = 13600                         # WinZip AES
MODE_PKZIP = 17225
MODE_7Z = 11600                       # PKZIP 传统加密（ZipCrypto）

_CACHE: dict | None = None            # status() 的探测结果缓存
_PW_CACHE: dict | None = None         # 已破解密码缓存（强度+salt+auth → 密码）
LAST_SPEED: dict = {}                    # mode → 最近一次实测有效速度（候选/秒）
DEFAULT_SPEED = {MODE_AES: 12e6, MODE_PKZIP: 40e6, MODE_7Z: 1.5e6}   # 本机实测口径
_LOCK = threading.Lock()

_DEV_RE = re.compile(r"Name\.+:\s*(.+)")
_TYPE_RE = re.compile(r"Type\.+:\s*(\w+)")
_ID_RE = re.compile(r"(\d+)")
_DISCRETE_RE = re.compile(r"NVIDIA|GeForce|RTX|GTX|Quadro|Tesla|Radeon|AMD|Arc", re.I)


# ------------------------------------------------------------------ 探测
def find_exe() -> str:
    """按 hashcat.txt → vendor\\hashcat\\**\\hashcat.exe 的顺序找。"""
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8-sig") as f:
                for line in f:
                    p = line.strip().strip('"')
                    if p and not p.startswith("#") and os.path.isfile(p):
                        return os.path.abspath(p)
        except OSError:
            pass
    root = os.path.join(BASE, "vendor", "hashcat")
    if os.path.isdir(root):
        for dirpath, _dirs, files in os.walk(root):
            if "hashcat.exe" in files:
                return os.path.join(dirpath, "hashcat.exe")
    return ""


def _run(exe: str, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """在 hashcat 自己目录里运行（否则找不到 OpenCL/modules）。"""
    cwd = os.path.dirname(exe)
    try:
        p = subprocess.run([exe] + args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -9, "", "超时"
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def status(refresh: bool = False) -> dict:
    """探测 hashcat 与 GPU。结果缓存（同一次运行只探一次）。"""
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not refresh:
            return _CACHE
        exe = find_exe()
        info = {"available": False, "exe": exe, "devices": [], "device_list": [],
                "error": "", "version": ""}
        if not exe:
            info["error"] = "没有找到 hashcat（可把 hashcat.txt 指向 hashcat.exe）"
            _CACHE = info
            return info
        rc, out, err = _run(exe, ["-I"], timeout=90)
        text = (out or "") + (err or "")
        m = re.search(r"hashcat \(v([\d.]+)\)", text)
        if m:
            info["version"] = m.group(1)
        # 只认 "Backend Device ID" 之后的 Name，避免把 OpenCL 平台名当成显卡
        for block in text.split("Backend Device ID #")[1:]:
            mid = _ID_RE.match(block)
            m = _DEV_RE.search(block)
            t = _TYPE_RE.search(block)
            if m:
                did = int(mid.group(1)) if mid else len(info["devices"]) + 1
                info["devices"].append(m.group(1).strip())
                info["device_list"].append({
                    "id": did, "name": m.group(1).strip(),
                    "type": (t.group(1).strip() if t else "")})
        info["available"] = bool(info["devices"])
        if not info["available"]:
            if "No devices found" in text or "CL_PLATFORM_NOT_FOUND" in text:
                info["error"] = "这台机器没有可用的 OpenCL 设备（装显卡驱动即可）"
            elif rc != 0:
                tail = (err or out or "").strip().splitlines()
                info["error"] = (tail[-1] if tail else f"hashcat 退出码 {rc}")
            else:
                info["error"] = "hashcat 没有检测到设备"
        _CACHE = info
        return info


def speed_hint(mode: int = 0) -> float:
    """给界面预估用的速度（候选/秒）：有实测用实测，没有用保守默认值；多卡按线性放大。"""
    if mode:
        base = LAST_SPEED.get(mode) or DEFAULT_SPEED.get(mode, 10e6)
    else:
        base = (max(LAST_SPEED.get(m, 0) for m in (MODE_AES, MODE_PKZIP, MODE_7Z))
                if LAST_SPEED else 0) or DEFAULT_SPEED[MODE_AES]
    return float(base) * gpu_count()


def exe_ok() -> bool:
    return bool(find_exe())


def device_label() -> str:
    st = status()
    if not st["available"]:
        return "CPU（hashcat 不可用）"
    return "GPU " + (st["devices"][0] if st["devices"] else "hashcat")


def gpu_ids() -> "list[int]":
    """要用哪些显卡：所有独显；一块都没有就交给 hashcat 自己挑。

    核显（Intel/AMD APU）不参与——多一块只是多花几秒初始化和调优，速度还不涨。
    """
    devs = status().get("device_list") or []
    if not devs:
        return []
    discrete = [int(d.get("id") or 0) for d in devs
                if _DISCRETE_RE.search(d.get("name") or "")]
    if discrete:
        return sorted(set(discrete))
    if len(devs) == 1:
        return [int(devs[0].get("id") or 0)]
    return []


def gpu_count() -> int:
    return max(1, len(gpu_ids()))


def device_args() -> "list[str]":
    """hashcat 的 -d 参数：多卡时写成 -d 1,2。"""
    ids = gpu_ids()
    if not ids:
        return []
    return ["-d", ",".join(str(i) for i in ids)]


def _preferred_device() -> int:
    """兼容旧调用：返回第一块要用的显卡。"""
    ids = gpu_ids()
    return ids[0] if ids else 0


# ------------------------------------------------------------------ AES zip 解析
def _aes_strength(extra: bytes) -> int:
    """extra 字段里找 0x9901（WinZip AES），返回强度 1/2/3。"""
    i = 0
    while i + 4 <= len(extra):
        try:
            hid, size = struct.unpack("<HH", extra[i:i + 4])
        except struct.error:
            return 0
        if hid == 0x9901 and size >= 7 and i + 4 + size <= len(extra):
            return extra[i + 8]            # version(2) vendor(2) strength(1) method(2)
        i += 4 + size
    return 0


def aes_entries(data: bytes) -> "list[dict]":
    """解析 zip 中央目录，返回 WinZip AES 条目的 salt / pv / 密文 / HMAC。"""
    out: list[dict] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
        zf.close()
    except Exception:  # noqa: BLE001
        return out
    for info in infos:
        if info.compress_type != 99:            # 99 = WinZip AES
            continue
        off = info.header_offset
        if off < 0 or off + 30 > len(data):
            continue
        try:
            sig, _ver, _flag, _meth, _t, _d, _crc, cs, _us, fnl, extl = struct.unpack(
                "<IHHHHHIIIHH", data[off:off + 30])
        except struct.error:
            continue
        if sig != 0x04034B50:
            continue
        base = off + 30 + fnl + extl
        local_extra = data[off + 30 + fnl:base]
        strength = _aes_strength(local_extra) or _aes_strength(info.extra)
        if strength not in SALT_LEN:
            continue
        csize = int(cs or info.compress_size or 0)
        blob = data[base:base + csize]
        sl = SALT_LEN[strength]
        if len(blob) <= sl + 12:
            continue
        out.append({"name": info.filename, "strength": strength,
                    "salt": blob[:sl], "pv": blob[sl:sl + 2],
                    "ct": blob[sl + 2:len(blob) - 10], "auth": blob[-10:],
                    "enc_size": len(blob)})
    return out


def zip2_hash(data: bytes, max_ct: int = MAX_CT) -> "dict | None":
    """挑密文最小的 AES 条目拼成 hashcat 的 `$zip2$` 串。

    hashcat 会校验 pv + 整段密文的 HMAC，所以 data 必须是完整密文；
    密文越小越省内存、越少 IO，密码对整包通用。
    """
    ents = [e for e in aes_entries(data) if 0 < len(e["ct"]) <= max_ct]
    if not ents:
        return None
    e = min(ents, key=lambda x: len(x["ct"]))
    ct = e["ct"]
    clen = min(len(ct), 0xFFFFFF)               # 字段只允许 6 位十六进制，13600 不读它
    h = (f"$zip2$*0*{e['strength']}*0*{e['salt'].hex()}*{e['pv'].hex()}*{clen:x}*"
         f"{ct.hex()}*{e['auth'].hex()}*$/zip2$")
    return {"hash": h, "strength": e["strength"], "ct": len(ct), "name": e["name"],
            "key": f"{e['strength']}:{e['salt'].hex()}:{e['auth'].hex()}"}


def aes_zip_info(data: bytes) -> dict:
    """给调用方判断能不能走 GPU：{aes, entries, ct, too_big}。"""
    ents = aes_entries(data)
    if not ents:
        return {"aes": False, "entries": 0, "ct": 0, "too_big": False}
    small = [e for e in ents if 0 < len(e["ct"]) <= MAX_CT]
    return {"aes": True, "entries": len(ents),
            "ct": min((len(e["ct"]) for e in ents), default=0),
            "too_big": not small}


# ------------------------------------------------------------------ ZipCrypto
def pkzip_entries(data: bytes) -> "list[dict]":
    """解析 zip 中央目录，返回传统加密（ZipCrypto，flag bit0）的条目。

    hashcat 的 17225 只认 stored(0) / deflate(8) 两种压缩法；
    另外 12 字节加密头 + 整段密文都要塞进哈希行（MAX_PK_DATA）。
    """
    out: list[dict] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
        zf.close()
    except Exception:  # noqa: BLE001
        return out
    for info in infos:
        if not (info.flag_bits & 0x1):           # 没加密
            continue
        if info.compress_type not in (0, 8):     # hashcat 17225 只支持 stored/deflate
            continue
        crc = int(info.CRC or 0) & 0xFFFFFFFF
        if crc == 0:                             # 流式写的包校验字节来自时间戳，信息不够
            continue
        if info.file_size <= 0 or info.compress_size <= 12:
            continue
        off = info.header_offset
        if off < 0 or off + 30 > len(data):
            continue
        try:
            sig, _ver, _flag, _meth, _t, _d, _crc, cs, _us, fnl, extl = struct.unpack(
                "<IHHHHHIIIHH", data[off:off + 30])
        except struct.error:
            continue
        if sig != 0x04034B50:
            continue
        base = off + 30 + fnl + extl
        csize = int(cs or info.compress_size or 0)
        blob = data[base:base + csize]
        if len(blob) <= 12:
            continue
        out.append({"name": info.filename, "method": info.compress_type,
                    "enc": blob, "crc": crc,
                    "comp_len": csize,                 # 含 12 字节头，和 zip2john 一致
                    "uncomp_len": int(info.file_size),
                    "offset": off, "add_off": 30 + fnl})
    return out


def pkzip_hash(data: bytes, max_data: int = MAX_PK_DATA) -> "dict | None":
    """挑密文最小的 ZipCrypto 条目拼成 `$pkzip2$`（data_type=2，带 CRC 全量校验）。

    不带 CRC 的“只校验头部”写法（data_type=1）hashcat 只在多文件时才继续比对，
    单文件永远不会报命中，所以这里必须放整段密文。
    """
    ents = [e for e in pkzip_entries(data) if len(e["enc"]) <= max_data]
    if not ents:
        return None
    e = min(ents, key=lambda x: len(x["enc"]))
    enc, crc = e["enc"], e["crc"]
    check = (crc >> 16) & 0xFFFF
    h = (f"$pkzip2$1*2*2*0*{e['comp_len']:x}*{e['uncomp_len']:x}*{crc:08x}"
         f"*{e['offset']:x}*{e['add_off']:x}*{e['method']}*{len(enc):x}"
         f"*{check:04x}*{check:04x}*{enc.hex()}*$/pkzip2$")
    return {"hash": h, "ct": len(enc), "name": e["name"], "crc": crc,
            "key": f"{crc:08x}:{len(enc)}:{enc[:12].hex()}"}


def pkzip_info(data: bytes) -> dict:
    """{zipcrypto, entries, ct, too_big}，给调用方解释为什么没走 GPU。"""
    ents = pkzip_entries(data)
    if not ents:
        return {"zipcrypto": False, "entries": 0, "ct": 0, "too_big": False}
    small = [e for e in ents if len(e["enc"]) <= MAX_PK_DATA]
    return {"zipcrypto": True, "entries": len(ents),
            "ct": min((len(e["enc"]) for e in ents), default=0),
            "too_big": not small}


# ------------------------------------------------------------------ 缓存
def _cache_load() -> dict:
    global _PW_CACHE
    if _PW_CACHE is None:
        try:
            with open(CACHE, "r", encoding="utf-8") as f:
                _PW_CACHE = json.load(f)
            if not isinstance(_PW_CACHE, dict):
                _PW_CACHE = {}
        except Exception:  # noqa: BLE001
            _PW_CACHE = {}
    return _PW_CACHE


def _cache_get(key: str) -> str:
    with _LOCK:
        return _cache_load().get(key) or ""


def _cache_put(key: str, pwd: str) -> None:
    global _PW_CACHE
    with _LOCK:
        d = _cache_load()
        d[key] = pwd
        _PW_CACHE = d
        try:
            os.makedirs(WORK, exist_ok=True)
            tmp = CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, CACHE)
        except OSError:
            pass


# ------------------------------------------------------------------ 运行
def _fmt_speed(hs: float) -> str:
    if hs >= 1e6:
        return f"{hs / 1e6:.1f} MH/s"
    if hs >= 1e3:
        return f"{hs / 1e3:.1f} kH/s"
    return f"{hs:.0f} H/s"


def _pot_result(pot: str, hash_text: str) -> str:
    """从 hashcat 的 potfile 里取出这次命中的密码。"""
    pw = ""
    try:
        if os.path.exists(pot):
            head = hash_text.strip()[:120]
            with open(pot, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if line.startswith(head) and ":" in line:
                        pw = line.rsplit(":", 1)[-1]
                        break
            os.remove(pot)
    except OSError:
        pass
    return pw


def _tail_error(tail: list, killed: str, rc: int) -> str:
    """从 hashcat 输出里判断失败原因（跑完没命中 / 真报错 / 被取消）。"""
    if killed:
        return killed
    plain = [ln.strip() for ln in tail if ln and not ln.startswith("{")
             and not ln.startswith("[") and not ln.startswith(("Started", "Stopped"))]
    for ln in plain:
        low = ln.lower()
        if any(k in low for k in ("no hashes loaded", "token length", "exception",
                                  "invalid", "error", "not supported",
                                  "out of memory", "cl_out_of_resources",
                                  "cl_unknown_error")):
            return ln[:200]
    if rc == 1:
        return "候选已跑完，未命中"
    if rc == 0:
        return "未命中"
    return f"hashcat 退出码 {rc}"


def _cleanup_run(hf: str, tmp_mask: "str | None", stamp: str, exe: str) -> None:
    base = os.path.dirname(exe)
    for tmp in (hf, tmp_mask, os.path.join(base, f"pd{stamp}.pid"),
                os.path.join(base, f"pd{stamp}.log")):
        if not tmp:
            continue
        try:
            os.remove(tmp)
        except OSError:
            pass


def _clean_stale(max_age: int = 3600) -> None:
    """清掉上次异常退出留下的临时文件（hash_/masks_/pot_/wl_）。"""
    try:
        now = time.time()
        for name in os.listdir(WORK):
            if not name.startswith(("hash_", "masks_", "pot_", "wl_")):
                continue
            fp = os.path.join(WORK, name)
            try:
                if now - os.path.getmtime(fp) > max_age:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass


def run_hash(hash_text: str, *, masks: "list[str] | None" = None,
             wordlist: str | None = None,
             charsets: "list[str] | None" = None, mode: int = MODE_AES,
             timeout: int = 1800, runtime: int = 0, log=None, cancel=None,
             pause=None, skip: int = 0, limit: int = 0) -> dict:
    """跑 hashcat。masks 走 -a 3（多行掩码写成掩码文件），wordlist 走 -a 0。

    skip/limit 用来分块跑（配合暂停/继续）；pause 置位时杀掉进程并返回
    {paused: True, processed: 这一块已经跑了多少}，进程被杀前收集到的进度就是它的值。

    返回 {ok, password, seconds, speed, rc, error, engine}。
    """
    st = status()
    if not st["available"]:
        return {"ok": False, "error": st["error"] or "hashcat 不可用", "engine": "hashcat"}
    if not masks and wordlist is None:
        return {"ok": False, "error": "没有给出掩码或字典", "engine": "hashcat"}
    os.makedirs(WORK, exist_ok=True)
    _clean_stale()
    stamp = f"{int(time.time() * 1000) % 100000000}"
    hf = os.path.join(WORK, f"hash_{stamp}.txt")
    pot = os.path.join(WORK, f"pot_{stamp}.txt")
    try:
        with open(hf, "w", encoding="utf-8") as f:
            f.write(hash_text.strip() + "\n")
    except OSError as exc:
        return {"ok": False, "error": f"写哈希文件失败：{exc}", "engine": "hashcat"}

    cmd = [st["exe"], "-m", str(mode), "--potfile-path", pot, "--restore-disable",
           "--session", f"pd{stamp}", "--logfile-disable",
           "--self-test-disable", "--hwmon-disable",
           "-w", "3", "--status", "--status-timer", "2", "--status-json"]
    cmd += device_args()
    if runtime and runtime > 0:
        cmd += ["--runtime", str(int(runtime))]
    if skip and skip > 0:
        cmd += ["--skip", str(int(skip))]
    if limit and limit > 0:
        cmd += ["--limit", str(int(limit))]
    tmp_mask = None
    if masks:
        for i, cs in enumerate((charsets or [])[:4]):
            cmd += [f"-{i + 1}", cs]
        if len(masks) == 1:
            cmd += ["-a", "3", hf, masks[0]]
        else:                       # 多个长度 → hashcat 的掩码文件（一行一个）
            tmp_mask = os.path.join(WORK, f"masks_{stamp}.txt")
            with open(tmp_mask, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(masks) + "\n")
            cmd += ["-a", "3", hf, tmp_mask]
    else:
        cmd += ["-a", "0", hf, wordlist]

    t0 = time.time()
    state = {"speed": 0.0, "logged": 0.0, "t": 0.0, "status": -1, "pct": 0.0}
    tail: list[str] = []

    def on_line(ln: str) -> None:
        tail.append(ln)
        if len(tail) > 80:
            del tail[:30]
        if not ln.startswith("{"):
            return
        try:
            d = json.loads(ln)
        except Exception:  # noqa: BLE001
            return
        # 进度：优先用 guess_base_percent（掩码文件时是整条队列的百分比）
        guess = d.get("guess") or {}
        pct = guess.get("guess_base_percent")
        if pct is None:
            pr = d.get("progress")
            if isinstance(pr, list) and len(pr) >= 2 and pr[1]:
                pct = float(pr[0]) * 100.0 / float(pr[1])
            elif isinstance(pr, (int, float)):
                pct = float(pr) / 100.0
        try:
            pct = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            return
        sp = 0.0
        for item in d.get("devices") or []:
            try:
                sp += float(item.get("speed") or 0)
            except (TypeError, ValueError):
                pass
        now = time.time() - t0
        state["t"] = now
        state["speed"] = sp
        state["pct"] = pct
        state["status"] = d.get("status", state["status"])
        if log is None or now < 6:
            return
        if state["logged"] and pct - state["logged"] < 10:
            return
        state["logged"] = pct
        msg = f"hashcat 进度 {pct:.0f}%"
        if sp:
            msg += f" · 当前 {_fmt_speed(sp)}"
        msg += f" · 已用 {now:.0f}s"
        log(msg)

    try:
        p = subprocess.Popen(cmd, cwd=os.path.dirname(st["exe"]),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动 hashcat 失败：{exc}", "engine": "hashcat"}

    q: "queue.Queue[str | None]" = queue.Queue()

    def reader() -> None:
        try:
            for ln in p.stdout:                      # type: ignore[union-attr]
                q.put(ln.rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    killed = ""
    paused = False
    while True:
        if cancel is not None and cancel.is_set():
            killed = "已取消"
            try:
                p.kill()
            except OSError:
                pass
            break
        if pause is not None and pause.is_set():
            paused = True
            killed = "已暂停"
            try:
                p.kill()
            except OSError:
                pass
            break
        if time.time() - t0 > timeout:
            killed = f"超时（{timeout}s）"
            try:
                p.kill()
            except OSError:
                pass
            break
        try:
            ln = q.get(timeout=0.4)
        except queue.Empty:
            continue
        if ln is None:
            break
        on_line(ln)
    rc = p.wait()
    seconds = time.time() - t0

    pw = _pot_result(pot, hash_text)
    _cleanup_run(hf, tmp_mask, stamp, st["exe"])
    err = "" if pw else _tail_error(tail, killed, rc)
    dev_err = (not pw and not killed and rc not in (0, 1)) or any(
        "CL_" in ln or "clWaitForEvents" in ln for ln in tail)
    done = int(limit * state["pct"] / 100.0) if (limit and limit > 0) else 0
    return {"ok": bool(pw), "password": pw, "rc": rc, "seconds": round(seconds, 1),
            "speed": state["speed"], "device_error": bool(dev_err),
            "paused": bool(paused and not pw), "processed": done,
            "error": err, "engine": "hashcat"}


def run_hash_stream(hash_text: str, source, *, mode: int = MODE_PKZIP,
                    timeout: int = 1800, max_feed: int = 0, runtime: int = 0,
                    log=None, cancel=None) -> dict:
    """边生成候选边喂给 hashcat 的 stdin（-a 0 读管道）。

    为什么要这条路：mode 17225 在"掩码键空间 >1M"时会走 hashcat 的 amplifier 路径，
    那条路上的 zlib 校验在 OpenCL 后端会 `CL_UNKNOWN_ERROR` 直接崩（17200/17225 都能复现，
    换 -n/-u/-O/换卡都没用）；而字典/管道模式（-a 0）稳定。所以 ZipCrypto + 大空间走管道。
    """
    st = status()
    if not st["available"]:
        return {"ok": False, "error": st["error"] or "hashcat 不可用", "engine": "hashcat"}
    os.makedirs(WORK, exist_ok=True)
    _clean_stale()
    stamp = f"{int(time.time() * 1000) % 100000000}"
    hf = os.path.join(WORK, f"hash_{stamp}.txt")
    pot = os.path.join(WORK, f"pot_{stamp}.txt")
    try:
        with open(hf, "w", encoding="utf-8") as f:
            f.write(hash_text.strip() + "\n")
    except OSError as exc:
        return {"ok": False, "error": f"写哈希文件失败：{exc}", "engine": "hashcat"}

    cmd = [st["exe"], "-m", str(mode), "-a", "0", hf,
           "--potfile-path", pot, "--restore-disable",
           "--session", f"pd{stamp}", "--logfile-disable",
           "--self-test-disable", "--hwmon-disable", "-w", "3", "--quiet"]
    cmd += device_args()
    if runtime and runtime > 0:
        cmd += ["--runtime", str(int(runtime))]

    t0 = time.time()
    tail: list[str] = []
    fed = 0
    try:
        p = subprocess.Popen(cmd, cwd=os.path.dirname(st["exe"]),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", bufsize=1 << 20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动 hashcat 失败：{exc}", "engine": "hashcat"}

    q: "queue.Queue[str | None]" = queue.Queue()

    def reader() -> None:
        try:
            for ln in p.stdout:                      # type: ignore[union-attr]
                q.put(ln.rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)

    def feeder() -> None:
        nonlocal fed
        buf: list[str] = []
        try:
            for cand in source:
                if cancel is not None and cancel.is_set():
                    break
                if max_feed and fed >= max_feed:
                    break
                buf.append(cand)
                fed += 1
                if len(buf) >= 20000:
                    p.stdin.write("\n".join(buf) + "\n")
                    buf.clear()
            if buf:
                p.stdin.write("\n".join(buf) + "\n")
                buf.clear()
        except (BrokenPipeError, OSError, ValueError):
            pass                                   # hashcat 提前退出（命中/被取消）
        finally:
            try:
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=feeder, daemon=True).start()

    killed = ""
    next_log = 10.0
    while True:
        if cancel is not None and cancel.is_set():
            killed = "已取消"
            try:
                p.kill()
            except OSError:
                pass
            break
        now = time.time() - t0
        if now > timeout:
            killed = f"超时（{timeout}s）"
            try:
                p.kill()
            except OSError:
                pass
            break
        if log is not None and now >= next_log:
            next_log = now + 15.0
            log(f"GPU 管破解：已喂 {fed:,} 个候选 · 已用 {now:.0f}s")
        try:
            ln = q.get(timeout=0.4)
        except queue.Empty:
            continue
        if ln is None:
            break
        tail.append(ln)
        if len(tail) > 80:
            del tail[:30]
    rc = p.wait()
    seconds = time.time() - t0
    pw = _pot_result(pot, hash_text)
    _cleanup_run(hf, None, stamp, st["exe"])
    err = "" if pw else _tail_error(tail, killed, rc)
    dev_err = (not pw and not killed and rc not in (0, 1)) or any(
        "CL_" in ln or "clWaitForEvents" in ln for ln in tail)
    return {"ok": bool(pw), "password": pw, "rc": rc, "seconds": round(seconds, 1),
            "speed": (fed / seconds) if seconds > 0 else 0.0, "fed": fed,
            "device_error": bool(dev_err), "error": err, "engine": "hashcat"}


def save_wordlist(cands: "list[str]") -> str:
    """把候选写成字典文件（hashcat -a 0）。"""
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, f"wl_{int(time.time() * 1000) % 100000000}.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(cands))
        f.write("\n")
    return path


def _pick_hash(data: bytes) -> "tuple":
    """选一条能交给 hashcat 的哈希。返回 (hash, mode, 说明, cache_key) 或 (None, 0, 原因, "")。"""
    aes = zip2_hash(data)
    if aes:
        return (aes["hash"], MODE_AES,
                f"AES-{AES_BITS[aes['strength']]} 单条密文 {aes['ct'] / 1024:.0f}KB",
                "aes:" + aes["key"])
    pk = pkzip_hash(data)
    if pk:
        return (pk["hash"], MODE_PKZIP,
                f"ZipCrypto 单条密文 {pk['ct'] / 1024:.0f}KB", "pkzip:" + pk["key"])
    ai = aes_zip_info(data)
    if ai["aes"]:
        return (None, 0, f"AES 密文太大（{ai['ct'] / 1e6:.1f}MB > {MAX_CT / 1e6:.0f}MB），退回 CPU", "")
    pi = pkzip_info(data)
    if pi["zipcrypto"]:
        return (None, 0, f"ZipCrypto 密文太大（最小 {pi['ct'] / 1024:.0f}KB > "
                         f"{MAX_PK_DATA / 1024:.0f}KB），退回 CPU", "")
    return (None, 0, "不是 WinZip AES / ZipCrypto 包，退回 CPU", "")


PK_CHUNK = 20_000_000      # 每次喂给 hashcat 的候选数上限（再多会踩到它内部的溢出，见下）
PK_REWIND = 1_000_000      # 崩掉后续跑时往回退一点，避免漏掉没处理的候选


def _stream_chunks(hash_text: str, factory, *, mode: int, count: int = 0,
                   timeout: int = 1800, log=None, cancel=None) -> dict:
    """分块管道流式破解（每块 ≤ 20M 候选），崩了就缩小块从断点重试。

    为什么这么绕：mode 17225 在 hashcat 里跑长任务会 `CL_UNKNOWN_ERROR`
    （掩码键空间 >1M 走 amplifier 时必崩；管道模式撑到 3000 万候选左右也会崩）。
    按 20M 分块喂、每块跑完让 hashcat 自己退出，就避开了这个坑；
    万一某块还是崩，就把块减半、退回 1M 候选重来，最多 400 块。
    """
    pos = 0
    chunk = PK_CHUNK
    spent = 0.0
    attempt = 0
    res: dict = {"ok": False, "error": "没有开始", "engine": "hashcat"}
    while attempt < 400:
        attempt += 1
        try:
            it = factory(pos)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"生成候选失败：{exc}", "engine": "hashcat"}
        res = run_hash_stream(hash_text, it, mode=mode, timeout=timeout,
                              max_feed=chunk, log=log, cancel=cancel)
        spent += res.get("seconds") or 0
        res["seconds"] = round(spent, 1)
        res["restarts"] = attempt - 1
        if res["ok"]:
            return res
        if cancel is not None and cancel.is_set():
            return res
        fed = int(res.get("fed") or 0)
        if res.get("device_error"):
            chunk = max(1_000_000, chunk // 2)
            pos = max(0, pos + fed - min(fed, PK_REWIND))
            if log:
                log(f"hashcat 崩了（{res.get('error')}）：退到 {pos:,}，块缩到 {chunk:,} 重试")
            if chunk <= 1_000_000 and fed <= PK_REWIND:
                res["error"] = f"{res.get('error')}（块已经降到最小还崩，放弃 GPU）"
                return res
            continue
        if fed <= 0:
            return res                       # 候选喂完了，hashcat 也跑完了
        pos += fed
        if fed < chunk:
            return res                       # 这一块是最后一块
        if log:
            done = f"{pos:,}"
            if count:
                done += f" / {count:,}（{pos * 100.0 / count:.0f}%）"
            log(f"GPU 已跑 {done}")
        chunk = min(PK_CHUNK, chunk * 2)     # 顺利的话下一块再放大回来
    return res


_MASK_SETS = {"?d": 10, "?l": 26, "?u": 26, "?s": 33, "?a": 62, "?b": 256}


def mask_space(mask: str, charsets: "list[str] | None" = None) -> int:
    """一条掩码的键空间大小（?? 是字面量问号，?1~?4 用自定义字符集）。"""
    total = 1
    i = 0
    cs = list(charsets or [])
    while i < len(mask):
        ch = mask[i]
        if ch == "?" and i + 1 < len(mask):
            nxt = mask[i + 1]
            if nxt == "?":
                i += 2
                continue
            if nxt in "1234":
                idx = int(nxt) - 1
                total *= max(1, len(cs[idx])) if idx < len(cs) else 1
            elif ("?" + nxt) in _MASK_SETS:
                total *= _MASK_SETS["?" + nxt]
            else:
                return 0
            i += 2
            continue
        i += 1
    return total


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def run_paused_wait(pause, cancel, log=None) -> bool:
    """暂停时在这里等；返回 True 表示等待期间被取消。"""
    if log:
        log("已暂停（进度已保存，点「继续」接着跑）")
    import time as _t
    while pause is not None and pause.is_set():
        if cancel is not None and cancel.is_set():
            return True
        _t.sleep(0.2)
    if log:
        log("继续破解…")
    return bool(cancel is not None and cancel.is_set())


_MASK_CHARS = {"?d": "0123456789",
               "?l": "abcdefghijklmnopqrstuvwxyz",
               "?u": "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def _mask_tokens(mask: str, charsets: "list[str] | None") -> "list[tuple[str, str]]":
    """把掩码拆成 [(token, 可用字符)]；token 为字面量时字符就是它自己。"""
    out: "list[tuple[str, str]]" = []
    cs = list(charsets or [])
    i = 0
    while i < len(mask):
        ch = mask[i]
        if ch == "?" and i + 1 < len(mask):
            nxt = mask[i + 1]
            if nxt == "?":
                out.append(("?", "?"))
            elif nxt in "1234":
                idx = int(nxt) - 1
                out.append((mask[i:i + 2], cs[idx] if idx < len(cs) else "?"))
            elif ("?" + nxt) in _MASK_CHARS:
                out.append((mask[i:i + 2], _MASK_CHARS["?" + nxt]))
            elif ("?" + nxt) == "?s":
                out.append(("?s", " !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"))
            elif ("?" + nxt) == "?a":
                out.append(("?a", _MASK_CHARS["?l"] + _MASK_CHARS["?u"] + _MASK_CHARS["?d"]))
            elif ("?" + nxt) == "?b":
                out.append(("?b", ""))          # 全字节，拆不了
            else:
                out.append((mask[i:i + 2], "?"))  # 未知：当字面量
            i += 2
            continue
        out.append((ch, ch))
        i += 1
    return out


def split_mask(mask: str, charsets: "list[str] | None", parts: int,
               max_subs: int = 256) -> "list[str]":
    """把掩码按**前导位置**拆成若干子掩码，覆盖完整、互不重叠。

    比如 ?d?d?d?d 拆 10 份 → 0?d?d?d、1?d?d?d …；abc?d?d 拆 10 份 → abc0?d、abc1?d …
    字面量位置原样保留，?b（全字节）位置不参与拆分。
    """
    parts = max(2, int(parts or 2))
    cap = max(4, min(max_subs, parts * 4))
    toks = _mask_tokens(mask, charsets)
    # 选最前面的若干个"可枚举"位置：展开后的份数不超过 cap，但尽量接近 parts
    k = 0
    prod = 1
    for i, (_tok, chars) in enumerate(toks):
        if len(chars) <= 1:
            k = i + 1
            continue
        if not chars or prod * len(chars) > cap:
            break
        prod *= len(chars)
        k = i + 1
    if prod < 2:
        return [mask]
    prefix, rest = toks[:k], toks[k:]
    var = [chars for _tok, chars in prefix if len(chars) > 1]
    if not var:
        return [mask]
    out: "list[str]" = []
    for combo in _combos(var, cap):
        it = iter(combo)
        lit = []
        for _tok, chars in prefix:
            if len(chars) > 1:
                ch = next(it)
                lit.append("??" if ch == "?" else ch)
            else:
                lit.append("??" if chars == "?" else chars)
        out.append("".join(lit) + "".join(t for t, _c in rest))
    return out or [mask]


def _combos(var: "list[str]", cap: int) -> "list[list[str]]":
    """各个可变位置的字符组合（每个位置一个字符），最多 cap 个。"""
    acc: "list[list[str]]" = [[]]
    for chars in var:
        nxt: "list[list[str]]" = []
        for pre in acc:
            for ch in chars:
                nxt.append(pre + [ch])
                if len(nxt) >= cap:
                    break
            if len(nxt) >= cap:
                break
        acc = nxt
        if len(acc) >= cap:
            break
    return acc


def _chunk_plan(masks: "list[str] | None", charsets, count: int, dict_count: int,
                speed: float = 0.0, chunk_secs: float = 90.0) -> "list[dict]":
    """把工作拆成 [{kind, mask, off, n}]，一块 = 一次 hashcat 调用。"""
    plan: "list[dict]" = []
    if masks:
        total = sum(mask_space(m, charsets) for m in masks) or count
        sp = float(speed or 12e6)
        per = max(2_000_000, int(sp * chunk_secs))
        for m in masks:
            sp_m = mask_space(m, charsets)
            if not sp_m:
                continue
            if sp_m <= per * 3:               # 小掩码不必拆
                plan.append({"kind": "mask", "mask": m, "off": 0, "n": sp_m})
                continue
            parts = min(256, max(2, int(total / per) if total > per else 2))
            subs = split_mask(m, charsets, parts)
            for sub in subs:
                plan.append({"kind": "mask", "mask": sub, "off": 0,
                             "n": mask_space(sub, charsets)})
    else:
        chunk = 200_000 if dict_count > 200_000 else max(1, dict_count)
        off = 0
        while off < dict_count:
            n = min(chunk, dict_count - off)
            plan.append({"kind": "dict", "mask": None, "off": off, "n": n})
            off += n
    return plan


def _run_plan(hash_text: str, plan: "list[dict]", *, mode: int, candidates,
              charsets, timeout: int, runtime: int, log, cancel, pause) -> dict:
    """按块跑；每块之间响应暂停/取消（暂停时先杀掉当前块，继续时重跑这一块）。"""
    import time as _t
    t_start = _t.time()
    for item in plan:
        if cancel is not None and cancel.is_set():
            return {"ok": False, "cancelled": True, "error": "已取消", "engine": "hashcat"}
        if pause is not None and pause.is_set():
            if run_paused_wait(pause, cancel, log):
                return {"ok": False, "cancelled": True, "error": "已取消", "engine": "hashcat"}
        left = runtime - int(_t.time() - t_start) if runtime else 0
        if runtime and left <= 0:
            return {"ok": False, "error": f"到时间上限了（已跑 {runtime}s）", "engine": "hashcat"}
        sliced = None
        try:
            if item["kind"] == "mask":
                r = run_hash(hash_text, masks=[item["mask"]], charsets=charsets,
                             mode=mode, timeout=timeout, runtime=left or 0,
                             log=log, cancel=cancel, pause=pause)
            else:
                if not candidates:
                    return {"ok": False, "error": "没有候选", "engine": "hashcat"}
                part = candidates[item["off"]:item["off"] + item["n"]]
                if not part:
                    continue
                sliced = save_wordlist(part)
                r = run_hash(hash_text, wordlist=sliced, charsets=charsets,
                             mode=mode, timeout=timeout, runtime=left or 0,
                             log=log, cancel=cancel, pause=pause)
        finally:
            if sliced:
                try:
                    os.remove(sliced)
                except OSError:
                    pass
        if r.get("ok"):
            r["seconds"] = round(_t.time() - t_start, 1)
            return r
        if r.get("paused"):
            if log:
                log("已暂停（这一块先作废，继续时重跑）")
            if run_paused_wait(pause, cancel, log):
                return {"ok": False, "cancelled": True, "error": "已取消", "engine": "hashcat"}
            # 继续：重跑刚才被打断的这一块
            continue_re = run_hash(hash_text,
                                   masks=[item["mask"]] if item["kind"] == "mask" else None,
                                   wordlist=sliced if item["kind"] == "dict" else None,
                                   charsets=charsets, mode=mode, timeout=timeout,
                                   runtime=runtime, log=log, cancel=cancel, pause=pause)
            if continue_re.get("ok"):
                continue_re["seconds"] = round(_t.time() - t_start, 1)
                return continue_re
            if continue_re.get("paused"):
                return {"ok": False, "paused": True, "error": "已暂停", "engine": "hashcat"}
            r = continue_re
        if not r.get("ok"):
            if r.get("device_error") or r.get("cancelled"):
                return r                      # 显卡崩了/参数错：直接上报
            if log and item.get("n"):
                log(f"  这一块跑完没命中（{_fmt_int(r.get('n') or item['n'])} 个）")
            continue                          # 正常跑完没命中：下一块
    return {"ok": False, "error": "候选跑完了，没找到", "engine": "hashcat"}


def crack_zip(data: bytes, *, masks: "list[str] | None" = None,
              charsets: "list[str] | None" = None,
              candidates: "list[str] | None" = None,
              iterator_factory=None, count: int = 0,
              timeout: int = 1800, runtime: int = 0, log=None, cancel=None,
              pause=None) -> dict:
    """zip 包 → hashcat。AES 走 13600，ZipCrypto 走 17225。

    masks 走 -a 3（最快，多个掩码=掩码文件）；candidates 走 -a 0。
    命中会写进缓存，下次同包秒回。
    """
    hash_text, mode, label, key = _pick_hash(data)
    if not hash_text:
        return {"ok": False, "engine": "hashcat", "error": label}
    cached = _cache_get(key)
    if cached:
        if log:
            log(f"GPU 缓存命中：{cached}")
        return {"ok": True, "password": cached, "engine": "cache", "seconds": 0.0,
                "speed": 0.0, "error": ""}
    if log:
        tip = f"{count:,} 个候选" if count else "字典"
        how = "掩码" if masks else "字典"
        log(f"GPU 破解中：{tip}（{label}，mode {mode}，{how}）")
    if not masks and not candidates:
        return {"ok": False, "engine": "hashcat", "error": "没有候选"}
    plan = _chunk_plan(masks, charsets, count, len(candidates or []),
                       speed=LAST_SPEED.get(mode) or DEFAULT_SPEED.get(mode, 12e6))
    if plan:
        res = _run_plan(hash_text, plan, mode=mode, candidates=candidates,
                        charsets=charsets, timeout=timeout, runtime=runtime,
                        log=log, cancel=cancel, pause=pause)
    else:
        wl = None
        try:
            if candidates:
                wl = save_wordlist(candidates)
            res = run_hash(hash_text, masks=masks, wordlist=wl, charsets=charsets,
                           mode=mode, timeout=timeout, runtime=runtime,
                           log=log, cancel=cancel, pause=pause)
        finally:
            if wl:
                try:
                    os.remove(wl)
                except OSError:
                    pass
    # 兜底：hashcat 的 pkzip 校验在旧版 OpenCL 后端会崩（v8 已修）。真崩了就走管道流式重来。
    if (not res["ok"] and res.get("device_error") and masks
            and mode == MODE_PKZIP and iterator_factory is not None):
        if log:
            log(f"掩码跑崩了（{res.get('error')}），改走管道流式重试…")
        res = _stream_chunks(hash_text, iterator_factory, mode=mode, count=count,
                             timeout=timeout, log=log, cancel=cancel)
    secs_all = res.get("seconds") or 0
    if count >= 20_000_000 and secs_all >= 5:      # 小任务的平均速度含启动开销，别拿来当估值
        LAST_SPEED[mode] = max(1.0, count / secs_all)
    if res["ok"]:
        _cache_put(key, res["password"])
        if log:
            secs = res.get("seconds") or 0
            avg = (count / secs) if (count and secs > 0) else (res.get("speed") or 0)
            log(f"GPU 破解成功：{res['password']}（{secs}s"
                + (f"，平均 {_fmt_speed(avg)}" if avg else "") + "）")
    elif log:
        log(f"GPU 没找到：{res.get('error') or '未命中'}")
    return res

def run_mask(hash_text: str, mask: str, mode: int = MODE_AES,
             timeout: int = 1800, workdir: str | None = None) -> dict:
    """兼容旧接口：跑掩码破解。返回 {ok, password, out, seconds}。"""
    res = run_hash(hash_text, masks=[mask], mode=mode, timeout=timeout)
    return {"ok": res["ok"], "password": res.get("password", ""), "rc": res.get("rc"),
            "out": "", "error": res.get("error", ""), "seconds": res.get("seconds", 0)}


# ---------------------------------------------------------------- 7-Zip（11600）
def seven_hash(path: str) -> tuple[str, str]:
    """从 7z 包里抽 11600 的哈希行；抽不出来返回 ("", 原因)。"""
    try:
        from media import sevenzip as SZ
    except Exception as exc:  # noqa: BLE001
        return "", f"7z 解析模块不可用：{exc}"
    r = SZ.dump_hashes(path)
    hs = r.get("hashes") or []
    if not hs:
        return "", r.get("error") or "抽不出哈希"
    return hs[0], ""


def crack_7z(data: bytes, *, candidates: "list[str] | None" = None,
             masks: "list[str] | None" = None, charsets: "list[str] | None" = None,
             timeout: int = 1800, runtime: int = 0, log=None, cancel=None,
             pause=None) -> dict:
    """7z 包直接走 11600 显卡破解（候选列表 → 字典模式，masks → 掩码模式）。"""
    import shutil
    import tempfile
    st = status()
    if not st["available"]:
        return {"ok": False, "error": st["error"] or "hashcat 不可用", "engine": "hashcat"}
    if not candidates and not masks:
        return {"ok": False, "error": "没有给出候选或掩码", "engine": "hashcat"}
    tmp = tempfile.mkdtemp(prefix="pd7zh_")
    try:
        path = os.path.join(tmp, "a.7z")
        with open(path, "wb") as f:
            f.write(data)
        line, err = seven_hash(path)
        if not line:
            return {"ok": False, "error": err or "抽不出 7z 哈希", "engine": "hashcat"}
        if log:
            how = "掩码" if masks else f"字典 {len(candidates or []):,} 条"
            log(f"GPU 破解 7z 中（{how}，mode {MODE_7Z}）…")
        plan = _chunk_plan(masks, charsets, sum(mask_space(m, charsets) for m in (masks or [])),
                           len(candidates or []),
                           speed=LAST_SPEED.get(MODE_7Z) or DEFAULT_SPEED[MODE_7Z])
        if plan:
            res = _run_plan(line, plan, mode=MODE_7Z, candidates=candidates,
                            charsets=charsets or [], timeout=timeout, runtime=runtime,
                            log=log, cancel=cancel, pause=pause)
        else:
            wl = None
            if candidates:
                wl = os.path.join(tmp, "words.txt")
                try:
                    with open(wl, "w", encoding="utf-8", errors="replace") as f:
                        for w in candidates:
                            f.write(str(w).replace("\n", " ") + "\n")
                except OSError as exc:
                    return {"ok": False, "error": f"写字典失败：{exc}", "engine": "hashcat"}
            res = run_hash(line, masks=masks or [], wordlist=wl, charsets=charsets or [],
                           mode=MODE_7Z, timeout=timeout, runtime=runtime, log=log,
                           cancel=cancel, pause=pause)
        res["mode"] = MODE_7Z
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
