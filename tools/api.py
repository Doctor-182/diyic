# -*- coding: utf-8 -*-
"""命令行打后端（UTF-8 安全，避免 pwsh 传中文被转码）。

用法：
    python tools/api.py state
    python tools/api.py cfg '{"out_on":false}'
    python tools/api.py inject "こんにちは" ja [dev]
    python tools/api.py metrics
    python tools/api.py region 260 240 620 150
    python tools/api.py ocr
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8899"


def post(path, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    a = sys.argv[1:] or ["state"]
    cmd = a[0]
    if cmd == "state":
        s = get("/api/state")
        print(json.dumps({k: s.get(k) for k in ("ok", "uptime_s", "mic", "asr", "hud")}, ensure_ascii=False))
        print("cfg:", json.dumps({k: v for k, v in s["cfg"].items() if k in
              ("out_on", "in_on", "ocr_on", "mic_dev", "out_dev", "loop_dev", "mic_on", "speed", "card")}, ensure_ascii=False))
    elif cmd == "cfg":
        arg = " ".join(a[1:]).strip()
        if arg.startswith("{"):
            patch = json.loads(arg)
        else:                                  # 也支持 k=v 形式：cfg out_on=false loop_dev=27
            patch = {}
            for tok in arg.split():
                k, _, v = tok.partition("=")
                if v.lower() in ("true", "false"):
                    patch[k] = v.lower() == "true"
                elif "," in v:
                    patch[k] = [int(x) for x in v.split(",") if x != ""]
                else:
                    try:
                        patch[k] = int(v)
                    except ValueError:
                        try:
                            patch[k] = float(v)
                        except ValueError:
                            patch[k] = v
        r = post("/api/cfg", patch)
        print(json.dumps({k: v for k, v in r["cfg"].items() if k in
              ("out_on", "in_on", "ocr_on", "mic_dev", "out_dev", "loop_dev", "speed", "card")}, ensure_ascii=False))
    elif cmd == "inject":
        body = {"text": a[1], "lang": (a[2] if len(a) > 2 else "zh")}
        if len(a) > 3:
            body["dev"] = int(a[3])
        print(json.dumps(post("/api/inject", body), ensure_ascii=False))
    elif cmd == "metrics":
        m = get("/api/metrics")
        print(json.dumps({k: m[k] for k in ("A", "B", "C")}, ensure_ascii=False))
        for row in m.get("rows", [])[-14:]:
            print("  %s %-6s %-4s | %s | %s | asr=%s mt=%s tts=%s e2e=%s" % (
                row.get("id", "")[:8], row.get("chain"), row.get("state"),
                (row.get("src") or "")[:34], (row.get("dst") or "")[:34],
                row.get("asr_ms"), row.get("mt_ms"), row.get("tts_ms"), row.get("e2e_ms")))
        print("  keys:", sorted(m.get("rows", [{}])[0].keys()) if m.get("rows") else "-")
    elif cmd == "region":
        r = post("/api/ocr/region", {"region": [int(x) for x in a[1:5]]})
        print(json.dumps({k: r.get(k) for k in ("ok", "region", "crop")}, ensure_ascii=False))
    elif cmd == "ocr":
        print(json.dumps(post("/api/ocr/test", {}), ensure_ascii=False))
    elif cmd == "clear":
        print(json.dumps(post("/api/clear", {}), ensure_ascii=False))
    else:
        print("unknown cmd")


if __name__ == "__main__":
    main()