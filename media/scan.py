"""媒体扫描任务：枚举 → 逐个分析 → 批量入库，支持暂停/继续/停止。"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import audio, config, novel, video
from . import ffmpeg as mffmpeg

EXTS = {"video": config.VIDEO_EXTS, "audio": config.AUDIO_EXTS, "novel": config.NOVEL_EXTS}
MIN_BYTES = {"video": config.MIN_VIDEO_BYTES, "audio": config.MIN_AUDIO_BYTES,
             "novel": config.MIN_NOVEL_BYTES}
SKIP_DIRS = {"_重复图片", "重复图片", "_查重存档", "$RECYCLE.BIN", "System Volume Information",
             ".git", "node_modules", "__pycache__"}


class SkipFile(Exception):
    """不是错误，只是这个文件没内容可分析（例如没有音轨的视频）。"""


def iter_files(root: str, kind: str, recursive: bool = True, skip_dirs: set[str] | None = None,
               include_video: bool = False, small: list | None = None):
    """枚举待分析文件。

    体积低于门槛的**不丢**：路径和原因塞进 `small`（调用方存成 status='skipped'），
    界面上"其他文件"里还能看到、还能预览——小视频/动图经常就是这样被漏掉的。
    """
    exts = EXTS[kind]
    if kind == "audio" and include_video:
        exts = set(exts) | set(config.VIDEO_EXTS)
    # 视频目录里常见的动图/图片：不参与视频查重，但收进"其他文件"，能看缩略图和预览
    anim = {".gif", ".webp", ".apng", ".avif"} if kind == "video" else set()
    lo = MIN_BYTES[kind]
    skip = SKIP_DIRS | (skip_dirs or set())
    if not os.path.isdir(root):
        return
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith("$")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in anim:
                    if small is not None:
                        small.append((os.path.join(dirpath, fn),
                                      "动图 / 图片，不参与视频查重（可以直接预览）"))
                    continue
                if ext not in exts:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size < lo:
                    if small is not None:
                        small.append((p, f"文件只有 {size/1024:.0f} KB，小于 {lo/1024:.0f} KB 门槛"))
                    continue
                yield p
    else:
        try:
            names = os.listdir(root)
        except OSError:
            return
        for fn in names:
            ext = os.path.splitext(fn)[1].lower()
            if ext in anim:
                if small is not None:
                    small.append((os.path.join(root, fn),
                                  "动图 / 图片，不参与视频查重（可以直接预览）"))
                continue
            if ext not in exts:
                continue
            p = os.path.join(root, fn)
            if os.path.isfile(p):
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size < lo:
                    if small is not None:
                        small.append((p, f"文件只有 {size/1024:.0f} KB，小于 {lo/1024:.0f} KB 门槛"))
                    continue
                yield p


def count_files(root: str, kind: str, recursive: bool = True) -> int:
    return sum(1 for _ in iter_files(root, kind, recursive))


def analyze_one(kind: str, path: str) -> dict:
    """分析单个文件，返回入库用的记录。"""
    st = os.stat(path)
    base = {"path": path, "size": int(st.st_size), "mtime": float(st.st_mtime)}
    if kind == "video":
        r = video.analyze(path)
        base.update({
            "dur": r["duration"], "w": r["w"], "h": r["h"],
            "sig": r["sig"], "aux": r["ph"], "aux2": None, "thumb": r["thumb"],
            "meta": {"duration": r["duration"], "duration_txt": r["duration_txt"],
                     "vcodec": r["vcodec"], "acodec": r["acodec"],
                     "has_audio": r["has_audio"], "frames": r["frames"], "dim": r["dim"]},
        })
    elif kind == "audio":
        pr = mffmpeg.probe(path)
        if not pr.get("has_audio"):
            raise SkipFile("没有音轨")
        r = audio.analyze(path, pr)
        base.update({
            "dur": r["duration"], "w": 0, "h": 0,
            "sig": r["sig"], "aux": r["times"], "aux2": r["bounds"],
            "meta": {"duration": r["duration"], "duration_txt": r["duration_txt"],
                     "codec": r["codec"], "bitrate": r["bitrate"],
                     "sr": r["sr"], "channels": r["channels"], "hashes": r["hashes"],
                     "from_video": os.path.splitext(path)[1].lower() in config.VIDEO_EXTS},
        })
    elif kind == "novel":
        r = novel.analyze(path)
        base.update({
            "dur": 0, "w": 0, "h": 0,
            "sig": (int(r["sig"]).to_bytes(8, "little") if r.get("sig") else None),
            "aux": None, "aux2": None,
            "meta": {"chapters": r["chapters"], "words": r["words"], "enc": r["enc"],
                     "name_norm": r["name_norm"], "has_content": bool(r.get("sig")),
                     "garbled": bool(r.get("garbled"))},
        })
    else:
        raise RuntimeError(f"未知类型 {kind}")
    return base


class MediaScanJob(threading.Thread):
    daemon = True

    def __init__(self, store, kind: str, roots: list[dict], workers: int = 3,
                 flush_rows: int = 40, flush_secs: float = 3.0, skip_dirs: set[str] | None = None,
                 include_video: bool = False):
        super().__init__(name=f"media-scan-{kind}")
        self.store = store
        self.kind = kind
        self.roots = roots
        self.workers = max(1, min(8, workers))
        self.flush_rows = max(1, flush_rows)
        self.flush_secs = max(0.5, flush_secs)
        self.skip_dirs = skip_dirs or set()
        self.include_video = bool(include_video)

        self.cancel_ev = threading.Event()
        self.pause_ev = threading.Event()
        self._lock = threading.Lock()
        self._state = {
            "running": True, "paused": False, "state": "scanning", "kind": kind,
            "total": 0, "done": 0, "hashed": 0, "reused": 0, "errors": 0,
            "elapsed": 0.0, "current": "", "root": "", "message": "准备中…",
            "writes": 0,
        }
        self._t0 = time.time()
        self._buffer: list[dict] = []
        self._last_flush = time.time()

    # ------------------------------------------------------------ 控制
    def pause(self):
        self.pause_ev.set()
        with self._lock:
            self._state["paused"] = True
            self._state["state"] = "paused"
            self._state["message"] = "已暂停"

    def resume(self):
        self.pause_ev.clear()
        with self._lock:
            self._state["paused"] = False
            self._state["state"] = "scanning"
            self._state["message"] = "继续扫描"

    def cancel(self):
        self.cancel_ev.set()
        self.pause_ev.clear()

    def is_paused(self) -> bool:
        return self.pause_ev.is_set()

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._state)
        s["elapsed"] = round(time.time() - self._t0, 1)
        done = max(1, s["done"])
        s["rate"] = round(s["done"] / max(0.1, s["elapsed"]), 1)
        return s

    # ------------------------------------------------------------ 主流程
    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _wait_if_paused(self):
        while self.pause_ev.is_set() and not self.cancel_ev.is_set():
            time.sleep(0.1)

    def _flush(self, force: bool = False):
        now = time.time()
        with self._lock:
            buf = self._buffer
            if not buf:
                return
            if not force and len(buf) < self.flush_rows and now - self._last_flush < self.flush_secs:
                return
            self._buffer = []
        self.store.upsert_many(self.kind, buf)
        self._last_flush = now
        with self._lock:
            self._state["writes"] += 1

    def run(self):
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", running=False, message=f"扫描出错：{exc}")
        finally:
            try:
                self._flush(force=True)
            except Exception:
                pass
            self._set(running=False)
            if self._state.get("state") not in ("error",):
                self._set(state="cancelled" if self.cancel_ev.is_set() else
                          ("paused" if self.pause_ev.is_set() else "done"))

    def _run(self):
        # 先枚举，拿到总数
        plan = []
        for r in self.roots:
            root = r["path"]
            if not os.path.isdir(root):
                self.store.set_root_status(self.kind, root, "error")
                continue
            small: list = []
            files = list(iter_files(root, self.kind, bool(r.get("recursive", 1)), self.skip_dirs,
                                     self.include_video, small))
            if small:
                try:
                    self.store.add_skipped(self.kind, root, small)
                except Exception:                      # noqa: BLE001
                    pass
            plan.append((root, files, small))
        total = sum(len(f) for _, f, _s in plan)
        self._set(total=total, message="分析中…")
        for root, _f, _s in plan:
            self.store.set_root_status(self.kind, root, "scanning", 0, 0)

        for root, files, small in plan:
            self._set(root=root)
            self.store.set_root_status(self.kind, root, "scanning", total, 0)
            existing = self.store.existing(self.kind, root)
            # 跳过的文件也要算"见过"，否则下面的 prune 会把刚记下的行删掉
            seen: set[str] = {p for p, _why in small}
            todo = []
            for p in files:
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                seen.add(p)
                old = existing.get(p)
                if old and old[0] == int(st.st_size) and abs(old[1] - float(st.st_mtime)) <= 0.5:
                    with self._lock:
                        self._state["reused"] += 1
                        self._state["done"] += 1
                    continue
                todo.append((p, st))
            self._flush(force=True)

            def work(item):
                p, _st = item
                self._wait_if_paused()
                if self.cancel_ev.is_set():
                    return None
                try:
                    rec = analyze_one(self.kind, p)
                    rec["root"] = root
                    return rec
                except SkipFile as exc:
                    try:
                        self.store.add_skipped(self.kind, root, [(p, str(exc) or "没有可分析的内容")])
                    except Exception:                  # noqa: BLE001
                        pass
                    with self._lock:
                        self._state["reused"] += 1
                    return None
                except Exception as exc:  # noqa: BLE001
                    self.store.mark_error(self.kind, p, root, str(exc))
                    with self._lock:
                        self._state["errors"] += 1
                    return None

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for rec in pool.map(work, todo):
                    if self.cancel_ev.is_set():
                        break
                    with self._lock:
                        self._state["done"] += 1
                        self._state["current"] = rec["path"] if rec else ""
                        if rec:
                            self._buffer.append(rec)
                            self._state["hashed"] += 1
                    self._set(done=self._state["done"])
                    self._flush()
                    self.store.set_root_status(self.kind, root, "scanning", total,
                                               self._state["done"])
            self._flush(force=True)

            if not self.cancel_ev.is_set() and not self.pause_ev.is_set():
                removed = self.store.prune_missing(self.kind, root, seen)
                self.store.set_root_status(self.kind, root, "ok", total, self._state["done"])
                if removed:
                    with self._lock:
                        self._state["message"] = f"已更新（移除 {removed} 个已删除文件）"
            if self.cancel_ev.is_set():
                self.store.set_root_status(self.kind, root, "cancelled", total, self._state["done"])
                break
            if self.pause_ev.is_set():
                self.store.set_root_status(self.kind, root, "paused", total, self._state["done"])
                break
