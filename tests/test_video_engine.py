# 生成测试视频并验证画帧特征能否区分「同源重编码」与「不同内容」
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import config, features, ffmpeg, video  # noqa: E402

OUT = os.path.join(BASE, "_test", "媒体样本")
os.makedirs(OUT, exist_ok=True)
ff = config.FFMPEG


def run(args):
    p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y"] + args,
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:400])


print("ffmpeg:", ffmpeg.version())

# A: 6 秒动态测试图（带纹理和运动）
a1 = os.path.join(OUT, "A_原版.mp4")
if not os.path.exists(a1):
    run(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", a1])
# A2: 同源重编码（480P、低码率、不同编码器参数）
a2 = os.path.join(OUT, "A_重编码480p.mp4")
if not os.path.exists(a2):
    run(["-i", a1, "-vf", "scale=854:480", "-c:v", "libx264", "-crf", "30",
         "-preset", "veryfast", "-pix_fmt", "yuv420p", a2])
# A3: 同源 + 上下加黑边（模拟不同版本）
a3 = os.path.join(OUT, "A_加黑边.mp4")
if not os.path.exists(a3):
    run(["-i", a1, "-vf", "scale=1280:720,pad=1280:800:0:40:black", "-c:v", "libx264",
         "-crf", "26", "-pix_fmt", "yuv420p", a3])
# B: 完全不同的内容
b1 = os.path.join(OUT, "B_不同内容.mp4")
if not os.path.exists(b1):
    run(["-f", "lavfi", "-i", "smptebars=size=1280x720:rate=30:duration=6",
         "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", b1])

print("\n分析中…")
recs = {}
for name in ("A_原版.mp4", "A_重编码480p.mp4", "A_加黑边.mp4", "B_不同内容.mp4"):
    p = os.path.join(OUT, name)
    t0 = __import__("time").time()
    r = video.analyze(p)
    recs[name] = r
    print(f"  {name:20s} {r['duration']:.1f}s {r['w']}x{r['h']} 帧{r['frames']} "
          f"dim{r['dim']} thumb{len(r['thumb'])}B 用时{__import__('time').time()-t0:.2f}s")

print("\n相似度矩阵（帧特征最优匹配）:")
names = list(recs)
print("            " + "".join(f"{n[:10]:>12s}" for n in names))
for i, n1 in enumerate(names):
    row = f"{n1[:10]:>12s}"
    for n2 in names:
        a = video.decode_sig(recs[n1]["sig"], recs[n1]["dim"])
        b = video.decode_sig(recs[n2]["sig"], recs[n2]["dim"])
        row += f"{features.frame_score(a, b):12.3f}"
    print(row)

print("\npHash 最小汉明距离:")
for n1 in names:
    row = f"{n1[:10]:>12s}"
    for n2 in names:
        hm = features.min_hamming(video.decode_ph(recs[n1]["ph"]), video.decode_ph(recs[n2]["ph"]))
        row += f"{hm:12d}"
    print(row)

print("\n门禁判定（相似度≥0.88 且 pHash≤26 视为重复）:")
for i, n1 in enumerate(names):
    for n2 in names[i + 1:]:
        a = video.decode_sig(recs[n1]["sig"], recs[n1]["dim"])
        b = video.decode_sig(recs[n2]["sig"], recs[n2]["dim"])
        s = features.frame_score(a, b)
        hm = features.min_hamming(video.decode_ph(recs[n1]["ph"]), video.decode_ph(recs[n2]["ph"]))
        dup = s >= config.VIDEO_THRESHOLD and (hm < 0 or hm <= config.VIDEO_PHASH_MAX)
        print(f"  {n1[:14]:16s} vs {n2[:14]:16s} 分数 {s:.3f} 汉明 {hm:2d} → {'重复' if dup else '不重复'}")
