# -*- coding: utf-8 -*-
"""anime-whisper int8 在 CPU 上的速度矩阵：线程数 × compute_type × 段长。"""
import os, sys, time, wave
sys.path.insert(0, r"D:\vrc无言势\_pylibs")
sys.path.insert(0, r"D:\vrc无言势")
import numpy as np
from vrdub import util

def load16(f):
    with wave.open(f, "rb") as w:
        raw = w.readframes(w.getnframes()); sr = w.getframerate(); ch = w.getnchannels()
    a = np.frombuffer(raw, "<i2")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype("<i2")
    x = a.astype(np.float32) / 32768.0
    if sr != 16000:
        x = util.resample(x, sr, 16000)
    return x

print("OMP_NUM_THREADS=%r CT2_NUM_THREADS=%r CPU_COUNT=%s"
      % (os.environ.get("OMP_NUM_THREADS"), os.environ.get("CT2_NUM_THREADS"), os.cpu_count()))
from faster_whisper import WhisperModel
MD = r"D:\vrc无言势\models\anime-whisper-ct2-int8"
x4 = load16(r"D:\vrc无言势\out\clips\ja0.wav")
x8 = np.concatenate([load16(r"D:\vrc无言势\out\clips\ja0.wav"), load16(r"D:\vrc无言势\out\clips\ja2.wav")])
print("时长 %.2fs / %.2fs" % (len(x4)/16000, len(x8)/16000))

for ct in ("int8", "float32"):
    for th in (2, 4, 8, 16):
        try:
            t0 = time.time()
            m = WhisperModel(MD, device="cpu", compute_type=ct, cpu_threads=th, num_workers=1,
                             local_files_only=True)
            tl = (time.time() - t0) * 1000
        except Exception as e:
            print("  %s/%d 载入失败 %r" % (ct, th, e)); continue
        for name, x in (("4s", x4), ("8s", x8)):
            m.transcribe(x, language="ja", beam_size=1, best_of=1, condition_on_previous_text=False,
                         without_timestamps=True, vad_filter=False, temperature=0.0,
                         no_speech_threshold=0.6, compression_ratio_threshold=2.4)  # 预热
            t = time.time()
            segs, info = m.transcribe(x, language="ja", beam_size=1, best_of=1,
                                      condition_on_previous_text=False, without_timestamps=True,
                                      vad_filter=False, temperature=0.0,
                                      no_speech_threshold=0.6, compression_ratio_threshold=2.4)
            t_first = None
            txt = ""
            for s in segs:
                if t_first is None:
                    t_first = (time.time() - t) * 1000
                txt += s.text
            dt = (time.time() - t) * 1000
            print("  %-7s th=%-2d %s 总 %6.0fms 首段 %6.0fms RTF %.2f  载入 %5.0fms  %r"
                  % (ct, th, name, dt, t_first or 0, dt / 1000.0 / (len(x)/16000.0), tl, txt[:40]), flush=True)
        del m
        try:
            import gc; gc.collect()
        except Exception:
            pass
