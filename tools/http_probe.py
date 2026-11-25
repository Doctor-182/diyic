import http.client, json, sys
for path in ["/", "/static/app.js", "/static/style.css", "/api/state", "/api/health", "/api/metrics"]:
    try:
        c = http.client.HTTPConnection("127.0.0.1", 8899, timeout=8)
        c.request("GET", path)
        r = c.getresponse(); b = r.read()
        print(path, r.status, len(b), b[:90].decode("utf-8", "replace").replace("\n", " "))
        c.close()
    except Exception as e:
        print(path, "ERR", repr(e))
