# -*- coding: utf-8 -*-
"""定位 ali HTTP 400：分别用不同长度音频打 ASR、用各种怪文本打 MT，打印状态与响应体。"""
import os, sys, json, time
sys.path.insert(0, "D:/vrc无言势")
import numpy as np
from vrdub import cfg as cfgmod, net as netmod, asr as asrmod, mt as mtmod, util

cfg = cfgmod.Cfg()
net = netmod.Net("ali", cfg.host, cfg.key, slots=4)
asr = asrmod.CloudASR(net, util.log)
mt = mtmod.Translator(cfg, net, util.log)

src = "D:/vrc无言势/out/clips/zh1.wav"
import wave
with wave.open(src, "rb") as w:
    sr = w.getframerate(); data = w.readframes(w.getnframes())
pcm = np.frombuffer(data, dtype="<i2")
print("source", sr, len(pcm)/sr, "s")

def try_asr(tag, p):
    try:
        r = asr.transcribe(p[None] if p.ndim == 1 else p)
        print(f"[ASR {tag}] OK {r['ms']:.0f}ms dur={r['dur']:.2f} text={r['text'][:30]!r}")
    except Exception as e:
        print(f"[ASR {tag}] ERR {e}")

for sec in (0.1, 0.3, 0.6, 1.0, 3.0, 10.0, 25.0):
    n = int(sec * sr)
    if n <= len(pcm):
        try_asr(f"{sec}s", pcm[:n])
    else:
        try_asr(f"{sec}s-repeat", np.tile(pcm, int(sec/ (len(pcm)/sr)) + 1)[:n])

for t in ["啥的", "沙的", "他杀了", "啊你", "。", "12345", "hello", "这是一个测试句子，用来验证短文本翻译会不会被拒绝。"]:
    try:
        r = mt.translate(t, src="auto", dst="ja")
        print(f"[MT {t!r}] OK {r['ms']:.0f}ms -> {r['text'][:30]!r}")
    except Exception as e:
        print(f"[MT {t!r}] ERR {e}")
