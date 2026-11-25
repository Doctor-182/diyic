# -*- coding: utf-8 -*-
"""回声判定真值表：只测 _is_echo 的决策分支（不碰声卡）。"""
import sys, time
sys.path.insert(0, '.')
from vrdub import chains, cfg as cfgmod, util

c = cfgmod.Cfg()
c.update({"echo_guard": True, "dst_lang": "ja"})


class Fake:
    def __init__(self, corr, off, gap, src_key="mic"):
        self.cfg = c
        self.src_key = src_key
        self.name = "A"
        self._v = (corr, off, gap)
        self.log = lambda *a, **k: None

    def echo_match(self, audio, t0, t1):
        return self._v


def call(corr, gap, text="这一句是复述的话", src_key="mic"):
    f = Fake(corr, -0.2, gap, src_key)
    return chains.StreamChain._is_echo(f, text, {"t0": time.time() - 2}, time.time(), b"x")


cases = [
    ("纯回声（电学回路实测）", 0.98, -12.0, True),
    ("真实漏音（估计）", 0.45, -20.0, True),
    ("我一开口压过漏音", 0.30, 9.0, False),
    ("复述同一句（无漏音）", 0.02, -3.0, False),
    ("边缘 0.10", 0.10, -8.0, False),
]
ok = True
for name, corr, gap, want in cases:
    got = call(corr, gap)
    flag = "OK " if got == want else "BAD"
    if got != want:
        ok = False
    print(f"{flag} {name:<22} corr={corr:.2f} gap={gap:+.0f} → {got}（期望 {want}）")
print("全部通过" if ok else "有失败")
