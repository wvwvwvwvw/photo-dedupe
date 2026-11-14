"""从 7z 包里抽取 hashcat -m 11600 的哈希串（7-Zip 的 AES-256 加密）。

7z 的结构（见 7zFormat.txt）：

    [32 字节签名头]  魔数 / 版本 / NextHeaderOffset / NextHeaderSize / CRC
    [packed 数据]    每个 folder 的压缩（+加密）后的字节
    [NextHeader]     包信息：PackInfo / UnpackInfo(文件夹、编码器) / SubStreamsInfo

加密的 folder 里编码器链形如 `LZMA2 -> AES-256`，AES 的 props 里带 salt / IV / 迭代次数，
CRC 是解压后数据的 CRC（存在包里，不需要密码就能读到）——这两样凑起来就是 hashcat 11600 要的东西：

    $7z$<type>$<numCyclesPower>$<salt_len>$<salt>$<iv_len>$<iv_hex>$<crc>$<data_len>$
        <unpack_size>$<密文 hex>[$<crc_len>$<压缩器属性 hex>]

这里只处理"能验证"的情况：LZMA1 / LZMA2 / Deflate / ZSTD / 不压缩；
BZip2、PPMd 之类 hashcat 校验不了，直接跳过。
"""

from __future__ import annotations

import io
import lzma
import os
import struct
import zlib

MAGIC = b"7z\xbc\xaf\x27\x1c"

# property id
K_END = 0x00
K_HEADER = 0x01
K_ARCHIVE_PROPS = 0x02
K_ADD_STREAMS = 0x03
K_MAIN_STREAMS = 0x04
K_FILES_INFO = 0x05
K_PACK_INFO = 0x06
K_UNPACK_INFO = 0x07
K_SUBSTREAMS_INFO = 0x08
K_SIZE = 0x09
K_CRC = 0x0A
K_FOLDER = 0x0B
K_CODERS_UNPACK_SIZE = 0x0C
K_NUM_UNPACK_STREAM = 0x0D
K_ENCODED_HEADER = 0x17

# 编码器方法 ID
M_AES = bytes([0x06, 0xF1, 0x07, 0x01])
M_LZMA1 = bytes([0x03, 0x01, 0x01])
M_LZMA2 = bytes([0x21])
M_DEFLATE = bytes([0x04, 0x01, 0x08])
M_ZSTD = bytes([0x04, 0xF7, 0x11, 0x01])
M_BZIP2 = bytes([0x04, 0x02, 0x02])
M_PPMD = bytes([0x03, 0x04, 0x01])
M_COPY = bytes([0x00])

DATA_TYPE = {M_LZMA1: 1, M_LZMA2: 2, M_DEFLATE: 7, M_ZSTD: 8}

DEFAULT_IV = b"\x00" * 16
DEFAULT_POWER = 19
MAX_DATA = 4 * 1024 * 1024          # 哈希里带的密文上限（hashcat 允许到 8MB）


class SevenZipError(Exception):
    pass


# ------------------------------------------------------------------ 基础读取
class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def eof(self) -> bool:
        return self.p >= len(self.d)

    def byte(self) -> int:
        if self.p >= len(self.d):
            raise SevenZipError("数据提前结束")
        b = self.d[self.p]
        self.p += 1
        return b

    def peek(self) -> int:
        return self.d[self.p] if self.p < len(self.d) else 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.d):
            raise SevenZipError("读越界")
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def number(self) -> int:
        """7z 的变长整数（高比特标记后面跟几个字节）。"""
        first = self.byte()
        mask = 0x80
        value = 0
        for i in range(8):
            if first & mask == 0:
                value |= (first & (mask - 1)) << (8 * i)
                return value
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]


def _read_bits(r: _Reader, n: int) -> "list[bool]":
    out = []
    mask = 0
    b = 0
    for _ in range(n):
        if mask == 0:
            b = r.byte()
            mask = 0x80
        out.append(bool(b & mask))
        mask >>= 1
    return out


def _read_digests(r: _Reader, n: int) -> "list[int]":
    """kCRC：先一位 allAreDefined，然后按位图读 CRC32。"""
    digests = [0] * n
    if n == 0:
        return digests
    defined = _read_bits(r, n)
    for i in range(n):
        if defined:
            digests[i] = r.u32()
    return digests


# ------------------------------------------------------------------ 结构解析
def _read_folder(r: _Reader) -> dict:
    num_coders = r.number()
    coders = []
    total_in = 0
    total_out = 0
    for _ in range(num_coders):
        flags = r.byte()
        if flags & 0xC0:
            raise SevenZipError("编码器标志异常")
        id_size = flags & 0x0F
        method = r.take(id_size)
        if flags & 0x10:
            num_in = r.number()
            num_out = r.number()
        else:
            num_in = num_out = 1
        props = r.take(r.number()) if flags & 0x20 else b""
        coders.append({"method": method, "props": props,
                       "in": num_in, "out": num_out})
        total_in += num_in
        total_out += num_out
    num_bind = total_out - 1
    binds = []
    for _ in range(max(0, num_bind)):
        binds.append((r.number(), r.number()))
    num_packed = total_in - max(0, num_bind)
    if num_packed == 1:
        used = {i for i, _o in binds}
        free = [i for i in range(total_in) if i not in used]
        packed = free[:1] or [0]
    elif num_packed > 1:
        packed = [r.number() for _ in range(num_packed)]
    else:
        packed = []
    return {"coders": coders, "unpack_sizes": [], "crc": None,
            "out_streams": total_out, "packed": packed}


def _read_pack_info(r: _Reader) -> dict:
    """PackInfo 的实际顺序是：kPackInfo 后先 PackPos、NumPackStreams，再 kSize + 各流大小。

    （7zFormat.txt 的文字顺序容易看反，这里按 7z 自己的写出顺序来。）
    """
    info = {"pack_pos": r.number(), "num_streams": r.number(),
            "sizes": [], "crcref": []}
    if r.byte() != K_SIZE:
        raise SevenZipError("PackInfo 里没有 kSize")
    info["sizes"] = [r.number() for _ in range(info["num_streams"])]
    while True:
        pid = r.byte()
        if pid == K_END:
            break
        if pid == K_CRC:
            info["crcref"] = _read_digests(r, info["num_streams"])
        else:
            r.take(r.number())        # 未知字段：跳过 size + data
    return info


def _read_unpack_info(r: _Reader) -> dict:
    if r.byte() != K_FOLDER:
        raise SevenZipError("UnpackInfo 里没有 kFolder")
    num_folders = r.number()
    external = r.byte()
    if external:
        raise SevenZipError("不支持 external folder 定义")
    folders = [_read_folder(r) for _ in range(num_folders)]
    digests: list[int] = []
    while True:
        pid = r.byte()
        if pid == K_END:
            break
        if pid == K_CODERS_UNPACK_SIZE:
            for f in folders:
                f["unpack_sizes"] = [r.number() for _ in range(f["out_streams"])]
        elif pid == K_CRC:
            digests = _read_digests(r, num_folders)
        else:
            r.take(r.number())
    for fi, f in enumerate(folders):
        f["crc"] = digests[fi] if fi < len(digests) and digests else None
    return {"folders": folders, "num_folders": num_folders}


def _folder_total(f: dict) -> int:
    return f["unpack_sizes"][-1] if f["unpack_sizes"] else 0


def _read_substreams_info(r: _Reader, folders: list) -> dict:
    nums = [1] * len(folders)
    sizes: list[list[int]] = [[] for _ in folders]
    digests: list[int] = []
    while True:
        pid = r.byte()
        if pid == K_END:
            break
        if pid == K_NUM_UNPACK_STREAM:
            nums = [r.number() for _ in folders]
        elif pid == K_SIZE:
            for fi, f in enumerate(folders):
                total = _folder_total(f)
                if nums[fi] <= 1:
                    sizes[fi] = [total]
                    continue
                acc = 0
                arr = []
                for _ in range(nums[fi] - 1):
                    sz = r.number()
                    arr.append(sz)
                    acc += sz
                arr.append(max(0, total - acc))
                sizes[fi] = arr
        elif pid == K_CRC:
            need = sum(nums[fi] for fi in range(len(folders))
                       if not (nums[fi] == 1 and folders[fi]["crc"] is not None))
            digests = _read_digests(r, need)
        else:
            r.take(r.number())
    for fi, f in enumerate(folders):
        if not sizes[fi]:
            sizes[fi] = [_folder_total(f)]
    return {"nums": nums, "sizes": sizes, "digests": digests}


def _read_streams_info(r: _Reader) -> dict:
    pack = None
    unpack = None
    subs = None
    while True:
        pid = r.byte()
        if pid == K_END:
            break
        if pid == K_PACK_INFO:
            pack = _read_pack_info(r)
        elif pid == K_UNPACK_INFO:
            unpack = _read_unpack_info(r)
        elif pid == K_SUBSTREAMS_INFO:
            subs = _read_substreams_info(r, (unpack or {}).get("folders") or [])
        else:
            raise SevenZipError(f"StreamsInfo 里遇到未知字段 0x{pid:02x}")
    return {"pack": pack, "unpack": unpack, "sub": subs}


def _parse_header(data: bytes) -> dict:
    r = _Reader(data)
    if r.byte() != K_HEADER:
        raise SevenZipError("不是 kHeader")
    main = None
    add = None
    while True:
        pid = r.byte()
        if pid == K_END:
            break
        if pid == K_ARCHIVE_PROPS:
            while r.byte() != K_END:
                r.number()
                r.take(r.number())
        elif pid == K_ADD_STREAMS:
            add = _read_streams_info(r)
        elif pid == K_MAIN_STREAMS:
            main = _read_streams_info(r)
        elif pid == K_FILES_INFO:
            # 文件信息：先 numFiles，再一堆「属性 id + size + data」，这里只要跳过
            num_files = r.number()
            for _ in range(num_files + 64):
                t = r.byte()
                if t == K_END:
                    break
                size = r.number()
                r.take(size)
        else:
            raise SevenZipError(f"Header 里遇到未知字段 0x{pid:02x}")
    return {"main": main, "add": add}


# ------------------------------------------------------------------ 解密头部
def _decode_stream(pack_data: bytes, folder: dict, out_size: int) -> bytes:
    """把 encoded header 那一小段解出来（只支持不加密的 LZMA1/LZMA2/Deflate）。"""
    coders = folder["coders"]
    buf = pack_data
    for c in reversed(coders):
        m = c["method"]
        if m == M_AES:
            raise SevenZipError("头部被加密了（-mhe=on），这种包抽不了哈希")
        if m == M_LZMA2:
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW,
                                        filters=[{"id": lzma.FILTER_LZMA2,
                                                  "dict_size": 1 << 26}])
            buf = dec.decompress(buf, max_length=out_size)
        elif m == M_LZMA1:
            props = c["props"][:5]
            if len(props) < 5:
                raise SevenZipError("LZMA1 属性不完整")
            lc = props[0] % 9
            lp = (props[0] // 9) % 5
            pb = props[0] // 45
            dict_size = struct.unpack("<I", props[1:5])[0]
            dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW,
                                        filters=[{"id": lzma.FILTER_LZMA1,
                                                  "lc": lc, "lp": lp, "pb": pb,
                                                  "dict_size": max(dict_size, 4096)}])
            buf = dec.decompress(buf, max_length=out_size)
        elif m == M_DEFLATE:
            buf = zlib.decompressobj(-15).decompress(buf, out_size)
        else:
            raise SevenZipError(f"头部压缩方式不认识：{m.hex()}")
    return buf[:out_size]


def _aes_props(props: bytes) -> dict:
    """7z AES 编码器的属性 → salt / iv / 迭代次数。"""
    out = {"salt": b"", "iv": DEFAULT_IV, "power": DEFAULT_POWER}
    if not props:
        return out
    b0 = props[0]
    out["power"] = b0 & 0x3F
    if (b0 & 0xC0) == 0:
        return out
    salt_len = (b0 >> 7) & 1
    iv_len = (b0 >> 6) & 1
    b1 = props[1] if len(props) > 1 else 0
    salt_len += b1 >> 4
    iv_len += b1 & 0x0F
    off = 2
    out["salt"] = props[off:off + salt_len]
    off += salt_len
    iv = props[off:off + iv_len]
    out["iv"] = (iv + b"\x00" * 16)[:16]
    return out


# ------------------------------------------------------------------ 主入口
def dump_hashes(path: str, max_data: int = MAX_DATA) -> dict:
    """返回 {"hashes": [...], "info": {...}}；抽不出来时 hashes 为空并给 error。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(32)
            if len(head) < 32 or head[:6] != MAGIC:
                return {"hashes": [], "error": "不是 7z 文件"}
            next_off, next_size = struct.unpack("<QQ", head[12:28])
            f.seek(32 + next_off)
            hdr = f.read(next_size)
            # 读 packed 数据（只读需要的前缀，最多 max_data）
            f.seek(0)
            blob = f.read(min(size, 32 + next_off + max_data))
    except OSError as exc:
        return {"hashes": [], "error": f"读不了：{exc}"}
    try:
        if hdr[:1] == bytes([K_ENCODED_HEADER]):
            r = _Reader(hdr)
            r.byte()
            streams = _read_streams_info(r)
            folders = (streams.get("unpack") or {}).get("folders") or []
            pack = streams.get("pack") or {}
            if not folders or not pack.get("sizes"):
                raise SevenZipError("encoded header 结构不完整")
            folder = folders[0]
            out_size = folder["unpack_sizes"][-1] if folder["unpack_sizes"] else 0
            start = 32 + pack["pack_pos"]
            packed = blob[start:start + pack["sizes"][0]]
            hdr = _decode_stream(packed, folder, out_size)
        info = _parse_header(hdr)
    except (SevenZipError, lzma.LZMAError, zlib.error, struct.error) as exc:
        return {"hashes": [], "error": str(exc)}
    main = info["main"] or {}
    pack = main.get("pack") or {}
    folders = (main.get("unpack") or {}).get("folders") or []
    sub = main.get("sub") or {}
    if not folders or not pack.get("sizes"):
        return {"hashes": [], "error": "没有可用的数据流（可能是空包）"}
    # pack stream 的绝对偏移
    offs = []
    pos = 32 + pack["pack_pos"]
    for s in pack["sizes"]:
        offs.append((pos, s))
        pos += s
    out_hashes = []
    infos = []
    pack_idx = 0
    for fi, folder in enumerate(folders):
        n_packed = sum(c["in"] for c in folder["coders"]) - (sum(c["out"] for c in folder["coders"]) - 1)
        n_packed = max(1, n_packed)
        my_packs = list(range(pack_idx, min(pack_idx + n_packed, len(offs))))
        pack_idx += n_packed
        aes = next((c for c in folder["coders"] if c["method"] == M_AES), None)
        if aes is None or not my_packs:
            continue
        others = [c for c in folder["coders"]
                  if c["method"] not in (M_AES, M_COPY)]
        comp = others[-1] if others else None
        if comp is None:
            data_type = 0
            coder_attrs = b""
        elif comp["method"] in DATA_TYPE:
            data_type = DATA_TYPE[comp["method"]]
            coder_attrs = comp["props"]
        else:
            infos.append({"skip": f"压缩方式 {comp['method'].hex()} 不支持（BZip2/PPMd 等）"})
            continue
        # CRC / 长度
        crc = folder.get("crc")
        crc_len = folder["unpack_sizes"][-1] if folder["unpack_sizes"] else 0
        if crc is None:
            # 退回 SubStreamsInfo 的第一段
            dgs = sub.get("digests") or []
            sizes = sub.get("sizes") or []
            if dgs:
                crc = dgs[0]
                if sizes and sizes[0]:
                    crc_len = sizes[0][0]
        if crc is None:
            infos.append({"skip": "包里没存 CRC，hashcat 验证不了"})
            continue
        pack_size = offs[my_packs[0]][1]
        if data_type == 0:
            # 不压缩：hashcat 会对 unpack_size 个解密字节算 CRC，所以密文必须够长
            unpack_size = crc_len
            if pack_size < unpack_size:
                infos.append({"skip": "包内数据不完整"})
                continue
            if unpack_size > max_data:
                infos.append({"skip": f"未压缩的 {unpack_size} 字节超过上限 {max_data}，没法做成哈希"})
                continue
            # AES 是按 16 字节块解密的：最后一块即使只用到几字节，也得把整块密文给全
            need = min(pack_size, ((unpack_size + 15) // 16) * 16)
            if need < unpack_size:
                infos.append({"skip": "密文不足一个整块"})
                continue
        else:
            if crc_len > 9_999_999:
                infos.append({"skip": f"解压后 {crc_len} 字节太大，hashcat 校验不了"})
                continue
            need = min(max_data, pack_size)
            need -= need % 16
            if need < 16:
                infos.append({"skip": "数据太短"})
                continue
            unpack_size = crc_len
        start, _sz = offs[my_packs[0]]
        ct = blob[start:start + need]
        if len(ct) < 16:
            infos.append({"skip": "密文不够 16 字节"})
            continue
        ap = _aes_props(aes["props"])
        if unpack_size > len(ct):          # 压缩数据时这个字段不参与校验，压到 data_len
            unpack_size = len(ct)
        parts = ["$7z$", str(data_type), "$", str(ap["power"]), "$",
                 str(len(ap["salt"])), "$", ap["salt"].hex(), "$",
                 str(len(ap["iv"].rstrip(b"\x00")) or 16), "$", ap["iv"].hex(), "$",
                 str(crc), "$", str(len(ct)), "$", str(unpack_size), "$", ct.hex()]
        if data_type != 0:
            parts += ["$", str(crc_len), "$", coder_attrs.hex()]
        out_hashes.append("".join(parts))
        infos.append({"folder": fi, "data_type": data_type, "crc": crc,
                      "crc_len": crc_len, "ct_len": len(ct), "pack": my_packs[0]})
    if not out_hashes and not infos:
        return {"hashes": [], "error": "没有加密的数据流（没设密码？）"}
    return {"hashes": out_hashes, "info": infos}
