# -*- coding: utf-8 -*-
"""调试 2：麦克风序列按「连续累加」重建，然后用 FFT 找全局最佳对齐。

上一版把回环块按 time.time() 摆放，块间抖动几毫秒 = 波形被撕碎，
所以哪怕 hist 修好了，相关也只有 0.1。这里所有序列都不带时间戳拼接。
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                     # noqa: E402
from vrdub import cfg as cfgmod, audio, util, tts       # noqa: E402
from vrdub import net as netmod                        # noqa: E402


def best_lag(x, y, maxlag):
    """返回 (lag, corr)：c[lag] = sum_i x[i+lag]*y[i]，corr 用滑动能量归一化。"""
    x = np.asarray(x, np.float64); y = np.asarray(y, np.float64)
    n = 1 << int(np.ceil(np.log2(len(x) + len(y))))
    c = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(y, n)), n)
    cx = np.concatenate([[0.0], np.cumsum(x * x)])
    ey = float(np.sum(y * y)) + 1e-12
    ex = cx[len(x)] - cx[0]
    lags, vals = [], []
    for lag in range(-maxlag, maxlag + 1):
        i = lag if lag >= 0 else n + lag
        if lag >= 0:
            e = cx[len(x)] - cx[lag] if lag + len(y) <= len(x) else ex
        else:
            e = cx[len(x) + lag] - cx[0]
        lags.append(lag)
        vals.append(c[i] / (np.sqrt(max(e, 1e-12) * ey) + 1e-12))
    vals = np.asarray(vals)
    k = int(np.argmax(np.abs(vals)))
    return lags[k], float(vals[k])


def main():
    c = cfgmod.Cfg()
    sy = tts.Synth(c, netmod.Net("ali", c.host, c.key, slots=2))
    pcm, first, _ = sy.synth("一二三四五六七八九十，测试波形对齐。", c.get("voice", ""), "zh")
    print(f"合成 {len(pcm)/2/24000:.2f}s 首包 {first:.0f}ms")

    blocks = []
    lock = threading.Lock()

    def on_block(block, rate):
        b = block.mean(axis=1) if getattr(block, "ndim", 1) == 2 else block
        with lock:
            blocks.append(np.asarray(b, np.float32).copy())

    cap = audio.Capture(26, on_block, kind="loop")
    cap.start()
    pl = audio.Player(19, mon_idx=18)
    pl.start()
    time.sleep(0.6)
    t0 = time.time()
    pl.push_tts_pcm(pcm, 24000)
    time.sleep(len(pcm) / 2.0 / 24000.0 + 0.9)
    t1 = time.time()
    cap.stop(); pl.stop()

    with lock:
        raw = np.concatenate(blocks) if blocks else np.zeros(0, np.float32)
    print(f"回环块 {len(blocks)} 个，总 {raw.size} 采样 @48k")
    mic = util.resample(raw, 48000, 8000)
    pt0, play = pl.hist_window(t0 - 2.5, t1 + 1.0)
    print(f"hist 窗口 {play.size} 采样 rms={np.sqrt(np.mean(play**2)):.4f}")
    print(f"mic  8k     {mic.size} 采样 rms={np.sqrt(np.mean(mic**2)):.4f}")
    lag, corr = best_lag(play, mic, maxlag=8000 * 4)
    print(f"全局最佳对齐：lag={lag}（{lag/8.0:+.0f} ms）归一化相关 {corr:.3f}")


if __name__ == "__main__":
    main()
