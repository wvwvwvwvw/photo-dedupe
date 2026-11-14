#!/usr/bin/env python3
"""图片查重 · 本地 Web 服务

启动后浏览器打开 http://127.0.0.1:8765 即可使用。
所有计算都在本机完成，不联网、不上传任何图片。

计算引擎：默认 GPU 优先（JPEG 走 nvJPEG + 显存内计算），没有 CUDA 就用 CPU。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import mimetypes
import os
import re
import shutil
import tempfile
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dedupe import archive, pipeline  # noqa: E402
from dedupe.formats import capabilities  # noqa: E402
from dedupe.grouping import (  # noqa: E402
    KEEP_LABELS,
    prepare as prepare_cluster,
    render as render_groups,
    select_members as grouping_select,
)
from dedupe.scanner import ScanJob, count_images  # noqa: E402
from dedupe.store import Store  # noqa: E402
from dedupe.runtime import reexec_with_gpu_python  # noqa: E402
from dedupe import patches as dpatches  # noqa: E402
from media import config as mconfig  # noqa: E402
from media import grouping as mgrouping  # noqa: E402
from media import ffmpeg as mffmpeg  # noqa: E402
from media.scan import MediaScanJob  # noqa: E402
from media.clean import CleanJob  # noqa: E402
from media.unzip import BruteJob, UnzipJob  # noqa: E402
from media.plain import PlainJob  # noqa: E402
from media.conv import ConvJob, OUT_ENCODINGS  # noqa: E402
from media import conv as mconv  # noqa: E402
from media.pack import ENC_LABELS as PACK_ENCS  # noqa: E402
from media import pack as mpack  # noqa: E402
from media import pipeline as mpipeline  # noqa: E402
from media import wordlists as mwords  # noqa: E402
from media.store import MediaStore  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
DATA = os.path.join(BASE, "data")
SETTINGS_PATH = os.path.join(BASE, "settings.json")
APP_NAME = "图片查重"
VERSION = "2.4"
HASH_VERSION = 4          # 哈希算法/管线变了就 +1，旧索引会自动重建
SMALL_LIB = 600           # 少于这个数量：不启用 GPU，直接 CPU 快速模式
config = mconfig


def static_tag() -> str:
    """web/ 下静态文件的版本号：文件一改就变，用来强制浏览器换新 JS/CSS。"""
    tag = []
    for name in ("app.js", "style.css", "index.html"):
        p = os.path.join(WEB, name)
        try:
            tag.append(f"{int(os.path.getmtime(p))}")
        except OSError:
            tag.append("0")
    return "-".join(tag)

def read_clipboard() -> dict:
    """读 Windows 剪贴板里的文本。"""
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持"}
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "Get-Clipboard -Raw")
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "读剪贴板超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读剪贴板失败：{exc}"}
    text = p.stdout or ""
    if not text.strip():
        return {"ok": False, "error": "剪贴板里没有文字"}
    return {"ok": True, "text": text}


def write_clipboard(text: str) -> dict:
    """把文本写进 Windows 剪贴板。"""
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持"}
    tmp = os.path.join(tempfile.gettempdir(), f"pd_clip_{os.getpid()}.txt")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text or "")
        script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                  f"Set-Clipboard -Value ([IO.File]::ReadAllText('{tmp}', "
                  "[Text.Encoding]::UTF8).TrimEnd([char]13,[char]10))")
        subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, text=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"写剪贴板失败：{exc}"}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"ok": True, "chars": len(text or "")}


UI_STATE_FILE = os.path.join(BASE, "data", "ui_state.json")
_UI_STATE_CACHE = [None]


def _ui_state() -> dict:
    if _UI_STATE_CACHE[0] is None:
        try:
            with open(UI_STATE_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            _UI_STATE_CACHE[0] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _UI_STATE_CACHE[0] = {}
    return _UI_STATE_CACHE[0]


def _ui_save() -> None:
    try:
        os.makedirs(os.path.dirname(UI_STATE_FILE), exist_ok=True)
        with open(UI_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_ui_state(), f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _pick_dir_key(key: str) -> str:
    return (key or "global").strip() or "global"


def remember_dir(path: str, key: str = "") -> None:
    """记住这次选的目录：既记到该输入框，也更新全局"上次位置"。"""
    p = str(path or "").strip()
    if not p:
        return
    folder = p if os.path.isdir(p) else os.path.dirname(p)
    if not folder or not os.path.isdir(folder):
        return
    st = _ui_state()
    dirs = st.setdefault("pick_dirs", {})
    if not isinstance(dirs, dict):
        dirs = {}
        st["pick_dirs"] = dirs
    dirs[_pick_dir_key(key)] = folder
    dirs["global"] = folder
    _ui_save()


def pick_start_dir(initial: str = "", key: str = "") -> str:
    """给内置浏览器选择器用的兜底起点。

    原生对话框现在交给系统自己记位置（见 native_pick_folder），
    这里只在系统不支持原生对话框时用：依次看「传进来的路径 → 该框上次 → 全局上次 → 桌面」。
    """
    dirs = _ui_state().get("pick_dirs") or {}
    if not isinstance(dirs, dict):
        dirs = {}
    cands = [str(initial or "").strip(), dirs.get(_pick_dir_key(key)) or "",
             dirs.get("global") or ""]
    for c in cands:
        if not c:
            continue
        if os.path.isdir(c):
            return c
        if os.path.isfile(c):
            return os.path.dirname(c)
    for c in (os.path.join(os.path.expanduser("~"), "Desktop"),
              os.path.expanduser("~")):
        if os.path.isdir(c):
            return c
    return ""


PICK_PS1 = os.path.join(BASE, "vendor", "picker", "pick_folder.ps1")


def native_pick_folder(title: str = "选择文件夹", initial: str = "",
                       key: str = "") -> dict:
    """调 Windows 原生文件夹对话框（现代 IFileOpenDialog，跟随系统上次的位置）。

    * 不传 initial（或路径不存在）时，由 Windows 自己决定从哪儿打开——
      和资源管理器一样，会记住上次用过的目录；
    * 传了存在的目录就以它为准（比如输入框里已经填好了路径）。
    """
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持原生选择框"}
    want = str(initial or "").strip()
    if want and not os.path.isdir(want):
        want = os.path.dirname(want) if os.path.isfile(want) else ""
    if os.path.isfile(PICK_PS1):
        env = dict(os.environ)
        env["PD_PICK_TITLE"] = str(title or "选择文件夹")
        env["PD_PICK_INITIAL"] = want
        env.pop("PD_PICK_PROBE", None)
        try:
            p = subprocess.run(
                ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy",
                 "Bypass", "-File", PICK_PS1],
                capture_output=True, text=True, timeout=600, encoding="utf-8",
                errors="replace", env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "选择框超时"}
        except Exception as exc:  # noqa: BLE001
            return _pick_folder_fallback(title, want, key, exc)
        out = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
        if out:
            line = out[-1]
            if line.startswith("OK|"):
                path = line[3:].strip()
                if path:
                    remember_dir(path, key)
                    return {"ok": True, "path": path}
                return {"ok": False, "error": "没有选择"}
            if line == "CANCEL":
                return {"ok": False, "error": "已取消", "cancelled": True}
            if line.startswith("FALLBACK|"):
                return _pick_folder_fallback(title, want, key,
                                             RuntimeError(line[9:]))
        return _pick_folder_fallback(title, want, key,
                                     RuntimeError(f"没输出：{(p.stderr or '')[:120]}"))
    return _pick_folder_fallback(title, want, key, RuntimeError("缺少 pick_folder.ps1"))


def _pick_folder_fallback(title: str, initial: str, key: str, why) -> dict:
    """老式 FolderBrowserDialog 兜底（系统太老 / 脚本缺失时）。"""
    t = str(title).replace("'", "''")
    init = str(initial or "").replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$f=New-Object System.Windows.Forms.Form;"
        "$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.FormBorderStyle='None';"
        "$f.Size=New-Object System.Drawing.Size(1,1);"
        "$f.StartPosition='CenterScreen';$f.Show();$f.Activate();"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$d.Description='{t}';"
        "$d.ShowNewFolderButton=$true;"
        f"if('{init}'){{$d.SelectedPath='{init}';}};"
        "if($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK)"
        "{Write-Output ('OK|'+$d.SelectedPath)}else{Write-Output 'CANCEL'};"
        "$f.Close();"
    )
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                           capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "选择框超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"打不开选择框：{exc}（{why}）"}
    out = (p.stdout or "").strip().splitlines()
    line = out[-1].strip() if out else ""
    if line.startswith("OK|"):
        path = line[3:].strip()
        if path:
            remember_dir(path, key)
            return {"ok": True, "path": path}
        return {"ok": False, "error": "没有选择"}
    return {"ok": False, "error": "已取消", "cancelled": True}


def native_pick_files(title: str = "选择文件（可多选）", initial: str = "",
                      filters: str = "", key: str = "") -> dict:
    """多选版本：返回 {"ok": True, "paths": [...]}。"""
    r = _native_pick(title, initial, filters, multi=True, key=key)
    if r.get("ok") and r.get("paths"):
        remember_dir(r["paths"][0], key)
    return r


def native_pick_file(title: str = "选择文件", initial: str = "",
                     filters: str = "", key: str = "") -> dict:
    r = _native_pick(title, initial, filters, multi=False, key=key)
    if r.get("ok") and r.get("paths"):
        remember_dir(r["paths"][0], key)
    return r


def _native_pick(title: str, initial: str, filters: str, multi: bool,
                 key: str = "") -> dict:
    """调 Windows 原生文件对话框（PowerShell + .NET）。

    filters 形如 `压缩包|*.zip;*.7z;*.rar|所有文件|*.*`，留空用默认的压缩包过滤器。
    没给 initial 时自动从"上次选的目录"打开；选完记住。
    """
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持原生选择框"}
    t = str(title).replace("'", "''")
    # 和文件夹选择框一致：不硬塞起始目录，让系统自己记住上次位置；
    # 只有调用方明确给了存在的路径才用它
    want = str(initial or "").strip()
    if want and not os.path.isdir(want):
        want = os.path.dirname(want) if os.path.isfile(want) else ""
    init = str(want).replace("'", "''")
    flt = str(filters or "压缩包|*.zip;*.7z;*.rar;*.tar;*.gz;*.001|所有文件|*.*")
    flt = flt.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$f=New-Object System.Windows.Forms.Form;"
        "$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.FormBorderStyle='None';"
        "$f.Size=New-Object System.Drawing.Size(1,1);"
        "$f.StartPosition='CenterScreen';$f.Show();$f.Activate();"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        f"$d.Title='{t}';"
        f"$d.Filter='{flt}';"
        f"if('{init}'){{$d.InitialDirectory='{init}';}};"
        f"$d.Multiselect=${'true' if multi else 'false'};"
        "if($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK)"
        "{$d.FileNames | ForEach-Object { Write-Output ('OK|'+$_) }}else{Write-Output 'CANCEL'};"
        "$f.Close();"
    )
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                           capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "选择框超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"打不开选择框：{exc}"}
    out = (p.stdout or "").strip().splitlines()
    paths = [ln.strip()[3:].strip() for ln in out if ln.strip().startswith("OK|")]
    paths = [p for p in paths if p]
    if paths and multi:
        return {"ok": True, "path": paths[0], "paths": paths}
    if paths:
        return {"ok": True, "path": paths[0]}
    return {"ok": False, "error": "已取消", "cancelled": True}


def ffmpeg_ok() -> bool:
    try:
        return mffmpeg.available()
    except Exception:
        return False


def media_engine_label(kind: str) -> str:
    if kind == "video":
        return "ffmpeg 抽帧 + MobileNetV2 画面特征"
    if kind == "audio":
        return "ffmpeg 解码 + 频谱指纹"
    return "书名相似度 + 章节数"

DEFAULT_SETTINGS = {
    "archive_mode": "app",     # app=软件目录 / images=图片文件夹 / custom=自定义
    "archive_path": "",
    "flush_rows": 500,         # 攒够多少张落一次库
    "flush_secs": 4.0,         # 或者最多等多少秒
    "engine": "auto",          # auto / gpu / cpu —— 由「重新测速.bat」写入推荐值
    "workers": 0,              # CPU 线程数，0=自动
    "load": 100,               # 占用百分比（5~100），降下来就是限速、降噪
    "bench": {},               # 上次测速结果
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
            got = json.load(f)
        if isinstance(got, dict):
            data.update({k: v for k, v in got.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return data


def save_settings(data: dict):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)


# ------------------------------------------------------------------ 文件系统

def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def safe_dir_list(path: str, want_files: bool = False) -> dict:
    path = os.path.abspath(path) if path else ""
    if not path or not os.path.isdir(path):
        drives = []
        for c in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{c}:\\"
            if os.path.exists(root):
                drives.append({"name": f"{c}: 盘", "path": root})
        home = os.path.expanduser("~")
        quick = []
        for label, sub in (("图片", "Pictures"), ("桌面", "Desktop"),
                           ("下载", "Downloads"), ("文档", "Documents")):
            p = os.path.join(home, sub)
            if os.path.isdir(p):
                quick.append({"name": label, "path": p})
        return {"path": "", "parent": "", "dirs": drives, "quick": quick,
                "images": 0, "files": []}

    dirs = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith("$"):
                        dirs.append({"name": e.name, "path": e.path})
                except OSError:
                    continue
    except OSError as exc:
        return {"error": str(exc)}
    dirs.sort(key=lambda d: d["name"].lower())

    files = []
    if want_files:
        # 只列存档文件（.json），按修改时间倒序，方便"上次导入的那个"排在前面
        try:
            with os.scandir(path) as it:
                for e in it:
                    if not e.name.lower().endswith(".json"):
                        continue
                    if e.name.lower() == "index.json":      # 存档目录自己的索引，不是存档
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    files.append({"name": e.name, "path": e.path,
                                  "size": int(st.st_size), "mtime": int(st.st_mtime)})
        except OSError:
            pass
        files.sort(key=lambda f: -f["mtime"])
        files = files[:300]

    parent = os.path.dirname(path.rstrip("\\/")) or path
    if parent == path:
        parent = ""
    return {"path": path, "parent": parent, "dirs": dirs[:500], "quick": [],
            "images": count_images(path, limit=20000), "files": files}


# ------------------------------------------------------------------ 回收站

def send_to_recycle_bin(paths: list[str]) -> tuple[list[str], list[dict]]:
    ok, failed = [], []
    if not paths:
        return ok, failed
    if os.name != "nt":
        dest = os.path.join(DATA, "trash", time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(dest, exist_ok=True)
        for p in paths:
            try:
                shutil.move(p, os.path.join(dest, os.path.basename(p)))
                ok.append(p)
            except Exception as exc:
                failed.append({"path": p, "error": str(exc)})
        return ok, failed

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION = 0x0004, 0x0010
    FOF_ALLOWUNDO, FOF_NOERRORUI = 0x0040, 0x0400

    exist = [p for p in paths if os.path.exists(p)]
    for p in paths:
        if p not in exist:
            failed.append({"path": p, "error": "文件已不存在"})
    if not exist:
        return ok, failed

    for start in range(0, len(exist), 500):
        chunk = exist[start:start + 500]
        buf = "\0".join(chunk) + "\0\0"
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = buf
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        try:
            rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        except Exception as exc:
            failed.extend({"path": p, "error": str(exc)} for p in chunk)
            continue
        if rc == 0 and not op.fAnyOperationsAborted:
            ok.extend(chunk)
        else:
            for p in chunk:
                op2 = SHFILEOPSTRUCTW()
                op2.hwnd = None
                op2.wFunc = FO_DELETE
                op2.pFrom = p + "\0\0"
                op2.pTo = None
                op2.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
                rc2 = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op2))
                if rc2 == 0 and not op2.fAnyOperationsAborted:
                    ok.append(p)
                else:
                    failed.append({"path": p, "error": f"系统返回码 {rc2}"})
    return ok, failed


def unique_dest(dest_dir: str, name: str) -> str:
    stem, ext = os.path.splitext(name)
    target = os.path.join(dest_dir, name)
    i = 1
    while os.path.exists(target):
        target = os.path.join(dest_dir, f"{stem}_{i}{ext}")
        i += 1
    return target


def move_files(paths: list[str], dest_dir: str) -> tuple[list[str], list[dict]]:
    ok, failed = [], []
    os.makedirs(dest_dir, exist_ok=True)
    for p in paths:
        try:
            if not os.path.exists(p):
                failed.append({"path": p, "error": "文件已不存在"})
                continue
            shutil.move(p, unique_dest(dest_dir, os.path.basename(p)))
            ok.append(p)
        except Exception as exc:
            failed.append({"path": p, "error": str(exc)})
    return ok, failed


def reveal_in_explorer(path: str) -> bool:
    try:
        if os.name == "nt":
            subprocess.Popen(f'explorer /select,"{path}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ 应用状态

class App:
    def __init__(self, data_dir: str = DATA, engine_mode: str = "auto"):
        self.data_dir = data_dir
        self.thumb_dir = os.path.join(data_dir, "thumbs")
        self.preview_dir = os.path.join(data_dir, "previews")
        os.makedirs(self.thumb_dir, exist_ok=True)
        os.makedirs(self.preview_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "library.db"))
        self.lock = threading.RLock()
        self.job: ScanJob | None = None
        self.engine = None
        self.engine_mode = engine_mode
        self.engine_lock = threading.Lock()
        self._idx = None            # (rev, root) -> numpy 索引
        self._cluster = None        # (rev, root, threshold, recall) -> 聚类结果
        self.log_path = os.path.join(data_dir, "actions.log")
        self.settings = load_settings()
        self._queue: list[dict] = []
        self._job_key = ()
        self._verify = {"running": False, "total": 0, "done": 0, "removed": 0, "started": 0}
        # 老库升级后缺 16×16 灰度指纹 → 后台补（可打断，下次启动接着补）
        # 灰度指纹不再在启动时补：老库缺多少不耽误用，等真正要看分组时再补，
        # 免得一开机后台就跑满 CPU（用户反馈过这一条）。
        self.patch_fill = {"running": False, "done": 0, "missing": 0}
        self._patch_thread: threading.Thread | None = None
        # ---------------- 多类型媒体（视频 / 音频 / 小说）
        self.media = MediaStore(os.path.join(data_dir, "library.db"))
        self.media_job = None
        self._media_cache: dict = {}
        self._media_lock = threading.RLock()
        self._check_hash_version()
        threading.Thread(target=self._pump, daemon=True, name="scan-queue").start()

    # ------------------------------------------------ 媒体：视频/音频/小说
    def media_roots(self, kind: str) -> dict:
        return {"kind": kind, "roots": self.media.list_roots(kind),
                "pending": self.media.pending_roots(kind),
                "stats": self.media.stats(kind),
                "ffmpeg": ffmpeg_ok()}

    def media_add_root(self, kind: str, path: str, recursive: bool = True) -> dict:
        if kind not in ("video", "audio", "novel"):
            return {"ok": False, "error": "类型不对"}
        if not str(path or "").strip():
            return {"ok": False, "error": "请填写文件夹路径"}
        p = self.norm(path)
        if not os.path.isdir(p):
            return {"ok": False, "error": f"目录不存在：{p}"}
        self.media.add_root(kind, p, recursive)
        return {"ok": True, "root": p}

    def media_remove_root(self, kind: str, path: str) -> dict:
        n = self.media.remove_root(kind, self.norm(path))
        self._media_cache.clear()
        return {"ok": True, "removed": n}

    def media_start(self, kind: str, paths: list[str] | None = None,
                    recursive: bool | None = None,
                    include_video: bool = False) -> dict:
        if kind not in ("video", "audio", "novel"):
            return {"ok": False, "error": "类型不对"}
        known = {r["path"]: r for r in self.media.list_roots(kind)}
        if paths:
            for p in paths:
                ap = self.norm(p)
                if not os.path.isdir(ap):
                    return {"ok": False, "error": f"目录不存在：{ap}"}
                if ap not in known:
                    self.media.add_root(kind, ap, recursive if recursive is not None else True)
            roots = [{"path": self.norm(p),
                      "recursive": recursive if recursive is not None else
                      bool(known.get(self.norm(p), {}).get("recursive", 1))} for p in paths]
        else:
            roots = [{"path": p, "recursive": bool(r.get("recursive", 1))}
                     for p, r in known.items()]
        if not roots:
            return {"ok": False, "error": "还没有添加文件夹"}
        with self._media_lock:
            if self.media_job is not None and self.media_job.is_alive():
                return {"ok": True, "queued": True, "position": 2,
                        "message": "已有扫描任务在跑，等它结束后再点一次"}
            self.media_job = MediaScanJob(
                self.media, kind, roots,
                workers=4 if kind != "video" else 3,
                flush_rows=30, flush_secs=2.0,
                skip_dirs={self.data_dir, self.archive_dir()},
                include_video=include_video)
            self.media_job.start()
        self._media_cache.clear()
        return {"ok": True, "kind": kind, "roots": roots,
                "engine": media_engine_label(kind)}

    # ------------------------------------------------ 独立暴力破解
    def brute_start(self, src: str, passwords=None, brute=None, recursive: bool = True,
                    dict_files=None) -> dict:
        passwords = self._merge_dicts(passwords, dict_files)
        if not str(src or "").strip():
            return {"ok": False, "error": "请先选目标文件夹或压缩包"}
        a = self.norm(src)
        if not os.path.exists(a):
            return {"ok": False, "error": f"路径不存在：{a}"}
        with self._media_lock:
            job = getattr(self, "brute_job", None)
            if job is not None and job.is_alive():
                return {"ok": False, "error": "已经在破解了，先等它结束"}
            pws = [str(x).strip() for x in (passwords or []) if str(x).strip()]
            self.brute_job = BruteJob(a, pws, brute or {}, recursive)
            self.brute_job.start()
        return {"ok": True, "src": a, "passwords": len(pws)}

    def brute_status(self) -> dict:
        with self._media_lock:
            job = getattr(self, "brute_job", None)
        if job is None:
            return {"running": False, "state": "idle", "total": 0, "done": 0,
                    "found": 0, "failed": 0, "results": [], "log": [],
                    "message": "还没有开始"}
        return job.snapshot()

    def brute_pause(self) -> dict:
        with self._media_lock:
            job = getattr(self, "brute_job", None)
        if job is None or not job.is_alive():
            return {"ok": False, "error": "没有正在跑的破解"}
        job.pause()
        return {"ok": True, "paused": True}

    def brute_resume(self) -> dict:
        with self._media_lock:
            job = getattr(self, "brute_job", None)
        if job is None or not job.is_alive():
            return {"ok": False, "error": "没有正在跑的破解"}
        job.resume()
        return {"ok": True, "paused": False}

    def brute_cancel(self) -> dict:
        with self._media_lock:
            job = getattr(self, "brute_job", None)
        if job:
            job.cancel()
        return {"ok": True}

    # ------------------------------------------------ 字典
    def _merge_dicts(self, passwords, dict_files) -> list:
        """把选中的字典文件读出来，拼到密码本前面（去重）。"""
        words = [str(x) for x in (passwords or []) if str(x or "").strip()]
        if not dict_files:
            return words
        try:
            paths = mwords.resolve(dict_files)
            if not paths:
                return words
            r = mwords.load_many(paths, limit=1_000_000)
            seen = set(words)
            extra = [w for w in r["words"] if w not in seen]
            return words + extra
        except Exception:  # noqa: BLE001
            return words

    # ------------------------------------------------ 打包流程（可编辑步骤）
    def pack_start(self, src: str, dst: str, steps=None,
                   mode: str = "single") -> dict:
        a, b = self.norm(src), self.norm(dst)
        if not a or not b:
            return {"ok": False, "error": "请先选好输入和输出文件夹"}
        if not os.path.exists(a):
            return {"ok": False, "error": f"输入路径不存在：{a}"}
        if os.path.abspath(a) == os.path.abspath(b):
            return {"ok": False, "error": "输出文件夹不能和输入一样"}
        norm = mpipeline.normalize_steps(steps)
        err = mpipeline.check_steps(norm)
        if err:
            return {"ok": False, "error": err}
        with self._media_lock:
            job = getattr(self, "pack_job", None)
            if job is not None and job.is_alive():
                return {"ok": False, "error": "已经在跑了，先等它结束"}
            self.pack_job = mpipeline.PipelineJob(a, b, norm, mode)
            self.pack_job.start()
        return {"ok": True, "src": a, "dst": b, "steps": norm, "mode": mode,
                "describe": mpipeline.describe(norm)}

    def pack_status(self) -> dict:
        job = getattr(self, "pack_job", None)
        meta = {"encodings": PACK_ENCS, "step_types": mpipeline.STEP_LABELS,
                "formats": mpack.formats_available(), "levels": mpack.LEVELS}
        if job is None:
            st = {"running": False, "state": "idle", "total": 0, "done": 0,
                  "log": [], "message": "还没有开始"}
            st.update(meta)
            return st
        s = job.snapshot()
        s.update(meta)
        return s

    def pack_check(self, steps=None) -> dict:
        norm = mpipeline.normalize_steps(steps)
        return {"ok": True, "steps": norm,
                "error": mpipeline.check_steps(norm),
                "describe": mpipeline.describe(norm)}

    def pack_cancel(self) -> dict:
        job = getattr(self, "pack_job", None)
        if job is not None:
            job.cancel()
        return {"ok": True}

    # ------------------------------------------------ 繁简 / 编码转换
    def conv_start(self, src: str, dst: str, *, t2s: bool = True,
                   out_enc: str = "utf-8", newline: str = "keep",
                   recursive: bool = True, overwrite: bool = False) -> dict:
        a, b = self.norm(src), self.norm(dst)
        if not a or not b:
            return {"ok": False, "error": "请先选好输入和输出文件夹"}
        if not os.path.exists(a):
            return {"ok": False, "error": f"输入路径不存在：{a}"}
        if os.path.abspath(a) == os.path.abspath(b):
            return {"ok": False, "error": "输出文件夹不能和输入一样（会覆盖原文件）"}
        with self._media_lock:
            job = getattr(self, "conv_job", None)
            if job is not None and job.is_alive():
                return {"ok": False, "error": "已经在转换了，先等它结束"}
            self.conv_job = ConvJob(a, b, t2s_on=t2s, out_enc=out_enc, newline=newline,
                                    recursive=recursive, overwrite=overwrite)
            self.conv_job.start()
        return {"ok": True, "src": a, "dst": b, "out_enc": out_enc, "t2s": t2s}

    def conv_status(self) -> dict:
        job = getattr(self, "conv_job", None)
        if job is None:
            return {"running": False, "state": "idle", "total": 0, "done": 0,
                    "log": [], "message": "还没有开始"}
        return job.snapshot()

    def conv_cancel(self) -> dict:
        job = getattr(self, "conv_job", None)
        if job is not None:
            job.cancel()
        return {"ok": True}

    def conv_preview(self, text: str) -> dict:
        """把一段文字按当前选项转一遍，返回结果给界面看效果。"""
        try:
            out = mconv.t2s(text) if text else ""
            st = mconv.dict_status()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "before": text, "after": out,
                "dict": st, "encodings": {k: v[0] for k, v in OUT_ENCODINGS.items()}}

    def dicts_state(self) -> dict:
        try:
            local = mwords.list_local()
            cat = mwords.list_catalog()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "dir": mwords.DICT_DIR, "local": local, "catalog": cat}

    def dicts_download(self, ids: "list[str]") -> dict:
        ids = [str(i) for i in (ids or []) if i]
        if not ids:
            return {"ok": False, "error": "没选要下载的字典"}
        with self._media_lock:
            job = getattr(self, "dict_job", None)
            if job is not None and getattr(job, "state", "") == "running":
                return {"ok": False, "error": "已经在下载了，等它下完"}
            import threading
            cancel = threading.Event()
            self.dict_cancel = cancel
            job = mwords.DownloadJob(ids)
            self.dict_job = job

            def work():
                try:
                    job.run(cancel=cancel)
                except Exception as exc:  # noqa: BLE001
                    job.log.append(f"[出错] {exc}")
                    job.state = "error"

            threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "ids": ids}

    def dicts_status(self) -> dict:
        job = getattr(self, "dict_job", None)
        if job is None:
            return {"ok": True, "state": "idle", "log": [], "items": []}
        return {"ok": True, "state": job.state, "log": list(job.log)[-40:],
                "items": list(job.items)}

    def dicts_import(self, paths: "list[str]") -> dict:
        """把外部字典拷进 dicts\\，以后直接在下拉里选。"""
        out = {"ok": True, "copied": [], "skipped": [], "error": ""}
        dst_dir = mwords.ensure_dir()
        for p in paths or []:
            src = os.path.abspath(str(p or ""))
            if not os.path.isfile(src):
                continue
            name = os.path.basename(src)
            dst = os.path.join(dst_dir, name)
            if os.path.abspath(dst) == src:
                out["skipped"].append(name)
                continue
            if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                out["skipped"].append(name)
                continue
            try:
                shutil.copyfile(src, dst)
                out["copied"].append(name)
            except OSError as exc:
                out.setdefault("errors", []).append(f"{name}: {exc}")
        return out

    def dicts_delete(self, names: "list[str]") -> dict:
        dst_dir = mwords.ensure_dir()
        done = []
        for n in names or []:
            p = os.path.join(dst_dir, os.path.basename(str(n or "")))
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    done.append(os.path.basename(p))
                except OSError:
                    pass
        return {"ok": True, "deleted": done}

    def dicts_load(self, entries: "list[str]", limit: int = 300_000) -> dict:
        """按名字/路径加载字典，返回预览（条数与样本）。"""
        paths = mwords.resolve(entries)
        if not paths:
            return {"ok": False, "error": "没找到这些字典"}
        r = mwords.load_many(paths, limit=limit)
        return {"ok": True, "total": r["total"], "files": [os.path.basename(x) for x in r["files"]],
                "sample": r["words"][:10], "errors": r["errors"],
                "truncated": r["total"] >= limit}

    # ------------------------------------------------ 已知明文攻击（bkcrack）
    def plain_start(self, zip_path: str, plain: bytes, offset: int = 0,
                    recover_password: bool = True, charset: str = "?d",
                    max_len: int = 8, auto: bool = False) -> dict:
        a = self.norm(zip_path)
        if not os.path.isfile(a):
            return {"ok": False, "error": f"压缩包不存在：{a}"}
        if not auto and (not plain or len(plain) < 12):
            return {"ok": False, "error": "已知明文至少要 12 字节（或者勾上「自动猜文件头」）"}
        with self._media_lock:
            job = getattr(self, "plain_job", None)
            if job is not None and job.is_alive():
                return {"ok": False, "error": "已经在跑明文攻击了，先等它结束"}
            self.plain_job = PlainJob(a, plain, offset, recover_password, charset,
                                      max_len, auto)
            self.plain_job.start()
        return {"ok": True, "zip": a, "plain_bytes": len(plain)}

    def plain_status(self) -> dict:
        with self._media_lock:
            job = getattr(self, "plain_job", None)
        if job is None:
            return {"running": False, "state": "idle", "total": 0, "done": 0,
                    "log": [], "result": {}, "message": "还没有开始"}
        return job.snapshot()

    def plain_cancel(self) -> dict:
        with self._media_lock:
            job = getattr(self, "plain_job", None)
        if job:
            job.cancel()
        return {"ok": True}

    # ------------------------------------------------ 解压（内存中逐层展开）
    def unzip_start(self, src: str, dst: str, passwords=None, brute=None,
                    recursive: bool = True, auto_brute: bool = True,
                    dict_files=None) -> dict:
        passwords = self._merge_dicts(passwords, dict_files)
        if not str(src or "").strip() or not str(dst or "").strip():
            return {"ok": False, "error": "请先选好输入和输出文件夹"}
        a, b = self.norm(src), self.norm(dst)
        if not os.path.isdir(a):
            return {"ok": False, "error": f"输入文件夹不存在：{a}"}
        if os.path.normcase(a) == os.path.normcase(b):
            return {"ok": False, "error": "输出文件夹不能和输入相同"}
        with self._media_lock:
            job = getattr(self, "unzip_job", None)
            if job is not None and job.is_alive():
                return {"ok": False, "error": "已经在解压了，先等它结束"}
            try:
                os.makedirs(b, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"建不了输出文件夹：{exc}"}
            pws = [str(x).strip() for x in (passwords or []) if str(x).strip()]
            self.unzip_job = UnzipJob(a, b, pws, brute or None, recursive,
                                      auto_brute=auto_brute)
            self.unzip_job.start()
        return {"ok": True, "src": a, "dst": b, "passwords": len(pws)}

    def unzip_status(self) -> dict:
        with self._media_lock:
            job = getattr(self, "unzip_job", None)
        if job is None:
            return {"running": False, "state": "idle", "total": 0, "done": 0,
                    "archives": 0, "files": 0, "bytes": 0, "failed": 0,
                    "current": "", "message": "还没有开始", "log": []}
        return job.snapshot()

    def unzip_cancel(self) -> dict:
        with self._media_lock:
            job = getattr(self, "unzip_job", None)
        if job:
            job.cancel()
        return {"ok": True}

    # ------------------------------------------------ 小说广告过滤
    def clean_start(self, src: str, dst: str, recursive: bool = True) -> dict:
        if not str(src or "").strip() or not str(dst or "").strip():
            return {"ok": False, "error": "请先选好输入和输出文件夹"}
        a = self.norm(src)
        b = self.norm(dst)
        if not os.path.isdir(a):
            return {"ok": False, "error": f"输入文件夹不存在：{a}"}
        if os.path.normcase(a) == os.path.normcase(b):
            return {"ok": False, "error": "输出文件夹不能和输入相同"}
        with self._media_lock:
            job = getattr(self, "clean_job", None)
            if job is not None and job.is_alive():
                return {"ok": False, "error": "已经在过滤了，先等它结束"}
            try:
                os.makedirs(b, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"建不了输出文件夹：{exc}"}
            self.clean_job = CleanJob(a, b, recursive)
            self.clean_job.start()
        return {"ok": True, "src": a, "dst": b}

    def clean_status(self) -> dict:
        with self._media_lock:
            job = getattr(self, "clean_job", None)
        if job is None:
            return {"running": False, "state": "idle", "total": 0, "done": 0,
                    "written": 0, "skipped": 0, "errors": 0, "removed_lines": 0,
                    "saved_bytes": 0, "current": "", "message": "还没有开始"}
        return job.snapshot()

    def clean_cancel(self) -> dict:
        with self._media_lock:
            job = getattr(self, "clean_job", None)
        if job:
            job.cancel()
        return {"ok": True}

    def media_status(self) -> dict:
        with self._media_lock:
            job = self.media_job
        base = {"running": False, "state": "idle", "kind": "", "total": 0, "done": 0,
                "hashed": 0, "reused": 0, "errors": 0, "elapsed": 0, "rate": 0,
                "message": "尚未扫描", "current": "", "root": "", "writes": 0,
                "stats": {k: self.media.stats(k) for k in ("video", "audio", "novel")}}
        if job is None:
            return base
        s = job.snapshot()
        s["stats"] = base["stats"]
        return s

    def media_pause(self) -> dict:
        if self.media_job:
            self.media_job.pause()
        return {"ok": True}

    def media_resume(self) -> dict:
        if self.media_job:
            self.media_job.resume()
        return {"ok": True}

    def media_cancel(self) -> dict:
        if self.media_job:
            self.media_job.cancel()
        return {"ok": True}

    def media_groups(self, kind: str, threshold: float | None = None,
                     min_size: int = 2, limit: int = 40, offset: int = 0,
                     root: str = "", quick: bool = False, content: bool = True) -> dict:
        t0 = time.time()
        items = self.media.all_items(kind, root)
        default = {"video": config.VIDEO_THRESHOLD, "audio": config.AUDIO_THRESHOLD,
                   "novel": config.NOVEL_NAME_THRESHOLD}.get(kind, 0.5)
        th = float(threshold if threshold is not None else default)
        key = (kind, root, th, len(items), quick, content)
        with self._media_lock:
            cached = None if quick else self._media_cache.get(kind)
        if cached and cached.get("key") == key:
            res = cached["res"]
        else:
            kw = {"threshold": th, "quick": quick, "min_size": min_size}
            if kind == "novel":                 # 只有小说有"正文比对"开关
                kw["content"] = content
            res = mgrouping.build(kind, items, **kw)
            if not quick:
                with self._media_lock:
                    self._media_cache[kind] = {"key": key, "res": res}
        by_id = {it["id"]: it for it in items}
        groups = []
        for gi, g in enumerate(res["groups"], 1):
            rep = by_id.get(g["rep"])
            if not rep:
                continue
            members = [by_id[m["id"]] for m in g["members"] if m["id"] in by_id]
            if len(members) + 1 < min_size:
                continue
            groups.append({
                "no": gi,
                "rep": self._media_row(rep, 1.0, True),
                "members": [dict(self._media_row(by_id[m["id"]], m["sim"], False),
                                 by_content=bool(m.get("by_content")),
                                 content_sim=m.get("content_sim"))
                            for m in g["members"]],
                "count": len(members) + 1,
                "max_sim": max([m["sim"] for m in g["members"]] or [1.0]),
                "min_sim": min([m["sim"] for m in g["members"]] or [1.0]),
                "reclaim": sum(int(m["size"] or 0) for m in members),
            })
        page = groups[offset:offset + limit]
        if kind == "novel" and page:
            self._add_chapter_overlap(page)
        total_bytes = sum(int(it.get("size") or 0) for it in items)
        return {
            "kind": kind, "threshold": th,
            "groups": page,
            "stats": {
                "items": len(items), "groups": len(groups),
                "duplicates": sum(g["count"] - 1 for g in groups),
                "reclaim": sum(g["reclaim"] for g in groups),
                "bytes": total_bytes,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "pairs": res.get("pairs", 0),
            },
            "page": {"offset": offset, "limit": limit, "total_groups": len(groups),
                     "has_more": offset + limit < len(groups)},
        }

    def _media_row(self, it: dict, sim: float, is_rep: bool) -> dict:
        meta = it.get("meta") or {}
        return {
            "id": it["id"], "path": it["path"], "root": it.get("root", ""),
            "name": it["name"], "size": it.get("size", 0), "mtime": it.get("mtime", 0),
            "dur": it.get("dur", 0),
            "dur_txt": meta.get("duration_txt") or "",
            "w": it.get("w", 0), "h": it.get("h", 0),
            "meta": meta, "sim": round(sim * 100, 1), "rep": is_rep,
            "has_thumb": bool(it.get("thumb")) if it.get("thumb") is not None else None,
        }

    def media_text(self, mid: int, limit: int = 6000) -> dict:
        """小说预览：读前面一段正文（不含广告行）。"""
        row = self.media.get(int(mid))
        if not row:
            return {"ok": False, "error": "没有这条记录"}
        if row.get("kind") != "novel":
            return {"ok": False, "error": "只有小说支持文本预览"}
        try:
            from media.clean import strip_ads
            from media.novel import read_text
            raw, src = read_text(row["path"])
            text, removed, total = strip_ads(raw)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读不了这个文件：{exc}"}
        limit = max(500, min(60000, int(limit)))
        return {"ok": True, "name": row["name"], "path": row["path"],
                "enc": src, "removed_lines": removed, "total_lines": total,
                "chars": len(text), "text": text[:limit],
                "truncated": len(text) > limit,
                "chapters": (row.get("meta") or {}).get("chapters", 0),
                "words": (row.get("meta") or {}).get("words", 0)}

    def media_thumb(self, mid: int) -> bytes | None:
        with self.media._lock:
            r = self.media._conn.execute(
                "SELECT thumb FROM media_items WHERE id=?", (mid,)).fetchone()
        return r["thumb"] if r and r["thumb"] else None

    _wave_cache: dict = {}

    def media_waveform(self, mid: int, buckets: int = 900) -> dict:
        """音频波形（给预览弹窗画），按 id+mtime+桶数缓存，最多留 8 条。"""
        rec = self.media.get(int(mid)) if mid else None
        if not rec:
            return {"ok": False, "error": "条目不存在"}
        p = rec.get("path") or ""
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": "文件不存在"}
        key = (int(mid), float(rec.get("mtime") or 0), int(buckets))
        hit = self._wave_cache.get(key)
        if hit is None:
            from media import ffmpeg as mffmpeg
            try:
                got = mffmpeg.waveform(p, buckets=buckets)
            except Exception as exc:                    # noqa: BLE001
                return {"ok": False, "error": f"解析失败：{exc}"}
            if not got:
                return {"ok": False, "error": "解不出音频（可能没有音轨）"}
            if len(self._wave_cache) >= 8:
                self._wave_cache.pop(next(iter(self._wave_cache)))
            hit = self._wave_cache[key] = got
        peaks, secs = hit
        return {"ok": True, "id": int(mid), "peaks": peaks, "duration": secs,
                "buckets": len(peaks) // 2}

    def media_file(self, mid: int) -> tuple[str, str] | None:
        """取媒体条目的真实文件路径和 MIME（预览用，只认库里的 id，不认任意路径）。"""
        rec = self.media.get(int(mid)) if mid else None
        if not rec:
            return None
        p = rec.get("path") or ""
        if not p or not os.path.isfile(p):
            return None
        ext = os.path.splitext(p)[1].lower()
        mime = MIME.get(ext) or "application/octet-stream"
        return p, mime

    def media_other(self, kind: str) -> dict:
        """查重没纳进来的文件（太小 / 打不开 / 后缀不对），给前端"其他文件"用。"""
        rows = self.media.other_items(kind)
        return {"ok": True, "kind": kind, "items": rows}

    def images_other(self) -> dict:
        """图片里分析失败的（损坏、后缀不匹配…）。"""
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT id, path, root, name, size, err FROM images "
                "WHERE status='error' ORDER BY size DESC LIMIT 500").fetchall()
        return {"ok": True, "kind": "image", "items": [dict(r) for r in rows]}

    def other_count(self) -> dict:
        out = {"image": 0}
        try:
            with self.store._lock:
                out["image"] = int(self.store._conn.execute(
                    "SELECT COUNT(*) FROM images WHERE status='error'").fetchone()[0])
        except Exception:                              # noqa: BLE001
            pass
        for k in ("video", "audio", "novel"):
            try:
                out[k] = self.media.count_other(k)
            except Exception:                          # noqa: BLE001
                out[k] = 0
        return out

    def open_path(self, path: str) -> dict:
        """用系统默认程序打开（视频编码浏览器放不了时的兜底）。"""
        p = self.norm(path) if str(path or "").strip() else ""
        if not p or not os.path.exists(p):
            return {"ok": False, "error": "文件不存在"}
        try:
            os.startfile(p)                             # noqa: S606  (Windows)
            return {"ok": True}
        except Exception as exc:                        # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def media_thumb_or_make(self, mid: int) -> bytes | None:
        """缩略图：先用库里的；没有就现抽一帧（太小的视频、动图也能有图）。"""
        data = self.media_thumb(mid)
        if data:
            return data
        rec = self.media.get(int(mid)) if mid else None
        if not rec:
            return None
        p = rec.get("path") or ""
        if rec.get("kind") != "video" or not os.path.isfile(p):
            return None
        try:
            from media import ffmpeg as mffmpeg
            data = mffmpeg.thumbnail(p, 320, 0.0)
        except Exception:                              # noqa: BLE001
            return None
        if data:
            try:
                self.media.set_thumb(int(mid), data)
            except Exception:                          # noqa: BLE001
                pass
        return data

    def media_remove(self, ids: list[int], mode: str, dest: str) -> dict:
        recs = []
        for i in ids:
            r = self.media.get(int(i))
            if r:
                recs.append(r)
        if not recs:
            return {"ok": False, "error": "没有找到这些条目"}
        paths = [r["path"] for r in recs]
        if mode == "move":
            target = dest or os.path.join(os.path.dirname(recs[0]["root"]), "重复媒体")
            os.makedirs(target, exist_ok=True)
            moved, failed = [], []
            for r in recs:
                try:
                    dst = os.path.join(target, os.path.basename(r["path"]))
                    base, ext = os.path.splitext(dst)
                    k = 1
                    while os.path.exists(dst):
                        dst = f"{base}_{k}{ext}"
                        k += 1
                    shutil.move(r["path"], dst)
                    moved.append(r)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"path": r["path"], "error": str(exc)})
            freed = sum(int(r["size"] or 0) for r in moved)
            ok_ids = [r["id"] for r in moved]
        else:
            ok_paths, failed = send_to_recycle_bin(paths)
            freed = sum(int(r["size"] or 0) for r in recs if r["path"] in set(ok_paths))
            ok_ids = [r["id"] for r in recs if r["path"] in set(ok_paths)]
        with self.media._lock:
            if ok_ids:
                self.media._conn.executemany(
                    "UPDATE media_items SET status='removed' WHERE id=?", [(i,) for i in ok_ids])
                self.media._conn.commit()
        self._media_cache.clear()
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{mode}\t{len(ok_ids)}\t{freed}\n")
        except Exception:
            pass
        return {"ok": True, "removed": len(ok_ids), "freed": freed, "failed": failed}

    def media_autoselect(self, kind: str, keep: str = "best", min_size: int = 2,
                         threshold: float | None = None, root: str = "") -> dict:
        res = self.media_groups(kind, threshold=threshold, min_size=min_size,
                                limit=1000000, offset=0, root=root)
        ids = []
        for g in res["groups"]:
            members = g["members"]
            if not members:
                continue
            if keep == "newest":
                keep_id = max([g["rep"]] + members, key=lambda x: x["mtime"] or 0)["id"]
            elif keep == "largest":
                keep_id = max([g["rep"]] + members, key=lambda x: x["size"] or 0)["id"]
            elif keep == "best":
                keep_id = g["rep"]["id"]
            else:
                keep_id = g["rep"]["id"]
            for m in members:
                if m["id"] != keep_id:
                    ids.append(m["id"])
        return {"ok": True, "ids": ids, "count": len(ids),
                "bytes": sum(int(x) for x in []) or 0, "groups": len(res["groups"])}

    def media_export_csv(self, kind: str, threshold: float | None = None,
                         min_size: int = 2) -> str:
        res = self.media_groups(kind, threshold=threshold, min_size=min_size,
                                limit=1000000, offset=0)
        lines = ["组号,建议,文件名,大小,时长,分辨率/章节,相似度,路径"]
        for g in res["groups"]:
            rep = g["rep"]
            lines.append(f"{g['no']},保留,{rep['name']},{rep['size']},{rep['dur_txt']},"
                         f"{self._media_desc(kind, rep)},100,{rep['path']}")
            for m in g["members"]:
                lines.append(f"{g['no']},重复,{m['name']},{m['size']},{m['dur_txt']},"
                             f"{self._media_desc(kind, m)},{m['sim']},{m['path']}")
        return "\ufeff" + "\r\n".join(lines)

    def _media_desc(self, kind: str, row: dict) -> str:
        meta = row.get("meta") or {}
        if kind == "novel":
            return f"{meta.get('chapters', 0)}章 {meta.get('words', 0)}字"
        if kind == "audio":
            return f"{row.get('dur_txt', '')} {meta.get('bitrate', 0)}kbps"
        return f"{row.get('w', 0)}x{row.get('h', 0)}"

    # ------------------------------------------------ 设置 / 存档目录
    def archive_dir(self) -> str:
        """存档统一放软件目录下（data\archives），不跟设置走，避免迁移后找不到。"""
        d = os.path.join(self.data_dir, "archives")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    def settings_view(self) -> dict:
        with self.lock:
            s = dict(self.settings)
        return {
            "settings": s,
            "load": int(s.get("load") or 100),
            "archive_dir": self.archive_dir(),
            "default_archive_dir": os.path.join(self.data_dir, "archives"),
            "data_dir": self.data_dir,
            "roots": [r["path"] for r in self.store.list_roots()],
        }

    def update_settings(self, payload: dict) -> dict:
        with self.lock:
            s = dict(self.settings)
            mode = payload.get("archive_mode")
            if mode in ("app", "images", "custom"):
                s["archive_mode"] = mode
            if "archive_path" in payload:
                s["archive_path"] = str(payload.get("archive_path") or "").strip()
            if "flush_rows" in payload:
                try:
                    s["flush_rows"] = max(20, min(200000, int(payload["flush_rows"])))
                except Exception:
                    pass
            if "flush_secs" in payload:
                try:
                    s["flush_secs"] = max(0.5, min(600.0, float(payload["flush_secs"])))
                except Exception:
                    pass
            if payload.get("engine") in ("auto", "gpu", "cpu"):
                s["engine"] = payload["engine"]
            if "workers" in payload:
                try:
                    s["workers"] = max(0, min(128, int(payload["workers"])))
                except Exception:
                    pass
            if "load" in payload:
                try:
                    s["load"] = max(5, min(100, int(payload["load"])))
                except Exception:
                    pass
            changed_engine = (s.get("load") != self.settings.get("load")
                              or s.get("workers") != self.settings.get("workers")
                              or s.get("engine") != self.settings.get("engine"))
            self.settings = s
            save_settings(s)
            if changed_engine:
                with self.engine_lock:
                    self.engine = None          # 下次扫描按新设置重建
        return {"ok": True, **self.settings_view()}

    def _check_hash_version(self):
        cur = self.store.get_meta("hash_version")
        want = str(HASH_VERSION)
        if cur != want:
            if cur is not None:
                self.store.clear_images()
                self.clear_caches()
            self.store.set_meta("hash_version", want)

    @property
    def gpu_info(self) -> dict:
        return pipeline.INFO.as_dict()

    def _sample_paths(self, k: int = 3):
        return self.store.sample_paths(k)

    def _add_chapter_overlap(self, page: list[dict]) -> None:
        """小说页：给每个成员算"和代表本重合多少章"（只算当前页，按 mtime 缓存）。"""
        from media import novel as _novel
        cache = getattr(self, "_chap_cache", None)
        if cache is None:
            cache = self._chap_cache = {}
        for g in page:
            rep = g.get("rep") or {}
            rp = rep.get("path")
            if not rp:
                continue
            for m in g.get("members") or []:
                mp = m.get("path")
                if not mp:
                    continue
                try:
                    key = (rp, os.path.getmtime(rp), mp, os.path.getmtime(mp))
                except OSError:
                    continue
                info = cache.get(key)
                if info is None:
                    try:
                        info = _novel.chapter_overlap(rp, mp)
                    except Exception:  # noqa: BLE001
                        info = {}
                    if len(cache) > 600:
                        cache.clear()
                    cache[key] = info
                if info:
                    m["chap"] = info

    def _quick_count(self, roots: list[str], limit: int = 600) -> int:
        """快速数一下要扫多少张（够 600 就停），用来判断该不该动用 GPU。"""
        from dedupe.scanner import iter_image_files
        n = 0
        for r in roots:
            if not os.path.isdir(r):
                continue
            try:
                for _ in iter_image_files(r, True, {self.data_dir, self.archive_dir()}):
                    n += 1
                    if n >= limit:
                        return n
            except Exception:
                continue
        return n

    def ensure_engine(self, sample_roots: list[str] | None = None, size_hint: int = 0):
        with self.engine_lock:
            if self.engine is not None:
                return self.engine
            mode = self.engine_mode
            if mode == "auto":
                pref = str(self.settings.get("engine") or "auto")
                if pref in ("gpu", "cpu"):
                    mode = pref          # 测速脚本给的推荐值
            try:
                workers = int(self.settings.get("workers") or 0) or None
            except Exception:
                workers = None
            try:
                load = int(self.settings.get("load") or 100)
            except Exception:
                load = 100

            # 已经跑过「重新测速」：直接用存下来的结论，不再每次重新比
            bench = self.settings.get("bench") or {}
            if mode == "auto" and bench.get("at"):
                saved = str(bench.get("recommended") or "")
                if saved in ("gpu", "cpu"):
                    mode = saved
                    try:
                        workers = int(bench.get("recommended_workers") or workers or 0) or None
                    except Exception:
                        pass
            # 小批量：不加载 torch、不做对比测试，直接用 CPU（省 10 秒启动和 2 GB 内存）
            if size_hint and size_hint < SMALL_LIB and mode != "gpu":
                self.engine = pipeline.make_engine("cpu", workers=workers, load=load)
                pipeline.INFO.reason = f"小批量（<{SMALL_LIB} 张）：直接用 CPU，不加载 GPU"
                return self.engine

            sample = self._sample_paths(3)
            if not sample and sample_roots:
                sample = self._first_files(sample_roots, 3)
            self.engine = pipeline.make_engine(mode, workers=workers,
                                               sample_paths=sample, load=load)
            return self.engine

    # ------------------------------------------------ 文件夹工作区
    def norm(self, p: str) -> str:
        return os.path.abspath(os.path.expanduser(str(p or "").strip().strip('"')))

    def add_root(self, path: str, recursive: bool = True) -> dict:
        if not str(path or "").strip():
            return {"ok": False, "error": "请填写文件夹路径"}
        p = self.norm(path)
        if not os.path.isdir(p):
            return {"ok": False, "error": f"目录不存在：{p}"}
        self.store.add_root(p, recursive)
        self.invalidate()
        return {"ok": True, "root": p}

    def remove_root(self, path: str) -> dict:
        p = self.norm(path)
        n = self.store.remove_root(p)
        self.invalidate()
        self.store.bump_rev()
        return {"ok": True, "removed": n}

    def roots(self) -> dict:
        return {"roots": self.store.list_roots(), "pending": self.store.pending_roots()}

    # ------------------------------------------------ 扫描
    def _build_tasks(self, paths: list[str] | None, recursive: bool | None) -> tuple[list, str]:
        known = {r["path"]: r for r in self.store.list_roots()}
        tasks = []
        if paths:
            for p in paths:
                ap = self.norm(p)
                if not os.path.isdir(ap):
                    return [], f"目录不存在：{ap}"
                if ap not in known:
                    self.store.add_root(ap, recursive if recursive is not None else True)
                r = recursive if recursive is not None else bool(known.get(ap, {}).get("recursive", True))
                tasks.append({"path": ap, "recursive": bool(r)})
        else:
            for ap, r in known.items():
                tasks.append({"path": ap, "recursive": bool(r.get("recursive", 1))})
        return tasks, ""

    def _launch(self, tasks: list) -> dict:
        # 先估个规模：小批量就别去加载 torch 了
        size_hint = self._quick_count([t["path"] for t in tasks])
        engine = self.ensure_engine([t["path"] for t in tasks], size_hint=size_hint)
        with self.lock:
            chunk = getattr(engine, "batch", 0) or 64
            if getattr(engine, "kind", "cpu") == "gpu":
                chunk = max(8, chunk)
            flush_rows = int(self.settings.get("flush_rows") or 500)
            flush_secs = float(self.settings.get("flush_secs") or 4.0)
            skip = {self.data_dir, self.archive_dir()}
            self._job_key = tuple(sorted(t["path"] for t in tasks))
            self.job = ScanJob(self.store, tasks, engine, skip_dirs=skip,
                               chunk=chunk, flush_rows=flush_rows, flush_secs=flush_secs)
            self.job.start()
        self.invalidate()
        return {"ok": True, "tasks": tasks, "engine": engine.name, "queued": False}

    def _ensure_patches(self) -> None:
        """第一次需要分组时才补指纹（低优先级），已经补过/在补就不重复起。"""
        if self._patch_thread is not None and self._patch_thread.is_alive():
            return
        try:
            if dpatches.missing_count(self.store) <= 0:
                return
        except Exception:                              # noqa: BLE001
            return
        self._patch_thread = threading.Thread(
            target=self._fill_patches_quiet, daemon=True, name="patch-backfill")
        self._patch_thread.start()

    def patch_status(self) -> dict:
        return {"ok": True, **self.patch_fill}

    def _fill_patches_quiet(self) -> None:
        """后台把缺的灰度指纹补齐；补完聚类自动用上（不用重启）。"""
        try:
            miss = dpatches.missing_count(self.store)
            if miss <= 0:
                return
            self.patch_fill = {"running": True, "done": 0, "missing": miss}
            print(f"  [指纹] 有 {miss:,} 张图缺灰度指纹，后台补算中（不影响使用）", flush=True)
            def on_progress(done: int, total: int) -> None:
                self.patch_fill["done"] = int(done)
                self.patch_fill["missing"] = int(total)

            filled = dpatches.fill_missing(
                self.store, workers=4, pace=0.05, on_progress=on_progress,
                log=lambda m: print("  [指纹] " + m, flush=True))
            self.patch_fill["done"] = int(filled)
            self.patch_fill["running"] = False
            print("  [指纹] 补算完成，下次分组更准", flush=True)
        except Exception as exc:  # noqa: BLE001
            self.patch_fill["running"] = False
            print(f"  [指纹] 补算失败：{exc}", flush=True)

    def start_scan(self, paths: list[str] | None = None, recursive: bool | None = None) -> dict:
        with self.lock:
            tasks, err = self._build_tasks(paths, recursive)
            if err:
                return {"ok": False, "error": err}
            if not tasks:
                return {"ok": False, "error": "还没有添加文件夹"}
            key = tuple(sorted(t["path"] for t in tasks))
            if self.job is not None and self.job.is_alive():
                if key == self._job_key:
                    return {"ok": True, "queued": False,
                            "message": "这些文件夹正在扫描中，稍等即可"}
                if any(q["key"] == key for q in self._queue):
                    pos = next(i for i, q in enumerate(self._queue) if q["key"] == key) + 2
                    return {"ok": True, "queued": True, "position": pos,
                            "message": f"这个任务已经在队列里了（第 {pos} 位）"}
                self._queue.append({"key": key, "tasks": tasks})
                pos = len(self._queue) + 1
                return {"ok": True, "queued": True, "position": pos,
                        "message": f"当前有任务在跑，已排队等待（第 {pos} 位）"}
        return self._launch(tasks)

    def _pump(self):
        """后台把排队的扫描任务接着跑起来。"""
        while True:
            time.sleep(1.2)
            try:
                with self.lock:
                    if self.job is not None and self.job.is_alive():
                        continue
                    if not self._queue:
                        continue
                    item = self._queue.pop(0)
                self._launch(item["tasks"])
            except Exception:
                pass

    # ------------------------------------------------ 对比更新某个文件夹
    def diff_root(self, path: str, limit_samples: int = 6) -> dict:
        """只看不写：这个文件夹相对当前索引，有多少新增/变化/删除。"""
        from dedupe.scanner import iter_image_files
        root = self.norm(path)
        if not os.path.isdir(root):
            return {"ok": False, "error": f"目录不存在：{root}"}
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT path, size, mtime FROM images WHERE status='ok' AND root=?",
                (root,)).fetchall()
        known = {r["path"]: (int(r["size"] or 0), float(r["mtime"] or 0)) for r in rows}
        seen: set[str] = set()
        new, changed, unchanged = [], [], 0
        for p in iter_image_files(root, True, {self.data_dir, self.archive_dir()}):
            try:
                st = os.stat(p)
            except OSError:
                continue
            seen.add(p)
            old = known.get(p)
            if old is None:
                new.append(p)
            elif old[0] != int(st.st_size) or abs(old[1] - float(st.st_mtime)) > 0.5:
                changed.append(p)
            else:
                unchanged += 1
        removed = [p for p in known if p not in seen]

        def sample(lst):
            return [os.path.basename(x) for x in lst[:limit_samples]]

        return {
            "ok": True, "root": root,
            "total": len(seen), "new": len(new), "changed": len(changed),
            "removed": len(removed), "unchanged": unchanged,
            "known": len(known),
            "samples": {"new": sample(new), "changed": sample(changed), "removed": sample(removed)},
            "need_update": bool(new or changed or removed),
        }

    def _first_files(self, roots: list[str], limit: int = 3):
        """从待扫描目录里挑几张图，用来校验 GPU 与 CPU 结果是否一致。"""
        from dedupe.scanner import iter_image_files
        out = []
        for r in roots:
            if not os.path.isdir(r):
                continue
            try:
                for p in iter_image_files(r, True, {self.data_dir}):
                    out.append(p)
                    if len(out) >= limit:
                        return out
            except Exception:
                continue
        return out

    def scan_status(self) -> dict:
        with self.lock:
            job = self.job
            queued = [{"paths": [t["path"] for t in q["tasks"]]} for q in self._queue]
        st = self.store.stats()
        base = {
            "running": False, "state": "idle", "total": 0, "done": 0,
            "hashed": 0, "reused": 0, "errors": 0, "elapsed": 0, "rate": 0,
            "message": "尚未扫描", "current": "", "root": "", "queue": [],
            "stats": st, "pending": self.store.pending_roots(), "engine": self.gpu_info,
            "queued": len(queued), "queued_tasks": queued,
        }
        if job is None:
            return base
        s = job.snapshot()
        s["stats"] = st
        s["pending"] = self.store.pending_roots()
        s["engine"] = self.gpu_info
        s["queued"] = len(queued)
        s["queued_tasks"] = queued
        return s

    def pause_scan(self) -> dict:
        with self.lock:
            job = self.job
        if job and job.is_alive():
            job.pause()
            return {"ok": True}
        return {"ok": False, "error": "没有正在运行的扫描"}

    def resume_scan(self) -> dict:
        with self.lock:
            job = self.job
        if job and job.is_alive():
            job.resume()
            return {"ok": True, "mode": "resume"}
        pending = self.store.pending_roots()
        if pending:
            return {"ok": True, "mode": "restart", **self.start_scan(pending)}
        return {"ok": False, "error": "没有未完成的扫描"}

    def cancel_scan(self) -> dict:
        with self.lock:
            job = self.job
        if job and job.is_alive():
            job.cancel()
            return {"ok": True}
        return {"ok": False, "error": "没有正在运行的扫描"}

    # ------------------------------------------------ 分组
    def index_arrays(self, root: str):
        rev = self.store.rev()
        key = (rev, root or "")
        with self.lock:
            if self._idx is not None and self._idx[0] == key:
                return self._idx[1]
        data = self.store.load_index(root or None)
        with self.lock:
            self._idx = (key, data)
        return data

    # ------------------------------------------------ 自动选择（保留规则）
    def _folder_mask(self, data: dict, folders: list[str]) -> np.ndarray:
        """标出"属于这些文件夹"的图片。支持选中的是根目录或其子目录。"""
        n = len(data["ids"])
        mask = np.zeros(n, dtype=bool)
        if not folders:
            return mask
        roots = data.get("roots") or []
        rc = data.get("rc")
        unresolved = []
        for f in folders:
            fl = f.rstrip("\\/").lower()
            hit = False
            if rc is not None:
                for i, p in enumerate(roots):
                    pl = p.rstrip("\\/").lower()
                    if fl == pl or pl.startswith(fl + "\\") or pl.startswith(fl + "/"):
                        mask |= (rc == i)
                        hit = True
            if not hit:
                unresolved.append(f)
        if unresolved:
            ids = self.store.ids_under(unresolved)
            if ids:
                mask |= np.isin(data["ids"], np.fromiter(ids, dtype=np.int64))
        return mask

    def _select_exact(self, keep: str, folder_mode: str, folders: list[str]) -> dict:
        """完全相同的文件：按摘要分组，每组留一张。"""
        rows = self.store.exact_dup_rows()
        if not rows:
            return {"ok": True, "ids": [], "count": 0, "bytes": 0, "groups": 0}
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["sha256"], []).append(r)
        folder_list = [f.rstrip("\\/").lower() for f in folders]

        def inside(r):
            p = r["path"].lower()
            return any(p == f or p.startswith(f + "\\") or p.startswith(f + "/") for f in folder_list)

        def quality(r):
            pix = max(1, int(r.get("pix") or 1))
            return (0.40 * math.log1p(pix) + 0.45 * math.log1p(float(r.get("sharp") or 0))
                    + 0.15 * math.log1p(float(r.get("size") or 0) / pix))

        def keyf(r):
            if keep == "newest":
                return float(r.get("mtime") or 0)
            if keep == "oldest":
                return -float(r.get("mtime") or 0)
            if keep == "resolution":
                return float(r.get("pix") or 0)
            return quality(r)

        drop_ids, drop_sizes, total_bytes, ngroups = [], [], 0, 0
        for digest, items in groups.items():
            if len(items) < 2:
                continue
            ngroups += 1
            survivor = max(items, key=lambda r: (
                1 if (inside(r) if folder_mode == "keep" else not inside(r)) else 0, keyf(r)))
            for r in items:
                if r is not survivor:
                    drop_ids.append(int(r["id"]))
                    drop_sizes.append(int(r.get("size") or 0))
                    total_bytes += int(r.get("size") or 0)
        return {"ok": True, "ids": drop_ids, "sizes": drop_sizes, "count": len(drop_ids),
                "bytes": total_bytes, "groups": ngroups}

    def autoselect(self, scope: str = "groups", keep: str = "quality",
                   folder_mode: str = "drop", folders: list[str] | None = None,
                   threshold: int = 10, min_size: int = 2, root: str = "",
                   recall: str = "standard") -> dict:
        folders = [self.norm(f) for f in (folders or []) if str(f).strip()]
        if scope == "exact":
            res = self._select_exact(keep, folder_mode, folders)
            res["keep_label"] = "完全相同"
            return res

        data = self.index_arrays(root)
        cluster = self._cluster_for(data, root, threshold, recall)
        prefer = None
        base_keep = keep
        if keep == "folders":
            inside = self._folder_mask(data, folders)
            prefer = inside if folder_mode == "keep" else ~inside
            base_keep = "quality"
        sel = grouping_select(data, cluster, min_size, base_keep, prefer)
        idxs = sel["idx"]
        ids = data["ids"][idxs].tolist() if len(idxs) else []
        sizes = data["size"][idxs].tolist() if len(idxs) else []
        return {"ok": True, "ids": [int(i) for i in ids], "sizes": [int(s) for s in sizes],
                "count": len(ids), "bytes": int(sel["bytes"]), "groups": int(sel["groups"]),
                "keep_label": KEEP_LABELS.get(keep, keep)}

    def _cluster_for(self, data: dict, root: str, threshold: int, recall: str) -> dict:
        key = (self.store.rev(), root or "", threshold, recall)
        with self.lock:
            if self._cluster and self._cluster.get("key") == key:
                return self._cluster["res"]
        cluster = prepare_cluster(data, threshold, recall)
        with self.lock:
            self._cluster = {"key": key, "res": cluster}
        return cluster

    def groups(self, threshold: int, min_size: int, sort: str, root: str,
               limit: int = 60, offset: int = 0, recall: str = "standard") -> dict:
        self._ensure_patches()
        data = self.index_arrays(root)
        cluster = self._cluster_for(data, root, threshold, recall)
        res = render_groups(self.store, data, cluster, min_size=min_size, sort=sort,
                            limit=limit, offset=offset, gpu_info=self.gpu_info)
        res["stats"]["roots"] = self.store.roots()
        res["stats"]["data_dir"] = self.data_dir
        res["stats"]["threshold"] = threshold
        return res

    def invalidate(self):
        with self.lock:
            self._idx = None
            self._cluster = None

    # ------------------------------------------------ 删除 / 移动
    def remove(self, ids: list[int], mode: str, dest: str) -> dict:
        recs = []
        for i in ids:
            r = self.store.get(i)
            if r:
                recs.append(r)
        paths = [r["path"] for r in recs]
        if mode == "move":
            dest = os.path.abspath(os.path.expanduser(dest or "").strip().strip('"'))
            if not dest:
                return {"ok": False, "error": "请先填写要移动到的文件夹"}
            ok, failed = move_files(paths, dest)
        else:
            ok, failed = send_to_recycle_bin(paths)

        ok_set = set(ok)
        ok_ids = [r["id"] for r in recs if r["path"] in ok_set]
        self.store.set_status(ok_ids, "moved" if mode == "move" else "deleted")
        freed = sum(int(r.get("size") or 0) for r in recs if r["path"] in ok_set)

        with open(self.log_path, "a", encoding="utf-8") as f:
            for r in recs:
                if r["path"] in ok_set:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{mode}\t"
                            f"{dest if mode == 'move' else 'recycle'}\t{r['path']}\n")

        self.invalidate()
        self.store.bump_rev()
        return {"ok": True, "removed": len(ok), "freed": freed,
                "failed": [{"path": f["path"], "error": f["error"]} for f in failed],
                "dest": dest if mode == "move" else "回收站"}

    # ------------------------------------------------ 缩略图 / 预览
    def _cache_image(self, rec: dict, target_dir: str, max_px: int, quality: int) -> str | None:
        if not rec:
            return None
        src = rec["path"]
        if not os.path.exists(src):
            return None
        # 文件名带路径校验码 + 修改时间：存档载入后 id 会重排，也不会用错缓存
        tag = (f"{rec['id']}_{zlib.crc32(src.encode('utf-8', 'ignore')) & 0xFFFFFFFF:x}_"
               f"{int(float(rec.get('mtime') or 0))}")
        out = os.path.join(target_dir, f"{tag}_{max_px}.jpg")
        try:
            if os.path.exists(out):
                return out
            from PIL import Image, ImageOps
            im = Image.open(src)
            try:
                im.draft("RGB", (max_px * 2, max_px * 2))
            except Exception:
                pass
            im.seek(0)
            im = ImageOps.exif_transpose(im) or im
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
            tmp = f"{out}.{os.getpid()}_{threading.get_ident()}.tmp"
            im.save(tmp, "JPEG", quality=quality, optimize=True)
            os.replace(tmp, out)
            return out
        except Exception:
            return None

    def thumb(self, img_id: int, w: int) -> str | None:
        w = max(64, min(int(w), 1024))
        return self._cache_image(self.store.get(img_id), self.thumb_dir, w, 80)

    def preview(self, img_id: int, max_px: int) -> str | None:
        max_px = max(320, min(int(max_px), 4096))
        return self._cache_image(self.store.get(img_id), self.preview_dir, max_px, 88)

    # ------------------------------------------------ 存档
    def archives(self, scope: str = "image") -> dict:
        return {"archives": archive.list_all(self.archive_dir(), scope), "scope": scope}

    def archive_save(self, name: str, settings: dict, scope: str = "image") -> dict:
        st = self.store.stats()
        media_total = sum(self.media.stats(k)["items"] for k in ("video", "audio", "novel"))
        if st["images"] == 0 and media_total == 0:
            return {"ok": False, "error": "当前没有扫描结果，先扫描再保存"}
        if st["images"] > 2_000_000:
            return {"ok": False, "error": f"{st['images']:,} 张太多了，存档会非常大；"
                                          f"建议按文件夹分别存档"}
        meta = archive.save(self.store, self.archive_dir(), name, settings,
                        media=self.media, scope=scope)
        return {"ok": True, "archive": meta}

    def archive_load(self, aid: str, scope: str = "image") -> dict:
        with self.lock:
            if self.job is not None and self.job.is_alive():
                return {"ok": False, "error": "扫描进行中，暂不能载入存档"}
        payload = archive.read(self.archive_dir(), aid, scope)
        if not payload:
            return {"ok": False, "error": "存档不存在或已损坏"}
        self.store.replace_all(payload.get("images") or [], payload.get("roots") or [])
        try:
            self.media.replace_all(payload.get("media") or {})
        except Exception:
            pass
        self._media_cache.clear()
        self.store.bump_rev()
        self.invalidate()
        self.clear_caches()
        # 存档里不带灰度指纹（BLOB 不进 JSON），载入后后台补一遍，
        # 否则分组会退回"只看哈希"，精确度会差一些。
        try:
            if dpatches.missing_count(self.store) > 0:
                threading.Thread(target=self._fill_patches_quiet, daemon=True,
                                 name="patch-backfill").start()
        except Exception:
            pass
        total = len(payload.get("images") or [])
        # 存档大了就别同步校验（几十万次 stat 会卡住界面），改成后台跑
        if total > 20000:
            self.start_verify()
            return {"ok": True, "name": payload.get("name"),
                    "settings": payload.get("settings") or {},
                    "images": total, "missing": 0, "verifying": True}
        missing = self.store.prune_missing_files()
        self.invalidate()
        self.store.bump_rev()
        return {"ok": True, "name": payload.get("name"),
                "settings": payload.get("settings") or {},
                "images": total - missing, "missing": missing}

    def archive_merge(self, aid: str, scope: str = "image") -> dict:
        payload = archive.read(self.archive_dir(), aid, scope)
        if not payload:
            return {"ok": False, "error": "存档不存在或已损坏"}
        added = self.store.merge_records(payload.get("images") or [], payload.get("roots") or [])
        try:
            added += self.media.merge_all(payload.get("media") or {})
        except Exception:
            pass
        self._media_cache.clear()
        missing = self.store.prune_missing_files()
        self.store.bump_rev()
        self.invalidate()
        return {"ok": True, "added": added, "missing": missing, "name": payload.get("name")}

    def archive_import(self, payload: dict) -> dict:
        try:
            meta = archive.save_upload(self.archive_dir(), payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "archive": meta}

    def archive_import_path(self, path: str) -> dict:
        """从磁盘上的某个 .json 直接导入存档（界面里点文件名即可）。"""
        p = self.norm(path) if str(path or "").strip() else ""
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": f"文件不存在：{p or '（空路径）'}"}
        try:
            with open(p, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            return {"ok": False, "error": f"读不了这个文件：{exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "不是有效的存档文件"}
        try:
            meta = archive.save_upload(self.archive_dir(), payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "archive": meta, "from": p}

    def archive_update(self, aid: str, settings: dict | None = None, scope: str = "image") -> dict:
        """用当前索引覆盖已有存档（载入 -> 更新文件夹 -> 覆盖保存）。"""
        with self.lock:
            if self.job is not None and self.job.is_alive():
                return {"ok": False, "error": "扫描进行中，等它跑完再更新存档"}
        items = archive.list_all(self.archive_dir(), scope)
        old = next((a for a in items if a.get("id") == aid), None)
        if not old:
            return {"ok": False, "error": "存档不存在"}
        st = self.store.stats()
        if st["images"] == 0 and sum(self.media.stats(k)["items"] for k in ("video", "audio", "novel")) == 0:
            return {"ok": False, "error": "当前没有扫描结果"}
        meta = archive.save(self.store, self.archive_dir(), old.get("name"), settings or {}, aid=aid,
                            media=self.media, scope=scope)
        return {"ok": True, "archive": meta}

    # ------------------------------------------------ 失效文件校验（可后台）
    def start_verify(self) -> dict:
        with self.lock:
            if self._verify.get("running"):
                return {"ok": True, "already": True, **self._verify}
            self._verify = {"running": True, "total": 0, "done": 0,
                            "removed": 0, "started": time.time()}
        total = self.store.stats()["images"]
        with self.lock:
            self._verify["total"] = total

        def run():
            def progress(done, tot):
                with self.lock:
                    self._verify["done"] = done
            try:
                n = self.store.prune_missing_files(progress=progress)
            except Exception:
                n = 0
            with self.lock:
                self._verify.update(running=False, removed=n, done=self._verify["total"])
            if n:
                self.invalidate()
                self.store.bump_rev()

        threading.Thread(target=run, daemon=True, name="verify").start()
        return {"ok": True, "started": True, "total": total}

    def verify_status(self) -> dict:
        with self.lock:
            return dict(self._verify)

    def clear_caches(self):
        for d in (self.thumb_dir, self.preview_dir):
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass

    # ------------------------------------------------ 导出
    def export_csv(self, threshold: int, min_size: int, root: str) -> str:
        res = self.groups(threshold, min_size, "reclaim", root, limit=3000, offset=0)
        buf = io.StringIO()
        buf.write("\ufeff")
        wr = csv.writer(buf)
        wr.writerow(["组号", "建议", "文件名", "完整路径", "分辨率", "文件大小(字节)",
                     "格式", "相似度%", "与最佳完全相同", "画质评分", "所在文件夹"])
        for g in res["groups"]:
            for im in g["images"]:
                wr.writerow([
                    g["no"], "保留" if im.get("best") else "重复",
                    im["name"], im["path"], f"{im['w']}x{im['h']}", im["size"],
                    im.get("fmt") or "", im.get("sim"),
                    "是" if im.get("same_as_best") else "",
                    im.get("score"), os.path.dirname(im["path"]),
                ])
        return buf.getvalue()


APP: App | None = None


# ------------------------------------------------------------------ HTTP

MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".ts": "video/mp2t",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".rmvb": "video/vnd.rn-realvideo",
    ".rm": "video/vnd.rn-realvideo", ".3gp": "video/3gpp", ".ogm": "video/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wma": "audio/x-ms-wma", ".ape": "audio/x-ape",
    ".mka": "audio/x-matroska", ".amr": "audio/amr",
    ".gif": "image/gif", ".webp": "image/webp", ".apng": "image/apng",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".bmp": "image/bmp", ".avif": "image/avif",
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"PhotoDedupe/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_file(self, file_path: str, mime: str) -> None:
        """边读边发，支持 HTTP Range（视频拖动进度条必需）。"""
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return self._json({"ok": False, "error": "文件读不到"}, 404)
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            part = rng[6:].split(",")[0].strip()
            a, _, b = part.partition("-")
            try:
                if a:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                elif b:                       # bytes=-N 取最后 N 字节
                    start = max(0, size - int(b))
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=600")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _bytes(self, body: bytes, ctype: str, status: int = 200, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _index(self):
        """首页：给 app.js / style.css 加上版本参数，改完代码浏览器自动拿新文件。"""
        try:
            with open(os.path.join(WEB, "index.html"), "r", encoding="utf-8") as f:
                html = f.read()
        except OSError as exc:
            return self._json({"ok": False, "error": str(exc)}, 500)
        tag = static_tag()
        html = html.replace('app.js"', f'app.js?v={tag}"').replace('style.css"', f'style.css?v={tag}"')
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str, cache: str = "no-store"):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._json({"ok": False, "error": "文件不存在"}, 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._bytes(body, ctype, cache=cache)

    MAX_BODY = 256 * 1024 * 1024        # 256MB：导入存档（内嵌 base64）也够用

    def _body(self) -> dict | None:
        """解析 JSON 请求体。返回 None 表示"格式不对"，调用方回 400。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0:
            return {}
        if n > self.MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:                              # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    def _same_origin(self) -> bool:
        """挡掉别的网站通过 http://127.0.0.1 调我们接口（删文件/开程序）。

        浏览器跨站请求一定会带 Origin，且和 Host 对不上；本机工具（curl/脚本）
        不带 Origin，照常放行。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True                             # 本机脚本/curl：没有 Origin
        if origin == "null":
            return False                            # file:// 之类，直接拒
        host = (self.headers.get("Host") or "").strip()
        try:
            return urllib.parse.urlparse(origin).netloc == host
        except Exception:                           # noqa: BLE001
            return False

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def do_GET(self):
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json({"ok": False, "error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._route_post()
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json({"ok": False, "error": str(exc)}, 500)
            except Exception:
                pass

    def _route_get(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = self._query()

        if path in ("/", "/index.html"):
            return self._index()

        if path == "/api/version":
            return self._json({"ok": True, "version": VERSION, "tag": static_tag()})
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if ".." in name or name.startswith("/"):
                return self._json({"ok": False}, 403)
            return self._file(os.path.join(WEB, name))
        if path == "/api/scan/status":
            return self._json(APP.scan_status())
        if path == "/api/roots":
            return self._json(APP.roots())
        if path == "/api/capabilities":
            return self._json({"ok": True, "version": VERSION,
                               "engine": APP.gpu_info, "formats": capabilities(),
                               "load": int(APP.settings.get("load") or 100)})
        if path == "/api/settings":
            return self._json({"ok": True, **APP.settings_view()})
        if path == "/api/verify/status":
            return self._json({"ok": True, **APP.verify_status()})
        if path == "/api/groups":
            def _int(k, d):
                try:
                    return int(q.get(k, d))
                except ValueError:
                    return d
            th = max(0, min(_int("threshold", 10), 40))
            mn = max(2, min(_int("min", 2), 50))
            limit = max(1, min(_int("limit", 60), 500))
            offset = max(0, _int("offset", 0))
            return self._json(APP.groups(th, mn, q.get("sort", "reclaim"), q.get("root", ""),
                                         limit, offset, q.get("recall", "standard")))
        if path == "/api/thumb":
            out = APP.thumb(int(q.get("id", 0)), int(q.get("w", 320)))
            if not out:
                return self._json({"ok": False}, 404)
            return self._file(out, cache="public, max-age=3600")
        if path == "/api/preview":
            out = APP.preview(int(q.get("id", 0)), int(q.get("max", 1400)))
            if not out:
                return self._json({"ok": False}, 404)
            return self._file(out, cache="public, max-age=3600")
        if path == "/api/fs":
            return self._json(safe_dir_list(q.get("path", ""),
                                            want_files=q.get("files") == "1"))
        if path == "/api/export":
            th = int(q.get("threshold", 10))
            mn = int(q.get("min", 2))
            body = APP.export_csv(th, mn, q.get("root", "")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             f"attachment; filename=duplicates_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/stats":
            return self._json({"ok": True, **APP.store.stats(), "roots": APP.store.roots()})
        # ---------------- 媒体（视频 / 音频 / 小说）
        if path == "/api/media/roots":
            return self._json(APP.media_roots(q.get("kind", "video")))
        if path == "/api/brute/hashcat":
            from media import hashcat as HC
            return self._json({"ok": True, "speed": round(HC.speed_hint(), 0),
                               **HC.status(refresh=q.get("refresh") == "1")})
        if path == "/api/pack/status":
            return self._json(APP.pack_status())
        if path == "/api/conv/status":
            return self._json(APP.conv_status())
        if path == "/api/dicts":
            return self._json(APP.dicts_state())
        if path == "/api/dicts/status":
            return self._json(APP.dicts_status())
        if path == "/api/plain/status":
            return self._json(APP.plain_status())
        if path == "/api/brute/status":
            return self._json(APP.brute_status())
        if path == "/api/unzip/found":
            from media.unzip import load_found
            return self._json({"ok": True, "found": load_found()})
        if path == "/api/unzip/status":
            return self._json(APP.unzip_status())
        if path == "/api/clean/status":
            return self._json(APP.clean_status())
        if path == "/api/media/status":
            return self._json(APP.media_status())
        if path == "/api/media/groups":
            th = q.get("threshold")
            return self._json(APP.media_groups(
                kind=q.get("kind", "video"),
                threshold=float(th) if th not in (None, "") else None,
                min_size=int(q.get("min", 2)),
                limit=int(q.get("limit", 40)),
                offset=int(q.get("offset", 0)),
                root=q.get("root", ""),
                quick=q.get("quick") == "1",
                content=q.get("content", "1") != "0"))
        if path == "/api/media/text":
            return self._json(APP.media_text(int(q.get("id", 0)), int(q.get("limit", 6000))))
        if path == "/api/media/thumb":
            data = APP.media_thumb_or_make(int(q.get("id", 0)))
            if not data:
                return self._json({"ok": False, "error": "没有缩略图"}, 404)
            return self._bytes(data, "image/jpeg", cache="max-age=3600")
        if path == "/api/media/other":
            return self._json(APP.media_other(q.get("kind", "video")))
        if path == "/api/images/other":
            return self._json(APP.images_other())
        if path == "/api/patch/status":
            return self._json(APP.patch_status())
        if path == "/api/other/count":
            return self._json({"ok": True, "counts": APP.other_count()})
        if path == "/api/media/waveform":
            return self._json(APP.media_waveform(int(q.get("id", 0)),
                                                 int(q.get("buckets", 900))))
        if path == "/api/media/file":
            got = APP.media_file(int(q.get("id", 0)))
            if not got:
                return self._json({"ok": False, "error": "文件不存在"}, 404)
            return self._stream_file(got[0], got[1])
        if path == "/api/media/export":
            body = APP.media_export_csv(q.get("kind", "video"),
                                        float(q["threshold"]) if q.get("threshold") else None,
                                        int(q.get("min", 2))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             f"attachment; filename={q.get('kind', 'media')}_dups_"
                             f"{time.strftime('%Y%m%d_%H%M%S')}.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/archives":
            return self._json(APP.archives(q.get("scope", "image")))
        if path == "/api/archives/export":
            aid = q.get("id", "")
            fp = archive.file_path(APP.archive_dir(), aid, q.get("scope", "image"))
            if not fp:
                return self._json({"ok": False, "error": "存档不存在"}, 404)
            name = urllib.parse.quote(f"存档_{aid}.json")
            body = os.path.getsize(fp)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{name}")
            self.send_header("Content-Length", str(body))
            self.end_headers()
            with open(fp, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        if path == "/api/reveal":
            rec = APP.store.get(int(q.get("id", 0)))
            target = (rec or {}).get("path") or str(q.get("path") or "")
            ok = bool(target) and os.path.exists(target) and reveal_in_explorer(target)
            return self._json({"ok": ok})
        return self._json({"ok": False, "error": "not found"}, 404)

    def _route_post(self):
        u = urllib.parse.urlparse(self.path)
        if not self._same_origin():
            return self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
        body = self._body()
        if body is None:
            return self._json({"ok": False, "error": "请求格式不对（需要 JSON 对象）"}, 400)
        if u.path == "/api/scan":
            return self._json(APP.start_scan(body.get("paths") or None, body.get("recursive")))
        if u.path == "/api/scan/pause":
            return self._json(APP.pause_scan())
        if u.path == "/api/scan/resume":
            return self._json(APP.resume_scan())
        if u.path == "/api/scan/cancel":
            return self._json(APP.cancel_scan())
        if u.path == "/api/roots":
            if body.get("action") == "remove":
                return self._json(APP.remove_root(body.get("path", "")))
            return self._json(APP.add_root(body.get("path", ""), bool(body.get("recursive", True))))
        # ---------------- 媒体（视频 / 音频 / 小说）
        if u.path == "/api/pick_folder":
            return self._json(native_pick_folder(body.get("title") or "选择文件夹",
                                                 body.get("initial") or "",
                                                 str(body.get("key") or "")))
        if u.path == "/api/pick_file":
            return self._json(native_pick_file(body.get("title") or "选择文件",
                                               body.get("initial") or "",
                                               body.get("filters") or "",
                                               str(body.get("key") or "")))
        if u.path == "/api/pick_files":
            return self._json(native_pick_files(body.get("title") or "选择文件（可多选）",
                                                body.get("initial") or "",
                                                body.get("filters") or "",
                                                str(body.get("key") or "")))
        if u.path == "/api/pack/start":
            return self._json(APP.pack_start(
                body.get("src") or "", body.get("dst") or "",
                body.get("steps") or [],
                str(body.get("mode") or "single")))
        if u.path == "/api/pack/check":
            return self._json(APP.pack_check(body.get("steps") or []))
        if u.path == "/api/pack/cancel":
            return self._json(APP.pack_cancel())
        if u.path == "/api/conv/start":
            return self._json(APP.conv_start(
                body.get("src") or "", body.get("dst") or "",
                t2s=bool(body.get("t2s", True)),
                out_enc=str(body.get("out_enc") or "utf-8"),
                newline=str(body.get("newline") or "keep"),
                recursive=bool(body.get("recursive", True)),
                overwrite=bool(body.get("overwrite", False))))
        if u.path == "/api/conv/cancel":
            return self._json(APP.conv_cancel())
        if u.path == "/api/clipboard/read":
            return self._json(read_clipboard())
        if u.path == "/api/clipboard/write":
            return self._json(write_clipboard(str(body.get("text") or "")))
        if u.path == "/api/conv/preview":
            return self._json(APP.conv_preview(str(body.get("text") or "")[:200000]))
        if u.path == "/api/dicts/download":
            return self._json(APP.dicts_download(body.get("ids") or []))
        if u.path == "/api/dicts/import":
            return self._json(APP.dicts_import(body.get("paths") or []))
        if u.path == "/api/dicts/delete":
            return self._json(APP.dicts_delete(body.get("names") or []))
        if u.path == "/api/dicts/load":
            return self._json(APP.dicts_load(body.get("entries") or [],
                                             int(body.get("limit") or 300000)))
        if u.path == "/api/brute/plan":
            _src = str(body.get("src") or "").strip()
            _mode = 0
            if _src:
                _p = APP.norm(_src)
                if os.path.isfile(_p) and _p.lower().endswith(".7z"):
                    from media import hashcat as _hc7
                    _mode = _hc7.MODE_7Z
            from media import unzip as _uz
            return self._json(_uz.plan_attack(body.get("brute") or {},
                                              len(body.get("passwords") or []), _mode))
            from media import unzip as _uz
            pws = [str(x).strip() for x in (body.get("passwords") or []) if str(x).strip()]
            return self._json(_uz.plan_attack(body.get("brute") or {}, len(pws)))
        if u.path == "/api/plain/start":
            plain = b""
            pf = str(body.get("plain_file") or "").strip()
            pf = APP.norm(pf) if pf else ""
            if pf and os.path.isfile(pf):
                try:
                    with open(pf, "rb") as f:
                        plain = f.read(1024 * 1024)
                except OSError:
                    plain = b""
            else:
                plain = str(body.get("plain_text") or "").encode("utf-8")
            auto = bool(body.get("auto"))
            return self._json(APP.plain_start(
                body.get("zip") or "", plain,
                int(body.get("offset") or 0),
                bool(body.get("recover_password", True)),
                str(body.get("charset") or "?d"),
                int(body.get("max_len") or 8), auto))
        if u.path == "/api/plain/cancel":
            return self._json(APP.plain_cancel())
        if u.path == "/api/brute/start":
            return self._json(APP.brute_start(body.get("src", ""),
                                              body.get("passwords") or [],
                                              body.get("brute") or {},
                                              bool(body.get("recursive", True)),
                                              body.get("dict_files") or []))
        if u.path == "/api/brute/pause":
            return self._json(APP.brute_pause())
        if u.path == "/api/brute/resume":
            return self._json(APP.brute_resume())
        if u.path == "/api/brute/cancel":
            return self._json(APP.brute_cancel())
        if u.path == "/api/unzip/start":
            return self._json(APP.unzip_start(body.get("src", ""), body.get("dst", ""),
                                              body.get("passwords") or [],
                                              body.get("brute"),
                                              bool(body.get("recursive", True)),
                                              bool(body.get("auto_brute", True)),
                                              body.get("dict_files") or []))
        if u.path == "/api/unzip/cancel":
            return self._json(APP.unzip_cancel())
        if u.path == "/api/clean/start":
            return self._json(APP.clean_start(body.get("src", ""), body.get("dst", ""),
                                              bool(body.get("recursive", True))))
        if u.path == "/api/clean/cancel":
            return self._json(APP.clean_cancel())
        if u.path == "/api/media/roots":
            return self._json(APP.media_add_root(body.get("kind", "video"),
                                                 body.get("path", ""),
                                                 bool(body.get("recursive", True))))
        if u.path == "/api/media/roots/remove":
            return self._json(APP.media_remove_root(body.get("kind", "video"),
                                                    body.get("path", "")))
        if u.path == "/api/media/scan":
            return self._json(APP.media_start(body.get("kind", "video"),
                                              body.get("paths"), body.get("recursive"),
                                              bool(body.get("include_video", False))))
        if u.path == "/api/media/pause":
            return self._json(APP.media_pause())
        if u.path == "/api/media/resume":
            return self._json(APP.media_resume())
        if u.path == "/api/media/cancel":
            return self._json(APP.media_cancel())
        if u.path == "/api/media/remove":
            return self._json(APP.media_remove([int(x) for x in (body.get("ids") or [])],
                                               body.get("mode", "trash"),
                                               body.get("dest", "")))
        if u.path == "/api/media/autoselect":
            return self._json(APP.media_autoselect(body.get("kind", "video"),
                                                   body.get("keep", "best"),
                                                   int(body.get("min", 2)),
                                                   body.get("threshold"),
                                                   body.get("root", "")))
        if u.path == "/api/remove":
            ids = [int(i) for i in body.get("ids", [])]
            mode = "move" if body.get("mode") == "move" else "trash"
            if not ids:
                return self._json({"ok": False, "error": "没有选中的图片"})
            return self._json(APP.remove(ids, mode, body.get("dest", "")))
        if u.path == "/api/archives":
            return self._json(APP.archive_save(body.get("name", ""), body.get("settings") or {},
                                               body.get("scope", "image")))
        if u.path == "/api/autoselect":
            return self._json(APP.autoselect(
                scope=body.get("scope", "groups"),
                keep=body.get("keep", "quality"),
                folder_mode=body.get("folder_mode", "drop"),
                folders=body.get("folders") or [],
                threshold=int(body.get("threshold", 10)),
                min_size=int(body.get("min", 2)),
                root=body.get("root", ""),
                recall=body.get("recall", "standard")))
        if u.path == "/api/verify":
            return self._json(APP.start_verify())
        if u.path == "/api/archives/import_path":
            return self._json(APP.archive_import_path(body.get("path", "")))
        if u.path == "/api/archives/update":
            return self._json(APP.archive_update(body.get("id", ""), body.get("settings") or {},
                                                 body.get("scope", "image")))
        if u.path == "/api/diff":
            return self._json(APP.diff_root(body.get("path", "")))
        if u.path == "/api/settings":
            return self._json(APP.update_settings(body))
        if u.path == "/api/archives/load":
            return self._json(APP.archive_load(body.get("id", ""), body.get("scope", "image")))
        if u.path == "/api/archives/merge":
            return self._json(APP.archive_merge(body.get("id", ""), body.get("scope", "image")))
        if u.path == "/api/archives/delete":
            return self._json({"ok": archive.remove(APP.archive_dir(), body.get("id", ""))})
        if u.path == "/api/archives/import":
            return self._json(APP.archive_import(body))
        if u.path == "/api/open_path":
            return self._json(APP.open_path(body.get("path", "")))
        if u.path == "/api/reveal":
            rec = APP.store.get(int(body.get("id", 0)))
            target = (rec or {}).get("path") or str(body.get("path") or "")
            ok = bool(target) and os.path.exists(target) and reveal_in_explorer(target)
            return self._json({"ok": ok})
        if u.path == "/api/reset":
            APP.store.clear()
            APP.invalidate()
            APP.clear_caches()
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not found"}, 404)


def free_port(start: int) -> int:
    import socket
    for p in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def detect_gpu() -> dict:
    """读 nvidia-smi（没有就返回 unavailable），给自检和 AI 适配用。"""
    info = {"nvidia_smi": False, "name": "", "driver": "", "cuda": ""}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, timeout=15, text=True,
            encoding="utf-8", errors="replace")
        if out.returncode == 0 and out.stdout.strip():
            first = out.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first.split(",")]
            info["name"] = parts[0] if parts else ""
            info["driver"] = parts[1] if len(parts) > 1 else ""
            info["nvidia_smi"] = bool(info["name"])
    except Exception:
        return info
    try:
        full = subprocess.run(["nvidia-smi"], capture_output=True, timeout=15, text=True,
                              encoding="utf-8", errors="replace").stdout or ""
        m = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", full)
        if m:
            info["cuda"] = m.group(1)
    except Exception:
        pass
    return info


def cuda_tag(cuda_version: str) -> str:
    """把 nvidia-smi 报告的 CUDA 版本换算成 torch 的轮子标签。"""
    try:
        major, minor = (cuda_version.split(".") + ["0"])[:2]
        v = float(f"{major}.{minor}")
    except Exception:
        return ""
    for need, tag in ((13.0, "cu130"), (12.8, "cu128"), (12.6, "cu126"),
                      (12.1, "cu121"), (11.8, "cu118")):
        if v >= need:
            return tag
    return ""


def selfcheck_json() -> dict:
    import platform
    bundled = os.path.normcase(sys.prefix) == os.path.normcase(os.path.join(BASE, "runtime"))
    data: dict = {
        "app": APP_NAME, "version": VERSION,
        "python": {"exe": sys.executable, "version": platform.python_version(),
                   "prefix": sys.prefix, "bundled": bundled},
        "deps": {}, "gpu": detect_gpu(), "gpu_python": {},
        "paths": {"base": BASE, "data": DATA,
                  "archives": os.path.join(DATA, "archives")},
        "writable": os.access(BASE if not os.path.isdir(DATA) else DATA, os.W_OK),
    }
    for mod in ("PIL", "numpy"):
        try:
            data["deps"][mod] = getattr(__import__(mod), "__version__", "?")
        except Exception:
            data["deps"][mod] = None
    try:
        import torch
        data["deps"]["torch"] = torch.__version__
        data["torch_cuda"] = bool(torch.cuda.is_available())
        if data["torch_cuda"]:
            data["torch_device"] = torch.cuda.get_device_name(0)
    except Exception:
        data["deps"]["torch"] = None
        data["torch_cuda"] = False

    gpu_py = gpu_python_path_file()
    data["gpu_python"] = {"path": gpu_py, "exists": bool(gpu_py and os.path.exists(gpu_py))}
    try:
        from media.unzip import find_7z
        sevenzip = find_7z()
    except Exception:
        sevenzip = ""
    try:
        from media import bkcrack as _bk
        bk = _bk.status()
    except Exception:
        bk = {"available": False, "version": ""}
    media = {"ffmpeg": os.path.isfile(mconfig.FFMPEG),
             "model": os.path.isfile(mconfig.MODEL),
             "sevenzip": sevenzip,
             "bkcrack": (bk.get("version") or "") if bk.get("available") else "",
             "opencc": (lambda: (lambda st: f"{st['chars']} 字 / {st['phrases']} 词")
                        (mconv.dict_status()))(),
             "onnxruntime": "", "providers": []}
    try:
        import onnxruntime as _ort
        media["onnxruntime"] = _ort.__version__
        media["providers"] = list(_ort.get_available_providers())
    except Exception:
        pass
    data["media"] = media

    try:
        from dedupe.formats import capabilities
        data["formats"] = capabilities()["count"]
    except Exception:
        data["formats"] = 0

    ok = bool(data["writable"] and data["deps"].get("PIL") and data["deps"].get("numpy"))
    data["ok"] = ok
    data["guide"] = "AGENT_SETUP.md"
    gpu = data["gpu"]
    advice = []
    if not ok:
        advice.append("依赖不完整：按 AGENT_SETUP.md 步骤 A 恢复 runtime\\ 或安装 Pillow+numpy")
    if not gpu["nvidia_smi"]:
        advice.append("没有检测到 NVIDIA 显卡：用 CPU 即可，无需安装 torch")
    elif data["torch_cuda"]:
        advice.append("GPU 已可用，无需操作")
    elif data["gpu_python"]["exists"]:
        advice.append("已配置可用的 GPU 环境（gpu_python.txt），启动时会自动切换")
    else:
        tag = cuda_tag(gpu.get("cuda") or "")
        advice.append(
            f"有 NVIDIA 显卡但没装 CUDA 版 torch：按 AGENT_SETUP.md 步骤 B 安装"
            f"（推荐 {tag or 'cu126'}，约 2 GB，6~10 分钟）")
    data["advice"] = advice
    return data


def gpu_python_path_file() -> str:
    cfg = os.path.join(BASE, "gpu_python.txt")
    if not os.path.exists(cfg):
        return ""
    try:
        with open(cfg, "r", encoding="utf-8-sig") as f:
            return next((ln.strip().strip('"') for ln in f
                         if ln.strip() and not ln.strip().startswith("#")), "")
    except Exception:
        return ""


def run_selfcheck(as_json: bool = False) -> int:
    """打印运行环境，用来确认迁移后能不能直接用。"""
    if as_json:
        data = selfcheck_json()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if data["ok"] else 1

    import platform

    print("=" * 62)
    print(f"  {APP_NAME} 环境自检   v{VERSION}")
    print("=" * 62)
    print(f"  Python      : {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"  解释器      : {sys.executable}")
    print(f"  前缀        : {sys.prefix}")
    inside = os.path.normcase(sys.prefix) == os.path.normcase(os.path.join(BASE, "runtime"))
    print(f"  内置运行时  : {'是（程序目录 runtime\\）' if inside else '否 —— 用的是本机其它 Python'}")
    for mod in ("PIL", "numpy"):
        try:
            m = __import__(mod)
            print(f"  {mod:<12}: {getattr(m, '__version__', '?')}")
        except Exception as exc:
            print(f"  {mod:<12}: 缺失！{exc}")
    try:
        import torch  # noqa: F401
        print(f"  torch       : {torch.__version__}  CUDA={torch.cuda.is_available()}")
    except Exception:
        print("  torch       : 未安装（没有 GPU 加速，纯 CPU 也能跑）")

    gpu = detect_gpu()
    if gpu["nvidia_smi"]:
        print(f"  显卡        : {gpu['name']}  驱动 {gpu['driver']}  CUDA {gpu['cuda'] or '?'}")
        print(f"  对应轮子    : {cuda_tag(gpu['cuda']) or '未知'}")
    else:
        print("  显卡        : 没检测到 nvidia-smi（用 CPU）")

    gpu_py = gpu_python_path_file()
    if gpu_py:
        state = "存在" if os.path.exists(gpu_py) else "不存在（会自动忽略）"
        print(f"  gpu_python  : {gpu_py}  [{state}]")
    else:
        print("  gpu_python  : 未配置（只用内置 Python）")

    try:
        import onnxruntime as _ort
        _provs = ", ".join(_ort.get_available_providers())
    except Exception:
        _ort, _provs = None, ""
    print("  ffmpeg      : " + ("有" if os.path.isfile(mconfig.FFMPEG) else "缺少（视频/音频查重不可用）"))
    print("  画面特征模型: " + ("有" if os.path.isfile(mconfig.MODEL) else "缺少"))
    print("  onnxruntime : " + (_ort.__version__ + "（" + _provs + "）" if _ort else "未安装（视频只能按 pHash 比）"))

    # 各家的显卡能用到什么，写在自检里，换电脑一看就明白
    has_dml = "DmlExecutionProvider" in _provs
    try:
        import torch as _t
        cuda_ok = _t.cuda.is_available()
    except Exception:
        cuda_ok = False
    try:
        from dedupe import pipeline as _pl
        dml_backend = _pl.dml_available()
    except Exception:                              # noqa: BLE001
        dml_backend = False
    if cuda_ok:
        img_gpu = "GPU（CUDA）"
    elif dml_backend:
        img_gpu = "GPU（DirectML）"
    else:
        img_gpu = "CPU"
    print(f"  加速能力    : 图片查重 {img_gpu} · 视频/音频 "
          + ("GPU（DirectML）" if has_dml else "CPU"))
    if not cuda_ok and not dml_backend:
        print("                （AMD/Intel 想给图片加速：装 torch-directml，见 AGENT_SETUP.md）")
    try:
        from dedupe.formats import capabilities
        c = capabilities()
        print(f"  图片格式    : {c['count']} 种" + (f"（可选插件：{'/'.join(c['optional'])}）" if c["optional"] else ""))
    except Exception as exc:
        print(f"  图片格式    : 检测失败 {exc}")

    print(f"  程序目录    : {BASE}")
    print(f"  数据目录    : {DATA}")
    writable = os.access(BASE, os.W_OK)
    print(f"  目录可写    : {'是' if writable else '否 —— 请把程序放到可写的位置'}")
    ok = writable and all(_has(m) for m in ("PIL", "numpy"))
    print("-" * 62)
    print("  结论：" + ("✅ 可以直接运行" if ok else "❌ 环境不完整，请运行「安装环境.bat」"))
    print("  机器可读：" + f"{os.path.basename(sys.executable)} app.py --selfcheck --json")
    print("  适配指南：AGENT_SETUP.md（换电脑 / 装 GPU 加速的完整步骤）")
    print("=" * 62)
    return 0 if ok else 1


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="图片查重 · 本地 Web 工具")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--engine", choices=["auto", "gpu", "cpu"], default="auto",
                    help="计算引擎：auto=CPU+GPU 混合（默认），gpu=只用显卡，cpu=只用 CPU")
    ap.add_argument("--load", type=int, default=0,
                    help="占用百分比 5~100（默认沿用设置里的值）；调低可以降噪")
    ap.add_argument("--fill-patches", action="store_true",
                    help="只补算图片灰度指纹然后退出（满速，用于手动维护）")
    ap.add_argument("--selfcheck", action="store_true", help="只打印运行环境然后退出")
    ap.add_argument("--json", action="store_true", help="配合 --selfcheck：输出机器可读的 JSON")
    args = ap.parse_args()

    if args.fill_patches:
        from dedupe import patches as _dp
        from dedupe.store import Store as _Store
        st = _Store(os.path.join(args.data, "library.db"))
        miss = _dp.missing_count(st)
        print(f"缺灰度指纹：{miss:,} 张")
        if miss:
            _dp.fill_missing(st, workers=8, log=lambda m: print(" ", m, flush=True))
        st.close()
        return 0

    if args.selfcheck:
        return run_selfcheck(as_json=args.json)

    if args.engine != "cpu":
        reexec_with_gpu_python()

    global APP
    APP = App(args.data, engine_mode=args.engine)
    if args.load:
        with APP.lock:
            APP.settings["load"] = max(5, min(100, int(args.load)))
            save_settings(APP.settings)
    threading.Thread(target=pipeline.probe_async, daemon=True, name="gpu-probe").start()

    port = free_port(args.port)
    httpd = ThreadingHTTPServer((args.host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{args.host}:{port}"

    print("=" * 60)
    print(f"  {APP_NAME} v{VERSION}  已启动")
    print(f"  打开浏览器访问： {url}")
    print(f"  数据目录： {args.data}")
    print(f"  计算引擎： {args.engine}（首次扫描时初始化，GPU 优先）")
    print("  关闭窗口或按 Ctrl+C 结束")
    print("=" * 60)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        APP.store.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
