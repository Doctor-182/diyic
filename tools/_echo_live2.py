# -*- coding: utf-8 -*-
import sys, time, json, urllib.request
sys.path.insert(0, '.')
BASE = "http://127.0.0.1:8899"
def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

print(post("/api/cfg", {"dst_lang": "zh", "mic_dev": 26}))
time.sleep(3.0)
r = post("/api/inject", {"text": "这一句用来验证回声拦截还活着。", "lang": "zh"})
print("inject:", r.get("ok"), r.get("dst"), r.get("first_pkt_ms"))
time.sleep(9.0)
print(post("/api/cfg", {"dst_lang": "ja", "mic_dev": 23}))
