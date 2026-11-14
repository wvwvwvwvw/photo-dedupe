"""多类型媒体查重：视频 / 音频 / 小说（图片仍在 dedupe 包里）。

目录约定：
  vendor\\ffmpeg.exe                 抽帧、解码音频、读元信息
  vendor\\mobilenetv2-7-feat.onnx   视频画面特征（1280 维）
"""

from __future__ import annotations

import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(BASE, "vendor")
FFMPEG = os.path.join(VENDOR, "ffmpeg.exe")
MODEL = os.path.join(VENDOR, "mobilenetv2-7-feat.onnx")

# ---------------------------------------------------------------- 支持的类型
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".rmvb", ".rm", ".wmv", ".ogm", ".flv", ".mov",
    ".ts", ".m4v", ".webm", ".mpg", ".mpeg", ".vob", ".asf", ".3gp", ".divx",
    ".f4v", ".m2ts", ".mts", ".tp", ".m1v", ".m2v", ".dat", ".rmvb", ".bik",
}

AUDIO_EXTS = {
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape",
    ".alac", ".aiff", ".aif", ".mka", ".amr", ".wv", ".tta", ".dsf", ".dff",
    ".mp2", ".ac3", ".dts", ".au", ".ra",
}

# 小说：txt 为主，epub/docx/html/md/rtf 用标准库解析，pdf 尽力而为
NOVEL_EXTS = {
    ".txt", ".epub", ".docx", ".html", ".htm", ".md", ".markdown",
    ".rtf", ".pdf", ".text", ".log", ".nfo",
}

MIN_VIDEO_BYTES = 64 * 1024
MIN_AUDIO_BYTES = 8 * 1024
MIN_NOVEL_BYTES = 256

KINDS = ("image", "video", "audio", "novel")
KIND_NAMES = {"image": "图片", "video": "视频", "audio": "音频", "novel": "小说"}

# ---------------------------------------------------------------- 默认参数
VIDEO_FRAMES = 8            # 均匀抽帧数
VIDEO_SIZE = 224            # 送进 CNN 的边长
VIDEO_THRESHOLD = 0.88      # 画面相似度阈值
VIDEO_PRE = 0.55            # 均值向量余弦预筛
VIDEO_PHASH_MAX = 26        # 64 位 pHash 交叉校验上限

AUDIO_SR = 8000
AUDIO_WINDOW = 45.0         # 每段抽样秒数
AUDIO_WINDOWS = 3           # 抽几段
AUDIO_INCLUDE_VIDEO = False  # 是否把视频里的音轨也算进音频查重
AUDIO_THRESHOLD = 0.35      # 指纹重合度阈值

NOVEL_NAME_THRESHOLD = 0.82  # 文件名相似度阈值


def has_video_engine() -> bool:
    return os.path.isfile(FFMPEG)


def has_model() -> bool:
    return os.path.isfile(MODEL)
