# -*- coding: utf-8 -*-
import http.client, ssl
PROXY = ("127.0.0.1", 2080)

def get_proxy(url, timeout=25):
    u = url.split("://", 1)[1]
    host, _, path = u.partition("/")
    c = http.client.HTTPSConnection(PROXY[0], PROXY[1], timeout=timeout,
                                    context=ssl.create_default_context())
    c.set_tunnel(host, 443)
    c.request("GET", "/" + path, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    r = c.getresponse()
    return r.status, r.read()

for u in ["https://huggingface.co/litagin/anime-whisper/raw/main/README.md",
          "https://raw.githubusercontent.com/litagin02/anime-whisper/main/evaluation.md",
          "https://huggingface.co/kotoba-tech/kotoba-whisper-v2.2/raw/main/README.md"]:
    try:
        st, b = get_proxy(u)
        print("==", u, st, len(b))
        print(b.decode("utf-8", "replace")[:600].replace(chr(10), " | "))
    except Exception as e:
        print("==", u, "ERR", type(e).__name__, e)
