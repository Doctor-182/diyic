# -*- coding: utf-8 -*-
import http.client, ssl, sys, json

def get(url, timeout=25):
    u = url.split("://", 1)[1]
    host, _, path = u.partition("/")
    ctx = ssl.create_default_context()
    for port in (443,):
        c = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        c.request("GET", "/" + path, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        r = c.getresponse()
        return r.status, r.read()
    return 0, b""

for u in ["https://huggingface.co/litagin/anime-whisper/raw/main/README.md",
          "https://raw.githubusercontent.com/litagin02/anime-whisper/main/README.md"]:
    try:
        st, b = get(u)
        print("==", u, st, len(b))
        txt = b.decode("utf-8", "replace")
        print(txt[:1200].replace("\n", " | ")[:1200])
    except Exception as e:
        print("==", u, "ERR", type(e).__name__, e)
