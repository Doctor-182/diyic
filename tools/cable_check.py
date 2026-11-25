# -*- coding: utf-8 -*-
"""CABLE 两端自检：往 #OUT 播一段音，同时从 #CAP 录，看电平到底走没走。"""
import os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vrdub import audio

OUT = int(sys.argv[1]) if len(sys.argv) > 1 else 19
CAP = int(sys.argv[2]) if len(sys.argv) > 2 else 24

p = audio.pa_inst()
for i in (OUT, CAP):
    d = p.get_device_info_by_index(i)
    print("%d -> %s | in%d out%d | %.0fHz" % (i, d["name"], d["maxInputChannels"],
                                              d["maxOutputChannels"], d["defaultSampleRate"]))
buf = []
cap = audio.Capture(CAP, lambda b, r: buf.append(np.asarray(b, np.float32).copy()), kind="loop")
cap.start()
time.sleep(0.6)
sr = 48000
t = np.arange(int(sr * 1.6), dtype=np.float32) / sr
tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
audio.play_pcm(OUT, tone, sr)
time.sleep(0.8)
cap.stop()
x = np.concatenate(buf) if buf else np.zeros(1, np.float32)
peak = float(np.max(np.abs(x))) if x.size else 0.0
print("CAP 录到 %.2fs peak=%.4f (%.1f dB)" % (x.size / 48000.0, peak, 20 * np.log10(max(peak, 1e-9))))
