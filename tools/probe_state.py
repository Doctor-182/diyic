import json,urllib.request
def get(p):
    with urllib.request.urlopen("http://127.0.0.1:8899"+p, timeout=10) as r: return json.loads(r.read().decode())
m=get("/api/metrics")
print("A",json.dumps(m["A"],ensure_ascii=False))
for r in m.get("rows") or []:
    if r.get("chain")!="A": continue
    print("--",r.get("id"),r.get("state"),"e2e",r.get("e2e_ms"),"asr",r.get("asr_ms"),"mt",r.get("mt_ms"),"tts",r.get("tts_ms"),"audio",r.get("audio_ms"),"echo",r.get("echo"))
    print("   src",r.get("src"))
    print("   dst",r.get("dst"))
    print("   note",r.get("note"))
