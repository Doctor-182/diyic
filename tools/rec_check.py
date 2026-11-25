# -*- coding: utf-8 -*-
"""录 CABLE Output（或任意输入设备）若干秒，打印电平时间线 —— 用来证明 TTS 音频真进了虚拟线。

用法：python tools\rec_check.py [dev] [secs]
"""
import sys, time, threading
sys.path.insert(0, "D:/vrc无言势")
import numpy as np
from vrdub import audio, util

dev = int(sys.argv[1]) if len(sys.argv) > 1 else 24
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 14.0

chunks = []
lk = threading.Lock()

def on_block(a, rate):
    with lk:
        chunks.append(np.asarray(a, dtype=np.float32).mean(axis=1) if a.ndim > 1 else a.copy())

cap = audio.Capture(dev, on_block, log=lambda *a, **k: None, kind="probe")
cap.start()
t0 = time.time()
while time.time() - t0 < secs:
    time.sleep(0.05)
cap.stop()
time.sleep(0.2)

with lk:
    mono = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
peak = float(np.abs(mono).max())
rms = float(np.sqrt(np.mean(mono ** 2)))
step = 2400                                  # 0.05 s @48k
bars = []
for i in range(0, len(mono), step):
    seg = mono[i:i + step]
    bars.append(20 * np.log10(float(np.sqrt(np.mean(seg ** 2))) + 1e-9))
loud = [i for i, d in enumerate(bars) if d > -45]
print("dev=%d secs=%.1f frames=%d peak=%.3f(%.1f dB) rms=%.1f dB 有声格=%d/%d"
      % (dev, secs, len(mono), peak, 20 * np.log10(peak + 1e-9),
         20 * np.log10(rms + 1e-9), len(loud), len(bars)))
if loud:
    print("最早有声 %.2fs 最晚 %.2fs" % (loud[0] * 0.05, loud[-1] * 0.05))
print("时间线（每行 2 s，每格 0.05 s，dB）:")
for i in range(0, len(bars), 40):
    print("  %5.1fs " % (i * 0.05) + " ".join("%4d" % int(max(-99, d)) for d in bars[i:i + 40]))
