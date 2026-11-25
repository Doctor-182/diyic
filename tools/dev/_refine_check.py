# -*- coding: utf-8 -*-
import sys, time, wave
sys.path.insert(0, r"D:\vrc无言势")
import numpy as np
from vrdub import util
from vrdub.asr import Transcriber

def load16(f):
    with wave.open(f, "rb") as z:
        raw = z.readframes(z.getnframes()); sr = z.getframerate()
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    return ((util.resample(x, sr, 16000) if sr != 16000 else x) * 32768).astype(np.int16)

cfg = {"asr_refine": "whisper", "whisper_device": "auto", "whisper_vram_min_mb": 1300,
       "whisper_beam": 1, "whisper_threads": 6, "whisper_slow_ms": 2500,
       "whisper_idle_s": 180.0, "asr_engine": "hybrid"}
log = lambda m, k="info", s="x": print("  [%s] %s" % (k, m))
t = Transcriber(cfg, None, log=log)
t0 = time.time()
print("refiner:", t.refiner(), "| 准备耗时 %.0fms" % ((time.time()-t0)*1000))
for f in ("ja1", "ja3"):
    a = load16(r"D:\vrc无言势\out\clips\%s.wav" % f)
    txt, eng, ms = t.refine(a, "ja")
    print("%-4s -> %-8s %5.0fms  %s" % (f, eng, ms, txt))
print("stat:", t.stat())
print("idle:", t.idle(0.0), "(立即释放)")
