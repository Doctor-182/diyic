"""顺序注入 + 等干净，检查译文质量（避开并发/切换干扰）。"""
import http.client, json, time, sys

def req(path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", 8899, timeout=40)
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    c.request("POST" if body is not None else "GET", path, body=data,
              headers={"Content-Type": "application/json; charset=utf-8"} if data else {})
    r = c.getresponse(); b = r.read(); c.close()
    return r.status, json.loads(b.decode("utf-8"))

req("/api/cfg", {"card": "natural", "out_on": True})
time.sleep(0.4)
sentences = [
    "这个模型的声音我已经换成你的音色了。",
    "你那边能听清吗？我刚把麦克风换了个位置。",
    "如果延迟还能再低一点，那就真的很像同传了。",
]
for txt in sentences:
    st, _ = req("/api/inject", {"text": txt, "lang": "zh"})
    print("inject", st, txt)
    time.sleep(11)
time.sleep(4)
st, m = req("/api/metrics")
rows = [g for g in m.get("rows", []) if g.get("chain") == "A" and g.get("state") in ("done", "refined")][-3:]
for g in rows:
    print("-", g.get("src"))
    print("  =>", g.get("dst"), "| e2e", g.get("e2e_ms"), "audio", g.get("audio_ms"), "tts", g.get("tts_ms"))
