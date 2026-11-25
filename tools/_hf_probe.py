# -*- coding: utf-8 -*-
import http.client, ssl, json

def get(url, timeout=30):
    u = url.split("://", 1)[1]
    host, _, path = u.partition("/")
    c = http.client.HTTPSConnection("127.0.0.1", 2080, timeout=timeout, context=ssl.create_default_context())
    c.set_tunnel(host, 443)
    c.request("GET", "/" + path, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    r = c.getresponse()
    return r.status, r.read()

st, b = get("https://raw.githubusercontent.com/litagin02/anime-whisper/main/evaluation.md")
print("### eval table", st)
print(b.decode("utf-8", "replace"))

for q in ["kotoba-whisper", "anime-whisper", "Fun-ASR"]:
    st, b = get("https://huggingface.co/api/models?search=" + q.replace(" ", "%20") + "&limit=40&sort=downloads&direction=-1")
    print("### search", q, st)
    try:
        data = json.loads(b.decode("utf-8"))
        for m in data:
            print("  ", m.get("modelId"), "| dl", m.get("downloads"), "| tags", ",".join([t for t in (m.get("tags") or []) if t in ("ct2","ctranslate2","whisper","onnx","gguf","japanese","ja")]))
    except Exception as e:
        print("  parse err", e, b[:200])
