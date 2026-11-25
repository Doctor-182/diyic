# -*- coding: utf-8 -*-
import http.client, json, time, sys

HOST, PORT = "127.0.0.1", 8899

def call(method, path, obj=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=15)
    b = json.dumps(obj).encode() if obj is not None else None
    h = {"Content-Type": "application/json"} if b else {}
    c.request(method, path, body=b, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return json.loads(raw or b"{}")
    except Exception:
        print("BAD", path, r.status, repr(raw[:200]))
        return {}

print("clear:", call("POST", "/api/clear"))
print("state keys:", list(call("GET", "/api/state").keys())[:8])
