# -*- coding: utf-8 -*-
"""回声检测的物理验证：放一段 TTS 进耳机，同时录麦克风，算包络相关。

正例：麦克风听到的是我们刚放的音频 -> 相关应该很高（>=0.6）。
反例：换一段完全不同的音频，和上一段的包络做相关 -> 应该接近 0（说明「重说一遍」不会被误杀）。
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                     # noqa: E402

from vrdub import cfg as cfgmod, audio, util, tts      # noqa: E402
from vrdub import net as netmod                        # noqa: E402


def corr_env(ta, va, tb, vb):
    ta = np.array(ta, dtype=np.float64)
    va = np.clip(np.array(va, dtype=np.float64), -70.0, 0.0)
    tb = np.array(tb, dtype=np.float64)
    vb = np.clip(np.array(vb, dtype=np.float64), -70.0, 0.0)
    if len(ta) < 8 or len(tb) < 12 or va.std() < 0.5:
        return 0.0, -99.0, -99.0
    best, bmean = 0.0, -99.0
    for lag in np.arange(-0.60, 0.61, 0.02):
        v2 = np.interp(ta + lag, tb, vb)
        if v2.std() < 1.5:
            continue
        c = float(np.corrcoef(va, v2)[0, 1])
        if c == c and c > best:
            best, bmean = c, float(v2.mean())
    return best, float(va.mean()), bmean


def main():
    c = cfgmod.Cfg()
    mic_dev = int(c.get("mic_dev", 23))
    out_dev = int(c.get("out_dev", 19))
    mon_dev = int(c.get("mon_dev", 18))
    print(f"设备 mic=#{mic_dev} out=#{out_dev} mon=#{mon_dev}")

    net = netmod.Net("ali", c.host, c.key, slots=2)
    sy = tts.Synth(c, net)
    voice = c.get("voice", "")

    def say(text, lang="zh"):
        pcm, first, total = sy.synth(text, voice, lang)
        print(f"  合成 {lang} '{text[:12]}...' {len(pcm)/2/24000:.2f}s 首包 {first:.0f}ms")
        return pcm

    mic = []
    mlock = threading.Lock()

    def on_block(block, rate):
        mono = block.mean(axis=1) if getattr(block, "ndim", 1) == 2 else block
        with mlock:
            mic.append((time.time(), util.db_of(mono)))

    cap = audio.Capture(mic_dev, on_block, kind="in")
    cap.start()
    pl = audio.Player(out_dev, mon_idx=mon_dev)
    pl.start()
    time.sleep(0.6)

    pcm_a = say("等一下，我记得我们最开始加的不是这个配料啊，怎么味道完全变了。")
    ta0 = time.time()
    pl.push_tts_pcm(pcm_a, 24000)
    dur_a = len(pcm_a) / 2.0 / 24000.0
    time.sleep(dur_a + 1.0)
    ta1 = time.time()

    pcm_b = say("今天天气不错，我们去外面走走吧，顺便把那个快递拿回来。")
    tb0 = time.time()
    pl.push_tts_pcm(pcm_b, 24000)
    dur_b = len(pcm_b) / 2.0 / 24000.0
    time.sleep(dur_b + 1.0)
    tb1 = time.time()

    time.sleep(0.4)
    cap.stop()
    pl.stop()

    with mlock:
        m = list(mic)
    with pl.env_lock:
        e = list(pl.env)

    A = [(t, d) for t, d in m if ta0 - 0.2 <= t <= ta1]
    B = [(t, d) for t, d in m if tb0 - 0.2 <= t <= tb1]
    EA = [(t, d) for t, d in e if ta0 - 1.5 <= t <= ta1 + 1.2]
    EB = [(t, d) for t, d in e if tb0 - 1.5 <= t <= tb1 + 1.2]

    ca, mi_a, oo_a = corr_env([x[0] for x in A], [x[1] for x in A],
                              [x[0] for x in EA], [x[1] for x in EA])
    cb, mi_b, oo_b = corr_env([x[0] for x in B], [x[1] for x in B],
                              [x[0] for x in EB], [x[1] for x in EB])
    # 反例：B 段的麦克风 vs A 段我们放出去的音频（模拟「用户重说一遍」：内容同源、时间错开）
    cx, mi_x, oo_x = corr_env([x[0] for x in B], [x[1] for x in B],
                              [x[0] for x in EA], [x[1] for x in EA])

    print(f"\n正例 段A 麦克风 vs 段A 输出：相关 {ca:.2f}（麦克风 {mi_a:.0f} dB / 输出 {oo_a:.0f} dB）")
    print(f"正例 段B 麦克风 vs 段B 输出：相关 {cb:.2f}（麦克风 {mi_b:.0f} dB / 输出 {oo_b:.0f} dB）")
    print(f"反例 段B 麦克风 vs 段A 输出：相关 {cx:.2f}（错开时间，应该很低）")
    print("\n结论：" + ("回声判据可用（正例高、反例低）" if ca >= 0.6 and cx < 0.4 else "需要调阈值"))


if __name__ == "__main__":
    main()
