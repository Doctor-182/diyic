# -*- coding: utf-8 -*-
"""验证 Player 的监听回路：用真实 Player（#19 主输出 + #18 监听）放一段 440Hz，
同时用 #25 回环把它录回来。听不见合成音时先跑这个。

    python tools/mon_player_check.py [主输出 19] [监听 18] [回环 25]
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from vrdub import audio


def main():
    out_dev = int(sys.argv[1]) if len(sys.argv) > 1 else 19
    mon_dev = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    cap_dev = int(sys.argv[3]) if len(sys.argv) > 3 else 25

    blocks = []
    cap = audio.Capture(cap_dev, lambda a, r: blocks.append(a.copy()), log=print, kind="loop")
    cap.start()
    time.sleep(0.3)

    pl = audio.Player(out_dev, mon_idx=mon_dev, log=print)
    pl.start()
    time.sleep(0.5)

    sr = 24000
    t = np.arange(int(sr * 1.5)) / sr
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    pcm = (tone * 32767).astype("<i2").tobytes()
    pl.push_tts_pcm(pcm, sr)
    time.sleep(2.5)
    pl.stop()
    cap.stop()

    x = np.concatenate(blocks).reshape(-1) if blocks else np.zeros(1, np.float32)
    peak = float(np.abs(x).max())
    db = 20 * np.log10(peak) if peak > 0 else -99.0
    print("录到 %.2fs · peak=%.3f (%.1f dB)" % (x.size / 48000.0, peak, db))
    g = x[: (x.size // 4800) * 4800].reshape(-1, 4800)
    print("电平：" + "".join("#" if float(np.abs(b).max()) > 0.02 else "." for b in g))
    print("结论：" + ("Player 的监听回路有声 ✅" if peak > 0.02 else "Player 监听回路没声 ❌"))
    print("（注意：#25 是「#18 这一路」的回环；耳机物理音量/静音另算）")


main()
