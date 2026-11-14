"""可编辑的压缩流程（流水线）。

* **两种模式**
  - `single` 单次压缩：整个输入当成**一个任务**，最后产出一个包
  - `batch` 批量压缩：输入下的**每个文件/文件夹各算一个任务**，产出多个包
* **合并步骤只作用于单个任务内部**：批量模式下 12 个任务就是 12 个包，
  合并只会把某个任务内部的多个包合成一个，绝不会把不同任务搅在一起
* 每一步之间传的是内存里的字节，只有最后一步写盘

步骤：
* `pack`   压缩：格式(zip/7z/tar/tar.gz/tar.bz2/tar.xz)、加密、密码、多层、级别、
           "每个子项各打一包"（用来配合合并步骤）
* `merge`  合并：把**当前任务**里的多个包合成一个（沿用上一次压缩的设置）
* `split`  拆分：按大小切成 name.z01…name.zip（其它格式按字节切）
* `rename` 改输出名：{name} {index} {date} {time} {ext}
* `flatten` 拍平嵌套
"""

from __future__ import annotations

import io
import os
import re
import threading
import time
import zipfile

from media import pack as P
from media.unzip import fix_zip_name as _fix_name


# ---------------------------------------------------------------- 步骤定义
STEP_LABELS = {
    "pack": "压缩",
    "merge": "合并再压一次（任务内）",
    "split": "拆分",
    "rename": "改输出名",
    "flatten": "拍平嵌套",
}


def default_steps() -> "list[dict]":
    return [{"type": "pack", "fmt": P.FMT_ZIP, "enc": "none", "password": "",
             "multi": False, "level": 6, "each_file": False}]


def normalize_steps(steps: "list[dict] | None") -> "list[dict]":
    out: "list[dict]" = []
    for raw in steps or []:
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type") or "").strip()
        if t not in STEP_LABELS:
            continue
        s = {"type": t}
        if t == "pack":
            fmt = str(raw.get("fmt") or P.FMT_ZIP)
            s["fmt"] = fmt if fmt in P.FORMATS else P.FMT_ZIP
            enc = str(raw.get("enc") or "none")
            s["enc"] = enc if enc in P.ENC_LABELS else "none"
            s["password"] = str(raw.get("password") or "")
            s["multi"] = bool(raw.get("multi"))
            try:
                lvl = int(raw.get("level"))
            except (TypeError, ValueError):
                lvl = 6
            s["level"] = lvl if lvl in P.LEVELS else min(P.LEVELS, key=lambda x: abs(x - lvl))
            s["each_file"] = bool(raw.get("each_file"))
        elif t == "merge":
            s["name"] = str(raw.get("name") or "").strip()
            fmt = str(raw.get("fmt") or P.FMT_ZIP)
            s["fmt"] = fmt if fmt in P.FORMATS else P.FMT_ZIP
            enc = str(raw.get("enc") or "none")
            s["enc"] = enc if enc in P.ENC_LABELS else "none"
            s["password"] = str(raw.get("password") or "")
            try:
                lvl = int(raw.get("level"))
            except (TypeError, ValueError):
                lvl = 6
            s["level"] = lvl if lvl in P.LEVELS else min(P.LEVELS, key=lambda x: abs(x - lvl))
        elif t == "split":
            mode = str(raw.get("mode") or "")
            try:
                parts = int(raw.get("parts") or 0)
            except (TypeError, ValueError):
                parts = 0
            try:
                size_mb = float(raw.get("size_mb") or 0)
            except (TypeError, ValueError):
                size_mb = 0.0
            if not mode:
                mode = "count" if parts >= 2 else ("size" if size_mb > 0 else "count")
            s["mode"] = mode if mode in ("count", "size") else "count"
            s["parts"] = parts if parts >= 2 else 0
            s["size_mb"] = size_mb if size_mb > 0 else 0.0
            try:
                s["min_size_mb"] = float(raw.get("min_size_mb") or 0)
            except (TypeError, ValueError):
                s["min_size_mb"] = 0.0
            try:
                s["max_size_mb"] = float(raw.get("max_size_mb") or 0)
            except (TypeError, ValueError):
                s["max_size_mb"] = 0.0
            try:
                s["min_parts"] = int(raw.get("min_parts") or 0)
            except (TypeError, ValueError):
                s["min_parts"] = 0
            try:
                s["max_parts"] = int(raw.get("max_parts") or 0)
            except (TypeError, ValueError):
                s["max_parts"] = 0
        elif t == "rename":
            s["pattern"] = str(raw.get("pattern") or "{name}").strip() or "{name}"
        out.append(s)
    return out


def check_steps(steps: "list[dict]") -> str:
    steps = normalize_steps(steps)
    if not steps:
        return "流程是空的，先加一步「压缩」"
    if not any(s["type"] == "pack" for s in steps):
        return "至少要有一步「压缩」，否则没有东西可写"
    for s in steps:
        if s["type"] not in ("pack", "merge"):
            continue
        fmt = s.get("fmt") or P.FMT_ZIP
        if s["enc"] != "none" and not s["password"]:
            return "有加密步骤但没填密码"
        if fmt in P.TAR_MODES and s["enc"] != "none":
            return "tar 系列格式不支持加密，换 zip 或 7z"
        if fmt == P.FMT_7Z:
            if not P.find_7z():
                return "找不到 7z.exe，7z 格式用不了（装个 7-Zip 或换 zip）"
            if s["type"] == "pack" and s.get("multi"):
                return "7z 格式暂不支持多层压缩（多层请用 zip）"
    return ""


def describe(steps: "list[dict]") -> "list[str]":
    steps = normalize_steps(steps)
    out = []
    for i, s in enumerate(steps, 1):
        t = s["type"]
        if t == "pack":
            bits = [P.FORMATS.get(s.get("fmt") or P.FMT_ZIP, "zip").split("（")[0]]
            bits.append(P.ENC_LABELS.get(s["enc"], "不加密"))
            if s["enc"] != "none":
                bits.append("有密码" if s["password"] else "缺密码")
            if s["multi"]:
                bits.append("多层")
            bits.append(P.LEVELS.get(s["level"], ""))
            if s["each_file"]:
                bits.append("子项各一包")
            out.append(f"{i}. 压缩（{'，'.join([b for b in bits if b])}）")
        elif t == "merge":
            enc = s.get("enc") or "none"
            bits = [P.FORMATS.get(s.get("fmt") or P.FMT_ZIP, "zip").split("（")[0],
                    P.ENC_LABELS.get(enc, "不加密")]
            if enc != "none":
                bits.append("有密码" if s.get("password") else "缺密码")
            bits.append(P.LEVELS.get(s.get("level"), ""))
            out.append(f"{i}. 合并再压一次" + (f"（{s['name']}）" if s["name"] else "")
                       + f"：{'，'.join([b for b in bits if b])}，会取消之前的拆分")
        elif t == "split":
            if s.get("mode") == "count":
                bits = [f"按数量 {s['parts'] or 2} 份"]
                if s.get("min_size_mb"):
                    bits.append(f"小于 {s['min_size_mb']:g}MB 不拆")
                if s.get("max_size_mb"):
                    bits.append(f"每卷超 {s['max_size_mb']:g}MB 额外拆")
                out.append(f"{i}. 拆分（{'，'.join(bits)}）")
            else:
                bits = [f"按体积每卷 {s['size_mb'] or 700:g} MB"]
                if s.get("min_parts"):
                    bits.append(f"至少 {s['min_parts']} 份")
                if s.get("max_parts"):
                    bits.append(f"不超过 {s['max_parts']} 份")
                out.append(f"{i}. 拆分（{'，'.join(bits)}）")
        elif t == "rename":
            out.append(f"{i}. 改输出名（{s['pattern']}）")
        elif t == "flatten":
            out.append(f"{i}. 拍平嵌套")
    return out


# ---------------------------------------------------------------- 工作对象
class Item:
    def __init__(self, name: str, *, src: str = "", is_dir: bool = False,
                 data: bytes = b"", kind: str = "", fmt: str = ""):
        self.name = name
        self.src = src
        self.is_dir = is_dir
        self.data = data
        self.kind = kind            # "" / "archive"
        self.fmt = fmt or P.FMT_ZIP
        self.enc = "none"
        self.password = ""
        self.level = 6
        self.split = {"mode": "count", "parts": 0, "size_mb": 0.0,
                      "min_size_mb": 0.0, "max_size_mb": 0.0,
                      "min_parts": 0, "max_parts": 0}


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = name.strip(" .")
    return name or "未命名"


def _render_name(pattern: str, item: Item, index: int) -> str:
    now = time.localtime()
    base, ext = item.name, ""
    for suf in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar", ".zip", ".7z"):
        if base.lower().endswith(suf):
            base, ext = base[: -len(suf)], suf
            break
    try:
        out = pattern.format(name=base, index=index, date=time.strftime("%Y%m%d", now),
                             time=time.strftime("%H%M%S", now), ext=ext.strip("."))
    except (KeyError, IndexError, ValueError):
        out = pattern
    return _safe_name(out)


# ---------------------------------------------------------------- 步骤实现
def _collect_entries(path: str, base: str, *, multi: bool, depth: int, step: dict,
                     log, cancel) -> "list[dict]":
    """把一个目录展开成归档条目；multi 时子目录先各自打成一个包。"""
    fmt = step["fmt"]
    enc = step["enc"]
    pwd = step["password"]
    level = step["level"]
    entries: "list[dict]" = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return entries
    for name in names:
        if cancel is not None and cancel.is_set():
            return entries
        full = os.path.join(path, name)
        rel = f"{base}/{name}" if base else name
        if os.path.isdir(full):
            if multi and depth < P.MAX_DEPTH:
                sub = _collect_entries(full, "", multi=multi, depth=depth + 1,
                                       step=step, log=log, cancel=cancel)
                blob = P.archive_bytes(sub, fmt=fmt, enc=enc, password=pwd, level=level)
                entries.append({"name": rel + P.archive_suffix(fmt), "data": blob})
                if log:
                    log(f"      子树 {rel} → {rel}{P.archive_suffix(fmt)}"
                        f"（{len(blob) / 1024:.0f} KB，{len(sub)} 项）")
            else:
                entries.extend(_collect_entries(full, rel, multi=multi,
                                                depth=depth + 1, step=step,
                                                log=log, cancel=cancel))
            continue
        try:
            if os.path.getsize(full) > P.MAX_FILE:
                if log:
                    log(f"      跳过超大文件 {rel}")
                continue
        except OSError:
            continue
        entries.append({"name": rel, "src": full})
    return entries


def _pack_one(item: Item, step: dict, log, cancel) -> "list[Item]":
    """把一个对象打成包；each_file 时展开成"每个子项一个包"。"""
    fmt = step["fmt"]
    enc = step["enc"]
    pwd = step["password"]
    level = step["level"]
    multi = step["multi"]
    each = step["each_file"]

    if item.kind == "archive":                      # 已有包 → 再套一层
        name = item.name if item.name.lower().endswith(P.archive_suffix(fmt)) \
            else item.name + P.archive_suffix(fmt)
        blob = P.archive_bytes([{"name": name, "data": item.data}],
                               fmt=fmt, enc=enc, password=pwd, level=level)
        out = Item(item.name, data=blob, kind="archive", fmt=fmt)
        out.enc, out.password, out.level = enc, pwd, level
        log(f"    再套一层：{item.name}（{len(blob) / 1024:.0f} KB）")
        return [out]

    def make(entries, label: str) -> "Item | None":
        if not entries:
            log(f"    {label}：空，跳过")
            return None
        blob = P.archive_bytes(entries, fmt=fmt, enc=enc, password=pwd, level=level)
        it = Item(item.name if not each else label, data=blob, kind="archive", fmt=fmt)
        it.enc, it.password, it.level = enc, pwd, level
        return it

    if item.is_dir:
        if each:                                    # 每个子项各打一包
            made: "list[Item]" = []
            try:
                children = sorted(os.listdir(item.src))
            except OSError:
                children = []
            for child in children:
                if cancel is not None and cancel.is_set():
                    break
                full = os.path.join(item.src, child)
                is_dir = os.path.isdir(full)
                sub = Item(child if is_dir else os.path.splitext(child)[0],
                           src=full, is_dir=is_dir)
                made.extend(_pack_one(sub, step, log, cancel))
            return made
        entries = _collect_entries(item.src, "", multi=multi, depth=0, step=step,
                                   log=log, cancel=cancel)
        it = make(entries, item.name)
        if it:
            log(f"    压缩 {item.name}：{len(entries)} 项 → {len(it.data) / 1024:.0f} KB")
            return [it]
        return []

    data = item.data or (open(item.src, "rb").read() if item.src else b"")
    entries = [{"name": os.path.basename(item.src) or item.name, "data": data}]
    it = make(entries, item.name)
    if it:
        log(f"    压缩文件 {item.name}：{len(it.data) / 1024:.0f} KB")
        return [it]
    return []


def _apply_pack(items: "list[Item]", step: dict, log, cancel) -> "list[Item]":
    out: "list[Item]" = []
    for it in items:
        if cancel is not None and cancel.is_set():
            return out
        out.extend(_pack_one(it, step, log, cancel))
    return out


def _apply_merge(items: "list[Item]", step: dict, log, cancel) -> "list[Item]":
    """合并成一个包：把当前内容**重新压一次**，用这一步自己的格式 / 加密 / 密码 / 级别。

    * 多个包 → 一个新的包，里面装这些包
    * 只有一个包 → 用这一步的设置重新打包（比如最后统一加密），名字沿用原来的
    * 无论哪种，都会取消之前「拆分」留下的分卷设置（想拆分请把拆分步骤放到合并后面）
    """
    if not items:
        return items
    fmt = step.get("fmt") or P.FMT_ZIP
    enc = step.get("enc") or "none"
    pwd = step.get("password") or ""
    level = int(step.get("level") or 6)
    suffix = P.archive_suffix(fmt)
    if len(items) == 1:
        it = items[0]
        base = _safe_name(it.name)          # 名字沿用，避免批量模式下互相覆盖
        entries = [{"name": it.name if it.name.lower().endswith(suffix)
                    else it.name + suffix, "data": it.data}]
        blob = P.archive_bytes(entries, fmt=fmt, enc=enc, password=pwd, level=level)
        out = Item(base, data=blob, kind="archive", fmt=fmt)
        out.enc, out.password, out.level = enc, pwd, level
        log(f"    只有 1 个包 → 用 {P.ENC_LABELS.get(enc, '不加密')} 重新打包"
            f"（{len(blob) / 1048576:.2f} MB，已取消之前的拆分）")
        return [out]
    base = _safe_name(step.get("name") or "合并包")
    entries = [{"name": (it.name if it.name.lower().endswith(suffix)
                         else it.name + suffix), "data": it.data} for it in items]
    blob = P.archive_bytes(entries, fmt=fmt, enc=enc, password=pwd, level=level)
    out = Item(base, data=blob, kind="archive", fmt=fmt)
    out.enc, out.password, out.level = enc, pwd, level
    log(f"    合并 {len(items)} 个包 → {base}{suffix}"
        f"（{len(blob) / 1048576:.2f} MB，{P.ENC_LABELS.get(enc, '不加密')}）")
    return [out]


def _apply_split(items: "list[Item]", step: dict, log) -> "list[Item]":
    opts = {k: step.get(k) for k in ("mode", "parts", "size_mb",
                                     "min_size_mb", "max_size_mb",
                                     "min_parts", "max_parts")}
    if opts.get("mode") == "count" and not opts.get("parts"):
        opts["parts"] = 2
    if opts.get("mode") == "size" and not opts.get("size_mb"):
        opts["size_mb"] = 700.0
    for it in items:
        it.split = dict(opts)
    if opts["mode"] == "count":
        log(f"    {len(items)} 个包：按数量拆 {opts['parts']} 份"
            + (f"，小于 {opts['min_size_mb']:g}MB 不拆" if opts.get("min_size_mb") else "")
            + (f"，每卷超 {opts['max_size_mb']:g}MB 额外拆" if opts.get("max_size_mb") else ""))
    else:
        log(f"    {len(items)} 个包：按体积每卷 {opts['size_mb']:g} MB"
            + (f"，至少 {opts['min_parts']} 份" if opts.get("min_parts") else "")
            + (f"，不超过 {opts['max_parts']} 份" if opts.get("max_parts") else ""))
    return items


def _apply_rename(items: "list[Item]", step: dict, log) -> "list[Item]":
    pattern = step.get("pattern") or "{name}"
    for i, it in enumerate(items, 1):
        old = it.name
        it.name = _render_name(pattern, it, i)
        log(f"    {old} → {it.name}")
    return items


def _apply_flatten(items: "list[Item]", log, cancel) -> "list[Item]":
    out: "list[Item]" = []
    for it in items:
        try:
            zf = zipfile.ZipFile(io.BytesIO(it.data))
        except Exception:  # noqa: BLE001
            out.append(it)
            continue
        entries = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                name = _fix_name(info)
                if name.lower().endswith(".zip"):
                    inner = zipfile.ZipFile(io.BytesIO(zf.read(info)))
                    for n in inner.infolist():
                        if n.filename.endswith("/"):
                            continue
                        entries.append({"name": f"{it.name}/{_fix_name(n)}",
                                        "data": inner.read(n)})
                    inner.close()
                else:
                    entries.append({"name": f"{it.name}/{name}", "data": zf.read(info)})
            except Exception:  # noqa: BLE001
                continue
        zf.close()
        if entries:
            blob = P.zip_bytes(entries, level=6)
            new = Item(it.name, data=blob, kind="archive", fmt=P.FMT_ZIP)
            out.append(new)
            log(f"    拍平 {it.name}：{len(entries)} 项")
        else:
            out.append(it)
    return out


# ---------------------------------------------------------------- 作业
class PipelineJob(threading.Thread):
    daemon = True

    def __init__(self, src: str, dst: str, steps: "list[dict]",
                 mode: str = "single"):
        super().__init__(name="pipeline-job")
        self.src = os.path.abspath(src)
        self.dst = os.path.abspath(dst)
        self.steps = normalize_steps(steps)
        self.mode = mode if mode in ("single", "batch") else "single"
        self.cancel_ev = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._st = {"running": True, "state": "running", "total": 0, "done": 0,
                    "stage": "", "archives": 0, "parts": 0, "bytes": 0,
                    "failed": 0, "current": "", "message": "准备中…", "log": [],
                    "src": self.src, "dst": self.dst, "mode": self.mode,
                    "steps": self.steps}

    def log(self, msg: str) -> None:
        with self._lock:
            lst = self._st["log"]
            lst.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(lst) > 600:
                del lst[:180]

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

    # ---- 任务列表
    def _tasks(self) -> "list[Item]":
        if self.mode == "single":
            if os.path.isfile(self.src):
                base = os.path.splitext(os.path.basename(self.src))[0]
                return [Item(base, src=self.src)]
            return [Item(os.path.basename(self.src.rstrip("\\/")) or "打包",
                         src=self.src, is_dir=True)]
        out: "list[Item]" = []
        if os.path.isfile(self.src):
            return [Item(os.path.splitext(os.path.basename(self.src))[0],
                         src=self.src)]
        try:
            names = sorted(os.listdir(self.src))
        except OSError:
            return out
        for name in names:
            full = os.path.join(self.src, name)
            if os.path.isdir(full):
                out.append(Item(name, src=full, is_dir=True))
            else:
                out.append(Item(os.path.splitext(name)[0], src=full))
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
        err = check_steps(self.steps)
        if err:
            self._set(state="error", message=err)
            self.log(err)
            return
        if not os.path.exists(self.src):
            self._set(state="error", message=f"输入路径不存在：{self.src}")
            return
        try:
            os.makedirs(self.dst, exist_ok=True)
        except OSError as exc:
            self._set(state="error", message=f"建不了输出目录：{exc}")
            return

        tasks = self._tasks()
        self._set(total=len(tasks))
        self.log("模式：" + ("单次压缩（整个输入一个任务）" if self.mode == "single"
                            else "批量压缩（每个文件/文件夹一个任务）"))
        self.log(f"输入：{self.src} → {len(tasks)} 个任务，输出到 {self.dst}")
        for line in describe(self.steps):
            self.log(f"  {line}")
        if not tasks:
            self._set(message="没有可处理的对象")
            return

        for ti, task in enumerate(tasks, 1):
            if self.cancel_ev.is_set():
                self.log("已取消")
                return
            self._set(current=task.name, done=ti - 1,
                      stage=f"任务 {ti}/{len(tasks)}：{task.name}")
            self.log(f"━ 任务 {ti}/{len(tasks)}：{task.name}")
            items: "list[Item]" = [task]
            done_steps = True
            for si, step in enumerate(self.steps, 1):
                if self.cancel_ev.is_set():
                    self.log("已取消")
                    return
                t = step["type"]
                self.log(f"  [{si}/{len(self.steps)}] {STEP_LABELS[t]}")
                if t == "pack":
                    items = _apply_pack(items, step, self.log, self.cancel_ev)
                elif t == "merge":
                    items = _apply_merge(items, step, self.log, self.cancel_ev)
                elif t == "split":
                    items = _apply_split(items, step, self.log)
                elif t == "rename":
                    items = _apply_rename(items, step, self.log)
                elif t == "flatten":
                    items = _apply_flatten(items, self.log, self.cancel_ev)
                if not items:
                    with self._lock:
                        self._st["failed"] += 1
                    self.log(f"  第 {si} 步之后这个任务没有内容了，跳过")
                    done_steps = False
                    break
            if done_steps:
                self._write_items(items)
        self._set(done=len(tasks))
        self.log(f"完成：{self._st['archives']} 个包 / {self._st['parts']} 个文件 / "
                 f"{self._st['bytes'] / 1048576:.2f} MB，失败 {self._st['failed']}")

    def _write_items(self, items: "list[Item]") -> None:
        for it in items:
            if self.cancel_ev.is_set():
                return
            if it.kind != "archive" or not it.data:
                with self._lock:
                    self._st["failed"] += 1
                self.log(f"  {it.name}：没有压缩产物，跳过（流程里要有压缩）")
                continue
            suffix = P.archive_suffix(it.fmt)
            base = it.name if it.name.lower().endswith(suffix) else it.name + suffix
            target = os.path.join(self.dst, _safe_name(base[: -len(suffix)]))
            sp = it.split or {}
            mb = 1024 * 1024
            part = int((sp.get("size_mb") or 0) * mb) if sp.get("mode") == "size" else 0
            cnt = int(sp.get("parts") or 0) if sp.get("mode") == "count" else 0
            try:
                files = P.write_split(
                    target, it.data, part, log=None, suffix=suffix, count=cnt,
                    min_size=int((sp.get("min_size_mb") or 0) * mb),
                    max_size=int((sp.get("max_size_mb") or 0) * mb),
                    min_parts=int(sp.get("min_parts") or 0),
                    max_parts=int(sp.get("max_parts") or 0))
            except OSError as exc:
                with self._lock:
                    self._st["failed"] += 1
                self.log(f"  {it.name} 写失败：{exc}")
                continue
            with self._lock:
                self._st["archives"] += 1
                self._st["parts"] += len(files)
                self._st["bytes"] += len(it.data)
            self.log(f"  写出 {os.path.basename(base)}：{len(files)} 个文件，"
                     f"{len(it.data) / 1048576:.2f} MB")
