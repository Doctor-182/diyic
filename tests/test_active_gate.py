# -*- coding: utf-8 -*-
r"""占空比闸真值表：咔哒一声不能当一句话送进识别。"""
import numpy as np

import vrdub.vad as V
from vrdub.vad import Segmenter


class C(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class Clock:
    def __init__(self):
        self.t = 1e6

    def time(self):
        return self.t

    def tick(self, dt=0.01):
        self.t += dt


BASE = {"mic_db": -50.0, "max_speech": 30.0, "min_speech": 0.2, "end_silence": 0.4,
        "partial_every": 0, "mic_gate_db": 6.0, "active_ms": 120, "speech_ratio": 0.12,
        "valley_cut": False}


def plan_run(plan_or_audio, rate=48000, taps=None):
    """plan_or_audio 可以是 [(块数, dB)]，也可以直接给 float32 音频（按 10 ms 切块）。"""
    segs = []
    clk = Clock()
    old = V.time
    V.time = clk
    try:
        s = Segmenter(C(BASE), segs.append, log=lambda m, *a, **k: (taps.append(m) if taps is not None else None), name="B")
        s.cfg_changed()                     # 真机 app 启动时就会调，测试也得走这条路
        if isinstance(plan_or_audio, list):
            rng = np.random.RandomState(0)
            for cnt, db in plan_or_audio:
                for _ in range(cnt):
                    a = (rng.randn(480) * 0.3).astype(np.float32)
                    a *= (10 ** (db / 20.0)) / (np.sqrt((a ** 2).mean()) + 1e-9)
                    s.feed(a, rate=rate)
                    clk.tick(0.01)
        else:
            x = np.asarray(plan_or_audio, np.float32)
            for i in range(0, x.size, 480):
                s.feed(x[i:i + 480], rate=rate)
                clk.tick(0.01)
        s.flush()
    finally:
        V.time = old
    return s, segs


def tone(sec, db, rng=None):
    rng = rng or np.random.RandomState(7)
    n = int(48000 * sec)
    a = (rng.randn(n) * 0.3).astype(np.float32)
    a *= (10 ** (db / 20.0)) / (np.sqrt((a ** 2).mean()) + 1e-9)
    return a


ok = []

# 1) 一段真说话（0.8 s 有声 + 0.5 s 静音尾巴）→ 收
_, segs = plan_run([(20, -70), (80, -20), (50, -70)])
ok.append(("0.8s 真说话能过闸", len(segs) == 1 and segs[0]["dur"] > 0.7,
           "segs=%d" % len(segs)))

# 2) 咔哒：0.9 s 静音里夹 20 ms 尖峰 → 丢
x = np.concatenate([tone(0.4, -70), tone(0.02, -12), tone(0.5, -70)])
taps = []
_, segs2 = plan_run(x, taps=taps)
ok.append(("20ms 咔哒被弃", len(segs2) == 0, "segs=%d" % len(segs2)))
ok.append(("弃段日志说是发声太短", any("有效发声只有" in m for m in taps), str(taps[:2])))

# 3) 老鼠啃麦式的断续咔哒：0.6 s 内 6 个 10 ms 尖峰（占空 10%）→ 丢
parts = [tone(0.2, -70)]
for i in range(6):
    parts.append(tone(0.01, -14))
    parts.append(tone(0.09, -70))
x3 = np.concatenate(parts + [tone(0.4, -70)])
_, segs3 = plan_run(x3)
ok.append(("断续咔哒占空不足被弃", len(segs3) == 0, "segs=%d" % len(segs3)))

# 4) 轻声但持续的说话（-40 dB，门限 -50）→ 过
taps4 = []
_, segs4 = plan_run([(20, -70), (80, -40), (50, -70)], taps=taps4)
print("    taps4:", taps4[:4])
ok.append(("轻声持续说话能过", len(segs4) == 1, "segs=%d" % len(segs4)))

# 5) 极短的「嗯」0.18 s（占整段比例仍然低，但绝对量够）→ 过
_, segs5 = plan_run([(20, -70), (18, -22), (40, -70)])
ok.append(("0.18s 的「嗯」能过", len(segs5) == 1, "segs=%d" % len(segs5)))

bad = 0
for name, cond, msg in ok:
    print(("  PASS  " if cond else "  FAIL  ") + name + "  [" + msg + "]")
    bad += 0 if cond else 1
print("RESULT:", "ALL PASS" if bad == 0 else "%d FAILED" % bad)
raise SystemExit(1 if bad else 0)