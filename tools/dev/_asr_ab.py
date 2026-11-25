# -*- coding: utf-8 -*-
"""B 链识别引擎 A/B：同一批素材，本地 vs 云端，比文本与耗时。"""
import sys, os, time, wave, glob
sys.path.insert(0, '.')
import numpy as np
from vrdub import cfg as cfgmod, net as netmod, asr as asrmod, util

c = cfgmod.Cfg()
net = netmod.Net("ali", c.host, c.key, slots=4)
T = asrmod.Transcriber(c, net, util.log)

files = sorted(glob.glob("out/clips/ja*.wav")) + sorted(glob.glob("out/clips/en*.wav"))
if not files:
    print("没有素材：先跑 python -m vrdub.bench --dev")
    sys.exit(1)


def load(f):
    with wave.open(f, "rb") as w:
        raw = w.readframes(w.getnframes()); sr = w.getframerate(); ch = w.getnchannels()
    a = np.frombuffer(raw, "<i2")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype("<i2")
    if sr != 16000:
        x = a.astype(np.float32) / 32768.0
        x = util.resample(x, sr, 16000)
        a = (np.clip(x, -1, 1) * 32767).astype("<i2")
    return a


print("素材", len(files), "条")
print("本地模型预热…")
t = time.time(); T.local.transcribe(load(files[0]), "auto"); print(f"  预热 {(time.time()-t)*1000:.0f}ms")

rows = []
for f in files[:8]:
    a = load(f)
    t = time.time(); lo = T.local.transcribe(a, "auto"); t_lo = (time.time() - t) * 1000
    r = T.transcribe_cloud(a, "auto"); cl = r.get("text", ""); t_cl = r.get("ms") or (time.time() - t) * 1000
    sim = util.similarity(lo, cl)
    rows.append((os.path.basename(f), t_lo, t_cl, sim))
    print(f"\n{os.path.basename(f)}  {len(a)/16000:.2f}s")
    print(f"  本地 {t_lo:6.0f}ms  {lo!r}")
    print(f"  云端 {t_cl:6.0f}ms  {cl!r}")
    print(f"  相似 {sim:.2f}")

if rows:
    import statistics as st
    print(f"\n汇总 n={len(rows)} 本地 p50 {st.median([r[1] for r in rows]):.0f}ms / "
          f"云端 p50 {st.median([r[2] for r in rows]):.0f}ms / 平均相似 "
          f"{sum(r[3] for r in rows)/len(rows):.2f}")
