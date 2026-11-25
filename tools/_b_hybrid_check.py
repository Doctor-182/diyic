# -*- coding: utf-8 -*-
"""B 链 hybrid 验证：注入日语/英语（模拟对方说话），看字幕与 asr_ms。"""
import sys, time, json, urllib.request
sys.path.insert(0, '.')
BASE = "http://127.0.0.1:8899"
def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

for txt, lang in [("こんにちは、今日は一緒に遊べて嬉しいな。", "ja"),
                  ("Sorry I am late, the metro was packed.", "en")]:
    t = time.time()
    r = post("/api/inject", {"text": txt, "lang": lang})
    print(f"[{lang}] {time.time()-t:.1f}s ok={r.get('ok')} dst={r.get('dst','')!r} asr={r.get('asr_ms')} mt={r.get('mt_ms')}")
    time.sleep(4.0)

d = json.loads(urllib.request.urlopen(BASE + "/api/metrics", timeout=5).read())
for k in ("A", "B"):
    v = d.get(k) or {}
    print(k, {x: v.get(x) for x in ("n", "done", "drop", "asr_p50", "asr_p90", "mt_p50", "e2e_p50", "e2e_p90")})
