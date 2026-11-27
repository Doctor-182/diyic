# -*- coding: utf-8 -*-
r"""谷切真值表：到 max_speech 时该切在能量谷上，而不是按时间一刀切。"""
import numpy as np

import vrdub.vad as V
from vrdub.vad import Segmenter


class Clock:
    """vad 用 time.time() 算时长，测试里必须给假时钟，否则 195 块几毫秒就喂完了。"""

    def __init__(self, t0=1000000.0):
        self.t = t0

    def time(self):
        return self.t

    def tick(self, dt=0.01):
        self.t += dt


class C(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def blk(n, db, seed=0):
    rng = np.random.RandomState(seed)
    a = (rng.randn(n) * 0.3).astype(np.float32)
    a *= (10 ** (db / 20.0)) / (np.sqrt((a ** 2).mean()) + 1e-9)
    return a


def run(cfg, plan, rate=48000):
    """plan = [(块数, dB)]，每块 10 ms。返回 on_seg 收到的段。"""
    segs = []
    clk = Clock()
    old = V.time
    V.time = clk
    try:
        s = Segmenter(cfg, segs.append, log=lambda *a, **k: None, name="B")
        n = int(rate * 0.01)
        for k, (cnt, db) in enumerate(plan):
            for i in range(cnt):
                s.feed(blk(n, db, seed=k * 100 + i), rate=rate)
                clk.tick(0.01)
    finally:
        V.time = old
    return s, segs


BASE = {"mic_db": -50.0, "max_speech": 1.5, "min_speech": 0.2, "end_silence": 0.5,
        "partial_every": 0, "mic_gate_db": 6.0, "valley_cut": True,
        "valley_win": 1.0, "valley_gap": 6.0}

ok = []

# 1) 有谷：0.9s 响 → 0.15s 静 → 0.9s 响，应该在静处切断，前半段 ≈0.9s
c = C(BASE)
s, segs = run(c, [(90, -20), (15, -75), (90, -20)])
ok.append(("有谷时切开", len(segs) == 1 and 0.70 <= segs[0]["dur"] <= 1.25,
           "segs=%d dur=%s" % (len(segs), [round(x["dur"], 2) for x in segs])))
ok.append(("前半段音频停在谷上", 0.70 <= segs[0]["audio"].size / 16000.0 <= 1.25,
           "audio=%.2fs" % (segs[0]["audio"].size / 16000.0)))
ok.append(("切完还在说话（后半段留着）", s.in_speech and s.buf_n > 0,
           "in_speech=%s buf=%.2fs" % (s.in_speech, s.buf_n / 16000.0)))

# 2) 一直响没有谷 → 只能硬切
s2, segs2 = run(C(BASE), [(300, -20)])
ok.append(("无谷时硬切", len(segs2) == 1 and segs2[0]["dur"] >= 1.4,
           "segs=%d dur=%s" % (len(segs2), [round(x["dur"], 2) for x in segs2])))

# 3) 关掉 valley_cut → 硬切
c3 = C(BASE); c3["valley_cut"] = False
s3, segs3 = run(c3, [(90, -20), (15, -75), (90, -20)])
ok.append(("关掉开关就回到硬切", len(segs3) == 1 and segs3[0]["dur"] >= 1.4,
           "segs=%d dur=%s" % (len(segs3), [round(x["dur"], 2) for x in segs3])))

# 4) 谷离尾巴太近（不到 min_speech）→ 不切，整段收
c4 = C(BASE); c4["max_speech"] = 1.6; c4["min_speech"] = 0.30
s4, segs4 = run(c4, [(145, -20), (10, -75), (10, -20)])
ok.append(("尾巴不够就当没谷", all(x["dur"] >= 1.5 for x in segs4) or not segs4,
           "durs=%s" % [round(x["dur"], 2) for x in segs4]))

# 5) 谷不够深（只低 2 dB）→ 不算谷
c5 = C(BASE)
s5, segs5 = run(c5, [(90, -20), (15, -22), (90, -20)])
ok.append(("浅谷不算谷", len(segs5) == 1 and segs5[0]["dur"] >= 1.4,
           "durs=%s" % [round(x["dur"], 2) for x in segs5]))

bad = 0
for name, cond, msg in ok:
    print(("  PASS  " if cond else "  FAIL  ") + name + "  [" + msg + "]")
    bad += 0 if cond else 1
print("RESULT:", "ALL PASS" if bad == 0 else "%d FAILED" % bad)
raise SystemExit(1 if bad else 0)