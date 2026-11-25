# -*- coding: utf-8 -*-
"""B 链到底有没有电平：切到 device 听 CABLE Output(24)，注入 TTS，边注入边采 B 电平。"""
import json, sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import http.client

H, P = "127.0.0.1", 8899

def call(m, p, o=None):
    c = http.client.HTTPConnection(H, P, timeout=20)
    b = json.dumps(o).encode() if o is not None else None
    c.request(m, p, body=b, headers={"Content-Type": "application/json"} if b else {})
    r = c.getresponse(); raw = r.read(); c.close()
    try:
        return json.loads(raw or b"{}")
    except Exception:
        return {}

st = call("GET", "/api/state")
print("keys:", list(st.keys()))
print("levels sample:", json.dumps(st.get("levels"), ensure_ascii=False))
cfg = st.get("cfg") or {}
before = {"loop_src": cfg.get("loop_src"), "dev_names": cfg.get("dev_names"), "echo_guard": cfg.get("echo_guard")}
names = dict(before.get("dev_names") or {})
names["loop"] = "CABLE Output (VB-Audio Virtual Cable)"
call("POST", "/api/cfg", {"dev_names": names})
call("POST", "/api/cfg", {"loop_src": "device"})
call("POST", "/api/inject", {"text": "もしもし、聞こえてる？", "lang": "ja"})
hi = -999.0
t0 = time.time()
while time.time() - t0 < 8.0:
    s = call("GET", "/api/state")
    lv = s.get("levels") or {}
    b = lv.get("B")
    if isinstance(b, (int, float)) and b > hi:
        hi = float(b)
    time.sleep(0.15)
print("B 电平峰值:", hi)
print("loop_dev:", (call("GET", "/api/state").get("cfg") or {}).get("loop_dev"))
for k, v in before.items():
    call("POST", "/api/cfg", {k: v})
