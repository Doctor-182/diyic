# -*- coding: utf-8 -*-
"""虚拟线矩阵：哪根出、哪根进是同一根线。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vrdub import audio

pairs = [(19, 24), (20, 27), (19, 27), (20, 24), (20, 26), (19, 26)]
sr = 48000
t = np.arange(int(sr * 0.8), dtype=np.float32) / sr
tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
for out, cap in pairs:
    buf = []
    c = audio.Capture(cap, lambda b, r: buf.append(np.asarray(b, np.float32).copy()),
                      log=lambda *a, **k: None, kind="loop")
    try:
        c.start()
    except Exception as e:
        print("#%d -> #%d 打不开：%r" % (out, cap, e)); continue
    time.sleep(0.3)
    try:
        audio.play_pcm(out, tone, sr)
    except Exception as e:
        print("#%d -> #%d 播不了：%r" % (out, cap, e)); c.stop(); continue
    time.sleep(0.5)
    c.stop()
    x = np.concatenate(buf) if buf else np.zeros(1, np.float32)
    pk = float(np.max(np.abs(x))) if x.size else 0.0
    print("#%d -> #%d  peak=%.3f  %s" % (out, cap, pk, "通" if pk > 0.05 else "静音"))
