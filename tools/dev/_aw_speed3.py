# -*- coding: utf-8 -*-
import os, sys, time, wave
sys.path.insert(0, r"D:\vrc无言势")
import numpy as np
from vrdub import util
import ctranslate2, faster_whisper
print("ct2", ctranslate2.__version__, "| fw", faster_whisper.__version__)
print("cpu isa:", ctranslate2.get_supported_compute_types("cpu"))

def load16(f):
    with wave.open(f, "rb") as w:
        raw = w.readframes(w.getnframes()); sr = w.getframerate()
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    return util.resample(x, sr, 16000) if sr != 16000 else x

x = load16(r"D:\vrc无言势\out\clips\ja0.wav")
from faster_whisper import WhisperModel
MD = r"D:\vrc无言势\models\anime-whisper-ct2-int8"
for th in (4, 8, 12):
    m = WhisperModel(MD, device="cpu", compute_type="int8", cpu_threads=th, num_workers=1,
                     local_files_only=True)
    m.transcribe(x, language="ja", beam_size=1, best_of=1, condition_on_previous_text=False,
                 without_timestamps=True, vad_filter=False, temperature=0.0)
    t = time.time()
    segs, _ = m.transcribe(x, language="ja", beam_size=1, best_of=1, condition_on_previous_text=False,
                           without_timestamps=True, vad_filter=False, temperature=0.0)
    txt = "".join(s.text for s in segs)
    print("th=%-2d %6.0fms  %r" % (th, (time.time() - t) * 1000, txt[:36]), flush=True)
    del m
