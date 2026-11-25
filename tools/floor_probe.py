# -*- coding: utf-8 -*-
"""#25 / #23 静默本底（2.5 s，不播放任何东西）。"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np                                  # noqa: E402
from vrdub import audio, util                       # noqa: E402


def probe(dev, kind, secs=2.5):
    dbs = []

    def cb(block, rate):
        b = np.asarray(block, np.float32)
        if b.ndim > 1:
            b = b.mean(axis=1)
        dbs.append(20.0 * float(np.log10(np.sqrt(np.mean(b ** 2)) + 1e-9)))

    cap = audio.Capture(dev, cb, kind=kind)
    cap.start()
    time.sleep(secs)
    cap.stop()
    if not dbs:
        print(f"#{dev} 没数据")
        return
    a = np.array(dbs)
    print(f"#{dev} {kind}: 块 {len(a)} 中位 {np.median(a):.1f} dB 最小 {a.min():.1f} 最大 {a.max():.1f} p90 {np.percentile(a,90):.1f}")


probe(25, "loop")
probe(23, "mic")
