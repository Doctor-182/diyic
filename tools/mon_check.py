# -*- coding: utf-8 -*-
"""监听自检：把一段合成音只播到「监听设备」，同时用回环把它录回来，
证明这条路真的出声了（而不是「打开成功但其实没声音」）。

用法：
    python -m tools.mon_check                 # 播到 #18，录 #25
    python -m tools.mon_check 18 25 "你好"
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from vrdub import audio, cfg as cfgmod, util


def main():
    out_dev = int(sys.argv[1]) if len(sys.argv) > 1 else 18
    cap_dev = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    text = sys.argv[3] if len(sys.argv) > 3 else "监听自检，一二三四五六七八。"

    c = cfgmod.Cfg()
    from vrdub import tts as ttsmod
    from vrdub import net as netmod
    syn = ttsmod.Synth(c, netmod.Net("ali", c.host, c.key, slots=4))
    pcm, first, total = syn.synth(text, c.get("voice"), "zh")
    audio_s = len(pcm) / 2.0 / 24000.0
    print("合成：首包 %.0fms / %.2fs 音频" % (first, audio_s))

    blocks = []
    cap = audio.Capture(cap_dev, lambda a, r: blocks.append(a.copy()), log=print, kind="loop")
    cap.start()
    time.sleep(0.4)
    threading.Thread(target=lambda: audio.play_pcm(out_dev, pcm, 24000, 1.0, log=print),
                     daemon=True).start()
    time.sleep(audio_s + 1.6)
    cap.stop()

    if not blocks:
        print("没采到任何数据")
        return
    x = np.concatenate(blocks).reshape(-1)
    peak = float(np.abs(x).max()) if x.size else 0.0
    rms = float(np.sqrt((x ** 2).mean())) if x.size else 0.0
    db = lambda v: (20 * np.log10(v) if v > 0 else -99.0)
    print("录到 %.2fs · peak=%.3f (%.1f dB) · rms=%.1f dB"
          % (x.size / 48000.0, peak, db(peak), db(rms)))
    g = x[: (x.size // 4800) * 4800].reshape(-1, 4800)
    line = "".join("#" if float(np.abs(b).max()) > 0.02 else "." for b in g)
    print("电平（每格 0.1s）：" + line)
    print("结论：" + ("这条路上真的有声音 ✅" if peak > 0.02 else "这条路没有声音 ❌（设备/静音/选错）"))


main()
