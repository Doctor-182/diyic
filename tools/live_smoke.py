"""对着运行中的后端灌几句真话：验证 A 链真的合成并出声（out_on 默认开）。"""
import http.client, json, time, sys

H, P = "127.0.0.1", 8899

def req(path, body=None, method=None):
    c = http.client.HTTPConnection(H, P, timeout=40)
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    c.request(method or ("POST" if body is not None else "GET"), path, body=data,
              headers={"Content-Type": "application/json; charset=utf-8"} if data else {})
    r = c.getresponse(); b = r.read(); c.close()
    try: return r.status, json.loads(b.decode("utf-8"))
    except Exception: return r.status, b[:200]

print("state:", req("/api/state")[1]["cfg"]["out_on"], req("/api/state")[1]["cfg"]["out_dev"])
lines = [
    ("今天有点迟到了，路上堵车。", "zh"),
    ("你那边能听清吗？我刚把麦克风换了个位置。", "zh"),
    ("这个模型的声音，我已经换成你的音色了。", "zh"),
    ("如果延迟还能再低一点，那就真的很像同传了。", "zh"),
]
for txt, lang in lines:
    st, r = req("/api/inject", {"text": txt, "lang": lang})
    print("inject", st, txt)
    time.sleep(3.2)
time.sleep(9)
st, m = req("/api/metrics")
print(json.dumps(m, ensure_ascii=False)[:900])
st, s = req("/api/state")
segs = s.get("segs", [])[-8:]
for g in segs:
    print(g.get("chain"), g.get("state"), "asr", g.get("asr_ms"), "mt", g.get("mt_ms"), "tts", g.get("tts_ms"),
          "audio", g.get("audio_ms"), "e2e", g.get("e2e_ms"), "|", (g.get("src") or "")[:18], "→", (g.get("dst") or "")[:26])
