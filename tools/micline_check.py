# -*- coding: utf-8 -*-
"""验证「合成音有没有真进 VRChat 的麦克风线」：
一边往后端发 /api/inject，一边录 CABLE Output（VRChat 的麦克风就是它），看电平。
用法: python tools/micline_check.py [cap_dev=24] [seconds=9]
"""
import json, os, sys, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vrdub import audio

CAP = int(sys.argv[1]) if len(sys.argv) > 1 else 24
SEC = float(sys.argv[2]) if len(sys.argv) > 2 else 9.0
BASE = "http://127.0.0.1:8899"

buf = []


def cb(block, rate):
    buf.append(np.asarray(block, np.float32).copy())


def inject(text):
    body = json.dumps({"text": text, "lang": "zh"}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/inject", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    c = audio.Capture(CAP, cb, log=lambda *a, **k: None, kind="loop")
    c.start()
    time.sleep(1.0)
    t0 = time.time()
    r = inject("测试合成音有没有进虚拟麦克风线。")
    print("inject ->", json.dumps(r, ensure_ascii=False)[:160], flush=True)
    while time.time() - t0 < SEC:
        time.sleep(0.1)
    c.stop()
    x = np.concatenate(buf) if buf else np.zeros(1, np.float32)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    db = 20 * np.log10(max(peak, 1e-9))
    nz = int((np.abs(x) > 0.01).sum())
    print("录到 %.2fs · peak=%.3f (%.1f dB) · 有声采样 %d/%d" % (x.size / 48000.0, peak, db, nz, x.size))
    print("结论：" + ("合成音确实进了 CABLE Output（VRChat 麦克风选它就能被对方听到）"
                    if peak > 0.05 else "这条线上没有声音 —— 检查播放设备 out_dev 是不是 CABLE Input"))
    # 顺便看后端认为自己在往哪播
    with urllib.request.urlopen(BASE + "/api/state", timeout=8) as rr:
        st = json.loads(rr.read().decode("utf-8"))
    print("后端 out_dev =", st.get("cfg", {}).get("out_dev"), "| devices.output 里：",
          [d.get("name") for d in st.get("devices", {}).get("output", [])][:6])


if __name__ == "__main__":
    main()
