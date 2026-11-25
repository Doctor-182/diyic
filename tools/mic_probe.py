import json, sys, time, threading, urllib.request
sys.path.insert(0,"D:/vrc无言势")
import numpy as np
from vrdub import audio
def get(p):
    with urllib.request.urlopen("http://127.0.0.1:8899"+p, timeout=10) as r: return json.loads(r.read().decode())
def post(p,b):
    req=urllib.request.Request("http://127.0.0.1:8899"+p, data=json.dumps(b).encode(), headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read().decode())
s=get("/api/state")
print("cfg", {k:s["cfg"].get(k) for k in ("mic_dev","out_dev","loop_dev","mic_on","mic_db","end_silence")})
print("mic", s.get("mic"))
# 播 1.5s 440Hz 到 19，同时每 150ms 读一次 mic 电平
sr=48000; t=np.arange(int(sr*1.5))/sr
x=(0.3*np.sin(2*np.pi*440*t)).astype(np.float32)
pcm=(x*32767).astype("<i2").tobytes()
threading.Thread(target=lambda: audio.play_pcm(19, pcm, 48000, 1.0), daemon=True).start()
for i in range(14):
    time.sleep(0.15)
    st=get("/api/state")
    print(f"  t={i*0.15:.2f}s mic.lv={json.dumps(st.get('mic'))[:120]} levels={json.dumps(st.get('levels') )[:120]}")
