"""图片查重 - 本地索引（SQLite）。

只存哈希和元数据，不复制图片本身；缩略图单独缓存在 data/thumbs。
一个"工作区"可以包含多个文件夹（roots 表），扫描结果逐步累积，
支持存档（archive）导入导出，便于换机器继续处理。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    path    TEXT NOT NULL UNIQUE,
    root    TEXT NOT NULL,
    name    TEXT NOT NULL,
    size    INTEGER NOT NULL DEFAULT 0,
    mtime   REAL    NOT NULL DEFAULT 0,
    w       INTEGER NOT NULL DEFAULT 0,
    h       INTEGER NOT NULL DEFAULT 0,
    fmt     TEXT,
    pix     INTEGER NOT NULL DEFAULT 0,
    phash   TEXT,
    phash_c TEXT,
    trim    TEXT,
    dhash   TEXT,
    ahash   TEXT,
    sharp   REAL NOT NULL DEFAULT 0,
    mr      INTEGER DEFAULT 128,
    mg      INTEGER DEFAULT 128,
    mb      INTEGER DEFAULT 128,
    sha256  TEXT,
    patch   BLOB,                          -- 16x16 灰度指纹（256 字节），判重第二阶段确认
    status  TEXT NOT NULL DEFAULT 'ok',   -- ok / moved / deleted / error
    err     TEXT,
    added   REAL
);
CREATE INDEX IF NOT EXISTS idx_images_root   ON images(root);
CREATE INDEX IF NOT EXISTS idx_images_sha    ON images(sha256);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);

CREATE TABLE IF NOT EXISTS roots (
    path      TEXT PRIMARY KEY,
    recursive INTEGER NOT NULL DEFAULT 1,
    added     REAL,
    last_scan REAL NOT NULL DEFAULT 0,
    status    TEXT NOT NULL DEFAULT 'ok',  -- ok / paused / error
    err       TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

FIELDS = [
    "path", "root", "name", "size", "mtime", "w", "h", "fmt", "pix",
    "phash", "phash_c", "trim", "dhash", "ahash", "sharp", "mr", "mg", "mb", "sha256",
    "patch",
]

ARCHIVE_FIELDS = ["name", "size", "mtime", "w", "h", "fmt", "pix", "phash",
                  "phash_c", "trim", "dhash", "ahash", "sharp", "mr", "mg", "mb", "sha256"]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._migrate()
            self._conn.execute("PRAGMA journal_mode=WAL")
            # 尽量减少写盘次数：正常提交不 fsync，靠 checkpoint 合并写
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-65536")        # 64MB 页缓存
            self._conn.execute("PRAGMA wal_autocheckpoint=4000")   # 攒多点再 checkpoint
            self._conn.execute("PRAGMA journal_size_limit=33554432")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA mmap_size=268435456")
            self._conn.executescript(SCHEMA)
            # 兼容旧库：缺列时补上
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(images)")}
            for col, ddl in (("phash_c", "TEXT"), ("trim", "TEXT")):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE images ADD COLUMN {col} {ddl}")
            # 旧库里已有图片但没有 roots 记录时补登记
            self._conn.execute(
                "INSERT OR IGNORE INTO roots(path, recursive, added, last_scan, status) "
                "SELECT DISTINCT root, 1, ?, 0, 'ok' FROM images",
                (time.time(),),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 基础
    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def bump_rev(self):
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(k, v) VALUES('rev', ?1) "
                "ON CONFLICT(k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + 1 AS TEXT)",
                (str(time.time()),),
            )
            self._conn.commit()

    def rev(self) -> str:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k='rev'").fetchone()
            cnt = self._conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            return f"{row['v'] if row else 0}:{cnt}"

    def get_meta(self, k: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return row["v"] if row else default

    def set_meta(self, k: str, v: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 图片写入
    def upsert(self, rec: dict):
        vals = [rec.get(f) for f in FIELDS]
        sql = (
            f"INSERT INTO images ({','.join(FIELDS)}, status, added) "
            f"VALUES ({','.join('?' * len(FIELDS))}, 'ok', ?) "
            f"ON CONFLICT(path) DO UPDATE SET "
            + ",".join(f"{f}=excluded.{f}" for f in FIELDS if f != "path")
            + ", status='ok', err=NULL"
        )
        with self._lock:
            self._conn.execute(sql, vals + [time.time()])
            self._conn.commit()

    def set_status(self, ids, status: str):
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE images SET status=? WHERE id=?",
                [(status, int(i)) for i in ids],
            )
            self._conn.commit()

    def upsert_many(self, recs: list[dict]):
        """批量写入（一个事务），比逐条 commit 快几十倍 —— 千万级扫描的关键。"""
        if not recs:
            return
        sql = (
            f"INSERT INTO images ({','.join(FIELDS)}, status, added) "
            f"VALUES ({','.join('?' * len(FIELDS))}, 'ok', ?) "
            f"ON CONFLICT(path) DO UPDATE SET "
            + ",".join(f"{f}=excluded.{f}" for f in FIELDS if f != "path")
            + ", status='ok', err=NULL"
        )
        now = time.time()
        payload = [[rec.get(f) for f in FIELDS] + [now] for rec in recs]
        with self._lock:
            self._conn.executemany(sql, payload)
            self._conn.commit()

    def mark_error(self, path: str, root: str, err: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO images(path, root, name, status, err, added) "
                "VALUES(?,?,?,'error',?,?) "
                "ON CONFLICT(path) DO UPDATE SET status='error', err=excluded.err",
                (path, root, os.path.basename(path), str(err)[:500], time.time()),
            )
            self._conn.commit()

    def prune_missing(self, root: str, seen_paths: set[str]) -> int:
        """删除本次扫描中已不存在的记录（仅针对同一个 root）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path FROM images WHERE root=?", (root,)
            ).fetchall()
            gone = [r["id"] for r in rows if r["path"] not in seen_paths]
            if gone:
                self._conn.executemany("DELETE FROM images WHERE id=?", [(i,) for i in gone])
                self._conn.commit()
            return len(gone)

    def prune_missing_files(self, chunk: int = 2000, progress=None) -> int:
        """全库校验：把磁盘上已经不存在的文件从索引里删掉。

        载入存档、或者手动"校验"时用，避免把已经删掉的图片又恢复出来。
        progress(done, total) 可选，用来在界面上显示进度。
        """
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM images WHERE status='ok'").fetchone()[0]
        gone = []
        last = 0
        done = 0
        while True:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, path FROM images WHERE status='ok' AND id>? "
                    "ORDER BY id LIMIT ?", (last, chunk)).fetchall()
            if not rows:
                break
            for r in rows:
                last = r["id"]
                if not os.path.exists(r["path"]):
                    gone.append((r["id"],))
            done += len(rows)
            if progress:
                try:
                    progress(done, total)
                except Exception:
                    pass
            if len(rows) < chunk:
                break
        if gone:
            with self._lock:
                self._conn.executemany("DELETE FROM images WHERE id=?", gone)
                self._conn.commit()
        return len(gone)

    # ------------------------------------------------------------ 读取
    def by_path(self, path: str):
        with self._lock:
            r = self._conn.execute("SELECT * FROM images WHERE path=?", (path,)).fetchone()
            return dict(r) if r else None

    def get(self, img_id: int):
        with self._lock:
            r = self._conn.execute("SELECT * FROM images WHERE id=?", (int(img_id),)).fetchone()
            return dict(r) if r else None

    def all_ok(self, root: str | None = None):
        sql = "SELECT * FROM images WHERE status='ok'"
        args: tuple = ()
        if root:
            sql += " AND root=?"
            args = (root,)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def get_many(self, ids: list[int]):
        if not ids:
            return []
        out = []
        for i in range(0, len(ids), 400):
            chunk = [int(x) for x in ids[i:i + 400]]
            q = f"SELECT * FROM images WHERE id IN ({','.join('?' * len(chunk))})"
            with self._lock:
                out.extend(dict(r) for r in self._conn.execute(q, chunk).fetchall())
        return out

    def sample_paths(self, limit: int = 3):
        with self._lock:
            return [r["path"] for r in self._conn.execute(
                "SELECT path FROM images WHERE status='ok' ORDER BY id LIMIT ?",
                (int(limit),)).fetchall()]

    def ids_under(self, prefixes: list[str]) -> set[int]:
        """所有落在这些目录（含子目录）下的图片 id。"""
        out: set[int] = set()
        with self._lock:
            for p in prefixes:
                base = str(p).rstrip("\\/")
                like = base + os.sep + "%"
                alt = base + "/%"
                for r in self._conn.execute(
                        "SELECT id FROM images WHERE status='ok' AND (path=? OR path LIKE ? OR path LIKE ?)",
                        (base, like, alt)):
                    out.add(int(r[0]))
        return out

    def exact_dup_rows(self) -> list[dict]:
        """只返回"内容摘要重复"的那些行（用于一键删除完全相同的文件）。"""
        with self._lock:
            digs = [r[0] for r in self._conn.execute(
                "SELECT sha256 FROM images WHERE status='ok' AND sha256<>'' "
                "GROUP BY sha256 HAVING COUNT(*)>1").fetchall()]
            rows: list[dict] = []
            for i in range(0, len(digs), 400):
                chunk = digs[i:i + 400]
                q = ("SELECT id, sha256, size, mtime, pix, sharp, path FROM images "
                     f"WHERE status='ok' AND sha256 IN ({','.join('?' * len(chunk))})")
                rows.extend(dict(r) for r in self._conn.execute(q, chunk).fetchall())
        return rows

    def clear_images(self):
        with self._lock:
            self._conn.execute("DELETE FROM images")
            self._conn.commit()

    def _migrate(self) -> None:
        """给老库补上后来加的列（patch 指纹）。"""
        try:
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(images)").fetchall()}
            if "patch" not in cols:
                self._conn.execute("ALTER TABLE images ADD COLUMN patch BLOB")
                self._conn.commit()
        except Exception:                                # noqa: BLE001
            pass

    def load_index(self, root: str | None = None) -> dict:
        """把聚类需要的列读成 numpy 数组（千万级也只占几百 MB）。

        只读 id/哈希/尺寸等数值列，路径和文件名等到出页时再按 id 取，
        这样 UI 不会一次性把几百万行塞进内存。
        """
        where = "status='ok' AND phash IS NOT NULL AND phash<>''"
        args: tuple = ()
        if root:
            where += " AND root=?"
            args = (root,)
        with self._lock:
            n = self._conn.execute(f"SELECT COUNT(*) FROM images WHERE {where}", args).fetchone()[0]
        empty = {
            "ids": np.empty(0, np.int64), "ph": np.empty(0, np.uint64),
            "phc": np.empty(0, np.uint64), "pix": np.empty(0, np.int32),
            "size": np.empty(0, np.int64), "sharp": np.empty(0, np.float32),
            "mtime": np.empty(0, np.int64), "ar": np.empty(0, np.float32),
            "rc": np.empty(0, np.int16), "dh": np.empty(0, np.uint64),
            "patch": np.zeros((0, 256), np.uint8), "has_patch": np.empty(0, bool),
        }
        if not n:
            return empty

        roots = [r["path"] for r in self.list_roots()]
        if roots:
            esc = [p.replace("'", "''") for p in roots]
            case = "CASE root " + " ".join(f"WHEN '{p}' THEN {i}" for i, p in enumerate(esc)) + " ELSE -1 END"
        else:
            case = "-1"

        ids = np.empty(n, np.int64)
        ph = np.empty(n, np.uint64)
        phc = np.empty(n, np.uint64)
        pix = np.empty(n, np.int32)
        size = np.empty(n, np.int64)
        sharp = np.empty(n, np.float32)
        mtime = np.empty(n, np.int64)
        ar = np.empty(n, np.float32)
        rc = np.empty(n, np.int16)
        dh = np.zeros(n, np.uint64)
        patch = np.zeros((n, 256), np.uint8)
        has_patch = np.zeros(n, bool)

        i = 0
        sql = (f"SELECT id, phash, phash_c, pix, size, sharp, mtime, "
               f"CAST(w AS REAL) / MAX(h, 1) AS ar, {case} AS rc, dhash, patch FROM images "
               f"WHERE {where} ORDER BY id")
        with self._lock:
            cur = self._conn.execute(sql, args)
            while True:
                rows = cur.fetchmany(20000)
                if not rows:
                    break
                k = len(rows)
                if i + k > n:
                    k = n - i
                    rows = rows[:k]
                ids[i:i + k] = [r[0] for r in rows]
                ph[i:i + k] = [int(r[1], 16) for r in rows]
                phc[i:i + k] = [int(r[2] or r[1], 16) for r in rows]
                pix[i:i + k] = [r[3] or 0 for r in rows]
                size[i:i + k] = [r[4] or 0 for r in rows]
                sharp[i:i + k] = [r[5] or 0 for r in rows]
                mtime[i:i + k] = [int(r[6] or 0) for r in rows]
                ar[i:i + k] = [r[7] or 1.0 for r in rows]
                rc[i:i + k] = [r[8] for r in rows]
                dh[i:i + k] = [int(r[9], 16) if r[9] else 0 for r in rows]
                for j, r in enumerate(rows):
                    pb = r[10]
                    if pb is not None and len(pb) == 256:
                        patch[i + j] = np.frombuffer(pb, dtype=np.uint8)
                        has_patch[i + j] = True
                i += k
                if i >= n:
                    break
        if i < n:
            for arr in (ids, ph, phc, pix, size, sharp, mtime, ar, rc, dh,
                        patch, has_patch):
                arr.resize(i, refcheck=False)
        return {"ids": ids, "ph": ph, "phc": phc, "pix": pix,
                "size": size, "sharp": sharp, "mtime": mtime, "ar": ar,
                "rc": rc, "dh": dh, "patch": patch, "has_patch": has_patch,
                "roots": roots}

    def roots(self):
        with self._lock:
            return [
                dict(r) for r in self._conn.execute(
                    "SELECT root, COUNT(*) AS n FROM images WHERE status='ok' "
                    "GROUP BY root ORDER BY n DESC"
                ).fetchall()
            ]

    def stats(self):
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM images WHERE status='ok'"
            ).fetchone()[0]
            errors = self._conn.execute(
                "SELECT COUNT(*) FROM images WHERE status='error'"
            ).fetchone()[0]
            bytes_ = self._conn.execute(
                "SELECT COALESCE(SUM(size),0) FROM images WHERE status='ok'"
            ).fetchone()[0]
            return {"images": total, "errors": errors, "bytes": bytes_}

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM images")
            self._conn.execute("DELETE FROM roots")
            self._conn.execute("DELETE FROM meta")
            self._conn.commit()

    # ------------------------------------------------------------ 文件夹工作区
    def add_root(self, path: str, recursive: bool = True):
        with self._lock:
            self._conn.execute(
                "INSERT INTO roots(path, recursive, added, last_scan, status) "
                "VALUES(?,?,?,0,'ok') "
                "ON CONFLICT(path) DO UPDATE SET recursive=excluded.recursive, status='ok', err=NULL",
                (path, 1 if recursive else 0, time.time()),
            )
            self._conn.commit()

    def list_roots(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.path, r.recursive, r.added, r.last_scan, r.status, r.err, "
                "  (SELECT COUNT(*) FROM images i WHERE i.root=r.path AND i.status='ok') AS images "
                "FROM roots r ORDER BY r.added"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["exists"] = os.path.isdir(d["path"])
                out.append(d)
            return out

    def set_root_status(self, path: str, status: str, err: str | None = None, touch: bool = False):
        with self._lock:
            if touch:
                self._conn.execute(
                    "UPDATE roots SET status=?, err=?, last_scan=? WHERE path=?",
                    (status, err, time.time(), path),
                )
            else:
                self._conn.execute(
                    "UPDATE roots SET status=?, err=? WHERE path=?",
                    (status, err, path),
                )
            self._conn.commit()

    def remove_root(self, path: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM images WHERE root=?", (path,))
            n = cur.rowcount
            self._conn.execute("DELETE FROM roots WHERE path=?", (path,))
            self._conn.commit()
            return n

    def pending_roots(self):
        """上次没扫完的文件夹：暂停的、或者被强行关掉卡在 scanning 的。"""
        with self._lock:
            return [
                r["path"] for r in self._conn.execute(
                    "SELECT path FROM roots WHERE status IN ('paused','scanning')"
                ).fetchall()
            ]

    # ------------------------------------------------------------ 存档读写
    def dump_images(self):
        """导出全部有效记录（用于存档）。

        只取存档需要的列：像 `patch`（16×16 灰度指纹，BLOB）这类二进制字段
        不进存档——json 存不了 bytes，带进去会让"覆盖存档"写到一半就抛错。
        """
        cols = ", ".join(["path", "root"] + ARCHIVE_FIELDS)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {cols} FROM images WHERE status='ok' ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def dump_roots(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, recursive, added, last_scan, status FROM roots ORDER BY added"
            ).fetchall()
            return [dict(r) for r in rows]

    def replace_all(self, images: list[dict], roots: list[dict]):
        """整库替换（载入存档）。"""
        cols = ["path", "root", "name", "size", "mtime", "w", "h", "fmt", "pix",
                "phash", "phash_c", "trim", "dhash", "ahash", "sharp", "mr", "mg", "mb", "sha256",
                "patch"]
        ph = ",".join("?" * len(cols))
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.execute("DELETE FROM images")
                self._conn.execute("DELETE FROM roots")
                self._conn.executemany(
                    f"INSERT INTO images ({','.join(cols)}, status, added) VALUES ({ph}, 'ok', ?)",
                    [[(r.get(c) if c != "pix" else r.get("pix") or (r.get("w", 0) * r.get("h", 0)))
                      for c in cols] + [time.time()] for r in images],
                )
                self._conn.executemany(
                    "INSERT INTO roots(path, recursive, added, last_scan, status) VALUES(?,?,?,?,?)",
                    [(r["path"], int(r.get("recursive", 1)), r.get("added") or time.time(),
                      r.get("last_scan") or 0, r.get("status") or "ok") for r in roots],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def merge_records(self, images: list[dict], roots: list[dict]) -> int:
        """合并存档（按路径去重），返回新增数量。"""
        existing = set()
        with self._lock:
            existing = {r["path"] for r in self._conn.execute("SELECT path FROM images")}
        added = 0
        for r in images:
            if r.get("path") in existing:
                continue
            self.upsert({**r, "root": r.get("root") or (roots[0]["path"] if roots else "")})
            added += 1
        for r in roots:
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO roots(path, recursive, added, last_scan, status) "
                    "VALUES(?,?,?,?,?)",
                    (r["path"], int(r.get("recursive", 1)), r.get("added") or time.time(),
                     r.get("last_scan") or 0, r.get("status") or "ok"),
                )
                self._conn.commit()
        return added
