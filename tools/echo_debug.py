# -*- coding: utf-8 -*-
"""调试：hist 与真实回环（#26 = CABLE Input 的回环）到底差在哪。"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                     # noqa: E402
from vrdub import cfg as cfgmod, audio, util, tts       # noqa: E402
from vrdub import net as netmod                        # noqa: E402


def main():
    c = cfgmod.Cfg()
    sy = tts.Synth(c, netmod.Net("ali", c.host, c.key, slots=2))
    pcm, first, _ = sy.synth("一二三四五六七八九十，测试波形对齐。", c.get("voice", ""), "zh")
    print(f"合成 {len(pcm)/2/24000:.2f}s 首包 {first:.0f}ms")

    chunks, lk = [], threading.Lock()

    def on_block(block, rate):
        b = block.mean(axis=1) if getattr(block, "ndim", 1) == 2 else block
        with lk:
            chunks.append((time.time() - len(b) / float(rate),
                           util.resample(np.asarray(b, np.float32), rate, 8000)))

    cap = audio.Capture(26, on_block, kind="loop")
    cap.start()
    pl = audio.Player(19, mon_idx=18)
    pl.start()
    time.sleep(0.5)
    t0 = time.time()
    pl.push_tts_pcm(pcm, 24000)
    time.sleep(len(pcm) / 2.0 / 24000.0 + 0.8)
    t1 = time.time()
    cap.stop(); pl.stop()

    with lk:
        items = [(ts, a) for ts, a in chunks if ts + len(a) / 8000.0 >= t0 - 0.3 and ts <= t1]
    ts0 = min(x[0] for x in items)
    n = int(round((max(x[0] + len(x[1]) / 8000.0 for x in items) - ts0) * 8000)) + 1
    mic = np.zeros(n, np.float32)
    for ts, a in items:
        i = int(round((ts - ts0) * 8000)); j = min(n, i + len(a))
        if j > i:
            mic[i:j] = a[:j - i]

    pt0, play = pl.hist_window(t0 - 1.5, t1 + 0.2)
    print(f"hist: {len(play)} 采样 rms={np.sqrt(np.mean(play**2)):.4f} 非零占比={np.mean(np.abs(play) > 1e-4):.2f}")
    print(f"mic : {len(mic)} 采样 rms={np.sqrt(np.mean(mic**2)):.4f} 非零占比={np.mean(np.abs(mic) > 1e-4):.2f}")
    k0 = int(round((ts0 - pt0) * 8000))
    cb, k, gap = util.xcorr_norm(play, mic, k0 - 9600, k0 + 9600)
    print(f"互相关：峰值 {cb:.3f} 位置 {k}（期望 {k0}，偏移 {(k-k0)/8.0:+.0f} ms）能量差 {gap:+.0f} dB")

    # 直接看两段能量包络的形状（每 100 ms 一格）
    def env(a, step=800):
        return [round(float(20*np.log10(np.sqrt(np.mean(a[i:i+step]**2))+1e-6)), 0)
                for i in range(0, max(1, len(a)-step), step)]
    em, ep = env(mic), env(play)
    print("mic 包络:", em[:40])
    print("play包络:", ep[:40])


if __name__ == "__main__":
    main()
