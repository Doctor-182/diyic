# -*- coding: utf-8 -*-
"""校验匹配滤波的链路对齐 + 阈值灵敏度（用耳机回环 #25 代替麦克风）。

回环录到的就是「我们放出去的声音」本身，所以这里考的是：
  1. hist_window / 重采样 / 时移搜索这套管线对不对（对齐后相关应该接近 1）；
  2. 叠加 -10 / -20 / -26 dB 衰减 + 噪声后相关掉到多少（决定阈值）。
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                     # noqa: E402

from vrdub import cfg as cfgmod, audio, util, tts      # noqa: E402
from vrdub import net as netmod                        # noqa: E402


def main():
    c = cfgmod.Cfg()
    out_dev = int(c.get("out_dev", 19))
    mon_dev = int(c.get("mon_dev", 18))
    loop_dev = int(c.get("loop_dev", 25))
    sy = tts.Synth(c, netmod.Net("ali", c.host, c.key, slots=2))
    pcm, first, _ = sy.synth("等一下，我记得我们最开始加的不是这个配料啊，味道怎么完全变了。",
                             c.get("voice", ""), "zh")
    print(f"合成 {len(pcm)/2/24000:.2f}s 首包 {first:.0f}ms；回环录 #{loop_dev}（耳机那一路）")

    chunks, lk = [], threading.Lock()

    def on_block(block, rate):
        b = block.mean(axis=1) if getattr(block, "ndim", 1) == 2 else block
        with lk:
            chunks.append((time.time() - len(b) / float(rate),
                           util.resample(np.asarray(b, np.float32), rate, 8000)))

    cap = audio.Capture(loop_dev, on_block, kind="loop")
    cap.start()
    pl = audio.Player(out_dev, mon_idx=mon_dev)
    pl.start()
    time.sleep(0.5)
    t0 = time.time()
    pl.push_tts_pcm(pcm, 24000)
    dur = len(pcm) / 2.0 / 24000.0
    time.sleep(dur + 0.8)
    t1 = time.time()
    cap.stop(); pl.stop()

    with lk:
        items = [(ts, a) for ts, a in chunks if ts + len(a) / 8000.0 >= t0 - 0.2 and ts <= t1]
    if not items:
        print("回环没录到东西")
        return
    ts0 = min(x[0] for x in items)
    n = int(round((max(x[0] + len(x[1]) / 8000.0 for x in items) - ts0) * 8000)) + 1
    mic = np.zeros(n, np.float32)
    for ts, a in items:
        i = int(round((ts - ts0) * 8000)); j = min(n, i + len(a))
        if j > i:
            mic[i:j] = a[:j - i]

    pt0, play = pl.hist_window(t0 - 1.2, t1 + 0.2)
    k0 = int(round((ts0 - pt0) * 8000))
    print(f"麦克风 {len(mic)} 采样 / 播放 {len(play)} 采样 / 期望对齐 k0={k0}")
    rng = np.random.RandomState(7)
    for atten in (0, -10, -20, -26, -32):
        g = 10 ** (atten / 20.0)
        noise = rng.randn(len(mic)).astype(np.float32) * float(np.std(mic) or 1.0) * g * 0.3
        y = (mic * g + noise).astype(np.float32)
        cb, k, gap = util.xcorr_norm(play, y, k0 - 9600, k0 + 9600)
        print(f"  衰减 {atten:>4} dB → 峰值相关 {cb:.3f}  偏移 {(k - k0) / 8.0:+.0f} ms  能量差 {gap:+.0f} dB")


if __name__ == "__main__":
    main()
