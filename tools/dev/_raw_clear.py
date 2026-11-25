# -*- coding: utf-8 -*-
import http.client, json
c = http.client.HTTPConnection("127.0.0.1", 8899, timeout=10)
c.request("POST", "/api/clear", body=b"{}", headers={"Content-Type": "application/json"})
r = c.getresponse()
print("status", r.status, r.getheader("Content-Type"), len(r.read()))
c.close()
