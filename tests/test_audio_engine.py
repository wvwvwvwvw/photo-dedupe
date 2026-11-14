# 音频指纹验证：同源不同码率 / 剪掉开头 / 不同内容
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from media import audio, config, ffmpeg  # noqa: E402

OUT = os.path.join(BASE, "_test", "媒体样本")
os.makedirs(OUT, exist_ok=True)
ff = config.FFMPEG


def run(args):
    p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y"] + args, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:300])


# 源：40 秒「音乐型」信号（稳定谐波 + 颤音 + 轻微噪声），比纯噪声更接近真实音频
src = os.path.join(OUT, "A_源.wav")
if not os.path.exists(src):
    expr = ("0.30*sin(2*PI*440*t*(1+0.01*sin(2*PI*0.3*t)))"
            "+0.20*sin(2*PI*880*t)+0.12*sin(2*PI*1320*t)"
            "+0.08*sin(2*PI*1760*t)+0.05*sin(2*PI*220*t)")
    run(["-f", "lavfi", "-i", f"aevalsrc={expr}:s=44100:d=40",
         "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=40:seed=7:amplitude=0.05",
         "-filter_complex", "[0][1]amix=inputs=2:duration=shortest[aout]",
         "-map", "[aout]", "-ar", "44100", src])
cases = {
    "A_源.wav": None,
    "A_mp3_320.mp3": ["-i", src, "-c:a", "libmp3lame", "-b:a", "320k"],
    "A_mp3_96.mp3": ["-i", src, "-c:a", "libmp3lame", "-b:a", "96k"],
    "A_剪掉8秒.mp3": ["-ss", "8", "-i", src, "-c:a", "libmp3lame", "-b:a", "192k"],
}
for name, args in cases.items():
    p = os.path.join(OUT, name)
    if args and not os.path.exists(p):
        run(args + [p])
# B：不同内容（不同谐波结构）
b = os.path.join(OUT, "B_不同.wav")
if not os.path.exists(b):
    expr = ("0.30*sin(2*PI*523*t*(1+0.02*sin(2*PI*0.5*t)))"
            "+0.20*sin(2*PI*1046*t)+0.12*sin(2*PI*1569*t)"
            "+0.08*sin(2*PI*2092*t)+0.05*sin(2*PI*262*t)")
    run(["-f", "lavfi", "-i", f"aevalsrc={expr}:s=44100:d=40",
         "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=40:seed=99:amplitude=0.05",
         "-filter_complex", "[0][1]amix=inputs=2:duration=shortest[aout]",
         "-map", "[aout]", "-ar", "44100", b])

names = ["A_源.wav", "A_mp3_320.mp3", "A_mp3_96.mp3", "A_剪掉8秒.mp3", "B_不同.wav"]
print("分析中…")
sigs = {}
for n in names:
    p = os.path.join(OUT, n)
    if not os.path.exists(p):
        continue
    t0 = time.time()
    r = audio.analyze(p)
    sigs[n] = (audio.decode_sig(r["sig"]), audio.decode_times(r["times"]),
               audio.decode_bounds(r["bounds"], len(audio.decode_sig(r["sig"]))))
    print(f"  {n:16s} {r['duration']:5.1f}s 段{r['segments']} 哈希{r['hashes']:5d} "
          f"{r['codec']:6s} {r['bitrate']:4d}kbps 用时{time.time()-t0:.2f}s 存储{len(r['sig'])+len(r['times'])+len(r['bounds'])}B")

print("\n指纹匹配度矩阵:")
keys = list(sigs)
print("              " + "".join(f"{k[:9]:>11s}" for k in keys))
for k1 in keys:
    row = f"{k1[:12]:>14s}"
    for k2 in keys:
        row += f"{audio.similarity(sigs[k1], sigs[k2]):11.3f}"
    print(row)

print("\n判定（阈值 %.2f）:" % config.AUDIO_THRESHOLD)
for i, k1 in enumerate(keys):
    for k2 in keys[i + 1:]:
        s = audio.similarity(sigs[k1], sigs[k2])
        print(f"  {k1:16s} vs {k2:16s} {s:.3f} → {'重复' if s >= config.AUDIO_THRESHOLD else '不重复'}")
