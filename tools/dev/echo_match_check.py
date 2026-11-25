# -*- coding: utf-8 -*-
"""回声检测验证：真放 TTS 进耳机、同时录麦克风，用匹配滤波算相似度。

正例：麦克风里的 A 段 vs 我们放出去的 A 段 -> 相关应该明显（>=0.15）。
反例：麦克风里的 B 段 vs 我们放出去的 A 段（内容不同、时间错开）-> 应该接近 0。
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
    mic_dev = int(c.get("mic_dev", 23))
    out_dev = int(c.get("out_dev", 19))
    mon_dev = int(c.get("mon_dev", 18))
    print(f"设备 mic=#{mic_dev} out=#{out_dev} mon=#{mon_dev}")
    net = netmod.Net("ali", c.host, c.key, slots=2)
    sy = tts.Synth(c, net)
    voice = c.get("voice", "")

    chunks = []
    mlock = threading.Lock()

    def on_block(block, rate):
        b = block.mean(axis=1) if getattr(block, "ndim", 1) == 2 else block
        with mlock:
            chunks.append((time.time() - len(b) / float(rate),
                           util.resample(np.asarray(b, np.float32), rate, 8000)))

    def mic_wave(t0, t1):
        with mlock:
            items = [(ts, a) for ts, a in chunks if ts + len(a) / 8000.0 >= t0 and ts <= t1]
        if not items:
            return t0, np.zeros(0, np.float32)
        ts0 = min(ts for ts, _ in items)
        n = int(round((max(ts + len(a) / 8000.0 for ts, a in items) - ts0) * 8000)) + 1
        out = np.zeros(n, np.float32)
        for ts, a in items:
            i = int(round((ts - ts0) * 8000))
            j = min(n, i + len(a))
            if j > i:
                out[i:j] = a[:j - i]
        return ts0, out

    cap = audio.Capture(mic_dev, on_block, kind="in")
    cap.start()
    pl = audio.Player(out_dev, mon_idx=mon_dev)
    pl.start()
    time.sleep(0.6)

    def say_pcm(text):
        pcm, first, _ = sy.synth(text, voice, "zh")
        print(f"  合成 '{text[:10]}…' {len(pcm)/2/24000:.2f}s 首包 {first:.0f}ms")
        return pcm

    a_txt = "等一下，我记得我们最开始加的不是这个配料啊，味道怎么完全变了。"
    b_txt = "今天天气不错，我们去外面走走吧，顺便把那个快递拿回来。"
    pa, pb = say_pcm(a_txt), say_pcm(b_txt)

    ta0 = time.time(); pl.push_tts_pcm(pa, 24000)
    time.sleep(len(pa) / 2.0 / 24000.0 + 0.8)
    ta1 = time.time()

    tb0 = time.time(); pl.push_tts_pcm(pb, 24000)
    time.sleep(len(pb) / 2.0 / 24000.0 + 0.8)
    tb1 = time.time()

    time.sleep(0.3)
    cap.stop(); pl.stop()

    def score(seg_t0, seg_t1, hist_t0, hist_t1, label):
        mt, mw = mic_wave(seg_t0, seg_t1)
        ht, hw = pl.hist_window(hist_t0, hist_t1)
        if not len(mw) or not len(hw) or len(hw) < len(mw):
            print(f"{label}: 数据不足 mic={len(mw)} play={len(hw)}")
            return
        k0 = int(round((mt - ht) * 8000))
        cbest, k, gap = util.xcorr_norm(hw, mw, k0 - 9600, k0 + 9600)
        print(f"{label}: 峰值相关 {cbest:.3f}  偏移 {(k - k0) / 8.0:+.0f} ms  能量差 {gap:+.0f} dB")

    print("\n=== 匹配滤波结果 ===")
    score(ta0, ta1, ta0 - 1.2, ta1 + 0.2, "正例 麦克风A vs 播放A")
    score(tb0, tb1, tb0 - 1.2, tb1 + 0.2, "正例 麦克风B vs 播放B")
    score(tb0, tb1, ta0 - 1.2, ta1 + 0.2, "反例 麦克风B vs 播放A（内容不同）")


if __name__ == "__main__":
    main()
