"""图片查重 - 扫描任务（多文件夹队列 + 暂停/继续 + 断点续扫）。

面向大图库的几点：
* 分析交给 engine（GPU 优先），按批提交，写入也按批提交事务；
* 每批都能响应暂停/停止，批大小 16~64，响应时间 < 1 秒；
* 每分析完一批就落库，强关程序也不丢进度，重扫自动跳过已完成的文件。
"""

from __future__ import annotations

import os
import threading
import time

from .formats import is_image

SKIP_DIRS = {
    "$recycle.bin", "system volume information", "$winreagent",
    "node_modules", ".git", "__pycache__", "appdata",
    "_重复图片", "重复图片", "_查重存档",
}


def iter_image_files(root: str, recursive: bool = True, skip: set[str] | None = None):
    """遍历目录，产出图片的绝对路径。"""
    skip = skip or set()
    root = os.path.abspath(root)
    if not recursive:
        try:
            with os.scandir(root) as it:
                for e in it:
                    try:
                        if e.is_file() and is_image(e.name):
                            yield e.path
                    except OSError:
                        continue
        except OSError:
            return
        return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.lower() not in SKIP_DIRS
            and os.path.abspath(os.path.join(dirpath, d)) not in skip
        ]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if is_image(fn):
                yield os.path.join(dirpath, fn)


def count_images(root: str, limit: int = 200000) -> int:
    n = 0
    for _ in iter_image_files(root, True):
        n += 1
        if n >= limit:
            break
    return n


class ScanJob(threading.Thread):
    """一次扫描（可包含多个文件夹）。进度通过 snapshot() 轮询。"""

    def __init__(self, store, tasks: list[dict], engine, workers: int | None = None,
                 skip_dirs: set[str] | None = None, chunk: int = 32,
                 flush_rows: int = 500, flush_secs: float = 4.0):
        super().__init__(daemon=True, name="scan-job")
        self.store = store
        self.engine = engine
        self.tasks = [{"path": os.path.abspath(t["path"]), "recursive": bool(t.get("recursive", True))}
                      for t in tasks]
        self.workers = workers or min(8, (os.cpu_count() or 4))
        self.skip_dirs = skip_dirs or set()
        self.chunk = max(1, int(chunk))
        # 落库节奏：攒够多少张或过了多少秒才提交一次事务（减少写盘）
        self.flush_rows = max(1, int(flush_rows))
        self.flush_secs = max(0.5, float(flush_secs))
        self._buf: list[dict] = []
        self._writes = 0
        self._last_flush = time.time()
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self.status = {
            "state": "starting",
            "queue": [{"path": t["path"], "recursive": t["recursive"], "state": "pending",
                       "total": 0, "done": 0, "reused": 0} for t in self.tasks],
            "qindex": 0,
            "root": "",
            "total": 0,
            "done": 0,
            "hashed": 0,
            "reused": 0,
            "errors": 0,
            "current": "",
            "message": "准备中…",
            "elapsed": 0.0,
            "rate": 0.0,
            "writes": 0,
            "engine": getattr(engine, "name", ""),
        }

    # ------------------------------------------------------ 状态
    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self.status)
            s["queue"] = [dict(q) for q in self.status["queue"]]
        s["elapsed"] = round(time.time() - self._t0, 1)
        s["rate"] = round(s["done"] / max(0.001, s["elapsed"]), 1)
        s["running"] = self.is_alive()
        return s

    def _set(self, **kw):
        with self._lock:
            self.status.update(kw)

    def pause(self):
        self._pause.set()
        with self._lock:
            if self.status["state"] in ("hashing", "walking"):
                self.status["message"] = "正在暂停…"

    def resume(self):
        self._pause.clear()
        with self._lock:
            if self.status["state"] == "paused":
                self.status["state"] = "hashing"
                self.status["message"] = "继续扫描…"

    def cancel(self):
        self._cancel.set()
        self._pause.clear()
        self._set(message="正在停止…")

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _wait_if_paused(self) -> bool:
        """返回 False 表示期间被取消。"""
        if not self._pause.is_set():
            return True
        self._flush(force=True)          # 暂停前先把缓冲写掉
        self._set(state="paused", message="已暂停，点「继续」接着扫")
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(0.15)
        if self._cancel.is_set():
            return False
        self._set(state="hashing", message="继续扫描…")
        return True

    def _flush(self, force: bool = False):
        """把缓冲的记录一次性提交（一个事务）。

        攒批提交能把事务数从"每 16~64 张一次"降到"每几百张一次"，
        写盘次数少一个数量级，对机械盘/固态盘的寿命都更友好。
        """
        if not self._buf:
            return
        if not force:
            if len(self._buf) < self.flush_rows and (time.time() - self._last_flush) < self.flush_secs:
                return
        batch = self._buf
        self._buf = []
        try:
            self.store.upsert_many(batch)
            self._writes += 1
        except Exception:
            self._buf = batch          # 失败就留着下次再试
            raise
        finally:
            self._last_flush = time.time()
            with self._lock:
                self.status["writes"] = self._writes

    # ------------------------------------------------------ 主流程
    def run(self):
        try:
            self._run()
        except Exception as exc:  # pragma: no cover
            self._set(state="error", message=f"扫描失败：{exc}")
        finally:
            try:
                self._flush(force=True)
            except Exception:
                pass
            with self._lock:
                st = self.status["state"]
                if st != "error":
                    if self._cancel.is_set():
                        st = "cancelled"
                    elif self._pause.is_set():
                        st = "paused"
                    else:
                        st = "done"
                    self.status["state"] = st
                self.status["current"] = ""
                self.status["elapsed"] = round(time.time() - self._t0, 1)
            try:
                self.store.bump_rev()
            except Exception:
                pass

    def _run(self):
        # ---------- 1) 遍历，收集文件 ----------
        self._set(state="walking", message="正在统计文件…")
        plan: list[dict] = []
        seen: set[str] = set()
        for idx, task in enumerate(self.tasks):
            if self.cancelled:
                break
            root = task["path"]
            self._set(qindex=idx, root=root, message=f"正在统计：{root}")
            if not os.path.isdir(root):
                self.store.set_root_status(root, "error", "目录不存在")
                self._patch_queue(idx, state="error")
                continue
            files = []
            for path in iter_image_files(root, task["recursive"], self.skip_dirs):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                files.append((path, int(st.st_size), float(st.st_mtime)))
            plan.append({"idx": idx, "root": root, "files": files})
            self._patch_queue(idx, total=len(files))
            with self._lock:
                self.status["total"] += len(files)

        total = sum(len(p["files"]) for p in plan)
        self._set(total=total, message=f"共 {total} 张图片，开始分析…")
        if not plan:
            self._set(state="done", message="没有找到图片")
            return

        # ---------- 2) 逐文件夹分析 ----------
        done_total = 0
        hashed_total = 0
        reused_total = 0
        error_total = 0

        for p in plan:
            if self.cancelled:
                break
            idx, root, files = p["idx"], p["root"], p["files"]
            if not files:
                self._patch_queue(idx, state="done")
                self.store.set_root_status(root, "ok", touch=True)
                continue

            self._set(qindex=idx, root=root, state="hashing", message=f"分析中：{root}")
            self._patch_queue(idx, state="hashing")
            self.store.set_root_status(root, "scanning")

            # 复用旧索引（大小、修改时间都没变就跳过）
            pending = []
            done_here = 0
            reused_here = 0
            for path, size, mtime in files:
                rec = self.store.by_path(path)
                if (rec and rec.get("phash") and rec.get("status") == "ok"
                        and int(rec.get("size") or -1) == size
                        and abs(float(rec.get("mtime") or 0) - mtime) < 0.5):
                    reused_here += 1
                    done_here += 1
                else:
                    pending.append((path, size, mtime))
            reused_total += reused_here
            done_total += done_here
            with self._lock:
                self.status.update(done=done_total, reused=reused_total)
            self._patch_queue(idx, reused=reused_here, done=done_here)

            step = self.chunk if not pending else max(self.chunk, 1)
            for i in range(0, len(pending), step):
                if self.cancelled:
                    break
                if not self._wait_if_paused():
                    break
                batch = pending[i:i + step]
                paths = [b[0] for b in batch]
                try:
                    recs = self.engine.analyze_batch(paths)
                except Exception as exc:
                    recs = [{"path": q, "error": str(exc)} for q in paths]

                good = []
                for (path, size, mtime), rec in zip(batch, recs):
                    if not rec or rec.get("error"):
                        error_total += 1
                        self.store.mark_error(path, root, str((rec or {}).get("error", "分析失败")))
                    else:
                        rec["root"] = root
                        good.append(rec)
                if good:
                    self._buf.extend(good)
                    hashed_total += len(good)
                    self._flush()
                done_here += len(batch)
                done_total += len(batch)
                with self._lock:
                    self.status.update(done=done_total, hashed=hashed_total,
                                       errors=error_total, current=os.path.basename(paths[-1]))
                self._patch_queue(idx, done=done_here)

            if self.cancelled or self._pause.is_set():
                self.store.set_root_status(root, "paused")
                self._patch_queue(idx, state="paused")
                break

            self._flush(force=True)
            try:
                self.store.prune_missing(root, {f[0] for f in files})
            except Exception:
                pass
            self.store.set_root_status(root, "ok", touch=True)
            self._patch_queue(idx, state="done")

        if self.cancelled or self._pause.is_set():
            for i, q in enumerate(self.status["queue"]):
                if q["state"] in ("pending", "hashing"):
                    self.store.set_root_status(self.tasks[i]["path"], "paused")
                    self._patch_queue(i, state="paused")

        self._set(state="done" if not self.cancelled else "cancelled",
                  message=f"完成：分析 {hashed_total} 张，复用 {reused_total} 张")

    # ------------------------------------------------------ 小工具
    def _patch_queue(self, idx: int, **kw):
        with self._lock:
            if 0 <= idx < len(self.status["queue"]):
                self.status["queue"][idx].update(kw)
