"""压缩流程自检：单次/批量模式、格式、任务内合并、校验。"""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import zipfile
import io

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import pipeline as PL  # noqa: E402
from media import pack as P  # noqa: E402

W = os.path.join(BASE, "_test", "_pipe2_check")


def build() -> None:
    shutil.rmtree(W, ignore_errors=True)
    os.makedirs(os.path.join(W, "src", "A", "B"), exist_ok=True)
    os.makedirs(os.path.join(W, "src", "C"), exist_ok=True)
    open(os.path.join(W, "src", "A", "a1.txt"), "w", encoding="utf-8").write("A\n" * 30)
    open(os.path.join(W, "src", "A", "B", "b1.txt"), "w", encoding="utf-8").write("B\n" * 10)
    with open(os.path.join(W, "src", "A", "big.bin"), "wb") as f:
        f.write(os.urandom(400_000))          # 让 A 的包超过 1KB，拆分才有意义
    open(os.path.join(W, "src", "C", "c1.txt"), "w", encoding="utf-8").write("C\n" * 10)
    open(os.path.join(W, "src", "散的.txt"), "w", encoding="utf-8").write("loose\n" * 5)


def run(tag: str, steps, mode: str):
    out = os.path.join(W, "out_" + tag)
    shutil.rmtree(out, ignore_errors=True)
    job = PL.PipelineJob(os.path.join(W, "src"), out, steps, mode)
    job.start()
    job.join(timeout=300)
    st = job.snapshot()
    files = sorted(os.listdir(out)) if os.path.isdir(out) else []
    return st, files, out


def main() -> int:
    build()
    ok = True

    # ① 校验
    errs = [
        PL.check_steps([]),
        PL.check_steps([{"type": "merge"}]),
        PL.check_steps([{"type": "pack", "fmt": "tar", "enc": "aes", "password": "x"}]),
        PL.check_steps([{"type": "pack", "fmt": "7z", "multi": True}]),
    ]
    hit = all(errs) and "tar" in errs[2] and "7z" in errs[3]
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 流程校验：{errs}")

    # ② 批量：3 个任务 → 3 个包（文件也算任务）
    st, files, _ = run("batch", [{"type": "pack", "fmt": "zip"}], "batch")
    hit = st["state"] == "done" and files == ["A.zip", "C.zip", "散的.zip"]
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 批量压缩 → {files}")

    # ③ 单次：整个输入 → 1 个包
    st, files, _ = run("single", [{"type": "pack", "fmt": "zip"}], "single")
    hit = st["state"] == "done" and files == ["src.zip"]
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 单次压缩 → {files}")

    # ④ 格式后缀：tar / tar.gz / tar.bz2 / tar.xz / 7z
    for fmt, suffix in (("tar", ".tar"), ("tar.gz", ".tar.gz"),
                        ("tar.bz2", ".tar.bz2"), ("tar.xz", ".tar.xz"),
                        ("7z", ".7z")):
        st, files, _ = run("fmt_" + fmt.replace(".", "_"),
                           [{"type": "pack", "fmt": fmt}], "batch")
        hit = (st["state"] == "done" and len(files) == 3
               and all(f.endswith(suffix) for f in files))
        ok &= hit
        print(f"[{'OK ' if hit else 'FAIL'}] 格式 {fmt:<8} → {files}")

    # ⑤ 合并只在任务内：批量 + 每个子项各一包 + 合并 → 每个任务仍是 1 个包
    st, files, out = run("each_merge", [
        {"type": "pack", "fmt": "zip", "each_file": True},
        {"type": "merge", "name": "合并"},
    ], "batch")
    hit = st["state"] == "done" and len(files) == 3 and "合并.zip" in files
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 任务内合并 → {files}")

    # ⑥ 内容检查：合并包里是 A 的两个子项，不含 C 的东西
    merged = os.path.join(out, "合并.zip")
    names = []
    if os.path.exists(merged):
        names = sorted(zipfile.ZipFile(merged).namelist())
    hit = names == ["a1.zip", "b1.zip", "big.zip"]
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 合并包内容（只含任务 A）→ {names}")

    # ⑦ 拆分·按数量：3 份；A 有 400KB，S 只有几字节
    st, files, _ = run("count3", [
        {"type": "pack", "fmt": "zip"},
        {"type": "split", "mode": "count", "parts": 3},
    ], "batch")
    hit = (st["state"] == "done" and "A.z01" in files and "A.z02" in files
           and "A.zip" in files)
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 按数量 3 份 → {files}")

    # ⑧ 按数量 + 小于 500MB 不拆 → 不切
    st, files, _ = run("count_min", [
        {"type": "pack", "fmt": "zip"},
        {"type": "split", "mode": "count", "parts": 3, "min_size_mb": 500},
    ], "batch")
    hit = st["state"] == "done" and not any(f.endswith(".z01") for f in files)
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 小于 500MB 不拆 → {files}")

    # ⑨ 按数量 2 份，但每卷超 0.1MB 就额外拆（400KB → 4 份）
    st, files, _ = run("count_max", [
        {"type": "pack", "fmt": "zip"},
        {"type": "split", "mode": "count", "parts": 2, "max_size_mb": 0.1},
    ], "batch")
    parts = [f for f in files if f.startswith("A.")]
    hit = st["state"] == "done" and len(parts) >= 3
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 每卷超 0.1MB 额外拆（目标 2 份）→ {len(parts)} 份 {parts}")

    # ⑩ 按体积：0.1MB 每卷，至少 2 份 / 不超过 2 份
    st, files, _ = run("size_min", [
        {"type": "pack", "fmt": "zip"},
        {"type": "split", "mode": "size", "size_mb": 5, "min_parts": 4},
    ], "batch")
    parts = [f for f in files if f.startswith("A.")]
    hit = st["state"] == "done" and len(parts) == 4
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 按体积+至少 4 份 → {parts}")

    st, files, _ = run("size_max", [
        {"type": "pack", "fmt": "zip"},
        {"type": "split", "mode": "size", "size_mb": 0.05, "max_parts": 2},
    ], "batch")
    parts = [f for f in files if f.startswith("A.")]
    hit = st["state"] == "done" and len(parts) == 2
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 按体积+不超过 2 份 → {parts}")

    # ⑪ 分卷能被自家解压拼回来
    out = os.path.join(W, "out_count3")
    ex = os.path.join(W, "ex_split")
    shutil.rmtree(ex, ignore_errors=True)
    from media import unzip as UZ
    UZ.FOUND_FILE = os.path.join(W, "found.txt")
    try:
        os.remove(UZ.FOUND_FILE)
    except OSError:
        pass
    job = UZ.UnzipJob(out, ex, ["x"], None, True, auto_brute=False)
    job.start()
    job.join(timeout=300)
    sizes = []
    for dp, _d, fs in os.walk(ex):
        for f in fs:
            sizes.append(os.path.getsize(os.path.join(dp, f)))
    hit = 400000 in sizes
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 分卷回读 → {len(sizes)} 个文件，含 400KB={400000 in sizes}")

    # ⑫ 「拆分 → 合并」要拿回单个整包（不是一堆分卷），并把旧分卷清掉
    out = os.path.join(W, "out_merge_back")
    shutil.rmtree(out, ignore_errors=True)
    job = PL.PipelineJob(os.path.join(W, "src"), out, [
        {"type": "pack", "fmt": "zip"},
        {"type": "split", "mode": "count", "parts": 2},
    ], "batch")
    job.start(); job.join(timeout=300)
    before = sorted(os.listdir(out))
    job = PL.PipelineJob(os.path.join(W, "src"), out, [
        {"type": "pack", "fmt": "zip"},
        {"type": "split", "mode": "count", "parts": 2},
        {"type": "merge", "name": ""},
    ], "batch")
    job.start(); job.join(timeout=300)
    after = sorted(os.listdir(out))
    hit = (any(f.endswith(".z01") for f in before)
           and not any(f.endswith(".z01") for f in after)
           and "A.zip" in after)
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 拆分后再合并 → {before} ⇒ {after}")

    # ⑬ 合并步骤自带格式/加密：zip 压完，合并时统一转成 7z + AES
    st, files, _ = run("merge_fmt", [
        {"type": "pack", "fmt": "zip"},
        {"type": "merge", "fmt": "7z", "enc": "aes", "password": "pw9", "level": 9},
    ], "batch")
    hit = st["state"] == "done" and files == ["A.7z", "C.7z", "散的.7z"]
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 合并时改成 7z+AES → {files}")

    # ⑭ 合并步骤缺密码要被校验拦住
    err = PL.check_steps([{"type": "pack"},
                          {"type": "merge", "fmt": "7z", "enc": "aes", "password": ""}])
    hit = bool(err)
    ok &= hit
    print(f"[{'OK ' if hit else 'FAIL'}] 合并缺密码校验 → {err}")

    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
