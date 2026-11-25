# -*- coding: utf-8 -*-
import os, wave, numpy as np
p = r"D:\vrc无言势\models\anime-whisper-ct2-int8\model.bin"
print("model.bin:", os.path.exists(p), "%.1f MB" % (os.path.getsize(p)/1048576.0) if os.path.exists(p) else "")
md = r"D:\vrc无言势\models\anime-whisper-ct2-int8"
tot = sum(os.path.getsize(os.path.join(md,f)) for f in os.listdir(md) if os.path.isfile(os.path.join(md,f)))
print("目录合计 %.1f MB" % (tot/1048576.0))

import sys
sys.path.insert(0, r"D:\vrc无言势")
from vrdub import util
with wave.open(r"D:\vrc无言势\out\live_rec.wav","rb") as w:
    raw = w.readframes(w.getnframes()); sr = w.getframerate(); ch = w.getnchannels()
a = np.frombuffer(raw,"<i2").reshape(-1, ch).mean(axis=1).astype(np.float32)/32768.0
x = util.resample(a, sr, 16000)
print("时长 %.2fs 16k" % (len(x)/16000.0))
# 30ms 帧能量
H=480; n=len(x)//H
fr = x[:n*H].reshape(n,H)
db = 20*np.log10(np.sqrt((fr**2).mean(axis=1))+1e-9)
print("dBFS: min %.1f p10 %.1f p50 %.1f p90 %.1f max %.1f" % (db.min(), np.percentile(db,10), np.median(db), np.percentile(db,90), db.max()))
# 每 0.5s 一格的粗轮廓
for i in range(0, n, 16):
    seg = db[i:i+16]
    print("%5.1fs %6.1f dB %s" % (i*H/16000.0, seg.mean(), "#"*int(max(0,(seg.mean()+60)/2))))
