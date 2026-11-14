"""媒体索引库（视频 / 音频 / 小说），与图片库共用同一个 SQLite 文件。"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_roots (
    kind     TEXT NOT NULL,
    path     TEXT NOT NULL,
    recursive INTEGER DEFAULT 1,
    added    REAL,
    status   TEXT DEFAULT 'idle',
    total    INTEGER DEFAULT 0,
    done     INTEGER DEFAULT 0,
    PRIMARY KEY (kind, path)
);
CREATE TABLE IF NOT EXISTS media_items (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind   TEXT NOT NULL,
    path   TEXT NOT NULL,
    root   TEXT NOT NULL,
    name   TEXT,
    size   INTEGER DEFAULT 0,
    mtime  REAL DEFAULT 0,
    dur    REAL DEFAULT 0,
    w      INTEGER DEFAULT 0,
    h      INTEGER DEFAULT 0,
    sig    BLOB,
    aux    BLOB,
    aux2   BLOB,
    thumb  BLOB,
    meta   TEXT,
    status TEXT DEFAULT 'ok',
    UNIQUE(kind, path)
);
CREATE INDEX IF NOT EXISTS idx_media_kind ON media_items(kind, status);
CREATE INDEX IF NOT EXISTS idx_media_root ON media_items(root, status);
CREATE TABLE IF NOT EXISTS media_meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class MediaStore:
    def __init__(self, db_path: str):
        self.path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "cache_size=-32768",
                       "temp_store=MEMORY", "mmap_size=134217728"):
            try:
                self._conn.execute(f"PRAGMA {pragma}")
            except Exception:
                pass
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------ 根目录
    def add_root(self, kind: str, path: str, recursive: bool = True):
        with self._lock:
            self._conn.execute(
                "INSERT INTO media_roots(kind,path,recursive,added) VALUES(?,?,?,?) "
                "ON CONFLICT(kind,path) DO UPDATE SET recursive=excluded.recursive",
                (kind, path, 1 if recursive else 0, time.time()))
            self._conn.commit()

    def remove_root(self, kind: str, path: str) -> int:
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM media_items WHERE kind=? AND root=?",
                (kind, path)).fetchone()[0]
            self._conn.execute("DELETE FROM media_items WHERE kind=? AND root=?", (kind, path))
            self._conn.execute("DELETE FROM media_roots WHERE kind=? AND path=?", (kind, path))
            self._conn.commit()
            return int(n)

    def list_roots(self, kind: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM media_items i "
                "WHERE i.kind=r.kind AND i.root=r.path AND i.status='ok') AS items "
                "FROM media_roots r WHERE r.kind=? ORDER BY r.added", (kind,)).fetchall()
        return [dict(r) for r in rows]

    def set_root_status(self, kind: str, path: str, status: str, total: int = 0, done: int = 0):
        with self._lock:
            self._conn.execute(
                "UPDATE media_roots SET status=?, total=?, done=? WHERE kind=? AND path=?",
                (status, int(total), int(done), kind, path))
            self._conn.commit()

    def pending_roots(self, kind: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM media_roots WHERE kind=? AND status IN ('scanning','paused')",
                (kind,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 条目
    def upsert_many(self, kind: str, records: list[dict]):
        if not records:
            return
        now = time.time()
        rows = []
        for r in records:
            rows.append((
                kind, r["path"], r.get("root", ""), r.get("name") or os.path.basename(r["path"]),
                int(r.get("size") or 0), float(r.get("mtime") or 0),
                float(r.get("dur") or 0), int(r.get("w") or 0), int(r.get("h") or 0),
                r.get("sig"), r.get("aux"), r.get("aux2"), r.get("thumb"),
                (r.get("meta") if isinstance(r.get("meta"), str)
                 else json.dumps(r.get("meta") or {}, ensure_ascii=False)), "ok",
            ))
        with self._lock:
            self._conn.executemany(
                "INSERT INTO media_items(kind,path,root,name,size,mtime,dur,w,h,sig,aux,aux2,thumb,meta,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kind,path) DO UPDATE SET "
                "root=excluded.root,name=excluded.name,size=excluded.size,mtime=excluded.mtime,"
                "dur=excluded.dur,w=excluded.w,h=excluded.h,sig=excluded.sig,aux=excluded.aux,"
                "aux2=excluded.aux2,thumb=excluded.thumb,meta=excluded.meta,status='ok'",
                rows)
            self._conn.commit()

    def mark_error(self, kind: str, path: str, root: str, error: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO media_items(kind,path,root,name,status,meta) VALUES(?,?,?,?, 'error', ?) "
                "ON CONFLICT(kind,path) DO UPDATE SET status='error', meta=excluded.meta",
                (kind, path, root, os.path.basename(path),
                 json.dumps({"error": str(error)[:300]}, ensure_ascii=False)))
            self._conn.commit()

    def add_skipped(self, kind: str, root: str, items: list) -> int:
        """记录"跳过/失败"的文件：太小、没音轨、读不出来、后缀不对……

        只存路径和原因，不存指纹；界面上"其他文件"用它列出来给用户自己看。
        """
        if not items:
            return 0
        rows = []
        for it in items:
            path, reason = (it if isinstance(it, (tuple, list)) else (it, ""))
            try:
                st = os.stat(path)
                size, mtime = int(st.st_size), float(st.st_mtime)
            except OSError:
                size, mtime = 0, 0.0
            rows.append((kind, path, root, os.path.basename(path), size, mtime,
                         json.dumps({"reason": str(reason)[:200]}, ensure_ascii=False)))
        with self._lock:
            self._conn.executemany(
                "INSERT INTO media_items(kind,path,root,name,size,mtime,status,meta) "
                "VALUES(?,?,?,?,?,?, 'skipped', ?) "
                "ON CONFLICT(kind,path) DO UPDATE SET status='skipped', "
                "size=excluded.size, mtime=excluded.mtime, meta=excluded.meta",
                rows)
            self._conn.commit()
        return len(rows)

    def other_items(self, kind: str, limit: int = 500) -> list[dict]:
        """列"其他文件"：跳过的 + 分析失败的，按大小从大到小（大的更可惜）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,kind,path,root,name,size,mtime,dur,w,h,thumb,meta,status "
                "FROM media_items WHERE kind=? AND status IN ('skipped','error') "
                "ORDER BY size DESC LIMIT ?", (kind, int(limit))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                meta = json.loads(d.get("meta") or "{}")
            except Exception:                          # noqa: BLE001
                meta = {}
            d["meta"] = meta
            d["reason"] = meta.get("reason") or meta.get("error") or ""
            d["has_thumb"] = bool(d.pop("thumb", None))
            out.append(d)
        return out

    def count_other(self, kind: str) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM media_items WHERE kind=? AND status IN ('skipped','error')",
                (kind,)).fetchone()[0])

    def set_thumb(self, mid: int, blob: bytes) -> None:
        with self._lock:
            self._conn.execute("UPDATE media_items SET thumb=? WHERE id=?", (blob, int(mid)))
            self._conn.commit()

    def get(self, mid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM media_items WHERE id=?", (mid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d

    def get_many(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        out = []
        with self._lock:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                q = "SELECT * FROM media_items WHERE id IN (%s)" % ",".join("?" * len(chunk))
                for r in self._conn.execute(q, chunk).fetchall():
                    d = dict(r)
                    d["meta"] = json.loads(d.get("meta") or "{}")
                    out.append(d)
        return out

    def all_items(self, kind: str, root: str = "") -> list[dict]:
        sql = ("SELECT id,path,root,name,size,mtime,dur,w,h,meta,sig,aux,aux2 "
               "FROM media_items WHERE kind=? AND status='ok'")
        args: list = [kind]
        if root:
            sql += " AND root=?"
            args.append(root)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d.get("meta") or "{}")
            out.append(d)
        return out

    def existing(self, kind: str, root: str) -> dict[str, tuple[int, float]]:
        """已索引的路径 → (size, mtime)，用于增量跳过。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,size,mtime FROM media_items WHERE kind=? AND root=? AND status='ok'",
                (kind, root)).fetchall()
        return {r["path"]: (int(r["size"] or 0), float(r["mtime"] or 0)) for r in rows}

    def prune_missing(self, kind: str, root: str, seen: set[str]) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,path FROM media_items WHERE kind=? AND root=?", (kind, root)).fetchall()
            gone = [(r["id"],) for r in rows if r["path"] not in seen]
            if gone:
                self._conn.executemany("DELETE FROM media_items WHERE id=?", gone)
                self._conn.commit()
            return len(gone)

    def stats(self, kind: str, root: str = "") -> dict:
        where = "kind=? AND status='ok'"
        args: list = [kind]
        if root:
            where += " AND root=?"
            args.append(root)
        with self._lock:
            r = self._conn.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM(size),0) s, COALESCE(SUM(dur),0) d FROM media_items WHERE {where}",
                args).fetchone()
            err = self._conn.execute(
                f"SELECT COUNT(*) FROM media_items WHERE {where.replace(chr(39)+'ok'+chr(39), chr(39)+'error'+chr(39))}",
                args).fetchone()[0]
        return {"items": int(r["n"]), "bytes": int(r["s"]), "duration": float(r["d"]),
                "errors": int(err)}

    def clear(self, kind: str | None = None):
        with self._lock:
            if kind:
                self._conn.execute("DELETE FROM media_items WHERE kind=?", (kind,))
                self._conn.execute("DELETE FROM media_roots WHERE kind=?", (kind,))
            else:
                self._conn.execute("DELETE FROM media_items")
                self._conn.execute("DELETE FROM media_roots")
            self._conn.commit()

    def get_meta(self, k: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT v FROM media_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_meta(self, k: str, v: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO media_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v))
            self._conn.commit()


    # ------------------------------------------------------------ 存档导出/导入
    BLOB_FIELDS = ("sig", "aux", "aux2", "thumb")

    def dump_all(self) -> dict:
        """导出全部媒体记录（BLOB 转 base64），供存档使用。"""
        with self._lock:
            items = [dict(r) for r in self._conn.execute(
                "SELECT kind,path,root,name,size,mtime,dur,w,h,sig,aux,aux2,thumb,meta,status "
                "FROM media_items WHERE status='ok' ORDER BY id").fetchall()]
            roots = [dict(r) for r in self._conn.execute(
                "SELECT kind,path,recursive,added FROM media_roots").fetchall()]
        for it in items:
            for k in self.BLOB_FIELDS:
                v = it.get(k)
                it[k] = base64.b64encode(v).decode("ascii") if v else ""
        return {"items": items, "roots": roots}

    @staticmethod
    def _decode(rec: dict) -> dict:
        out = dict(rec)
        for k in MediaStore.BLOB_FIELDS:
            v = out.get(k) or ""
            if isinstance(v, str):
                try:
                    out[k] = base64.b64decode(v) if v else None
                except Exception:
                    out[k] = None
        return out

    def replace_all(self, payload: dict) -> int:
        data = payload or {}
        items = data.get("items") or []
        roots = data.get("roots") or []
        with self._lock:
            self._conn.execute("DELETE FROM media_items")
            self._conn.execute("DELETE FROM media_roots")
            self._conn.commit()
        for r in roots:
            self.add_root(r.get("kind", ""), r.get("path", ""), bool(r.get("recursive", 1)))
        by_kind: dict[str, list] = {}
        for rec in items:
            by_kind.setdefault(rec.get("kind", ""), []).append(self._decode(rec))
        for kind, recs in by_kind.items():
            if kind in ("video", "audio", "novel"):
                self.upsert_many(kind, recs)
        with self._lock:
            self._conn.execute(
                "UPDATE media_roots SET status='ok', total=(SELECT COUNT(*) FROM media_items i "
                "WHERE i.kind=media_roots.kind AND i.root=media_roots.path AND i.status='ok')")
            self._conn.commit()
        return len(items)

    def merge_all(self, payload: dict) -> int:
        """把另一个存档里的媒体并进来：按 (kind, path) 去重，只补新的。"""
        data = payload or {}
        items = data.get("items") or []
        roots = data.get("roots") or []
        for r in roots:
            kind = r.get("kind", "")
            path = r.get("path", "")
            if kind in ("video", "audio", "novel") and path:
                exists = self._conn.execute(
                    "SELECT 1 FROM media_roots WHERE kind=? AND path=?", (kind, path)).fetchone()
                if not exists:
                    self.add_root(kind, path, bool(r.get("recursive", 1)))
        added = 0
        by_kind: dict[str, list] = {}
        for rec in items:
            kind = rec.get("kind", "")
            if kind not in ("video", "audio", "novel"):
                continue
            row = self._conn.execute(
                "SELECT 1 FROM media_items WHERE kind=? AND path=? AND status='ok'",
                (kind, rec.get("path", ""))).fetchone()
            if row:
                continue
            by_kind.setdefault(kind, []).append(self._decode(rec))
        for kind, recs in by_kind.items():
            self.upsert_many(kind, recs)
            added += len(recs)
        return added

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
