# -*- coding: utf-8 -*-
"""中文素材上量一次云端识别的耗时分布：给 chatbox / A 链云端优先定预算用。

跑法：python tools/_cloud_lat_zh.py
输出：每条 zh*.wav 的云端耗时与文本，最后 p50/p90/max。
"""
import sys, os, time, wave, glob, statistics as st
sys.path.insert(0, '.')
import numpy as np
from vrdub import cfg as cfgmod, net as netmod, asr as asrmod, util

c = cfgmod.Cfg()
net = netmod.Net("ali", c.host, c.key, slots=4)
T = asrmod.Transcriber(c, net, util.log)

files = sorted(glob.glob("out/clips/zh*.wav"))
if not files:
    print("没有中文素材"); sys.exit(1)


def load(f):
    with wave.open(f, "rb") as w:
        raw = w.readframes(w.getnframes()); sr = w.getframerate(); ch = w.getnchannels()
    a = np.frombuffer(raw, "<i2")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype("<i2")
    if sr != 16000:
        x = a.astype(np.float32) / 32768.0
        x = util.resample(x, sr, 16000)
        a = (np.clip(x, -1, 1) * 32767).astype("<i2")
    return a


print("中文素材", len(files), "条")
T.local.transcribe(load(files[0]), "auto")     # 预热本地，别把载模型算进云端计时
rows = []
for f in files:
    a = load(f)
    t = time.time(); r = T.transcribe_cloud(a, "zh"); dt = (time.time() - t) * 1000
    txt = (r.get("text") or "").strip()
    rows.append((os.path.basename(f), len(a) / 16000.0, dt, txt))
    print(f"{os.path.basename(f)}  {len(a)/16000:.2f}s  {dt:6.0f}ms  {txt!r}")

d = [r[2] for r in rows]
print(f"\n云端中文 n={len(d)}  p50 {st.median(d):.0f}ms  p90 {sorted(d)[max(0,int(len(d)*0.9)-1)]:.0f}ms  max {max(d):.0f}ms")
