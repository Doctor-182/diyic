# -*- coding: utf-8 -*-
"""隔离测试：18 播 / 25 采，看回环到底有没有声音。"""
import os, sys, threading, time, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vrdub import audio as A, util

fp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "clips", "ja1.wav")
with wave.open(fp, "rb") as w:
    pcm = w.readframes(w.getnframes()); sr = w.getframerate()
x = A.util.pcm16_to_float(pcm)

levels = []
def cap():
    c = A.Capture(25, lambda d: levels.append(float(np.abs(d).max() if hasattr(d, "max") else 0)), log=util.log, kind="loop")
    c.start(); time.sleep(5.0); c.stop()

t = threading.Thread(target=cap, daemon=True); t.start()
time.sleep(0.7)
A.play_pcm(18, pcm, sr, 1.0)
t.join(6.0)
print("frames:", len(levels), "peak:", max(levels) if levels else None)
print("dev18:", A.pa_inst().get_device_info_by_index(18).get("name"))
print("dev25:", A.pa_inst().get_device_info_by_index(25).get("name"))
