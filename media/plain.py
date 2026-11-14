"""已知明文攻击作业：一个 zip 包 → 试出密钥 → 生成去掉密码的新包。

放这里而不是 unzip.py，是因为它只跟 bkcrack 有关，跟普通解压流程无关。
"""

from __future__ import annotations

import os
import threading
import time
import zipfile

from media import bkcrack as BK


class PlainJob(threading.Thread):
    daemon = True

    def __init__(self, zip_path: str, plain: bytes, offset: int = 0,
                 recover_password: bool = True, charset: str = "?d", max_len: int = 8,
                 auto: bool = False):
        super().__init__(name="plain-job")
        self.zip_path = os.path.abspath(zip_path)
        self.plain = plain or b""
        self.auto = bool(auto) or not self.plain    # 没给明文 → 自动猜文件头
        self.offset = max(0, int(offset or 0))
        self.recover = bool(recover_password)
        self.charset = charset or "?d"
        self.max_len = max(1, min(16, int(max_len or 8)))
        self.cancel_ev = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._st = {"running": True, "state": "running", "total": 0, "done": 0,
                    "current": "", "message": "准备中…", "log": [], "result": {},
                    "elapsed": 0, "zip": self.zip_path}

    # ---------- 状态 ----------
    def log(self, msg: str):
        with self._lock:
            lst = self._st["log"]
            lst.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(lst) > 300:
                del lst[:80]

    def cancel(self):
        self.cancel_ev.set()

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._st)
            s["log"] = list(self._st["log"])
            s["result"] = dict(self._st["result"])
        s["elapsed"] = round(time.time() - self._t0, 1)
        return s

    def _set(self, **kw):
        with self._lock:
            self._st.update(kw)

    # ---------- 主流程 ----------
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
        st = BK.status()
        if not st["available"]:
            self._set(state="error", message=st["error"] or "bkcrack 不可用")
            self.log(self._st["message"])
            return
        if self.auto:
            self.log(f"bkcrack {st['version']}：没给明文 → 自动猜文件头")
        else:
            self.log(f"bkcrack {st['version']}：已知明文 {len(self.plain)} 字节，偏移 {self.offset}")
            if len(self.plain) < BK.MIN_PLAIN:
                self._set(state="error", message=f"已知明文太少（要 ≥{BK.MIN_PLAIN} 字节）")
                self.log(self._st["message"])
                return
        try:
            with open(self.zip_path, "rb") as f:
                data = f.read(16 * 1024 * 1024)
        except OSError as exc:
            self._set(state="error", message=f"读不了压缩包：{exc}")
            return
        entries = BK.zipcrypto_entries(data)
        cands = [e for e in entries if e["size"] >= BK.MIN_PLAIN + self.offset]
        guesses: "list[dict]" = []
        if self.auto:
            from media import plainguess as PG
            guesses = PG.candidates(entries)
            if not guesses:
                msg = ("自动猜只对**不压缩（stored）**的加密条目有效；"
                       "这个包里的加密条目都是压缩过的，或者后缀认不出来")
                self._set(state="error", message=msg)
                self.log(msg)
                return
            self.log(f"自动猜出 {len(guesses)} 个候选：" +
                     "、".join(f"{g['entry']}={g['label']}" for g in guesses[:6]))
        if not entries:
            msg = "这个包里没有传统加密（ZipCrypto）的条目，明文攻击用不上"
            if b"PK\x03\x04" not in data[:4]:
                msg = "这不是 zip 包（或已损坏）"
            self._set(state="error", message=msg)
            self.log(msg)
            return
        if not cands:
            msg = f"有 {len(entries)} 个加密条目，但都太小（< {BK.MIN_PLAIN} 字节），试不了"
            self._set(state="error", message=msg)
            self.log(msg)
            return
        cands.sort(key=lambda e: e["size"])
        self._set(total=len(cands))
        self.log(f"候选条目 {len(cands)} 个：" +
                 "、".join(f"{c['name']}({c['size']}B)" for c in cands[:6]) +
                 ("…" if len(cands) > 6 else ""))
        jobs: "list[dict]" = []
        if self.auto:
            jobs = guesses
        else:
            jobs = [{"entry": e["name"], "label": "", "plain": self.plain} for e in cands]
        self._set(total=len(jobs))
        for idx, job in enumerate(jobs):
            if self.cancel_ev.is_set():
                self.log("已取消")
                return
            ent_name = job["entry"]
            plain = job["plain"]
            self._set(current=ent_name, done=idx)
            if self.auto:
                self.log(f"→ 试 {ent_name}，按 {job['label']} 的文件头猜（{len(plain)} 字节）")
            else:
                self.log(f"→ 试条目 {ent_name}")
            r = BK.attack(self.zip_path, ent_name, plain, offset=self.offset,
                          cancel=self.cancel_ev, log=self.log)
            if self.cancel_ev.is_set():
                self.log("已取消")
                return
            if not r.get("ok"):
                self.log(f"  没成功：{r.get('error')}")
                continue
            keys = r["keys"]
            out = self._out_path()
            self.log("  整包解密中…")
            d = BK.decrypt(self.zip_path, keys, out)
            if not d.get("ok"):
                self._set(state="error", message=f"拿到密钥但解密失败：{d.get('error')}")
                self.log(self._st["message"])
                return
            ok = self._verify(out)
            pwd = ""
            if self.recover:
                self.log(f"  顺便反推原密码（≤{self.max_len} 位 {self.charset}）…")
                pwd = BK.recover_password(keys, self.max_len, self.charset)
                if pwd:
                    self.log(f"  原密码：{pwd}")
                    try:
                        from media.unzip import save_found
                        save_found(pwd, self.zip_path)
                    except Exception:  # noqa: BLE001
                        pass
            with self._lock:
                self._st["result"] = {"keys": keys, "out": out, "entry": ent_name,
                                      "guess": job.get("label", ""),
                                      "password": pwd, "variant": r.get("variant", ""),
                                      "verified": bool(ok)}
            self._set(done=len(cands), message="完成")
            self.log(f"完成：解密包 {out}" + ("（校验通过）" if ok else "（校验没通过，可能只解对了一部分）"))
            return
        self._set(done=len(jobs), state="done", message="没试出来")
        self.log("所有候选都没试出来：明文对不上，或明文太短")

    # ---------- 辅助 ----------
    def _out_path(self) -> str:
        stem, _ext = os.path.splitext(self.zip_path)
        return stem + "_nopass.zip"

    @staticmethod
    def _verify(path: str) -> bool:
        """解开后能正常列出条目，并且每个文件都能读出来（说明密钥对）。"""
        try:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                return bad is None
        except Exception:  # noqa: BLE001
            return False
