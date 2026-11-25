# -*- coding: utf-8 -*-
"""现场验证 B 链精修：api_inject 让 TTS 把日语念进 CABLE Input，
B 链从 CABLE Output 听到，观察 asr_ms / refine_ms / engine / dst。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import http.client

HOST, PORT = "127.0.0.1", 8899

def call(method, path, obj=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=20)
    b = json.dumps(obj).encode() if obj is not None else None
    h = {"Content-Type": "application/json"} if b else {}
    c.request(method, path, body=b, headers=h)
    r = c.getresponse(); raw = r.read(); c.close()
    try:
        return json.loads(raw or b"{}")
    except Exception:
        print("BAD", path, r.status, repr(raw[:200])); return {}

cfg0 = call("GET", "/api/state").get("cfg") or {}
before = {k: cfg0.get(k) for k in ("loop_src", "loop_dev", "echo_guard", "out_dev")}
print("before:", before)

call("POST", "/api/clear")
for k, v in (("loop_src", "device"), ("loop_dev", 24), ("echo_guard", False)):
    call("POST", "/api/cfg", {k: v})
time.sleep(1.0)

JA = "もしもし、ちょっと聞こえてる？今日は少し遅れちゃうかも。"
print("inject:", call("POST", "/api/inject", {"text": JA, "lang": "ja"}))

seen = {}
t0 = time.time()
while time.time() - t0 < 22.0:
    st = call("GET", "/api/state")
    for s in st.get("segs") or []:
        if s.get("chain") == "B":
            seen[s["id"]] = s
    if any(s.get("refine_ms") for s in seen.values()):
        break
    time.sleep(0.5)

for sid, s in seen.items():
    print(json.dumps({k: s.get(k) for k in ("id", "state", "src", "dst", "engine",
                                            "asr_ms", "refine_ms", "mt_ms", "note")},
                     ensure_ascii=False))
if not seen:
    print("没有 B 段：loop_db=%s" % (call("GET", "/api/state").get("cfg", {}).get("loop_db"),))

for k, v in before.items():
    call("POST", "/api/cfg", {k: v})
print("restore:", {k: call("GET", "/api/state").get("cfg", {}).get(k) for k in before})
