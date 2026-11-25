# -*- coding: utf-8 -*-
"""探套餐端点支持哪些模型（只读，不花钱）。"""
import http.client, json, os

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
p = os.path.join(root, ".env.local")
for line in open(p, encoding="utf-8-sig"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")

key = env.get("DASHSCOPE_TOKENPLAN_KEY", "")
host = "token-plan.cn-beijing.maas.aliyuncs.com"
for path in ("/compatible-mode/v1/models", "/api/v1/models"):
    try:
        c = http.client.HTTPSConnection(host, timeout=25)
        c.request("GET", path, headers={"Authorization": "Bearer " + key})
        r = c.getresponse()
        b = r.read().decode("utf-8", "replace")
        print("==", path, r.status)
        print(b[:4000])
    except Exception as e:
        print("==", path, "ERR", type(e).__name__, e)
