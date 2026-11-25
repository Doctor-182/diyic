# -*- coding: utf-8 -*-
"""现场验证 B 链精修：把 ja1.wav 播到扬声器，让 B 链从扬声器回环听到，观察 refine_ms/engine/dst。"""
import json, os, sys, time, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vrdub import audio as A, util
import http.client

HOST, PORT = "127.0.0.1", 8899

def post(path, obj):
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    b = json.dumps(obj).encode()
    c.request("POST", path, body=b, headers={"Content-Type": "application/json"})
    r = c.getresponse().read()
    c.close()
    return json.loads(r or b"{}")

def get(path):
    c = http.client.HTTPConnection(HOST, PORT, timeout=10)
    c.request("GET", path)
    r = c.getresponse().read()
    c.close()
    return json.loads(r or b"{}")

def wav_pcm(fp):
    with wave.open(fp, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, (w.getnchannels(), w.getsampwidth())
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    return data, sr

pcm, sr = wav_pcm(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "clips", "ja1.wav"))
util.log("ja1.wav %d ms @ %d Hz" % (len(pcm) / 2 / sr * 1000, sr), "info", "t")

post("/api/clear", {})
# 临时改成从扬声器回环听（25=扬声器Rapoo[Loopback]），并把 A 链输出临时改成扬声器 18
print("cfg:", post("/api/cfg", {"loop_src": "device", "loop_dev": 25, "out_dev": 18}))
time.sleep(0.8)
A.play_pcm(18, pcm, sr, 1.0)
util.log("播放完毕，等 B 链出段…", "info", "t")

seen = {}
t0 = time.time()
while time.time() - t0 < 14.0:
    st = get("/api/state")
    for s in st.get("segs") or []:
        if s.get("chain") == "B":
            seen[s["id"]] = s
    if any(s.get("refine_ms") for s in seen.values()):
        break
    time.sleep(0.4)

for sid, s in seen.items():
    print(json.dumps({k: s.get(k) for k in ("id", "state", "src", "dst", "engine", "asr_ms", "refine_ms", "mt_ms", "note")}, ensure_ascii=False))
print("恢复:", post("/api/cfg", {"loop_src": "process", "loop_dev": 25, "out_dev": 19}))
