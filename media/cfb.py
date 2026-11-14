"""最小 OLE 复合文档（CFB / Compound File Binary）读取器。

只做一件事：按名字把流取出来。加密的 Office 文件（.docx/.xlsx/.pptx/.doc/.xls）
外面都套着这层容器，里面才有 EncryptionInfo / EncryptedPackage 之类的流。

格式要点（[MS-CFB]）：
* 512 字节文件头，扇区大小 2 的幂（通常 512，老文件 4096）
* FAT（扇区分配表）和 MiniFAT（小于 4096 字节的流专用）
* 目录项 128 字节一个：名字（UTF-16LE）、类型、起始扇区、大小
"""

from __future__ import annotations

import struct

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC

STREAM_OBJECT = 2
ROOT_ENTRY = 5


class CFBError(Exception):
    pass


class CFB:
    def __init__(self, data: bytes):
        if len(data) < 512 or data[:8] != MAGIC:
            raise CFBError("不是 OLE 复合文档")
        self.d = data
        (sector_shift,) = struct.unpack("<H", data[30:32])
        (mini_shift,) = struct.unpack("<H", data[32:34])
        self.sector_size = 1 << sector_shift
        self.mini_size = 1 << mini_shift
        (self.num_fat,) = struct.unpack("<I", data[44:48])
        (self.dir_start,) = struct.unpack("<I", data[48:52])
        (self.mini_cutoff,) = struct.unpack("<I", data[56:60])
        (self.mini_fat_start,) = struct.unpack("<I", data[60:64])
        (self.num_mini_fat,) = struct.unpack("<I", data[64:68])
        (self.difat_start,) = struct.unpack("<I", data[68:72])
        (self.num_difat,) = struct.unpack("<I", data[72:76])
        self._fat: "list[int]" = []
        self._minifat: "list[int]" = []
        self._dirs: "list[dict]" = []
        self._load_fat()
        self._load_dirs()
        self._load_minifat()

    # ---------- 扇区 ----------
    def _sector(self, idx: int) -> bytes:
        off = (idx + 1) * self.sector_size
        return self.d[off:off + self.sector_size]

    def _chain(self, start: int, fat: "list[int]") -> "list[int]":
        out = []
        cur = start
        seen = set()
        while cur not in (ENDOFCHAIN, FREESECT) and cur < len(fat) and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = fat[cur]
            if len(out) > 100000:
                break
        return out

    def _load_fat(self) -> None:
        fat_sectors = list(struct.unpack("<109I", self.d[76:512]))
        # DIFAT 后续扇区
        cur = self.difat_start
        n = self.num_difat
        seen = set()
        while cur not in (ENDOFCHAIN, FREESECT) and n > 0 and cur not in seen:
            seen.add(cur)
            block = self._sector(cur)
            vals = list(struct.unpack(f"<{self.sector_size // 4}I", block))
            fat_sectors.extend(vals[:-1])
            cur = vals[-1]
            n -= 1
        fat = []
        for si in fat_sectors:
            if si in (FREESECT, ENDOFCHAIN):
                continue
            block = self._sector(si)
            if len(block) < 4:
                continue
            fat.extend(struct.unpack(f"<{len(block) // 4}I", block))
        self._fat = fat

    def _load_minifat(self) -> None:
        if self.mini_fat_start in (ENDOFCHAIN, FREESECT):
            self._minifat = []
            return
        chain = self._chain(self.mini_fat_start, self._fat)
        raw = b"".join(self._sector(s) for s in chain)
        self._minifat = list(struct.unpack(f"<{len(raw) // 4}I", raw)) if raw else []

    def _load_dirs(self) -> None:
        chain = self._chain(self.dir_start, self._fat)
        raw = b"".join(self._sector(s) for s in chain)
        dirs = []
        for i in range(0, len(raw) - 127, 128):
            ent = raw[i:i + 128]
            name_len = struct.unpack("<H", ent[64:66])[0]
            name = ""
            if 2 <= name_len <= 64:
                try:
                    name = ent[:name_len - 2].decode("utf-16-le", "replace")
                except Exception:  # noqa: BLE001
                    name = ""
            (obj_type,) = struct.unpack("<B", ent[66:67])
            (start,) = struct.unpack("<I", ent[116:120])
            (size,) = struct.unpack("<Q", ent[120:128])
            dirs.append({"name": name, "type": obj_type, "start": start, "size": size})
        self._dirs = dirs
        self._root = next((d for d in dirs if d["type"] == ROOT_ENTRY), None)

    # ---------- 对外 ----------
    def names(self) -> "list[str]":
        return [d["name"] for d in self._dirs if d["type"] == STREAM_OBJECT]

    def read(self, name: str) -> bytes:
        ent = next((d for d in self._dirs
                    if d["type"] == STREAM_OBJECT and d["name"] == name), None)
        if ent is None:
            raise CFBError(f"没有这个流：{name}")
        size = ent["size"]
        if size >= (self.mini_cutoff or 4096) or self._root is None:
            chain = self._chain(ent["start"], self._fat)
            data = b"".join(self._sector(s) for s in chain)
        else:
            data = self._read_mini(ent["start"])
        return data[:size]

    def _read_mini(self, start: int) -> bytes:
        root = self._root
        assert root is not None
        mini_stream_chain = self._chain(root["start"], self._fat)
        mini = b"".join(self._sector(s) for s in mini_stream_chain)
        out = []
        cur = start
        seen = set()
        while cur not in (ENDOFCHAIN, FREESECT) and cur < len(self._minifat) and cur not in seen:
            seen.add(cur)
            off = cur * self.mini_size
            out.append(mini[off:off + self.mini_size])
            cur = self._minifat[cur]
            if len(out) > 100000:
                break
        return b"".join(out)


def open_cfb(data: bytes) -> CFB:
    return CFB(data)
