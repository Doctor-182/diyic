# -*- coding: utf-8 -*-
"""A 链真正关心的：本地中文识别到底够不够。zh0-4 干净 + 三种劣化，三路对比。
参考文本 = 干净素材的云端中文（qwen-audio 对中文很稳）。
anime-whisper 也跑一遍 zh，用来证明「拿日语模型识别我的话」是什么下场。
"""
import sys, time, wave, json, math
sys.path.insert(0, r"D:\vrc无言势")
import numpy as np
from vrdub import util
from vrdub import cfg as cfgmod, net as netmod
from vrdub.asr import Transcriber

SR = 16000

def load16(f):
    with wave.open(f, "rb") as z:
        raw = z.readframes(z.getnframes()); sr = z.getframerate()
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    return (util.resample(x, sr, SR) if sr != SR else x)

def to16(x):
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)

def rms(x):
    return float(np.sqrt(np.mean(x ** 2) + 1e-12))

def add_noise(x, snr_db):
    n = np.random.RandomState(7).randn(len(x)).astype(np.float32)
    n *= rms(x) / (rms(n) * (10 ** (snr_db / 20.0)))
    return x + n

def reverb(x, rt60=0.45):
    rs = np.random.RandomState(11)
    n = int(rt60 * SR)
    ir = rs.randn(n).astype(np.float32) * np.exp(-np.arange(n) / (0.25 * n))
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
    return 0.55 * x + 0.85 * np.convolve(x, ir)[:len(x)]

def bgm(x):
    t = np.arange(len(x)) / SR
    music = (0.11 * np.sin(2 * math.pi * 220 * t) + 0.07 * np.sin(2 * math.pi * 330 * t)
             + 0.05 * np.sin(2 * math.pi * 440 * t) + 0.05 * np.sin(2 * math.pi * 660 * t))
    return x + music.astype(np.float32)

c = cfgmod.Cfg()
net = netmod.Net("ali", c.host, c.key, slots=6)
quiet = lambda m, k="info", s="x": None
t = Transcriber(c, net, log=quiet)
t.refiner()
cases = [("clean", lambda x: x),
         ("noise5", lambda x: add_noise(x, 5.0)),
         ("room", lambda x: reverb(x)),
         ("far", lambda x: add_noise(reverb(x) * 0.25, 12.0))]
rows = []
print("先取参考：干净素材的云端中文", flush=True)
ref = {}
for f in ("zh0", "zh1", "zh2", "zh3", "zh4"):
    x0 = load16(r"D:\vrc无言势\out\clips\%s.wav" % f)
    r = t.cloud.transcribe(to16(x0)) or {}
    ref[f] = r.get("text") or ""
    print("  %s %.2fs -> %s" % (f, len(x0) / SR, ref[f]), flush=True)
sums = {}
for f in ("zh0", "zh1", "zh2", "zh3", "zh4"):
    x0 = load16(r"D:\vrc无言势\out\clips\%s.wav" % f)
    print("== %s  参考: %s" % (f, ref[f]), flush=True)
    for name, fn in cases:
        a = to16(fn(x0.copy()))
        t0 = time.time(); sv = t.local.transcribe(a, "zh"); sv_ms = (time.time() - t0) * 1000
        t0 = time.time(); cl = (t.cloud.transcribe(a) or {}).get("text") or ""; cl_ms = (time.time() - t0) * 1000
        aw = ""; aw_ms = 0.0
        try:
            aw, aw_ms = t.ja.transcribe(a, "zh")
        except Exception as e:
            aw = "<%s>" % e
        s_sv = util.similarity(sv, ref[f]); s_cl = util.similarity(cl, ref[f])
        s_aw = util.similarity(aw, ref[f]) if aw and not aw.startswith("<") else 0.0
        rows.append(dict(clip=f, case=name, sv=sv, aw=aw, cloud=cl, s_sv=round(s_sv, 3),
                         s_aw=round(s_aw, 3), s_cl=round(s_cl, 3),
                         sv_ms=round(sv_ms), aw_ms=round(aw_ms), cl_ms=round(cl_ms)))
        for e, v in (("sv", s_sv), ("aw", s_aw), ("cl", s_cl)):
            sums.setdefault((name, e), []).append(v)
        for e, v in (("sv_ms", sv_ms), ("aw_ms", aw_ms), ("cl_ms", cl_ms)):
            sums.setdefault((name, e), []).append(v)
        print("   %-6s SV %.3f (%5.0fms) | zh-whisper %.3f (%6.0fms) | CL %.3f (%6.0fms)"
              % (name, s_sv, sv_ms, s_aw, aw_ms, s_cl, cl_ms), flush=True)
        print("          SV: %s" % sv, flush=True)
        if s_aw < s_sv - 0.05:
            print("          AW: %s" % aw, flush=True)
open(r"D:\vrc无言势\out\asr_zh_hard.json", "w", encoding="utf-8").write(
    json.dumps(rows, ensure_ascii=False, indent=1))
print("\n按劣化汇总（相似度均值 / 耗时均值）", flush=True)
for name, _ in cases:
    print("  %-6s SV %.3f %5.0fms | AW %.3f %6.0fms | CL %.3f %6.0fms"
          % (name, np.mean(sums[(name, "sv")]), np.mean(sums[(name, "sv_ms")]),
             np.mean(sums[(name, "aw")]), np.mean(sums[(name, "aw_ms")]),
             np.mean(sums[(name, "cl")]), np.mean(sums[(name, "cl_ms")])), flush=True)
print("DONE", flush=True)
