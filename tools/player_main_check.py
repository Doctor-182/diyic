# -*- coding: utf-8 -*-
"""Player 主路（#19 CABLE Input）到底有没有出声：push tone → 从 #24 录。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vrdub import audio

buf = []
cap = audio.Capture(24, lambda b, r: buf.append(np.asarray(b, np.float32).copy()), kind="loop")
cap.start()
pl = audio.Player(19, mon_idx=0)
pl.start()
time.sleep(0.5)
sr = 48000
t = np.arange(int(sr * 1.5), dtype=np.float32) / sr
tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
pl.push_tts_pcm(tone, 48000)
time.sleep(1.6)
print("underrun", pl.underrun, "null", pl._null, "ch", pl.ch, "playing", pl.playing)
pl.stop()
time.sleep(0.3)
cap.stop()
x = np.concatenate(buf) if buf else np.zeros(1, np.float32)
peak = float(np.max(np.abs(x))) if x.size else 0.0
print("CAP %.2fs peak=%.4f (%.1f dB)" % (x.size / 48000.0, peak, 20 * np.log10(max(peak, 1e-9))))
