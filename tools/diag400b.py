# -*- coding: utf-8 -*-
"""ASR 400 的第二个假设：静音/极低电平片段被拒。"""
import sys
sys.path.insert(0, "D:/vrc无言势")
import numpy as np, wave
from vrdub import cfg as cfgmod, net as netmod, asr as asrmod

cfg = cfgmod.Cfg()
net = netmod.Net("ali", cfg.host, cfg.key, slots=2)
asr = asrmod.CloudASR(net, lambda *a, **k: None)

with wave.open("D:/vrc无言势/out/clips/zh1.wav", "rb") as w:
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")

def t(tag, p):
    try:
        r = asr.transcribe(p)
        print(f"[{tag}] OK {r['ms']:.0f}ms dur={r['dur']:.2f} -> {r['text'][:24]!r} skip={r.get('skip')} err={str(r.get('err'))[:40]}")
    except Exception as e:
        print(f"[{tag}] ERR {e}")

n = int(16000 * 2.0)
t("silence-2s", np.zeros(n, dtype="<i2"))
t("silence-5s", np.zeros(int(16000 * 5), dtype="<i2"))
noise = (np.random.randn(n) * 12).astype("<i2")
t("noise-loud-2s", noise)
t("noise-quiet-2s", (np.random.randn(n) * 0.3).astype("<i2"))
t("tone-1k-2s", (np.sin(np.arange(n) * 2 * np.pi * 1000 / 16000) * 4000).astype("<i2"))
t("speech-2s", pcm[:int(16000 * 2)])
t("speech-30s-tiled", np.tile(pcm, 9)[:int(16000 * 30)])
