# -*- coding: utf-8 -*-
import http.client, ssl, json

def get(url, timeout=40):
    u = url.split("://", 1)[1]
    host, _, path = u.partition("/")
    c = http.client.HTTPSConnection("127.0.0.1", 2080, timeout=timeout, context=ssl.create_default_context())
    c.set_tunnel(host, 443)
    c.request("GET", "/" + path, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    r = c.getresponse()
    return r.status, r.read()

for repo in ["quantumcookie/anime-whisper-ct2-int8", "jctv-tech/kotoba-whisper-v2.2-ct2",
             "kotoba-tech/kotoba-whisper-v2.0-faster", "quantumcookie/anime-whisper-ct2-fp16"]:
    st, b = get("https://huggingface.co/api/models/%s/tree/main" % repo)
    print("###", repo, st)
    try:
        for f in json.loads(b.decode("utf-8")):
            sz = f.get("size") or (f.get("lfs") or {}).get("size") or 0
            print("   ", f.get("path"), "%.1f MB" % (sz / 1048576.0))
    except Exception as e:
        print("   err", e, b[:200])
