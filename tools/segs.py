import json, sys, urllib.request as u
s = json.load(u.urlopen("http://127.0.0.1:8899/api/metrics", timeout=10))
ch = sys.argv[1] if len(sys.argv) > 1 else "A"
for g in s.get("rows", []):
    if g.get("chain") != ch:
        continue
    print(g.get("id"), g.get("state"), "asr", g.get("asr_ms"), "mt", g.get("mt_ms"), "tts", g.get("tts_ms"),
          "audio", g.get("audio_ms"), "e2e", g.get("e2e_ms"), "note", g.get("note"))
    print("   src:", g.get("src"))
    print("   dst:", g.get("dst"))
