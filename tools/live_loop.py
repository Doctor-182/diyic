import json, sys, time, urllib.request
sys.path.insert(0, "D:/vrc无言势")
BASE = "http://127.0.0.1:8899"
def post(p, b=None):
    req = urllib.request.Request(BASE + p, data=json.dumps(b or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read().decode())
def get(p):
    with urllib.request.urlopen(BASE + p, timeout=10) as r: return json.loads(r.read().decode())

print("cfg", post("/api/cfg", {"mic_dev": 24, "out_dev": 20, "loop_dev": 25,
                               "card": "natural", "hud_on": False, "dbg_timing": True,
                               "echo_guard": True})["cfg"]["mic_dev"])
time.sleep(1.0)
st = get("/api/state"); print("mic", st.get("mic"))
post("/api/clear")
texts = ["大家好，我这边麦克风刚换了位置，你那边能听清吗？",
         "刚才那个模型的声音，我觉得比我预想的自然多了。"]
for t in texts:
    print("inject:", post("/api/inject", {"text": t, "lang": "zh"}))
    time.sleep(9.0)
time.sleep(4.0)
m = get("/api/metrics")
print("\nA summary:", json.dumps(m["A"], ensure_ascii=False))
for r in (m.get("rows") or []):
    if r.get("chain") != "A": continue
    print(f"- [{r.get('state')}] e2e={r.get('e2e_ms')} asr={r.get('asr_ms')} mt={r.get('mt_ms')} "
          f"tts={r.get('tts_ms')} audio={r.get('audio_ms')} echo={r.get('echo')}")
    print("    src:", r.get("src"))
    print("    dst:", r.get("dst"))
print("\nlogs:")
for l in (get("/api/state").get("logs") or [])[-14:]:
    print("  ", l["t"] and time.strftime("%H:%M:%S", time.localtime(l["t"])), l["lv"], l["tag"], l["msg"][:120])
