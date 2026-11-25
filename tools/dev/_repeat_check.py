# -*- coding: utf-8 -*-
import sys, time, json, urllib.request
sys.path.insert(0, '.')
BASE = "http://127.0.0.1:8899"

def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

line = "我刚才没说清楚，现在我再重复一遍，你能听清吗？"
for i in (1, 2):
    t = time.time()
    r = post("/api/inject", {"text": line, "lang": "zh"})
    print(f"第{i}次 {time.time()-t:.1f}s state={r.get('ok')} dst={r.get('dst','')[:50]!r} "
          f"mt={r.get('mt_ms')} first={r.get('first_pkt_ms')} chatbox={r.get('chatbox')}")
    if i == 1:
        time.sleep(9)
