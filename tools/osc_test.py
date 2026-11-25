# -*- coding: utf-8 -*-
"""OSC chatbox 自测：本机 UDP 端口收包，检查编码、限速、截断。"""
import socket, sys, time
sys.path.insert(0, "D:/vrc无言势")
from vrdub import cfg as cfgmod, osc as oscmod

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.settimeout(0.4)

cfg = cfgmod.Cfg()
cfg.update({"osc_port": port, "chatbox_on": True})
cb = oscmod.OscChatbox(cfg, lambda *a, **k: print("LOG", a))
cb.start()

msgs = ["今日はちょっと遅れちゃった、ごめんね。", "ちょっと待って", "これは限速テスト1", "限速2", "限速3", "限速4", "限速5", "x" * 200]
for m in msgs:
    cb.send(m)
    time.sleep(0.12)

got = []
t0 = time.time()
while time.time() - t0 < 3.0:
    try:
        d, _ = s.recvfrom(4096)
        got.append(d)
    except socket.timeout:
        continue
print("收到", len(got), "包 / 发送", len(msgs), "条")
for d in got:
    parts = d.split(b"\x00")
    addr = parts[0].decode("utf-8", "replace")
    tags = [p for p in parts[1:] if p][0].decode() if len([p for p in parts[1:] if p]) else ""
    text = ""
    idx = d.find(tags.encode()) + len(tags) + 1
    rest = d[idx:]
    rest = rest[(4 - (len(tags) + 1) % 4) % 4:]
    body = rest.rstrip(b"\x00").decode("utf-8", "replace")
    print(f"  {addr} tags={tags!r} text={body[:40]!r} len={len(body)} raw={d[:24].hex()}")
print("stat", cb.stat)
