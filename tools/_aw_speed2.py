# -*- coding: utf-8 -*-
import os, sys, time, wave
sys.path.insert(0, r"D:\vrc无言势\_pylibs"); sys.path.insert(0, r"D:\vrc无言势")
import numpy as np
from vrdub import util

def load16(f):
    with wave.open(f, "rb") as w:
        raw = w.readframes(w.getnframes()); sr = w.getframerate(); ch = w.getnchannels()
    a = np.frombuffer(raw, "<i2")
    x = a.astype(np.float32) / 32768.0
    return util.resample(x, sr, 16000) if sr != 16000 else x

x = load16(r"D:\vrc无言势\out\clips\ja0.wav")
# numpy 基准：16 线程 fp32 矩阵乘 GFLOPS
a = np.random.rand(2048, 2048).astype(np.float32); b = np.random.rand(2048, 2048).astype(np.float32)
a @ b
t = time.time()
for _ in range(5):
    a @ b
g = 5 * 2 * 2048**3 / (time.time() - t) / 1e9
print("numpy fp32 matmul %.0f GFLOPS" % g, flush=True)

from faster_whisper import WhisperModel
MD = r"D:\vrc无言势\models\anime-whisper-ct2-int8"

def run(tag, **kw):
    ct = kw.pop("compute_type", "int8")
    th = kw.pop("cpu_threads", 8)
    t0 = time.time()
    m = WhisperModel(MD, device="cpu", compute_type=ct, cpu_threads=th, num_workers=1, local_files_only=True)
    ld = (time.time() - t0) * 1000
    m.transcribe(x, language="ja", beam_size=1, best_of=1, condition_on_previous_text=False,
                 without_timestamps=True, vad_filter=False, temperature=0.0, **kw)
    t = time.time()
    segs, _ = m.transcribe(x, language="ja", beam_size=1, best_of=1, condition_on_previous_text=False,
                           without_timestamps=True, vad_filter=False, temperature=0.0, **kw)
    txt = "".join(s.text for s in segs)
    dt = (time.time() - t) * 1000
    print("%-28s 载入 %5.0fms 识别 %6.0fms (4.08s)  %r" % (tag, ld, dt, txt[:36]), flush=True)
    del m

run("int8 th8")
run("int8_float32 th8", compute_type="int8_float32")
os.environ["CT2_FORCE_CPU_ISA"] = "AVX2"
run("int8 th8 ISA=AVX2")
os.environ["CT2_FORCE_CPU_ISA"] = "AVX512"
run("int8 th8 ISA=AVX512")
os.environ.pop("CT2_FORCE_CPU_ISA", None)
run("int8 th12", cpu_threads=12)
