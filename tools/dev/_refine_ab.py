# -*- coding: utf-8 -*-
"""B 链三种识别对比：SenseVoice（快路） / anime-whisper（精修） / 云端（参考）。"""
import sys, time, wave, json
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

c = cfgmod.Cfg()
net = netmod.Net("ali", c.host, c.key, slots=6)
quiet = lambda m, k="info", s="x": print("   ·%s %s" % (k, m))
t = Transcriber(c, net, log=quiet)
print("refiner:", t.refiner(), "| host:", c.host, "| asr_refine:", c.get("asr_refine"))
rows = []
for f in ("ja0", "ja1", "ja2", "ja3", "ja4"):
    a = load16(r"D:\vrc无言势\out\clips\%s.wav" % f)
    t0 = time.time(); sv = t.local.transcribe(a, "ja"); sv_ms = (time.time()-t0)*1000
    aw, aw_ms = t.ja.transcribe(a, "ja")
    t0 = time.time(); cl = (t.cloud.transcribe(a) or {}).get("text") or ""; cl_ms = (time.time()-t0)*1000
    rows.append(dict(clip=f, dur=round(len(a)/16000.0,2), sv=sv, sv_ms=round(sv_ms), aw=aw,
                     aw_ms=round(aw_ms), cloud=cl, cloud_ms=round(cl_ms),
                     sv_cloud=round(util.similarity(sv, cl), 3) if cl else 0.0,
                     aw_cloud=round(util.similarity(aw, cl), 3) if cl else 0.0,
                     sv_aw=round(util.similarity(sv, aw), 3)))
    print("== %s (%.2fs)" % (f, len(a)/16000.0))
    print("   SV  %5.0fms %s" % (sv_ms, sv))
    print("   AW  %5.0fms %s" % (aw_ms, aw))
    print("   CL  %5.0fms %s" % (cl_ms, cl))
    if cl: print("   相似度 SV/CL %.3f  AW/CL %.3f  SV/AW %.3f" % (rows[-1]["sv_cloud"], rows[-1]["aw_cloud"], rows[-1]["sv_aw"]))
open(r"D:\vrc无言势\out\refine_ab.json", "w", encoding="utf-8").write(json.dumps(rows, ensure_ascii=False, indent=1))
ok = [r for r in rows if r["cloud"]]
if ok:
    print("mean SV/CL %.3f  AW/CL %.3f" % (np.mean([r["sv_cloud"] for r in ok]), np.mean([r["aw_cloud"] for r in ok])))
print("mean ms SV %.0f  AW %.0f  CL %.0f" % (np.mean([r["sv_ms"] for r in rows]), np.mean([r["aw_ms"] for r in rows]), np.mean([r["cloud_ms"] for r in rows])))
