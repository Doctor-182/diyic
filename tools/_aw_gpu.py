# -*- coding: utf-8 -*-
import sys, time
sys.path.insert(0, r"D:\vrc无言势")
from vrdub import asr_whisper as aw
import ctranslate2
w = aw.WhisperASR(device="cuda", slow_ms=1e9, log=lambda m,k,t: print("  [log]", m))
print("ct2 cuda types:", ctranslate2.get_supported_compute_types("cuda") if True else "")
print("cuda device count:", ctranslate2.get_cuda_device_count())
print("ready:", w.ready(), "|", w.note)
from vrdub import util
import wave, numpy as np
def load16(f):
    with wave.open(f,"rb") as z: raw=z.readframes(z.getnframes()); sr=z.getframerate()
    x=np.frombuffer(raw,"<i2").astype(np.float32)/32768.0
    return (util.resample(x,sr,16000) if sr!=16000 else x)
a = (load16(r"D:\vrc无言势\out\clips\ja0.wav")*32768).astype(np.int16)
for i in range(5):
    txt, ms = w.transcribe(a, "ja")
    print("  run%d %7.0fms %r" % (i, ms, txt[:44]))
print("device:", w.device, "| stat:", w.stat())
