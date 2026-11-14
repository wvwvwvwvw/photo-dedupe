"""文档密码校验自检：Office（Standard/Agile）+ PDF（R2 对照 hashcat 向量、R4 现场造）。"""
from __future__ import annotations

import hashlib
import os
import struct
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import docs  # noqa: E402

FIX = os.path.join(BASE, "_test", "fixtures")
PWD = b"Password1234_"


def _make_r4_pdf(user_pw: bytes, owner_key=b"\x11" * 32) -> bytes:
    """现场造一个 R4（RC4-128）的加密 PDF，用来测解析路径。"""
    id0 = hashlib.md5(b"test-pdf-id").digest()
    o = docs._rc4(owner_key, docs._PDF_PAD) + b"\x22" * 16      # O 值（这里只需自洽）
    d = {"R": 4, "Length": 128, "P": -1, "O": o, "ID0": id0,
         "EncryptMetadata": True}
    key = docs._pdf_key(user_pw, d)
    x = hashlib.md5(docs._PDF_PAD + id0).digest()
    x = docs._rc4(key, x)
    for i in range(1, 20):
        x = docs._rc4(bytes(b ^ i for b in key), x)
    u = x + b"\x00" * 16
    body = (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            b"2 0 obj\n<< /Filter /Standard /V 4 /R 4 /Length 128 /P -1\n"
            b"  /O <" + o.hex().encode() + b">\n"
            b"  /U <" + u.hex().encode() + b">\n"
            b"  /CF << /StdCF << /CFM /V2 /Length 16 >> >>\n"
            b"  /StmF /StdCF /StrF /StdCF >>\nendobj\n"
            b"trailer\n<< /Root 1 0 R /Encrypt 2 0 R /ID [<" + id0.hex().encode() +
            b"><" + id0.hex().encode() + b">] >>\n%%EOF\n")
    return body


def main() -> int:
    ok = True
    # ① Office 三种向量
    for name, expect in (("ecma376standard_password.docx", True),
                         ("example_password.docx", True),
                         ("example_password.xlsx", True)):
        path = os.path.join(FIX, name)
        data = open(path, "rb").read()
        inf = docs.info(data)
        good = docs.check_password(data, PWD)[0]
        bad = docs.check_password(data, b"nope")[0]
        line = (inf.get("kind"), good, bad)
        hit = inf.get("encrypted") and good and not bad and expect
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] {name:<34} {line}")
    # ② PDF R2：hashcat 10400 官方向量
    H = ("$pdf$1*2*40*-1*0*16*01221086741440841668371056103222*32*"
         "27c3fecef6d46a78eb61b8b4dbc690f5f8a2912bbb9afc842c12d79481568b74*32*"
         "0000000000000000000000000000000000000000000000000000000000000000")
    f = H.split("$")[2].split("*")
    d = {"V": int(f[0]), "R": int(f[1]), "Length": int(f[2]), "P": int(f[3]),
         "ID0": bytes.fromhex(f[6]), "U": bytes.fromhex(f[8]), "O": bytes.fromhex(f[10]),
         "EncryptMetadata": bool(int(f[4]))}
    k = docs._pdf_key(b"hashcat", d)
    r2 = docs._rc4(k, docs._PDF_PAD) == d["U"][:32]
    ok &= r2
    print(f"[{'OK ' if r2 else 'FAIL'}] PDF R2（hashcat 10400 向量）U 校验={r2}")
    # ③ PDF R4：自造文件
    pdf = _make_r4_pdf(PWD)
    inf = docs.info(pdf)
    good = docs.check_password(pdf, PWD)[0]
    bad = docs.check_password(pdf, b"nope")[0]
    hit = inf.get("encrypted") and good and not bad
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] PDF R4（现场造）             {inf.get('kind')} "
          f"正确={good} 错误={bad}")
    # ④ 普通文件不应误判
    plain = os.path.join(FIX, "example_password.docx")
    r = docs.info(b"not a document at all" * 10)
    ok &= not r["encrypted"]
    print(f"[{'OK ' if not r['encrypted'] else 'FAIL'}] 普通数据不误判")
    # ④b qpdf 的 R6 样本（密码 wwwwww）；外加 hashcat 10600（R5）向量
    r6 = os.path.join(FIX, "qpdf_r6_wwwww.pdf")
    data6 = open(r6, "rb").read()
    inf6 = docs.info(data6)
    good6 = docs.check_password(data6, b"wwwww")[0]
    bad6 = docs.check_password(data6, b"nope")[0]
    hit6 = inf6.get("kind") == "PDF R6" and good6 and not bad6
    ok &= hit6
    print(f"[{'OK ' if hit6 else 'FAIL'}] PDF R6（qpdf 样本）            {inf6.get('kind')} "
          f"正确={good6} 错误={bad6}")
    H5 = ("$pdf$5*5*256*-1028*1*16*28562274676426582441147358074521*127*"
          "a3aab04cff2c536118870976d768f1fdd445754d6b2dd81fba10bb6e742acd7f"
          "2856227467642658244114735807452100000000000000000000000000000000"
          "0000000000000000000000000000000000000000000000000000000000000000"
          "0000000000000000000000000000000000000000000000000000000000000000"
          "*127*" + "0" * 254 + "*32*" + "0" * 64 + "*32*" + "0" * 64)
    f5 = H5.split("$")[2].split("*")
    d5 = {"V": int(f5[0]), "R": int(f5[1]), "Length": 256, "P": int(f5[3]),
          "ID0": bytes.fromhex(f5[6]), "U": bytes.fromhex(f5[8]),
          "O": bytes.fromhex(f5[10]), "EncryptMetadata": True}
    hit5 = (docs._pdf_check_r56(b"hashcat", d5)
            and not docs._pdf_check_r56(b"nope", d5))
    ok &= hit5
    print(f"[{'OK ' if hit5 else 'FAIL'}] PDF R5（hashcat 10600 向量）   U 校验={hit5}")
    # ④c 文件名里的密码
    from media import namepass as NP
    cand = NP.candidates("[合集修订版]《噪音旅行箱》限制版密码：hg;mxj+P_s;A09S.pdf")
    ok &= cand[:1] == ["hg;mxj+P_s;A09S"]
    print(f"[{'OK ' if cand[:1] == ['hg;mxj+P_s;A09S'] else 'FAIL'}] 文件名猜密码 → {cand}")

    # ⑤ 走完整的 find_password 流程（密码本 → 命中）
    from media import unzip as UZ
    UZ.FOUND_FILE = os.path.join(BASE, "_test", "found_doc_test.txt")
    for f in os.listdir(UZ.FOUND_FILE.rsplit(os.sep, 1)[0]):
        pass
    try:
        os.remove(UZ.FOUND_FILE)
    except OSError:
        pass
    for name in ("example_password.docx", "ecma376standard_password.docx"):
        data = open(os.path.join(FIX, name), "rb").read()
        pw, _enc = UZ.find_password(data, ["123456", "Password1234_"], {"auto": True},
                                    None, name)
        hit = bool(pw) and pw.decode() == "Password1234_"
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] find_password 流程 {name:<30} → {pw!r}")
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
