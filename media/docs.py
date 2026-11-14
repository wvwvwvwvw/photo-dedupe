"""文档类加密文件的密码校验：PDF、Office（docx/xlsx/pptx，Agile + Standard）。

为什么单独做：这些格式没有现成的 hashcat 哈希抽取工具（7z/rar 那套用不上），
但**密码校验**本身不复杂——算出候选密码对应的密钥，跟文件里存的校验值比一下即可。
校验是纯本地计算，候选密码来自解压/破解页的密码本或穷举。

支持范围：
* PDF：标准安全处理器 R2 / R3 / R4（RC4-40/128、AES-128）和 R5 / R6（AES-256）
* Office 2007+（.docx/.xlsx/.pptx）：Agile（2010+，AES+SHA）和 Standard（2007，AES+SHA1）
* Office 97-2003（.doc/.xls/.ppt）：暂不支持（要按格式逐个解析 FIB/BootRecord）

依赖：hashlib + Cryptodome（打包的 runtime 里就有），不装额外东西。
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import struct
import zipfile

try:
    from Cryptodome.Cipher import AES, ARC4
except Exception:  # noqa: BLE001
    from Crypto.Cipher import AES, ARC4  # type: ignore

PDF_MAGIC = b"%PDF-"
CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"

# ---------------------------------------------------------------- 嗅探


def sniff(data: bytes) -> str:
    """认格式：pdf / ooxml / ole / zip / 空。"""
    if data.startswith(PDF_MAGIC) or b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith(CFB_MAGIC):
        return "ole"
    if data.startswith(ZIP_MAGIC):
        return "zip"
    return ""


def is_encrypted(data: bytes) -> bool:
    return bool(info(data).get("encrypted"))


def info(data: bytes) -> dict:
    """判断一个文件是不是"有密码的文档"，返回 {format, encrypted, kind, detail}。"""
    fmt = sniff(data)
    if fmt == "pdf":
        try:
            d = _pdf_encrypt_dict(data)
        except Exception:  # noqa: BLE001
            return {"format": "pdf", "encrypted": False, "kind": "", "detail": ""}
        if not d:
            return {"format": "pdf", "encrypted": False, "kind": "", "detail": ""}
        return {"format": "pdf", "encrypted": True, "kind": f"PDF R{d.get('R')}",
                "detail": f"V={d.get('V')} 长度={d.get('Length') or 40}"}
    if fmt == "ole":
        try:
            from media import cfb
            f = cfb.CFB(data)
            names = f.names()
        except Exception:  # noqa: BLE001
            return {"format": "ole", "encrypted": False, "kind": "", "detail": ""}
        if "EncryptionInfo" in names and "EncryptedPackage" in names:
            return {"format": "ole", "encrypted": True, "kind": "Office 2007+",
                    "detail": "docx/xlsx/pptx"}
        if "WordDocument" in names:
            return {"format": "ole", "encrypted": True, "kind": "Word 97-2003",
                    "detail": "暂不支持"}
        if "Workbook" in names or "Book" in names:
            return {"format": "ole", "encrypted": True, "kind": "Excel 97-2003",
                    "detail": "暂不支持"}
        if "PowerPoint Document" in names:
            return {"format": "ole", "encrypted": True, "kind": "PowerPoint 97-2003",
                    "detail": "暂不支持"}
        return {"format": "ole", "encrypted": False, "kind": "", "detail": ""}
    return {"format": fmt, "encrypted": False, "kind": "", "detail": ""}


def check_password(data: bytes, pw: bytes) -> "tuple[bool, str]":
    """校验一个候选密码。返回 (对不对, 说明)。"""
    fmt = sniff(data)
    try:
        if fmt == "pdf":
            return _pdf_check(data, pw), "pdf"
        if fmt == "ole":
            kind = info(data).get("kind", "")
            if kind == "Office 2007+":
                return _ooxml_check(data, pw), "office"
            return False, f"{kind} 暂不支持"
    except Exception as exc:  # noqa: BLE001
        return False, f"校验出错：{exc}"
    return False, "不认识的格式"


def prepared(data: bytes) -> dict:
    """解析一次，返回 {kind, check(pw)->bool}，批量试密码时用这个（快很多）。"""
    fmt = sniff(data)
    if fmt == "pdf":
        d = _pdf_encrypt_dict(data)
        if not d:
            return {"kind": "", "check": lambda pw: False}
        r = int(d.get("R") or 0)

        def check_pdf(pw: bytes) -> bool:
            return _pdf_check_r56(pw, d) if r >= 5 else _pdf_check_with(d, pw)

        return {"kind": f"PDF R{r}", "check": check_pdf}
    if fmt == "ole":
        inf = info(data)
        if inf.get("kind") != "Office 2007+":
            return {"kind": inf.get("kind") or "", "check": lambda pw: False}
        from media import cfb
        f = cfb.CFB(data)
        ei = f.read("EncryptionInfo")
        major, minor, flags = struct.unpack("<HHI", ei[:8])
        body = ei[8:]
        if body[:1] == b"<" or flags & 0x40:
            params = _agile_params(body)

            def check_agile(pw: bytes) -> bool:
                return _agile_verify(params, pw)

            return {"kind": "Office Agile", "check": check_agile}
        params = _standard_params(body)

        def check_std(pw: bytes) -> bool:
            return _standard_verify(params, pw)

        return {"kind": "Office Standard", "check": check_std}
    return {"kind": "", "check": lambda pw: False}


def check_many(data: bytes, cands: "list[str]", cancel=None) -> "str | None":
    """一次解析、批量校验（CPU 上跑字典用）。"""
    p = prepared(data)
    chk = p["check"]
    for i, pw in enumerate(cands):
        if cancel is not None and i % 500 == 0 and cancel.is_set():
            return None
        if chk(pw.encode("utf-8")):
            return pw
    return None


# ---------------------------------------------------------------- PDF
_PDF_PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56,
    0xFF, 0xFA, 0x01, 0x08, 0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A])


def _rc4(key: bytes, data: bytes) -> bytes:
    return ARC4.new(key).encrypt(data)


def _pad_pw(pw: bytes) -> bytes:
    return (pw + _PDF_PAD)[:32]


def _pdf_token(d: dict, name: str):
    m = re.search(rb"/" + name.encode() + rb"\s*(<[0-9A-Fa-f\s]+>|[-\d]+|/(\w+)|\[[^\]]*\]|\([^)]*\))",
                  d.get("_raw", b""))
    return m.group(1) if m else None


def _hex_to_bytes(tok) -> bytes:
    if tok is None:
        return b""
    if isinstance(tok, bytes):
        tok = tok.decode("latin-1", "replace")
    s = str(tok).strip()
    if s.startswith("<") and s.endswith(">"):
        s = re.sub(r"/\s+", "", s[1:-1])
        try:
            return bytes.fromhex(s)
        except ValueError:
            return b""
    return b""


def _pdf_encrypt_dict(data: bytes) -> dict:
    """找出 PDF 的 /Encrypt 字典里我们需要的字段（R2~R6）。"""
    # 1) 找带 /Filter /Standard 的字典原文（整个文件都找：加密字典常在文件末尾）
    best = b""
    scan = data[:64 * 1024 * 1024]
    for m in re.finditer(rb"/(?:Filter\s*/Standard|CF\s*<<)", scan):
        start = max(0, scan.rfind(b"<<", 0, m.start()))
        end = scan.find(b">>", m.start())
        if start >= 0 and end > start:
            cand = scan[start:end + 2]
            if b"/V" in cand and b"/R" in cand and (b"/O" in cand or b"/U" in cand):
                best = cand
                break
    if not best:
        return {}
    d = {"_raw": best}

    def num(name: str, default: int = 0) -> int:
        t = _pdf_token(d, name)
        try:
            return int(re.sub(rb"[^\d-]", b"", t or b"") or default)
        except ValueError:
            return default

    d["V"] = num("V")
    d["R"] = num("R")
    d["Length"] = num("Length", 40)
    d["P"] = num("P")
    # O / U 是字符串（<hex> 或 (literal)）
    for name in ("O", "U", "OE", "UE", "Perms"):
        t = _pdf_token(d, name)
        if t and t.startswith(b"("):
            d[name] = t[1:-1]
        else:
            d[name] = _hex_to_bytes(t)
    if len(d["O"]) != 32 or len(d["U"]) < 32:
        # 直接按 hex 再试一遍
        for name in ("O", "U"):
            m = re.search(rb"/" + name.encode() + rb"\s*<([0-9A-Fa-f\s]+)>", best)
            if m:
                d[name] = bytes.fromhex(re.sub(rb"\s+", b"", m.group(1)).decode())
    if d["V"] == 0:
        return {}
    # 文档 ID（加密密钥要用）
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", data)
    d["ID0"] = bytes.fromhex(m.group(1).decode()) if m else b""
    d["EncryptMetadata"] = b"/EncryptMetadata false" not in best
    return d


def _pdf_key(pw: bytes, d: dict) -> bytes:
    """按 Algorithm 2 从密码算加密密钥（R2~R4）。"""
    r = int(d.get("R") or 2)
    n = max(5, int(d.get("Length") or 40) // 8) if r >= 3 else 5
    if r >= 5:
        return b""
    md = hashlib.md5()
    md.update(_pad_pw(pw))
    md.update(d["O"][:32])
    md.update(struct.pack("<i", int(d.get("P") or 0)))
    md.update(d["ID0"])
    if r >= 4 and not d.get("EncryptMetadata", True):
        md.update(b"\xff\xff\xff\xff")
    key = md.digest()
    if r >= 3:
        for _ in range(50):
            key = hashlib.md5(key[:n]).digest()
    return key[:n]


def _pdf_check(data: bytes, pw: bytes) -> bool:
    d = _pdf_encrypt_dict(data)
    if not d:
        return False
    return _pdf_check_with(d, pw)


def _pdf_check_with(d: dict, pw: bytes) -> bool:
    r = int(d.get("R") or 0)
    if r >= 5:
        return _pdf_check_r56(pw, d)
    key = _pdf_key(pw, d)
    if not key:
        return False
    u = d["U"]
    if r == 2:
        return _rc4(key, _PDF_PAD) == u[:32]
    x = hashlib.md5(_PDF_PAD + d["ID0"]).digest()
    x = _rc4(key, x)
    for i in range(1, 20):
        x = _rc4(bytes(b ^ i for b in key), x)
    return x == u[:16]


def _pdf_hash_r56(rev: int, pw: bytes, salt: bytes, udata: bytes) -> bytes:
    """PDF 2.0 的 Algorithm 2.B（R5 就是一次 SHA-256，R6 是强化哈希）。

    依据 pypdf / qpdf 的实现：R6 循环里用 K[16:32] 当 AES 的 IV，数据是
    (password + K + udata) 重复 64 份。
    """
    k = hashlib.sha256(pw + salt + udata).digest()
    if rev < 6:
        return k
    count = 0
    while True:
        count += 1
        k1 = (pw + k + udata) * 64
        e = AES.new(k[:16], AES.MODE_CBC, k[16:32]).encrypt(k1)
        fn = (hashlib.sha256, hashlib.sha384, hashlib.sha512)[sum(e[:16]) % 3]
        k = fn(e).digest()
        if count >= 64 and e[-1] <= count - 32:
            break
    return k[:32]


def _pdf_check_r56(pw: bytes, d: dict) -> bool:
    """R5/R6 的密码校验：先当用户密码试，再当所有者密码试。"""
    u = d.get("U") or b""
    o = d.get("O") or b""
    rev = int(d.get("R") or 5)
    if len(u) < 48:
        return False
    if len(pw) > 127:
        pw = pw[:127]
    # 用户密码：SHA-256(密码 + U 的校验盐)
    if _pdf_hash_r56(rev, pw, u[32:40], b"") == u[:32]:
        return True
    # 所有者密码：SHA-256(密码 + O 的校验盐 + U 前 48 字节)
    if len(o) >= 48 and _pdf_hash_r56(rev, pw, o[32:40], u[:48]) == o[:32]:
        return True
    return False


# ---------------------------------------------------------------- Office 2007+
def _ooxml_check(data: bytes, pw: bytes) -> bool:
    from media import cfb
    f = cfb.CFB(data)
    ei = f.read("EncryptionInfo")
    if len(ei) < 8:
        return False
    major, minor, flags = struct.unpack("<HHI", ei[:8])
    body = ei[8:]
    if body[:1] == b"<" or flags & 0x40:
        return _agile_check(body, pw)
    return _standard_check(body, pw)


def _standard_params(body: bytes) -> dict:
    """解析一次，返回校验需要的参数。"""
    out = {"ok": False}
    if len(body) < 16:
        return out
    (header_size,) = struct.unpack("<I", body[:4])
    hdr = body[4:4 + header_size]
    if len(hdr) < 28:
        return out
    _flags, _extra, alg_id, alg_hash, key_size, _prov, _res = struct.unpack("<7I", hdr[:28])
    ver = body[4 + header_size:]
    if len(ver) < 8:
        return out
    (salt_size,) = struct.unpack("<I", ver[:4])
    salt = ver[4:4 + salt_size]
    off = 4 + salt_size
    enc_verifier = ver[off:off + 16]
    off += 16
    if len(enc_verifier) < 16 or off + 4 > len(ver):
        return out
    (hash_size,) = struct.unpack("<I", ver[off:off + 4])
    off += 4
    enc_hash = ver[off:off + ((hash_size + 15) // 16) * 16]
    if len(enc_hash) < 20:
        return out
    out.update(ok=True, key_size=key_size, salt=salt,
               enc_verifier=enc_verifier, enc_hash=enc_hash)
    return out


def _standard_verify(params: dict, pw: bytes) -> bool:
    if not params.get("ok"):
        return False
    key = _standard_key(pw, params["key_size"], params["salt"])
    if not key:
        return False
    try:
        verifier = AES.new(key, AES.MODE_ECB).decrypt(params["enc_verifier"])
        calc = hashlib.sha1(verifier).digest()
        stored = AES.new(key, AES.MODE_ECB).decrypt(params["enc_hash"])[:20]
    except ValueError:
        return False
    return calc == stored


def _standard_check(body: bytes, pw: bytes) -> bool:
    if len(body) < 16:
        return False
    (header_size,) = struct.unpack("<I", body[:4])
    hdr = body[4:4 + header_size]
    if len(hdr) < 28:
        return False
    _flags, _extra, alg_id, alg_hash, key_size, _prov, _res = struct.unpack("<7I", hdr[:28])
    ver = body[4 + header_size:]
    if len(ver) < 8:
        return False
    (salt_size,) = struct.unpack("<I", ver[:4])
    salt = ver[4:4 + salt_size]
    off = 4 + salt_size
    enc_verifier = ver[off:off + 16]
    if len(enc_verifier) < 16:
        return False
    off += 16
    (hash_size,) = struct.unpack("<I", ver[off:off + 4])
    off += 4
    enc_hash = ver[off:off + ((hash_size + 15) // 16) * 16]
    if len(enc_hash) < 20:
        return False
    key = _standard_key(pw, key_size, salt)
    if not key:
        return False
    verifier = AES.new(key, AES.MODE_ECB).decrypt(enc_verifier)
    calc = hashlib.sha1(verifier).digest()
    stored = AES.new(key, AES.MODE_ECB).decrypt(enc_hash)[:20]
    return calc == stored


def _standard_key(pw: bytes, key_size: int, salt: bytes) -> bytes:
    """ECMA-376 Standard 的中间密钥（50000 轮 SHA1）。"""
    h = hashlib.sha1(salt + pw.decode("latin-1", "replace").encode("utf-16le")).digest()
    for i in range(50000):
        h = hashlib.sha1(struct.pack("<I", i) + h).digest()
    hfinal = hashlib.sha1(h + struct.pack("<I", 0)).digest()
    cb = 20
    x1 = hashlib.sha1(bytes(a ^ b for a, b in zip(hfinal, b"\x36" * cb))
                      + b"\x36" * (64 - cb)).digest()
    x2 = hashlib.sha1(bytes(a ^ b for a, b in zip(hfinal, b"\x5c" * cb))
                      + b"\x5c" * (64 - cb)).digest()
    return (x1 + x2)[:max(1, key_size // 8)]


def _agile_xml(body: bytes) -> "tuple[dict, dict]":
    """拆出 <keyData> 和 <encryptedKey> 的属性（都是十六进制字符串）。"""
    end = body.find(b"</encryption>")
    xml = body[:end + 13] if end > 0 else body
    text = xml.decode("utf-8", "replace")

    def attrs(tag: str) -> dict:
        m = re.search(r"<" + tag + r"\b([^>]*)>", text)
        if not m:
            return {}
        out = {}
        for k, v in re.findall(r"(\w+)\s*=\s*\"([^\"]*)\"", m.group(1)):
            out[k] = v
        return out

    return attrs("keyData"), attrs("p:encryptedKey")


def _derive_iterated(pw: bytes, salt: bytes, alg: str, spin: int) -> bytes:
    h = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256,
         "SHA384": hashlib.sha384, "SHA512": hashlib.sha512}.get(alg, hashlib.sha1)
    pw16 = pw.decode("latin-1", "replace").encode("utf-16le")
    x = h(salt + pw16).digest()
    for i in range(spin):
        x = h(struct.pack("<I", i) + x).digest()
    return x


def _derive_key(hval: bytes, block_key: bytes, alg: str, key_bits: int) -> bytes:
    h = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256,
         "SHA384": hashlib.sha384, "SHA512": hashlib.sha512}.get(alg, hashlib.sha1)
    out = h(hval + block_key).digest()
    n = max(1, key_bits // 8)
    return (out + b"\x36" * n)[:n]


def _agile_params(body: bytes) -> dict:
    key_data, ek = _agile_xml(body)
    if not ek:
        return {"ok": False}
    try:
        return {"ok": True,
                "salt": base64.b64decode(ek.get("saltValue", "")),
                "evhi": base64.b64decode(ek.get("encryptedVerifierHashInput", "")),
                "evhv": base64.b64decode(ek.get("encryptedVerifierHashValue", "")),
                "spin": int(ek.get("spinCount") or 100000),
                "alg": ek.get("hashAlgorithm") or "SHA512",
                "bits": int(ek.get("keyBits") or 256)}
    except (ValueError, TypeError):
        return {"ok": False}


def _agile_verify(params: dict, pw: bytes) -> bool:
    if not params.get("ok"):
        return False
    salt = params["salt"]
    alg = params["alg"]
    bits = params["bits"]
    if not salt or not params["evhi"] or not params["evhv"]:
        return False
    hval = _derive_iterated(pw, salt, alg, params["spin"])
    k1 = _derive_key(hval, bytes([0xFE, 0xA7, 0xD2, 0x76, 0x3B, 0x4B, 0x9E, 0x79]), alg, bits)
    k2 = _derive_key(hval, bytes([0xD7, 0xAA, 0x0F, 0x6D, 0x30, 0x61, 0x34, 0x4E]), alg, bits)
    try:
        verifier = AES.new(k1, AES.MODE_CBC, salt).decrypt(params["evhi"])
        expected = AES.new(k2, AES.MODE_CBC, salt).decrypt(params["evhv"])
    except ValueError:
        return False
    h = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256,
         "SHA384": hashlib.sha384, "SHA512": hashlib.sha512}.get(alg, hashlib.sha1)
    return h(verifier).digest() == expected


def _agile_check(body: bytes, pw: bytes) -> bool:
    key_data, ek = _agile_xml(body)
    if not ek:
        return False
    try:
        # Agile 的 XML 属性都是 base64
        salt = base64.b64decode(ek.get("saltValue", ""))
        evhi = base64.b64decode(ek.get("encryptedVerifierHashInput", ""))
        evhv = base64.b64decode(ek.get("encryptedVerifierHashValue", ""))
        spin = int(ek.get("spinCount") or 100000)
        alg = ek.get("hashAlgorithm") or "SHA512"
        bits = int(ek.get("keyBits") or 256)
    except (ValueError, TypeError):
        return False
    if not salt or not evhi or not evhv:
        return False
    hval = _derive_iterated(pw, salt, alg, spin)
    k1 = _derive_key(hval, bytes([0xFE, 0xA7, 0xD2, 0x76, 0x3B, 0x4B, 0x9E, 0x79]), alg, bits)
    k2 = _derive_key(hval, bytes([0xD7, 0xAA, 0x0F, 0x6D, 0x30, 0x61, 0x34, 0x4E]), alg, bits)
    try:
        verifier = AES.new(k1, AES.MODE_CBC, salt).decrypt(evhi)
        expected = AES.new(k2, AES.MODE_CBC, salt).decrypt(evhv)
    except ValueError:
        return False
    h = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256,
         "SHA384": hashlib.sha384, "SHA512": hashlib.sha512}.get(alg, hashlib.sha1)
    return h(verifier).digest() == expected
