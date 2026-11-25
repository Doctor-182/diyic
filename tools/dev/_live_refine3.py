# -*- coding: utf-8 -*-
"""现场验证 B 链精修：A 链 TTS 把日语念进 CABLE Input(#18)，
B 链改听这根线的回环（按名字绑，不猜号码），观察 asr_ms / refine_ms / engine / dst。
echo_guard 临时关掉，否则这段就是「自己刚放出去的声音」，会被当成回声丢掉。
跑完自动还原 loop_src / dev_names / echo_guard。
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import http.client

HOST, PORT = "127.0.0.1", 8899
# VB-Cable 的「放音端 CABLE Input / 录音端 CABLE Output」是同一根线：
# 往 #18 CABLE Input 写，从 #24 CABLE Output 读得到；读 #25 的 loopback 是空的（实测 0 帧）
LOOP_NAME = "CABLE Output (VB-Audio Virtual Cable)"


def call(method, path, obj=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=25)
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


st = call("GET", "/api/state")
cfg0 = st.get("cfg") or {}
before = {"loop_src": cfg0.get("loop_src"), "echo_guard": cfg0.get("echo_guard"),
          "dev_names": cfg0.get("dev_names")}
print("before:", json.dumps(before, ensure_ascii=False))

call("POST", "/api/clear")
names = dict(before.get("dev_names") or {})
names["loop"] = LOOP_NAME
# 顺序不能反：先写名字，再切 loop_src 触发重开，否则重开时用的还是旧名字
call("POST", "/api/cfg", {"dev_names": names})
time.sleep(0.3)
call("POST", "/api/cfg", {"loop_src": "device"})
call("POST", "/api/cfg", {"echo_guard": False})
time.sleep(1.5)
st = call("GET", "/api/state")
print("after cfg:", {k: (st.get("cfg") or {}).get(k) for k in ("loop_src", "echo_guard")},
      "loop_dev=", (st.get("cfg") or {}).get("loop_dev"))

JA = "もしもし、ちょっと聞こえてる？今日は少し遅れちゃうかも。"
print("inject:", json.dumps(call("POST", "/api/inject", {"text": JA, "lang": "ja"}), ensure_ascii=False))

seen = {}
t0 = time.time()
while time.time() - t0 < 26.0:
    st = call("GET", "/api/state")
    for s in st.get("last") or []:
        if s.get("chain") == "B":
            seen[s["id"]] = s
    if any(s.get("refine_ms") for s in seen.values()):
        break
    time.sleep(0.5)

print("---- B 段 ----")
for sid, s in seen.items():
    print(json.dumps({k: s.get(k) for k in ("id", "state", "src", "dst", "engine",
                                            "asr_ms", "refine_ms", "mt_ms", "note")},
                     ensure_ascii=False))
if not seen:
    print("没有 B 段：loop_db=", (call("GET", "/api/state").get("cfg") or {}).get("loop_db"))

for k, v in before.items():
    call("POST", "/api/cfg", {k: v})
time.sleep(0.5)
now = call("GET", "/api/state").get("cfg") or {}
print("restore:", json.dumps({k: now.get(k) for k in before}, ensure_ascii=False)[:200])
