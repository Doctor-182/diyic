# -*- coding: utf-8 -*-
"""三种精修模式的实测：auto（云端优先，失败退本地）/ cloud / off，以及 whisper 直连。"""
import sys, time, wave
sys.path.insert(0, r"D:\vrc无言势")
import numpy as np
from vrdub import util
from vrdub import cfg as cfgmod, net as netmod
from vrdub.asr import Transcriber

def load16(f):
    with wave.open(f, "rb") as z:
        raw = z.readframes(z.getnframes()); sr = z.getframerate()
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    return ((util.resample(x, sr, 16000) if sr != 16000 else x) * 32768).astype(np.int16)

a = load16(r"D:\vrc无言势\out\clips\ja1.wav")
c = cfgmod.Cfg()
net = netmod.Net("ali", c.host, c.key, slots=4)
log = lambda m, k="info", s="x": print("     ·%s %s" % (k, m))

for mode, use_net in (("auto", True), ("auto", False), ("cloud", True), ("whisper", True), ("off", True)):
    c2 = cfgmod.Cfg()
    c2.set_many({"asr_refine": mode}) if hasattr(c2, "set_many") else None
    d = c2.all(); d["asr_refine"] = mode
    t = Transcriber(d, (net if use_net else None), log=log)
    t0 = time.time()
    txt, eng, ms = t.refine(a, "ja")
    print("mode=%-7s net=%-5s refiner=%-7s -> %-8s %6.0fms(wall %5.0f) %s"
          % (mode, use_net, t.refiner(), eng, ms, (time.time()-t0)*1000, txt[:34]))
    if t.ja is not None and t.ja.is_loaded if hasattr(t.ja, "is_loaded") else False:
        pass
    print("      ja:", (t.ja.stat() if t.ja is not None else "未载入"))
