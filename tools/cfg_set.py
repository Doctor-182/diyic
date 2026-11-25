import json, sys, urllib.request as u
body = json.loads(sys.argv[1])
r = u.urlopen(u.Request("http://127.0.0.1:8899/api/cfg", data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json"}), timeout=10)
print(r.read().decode("utf-8")[:300])
