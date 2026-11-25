# -*- coding: utf-8 -*-
"""回声检测端到端验证：真的走 vad.Segmenter + Player.hist_window + util.xcorr_norm。

三组：
  1. 正例：把 TTS 放出去，用耳机回环 #25 当「麦克风」——录到的就是那段合成音，
     相关应该很高（>0.5），说明整条管线（分段/重采样/时间轴/匹配滤波）是对的。
  2. 反例：同一路回环，但拿「另一句话」的播放窗口去比 —— 相关应该低（<0.1）。
  3. 声学路：真麦克风 #23（耳机摆桌上，耦合很弱），看真实漏音的量级落在哪。
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                     # noqa: E402
from vrdub import cfg as cfgmod, audio, util, tts, vad  # noqa: E402
from vrdub import net as netmod                        # noqa: E402

S1 = "我们先把服务器开起来，然后一起去打那个新副本吧。"
S2 = "今天的天气还不错，不过我觉得晚上可能会下雨。"


class Rig:
    def __init__(self, cap_dev, kind, player, thr=-40.0):
        self.c = cfgmod.Cfg()
        self.player = player
        self.segs = []
        self.blocks = 0
        self.peak = 0.0
        self.err = None
        self.seg = vad.Segmenter(self.c, self._on_seg, name="A")
        self.seg.thr = float(thr)
        self.cap = audio.Capture(cap_dev, self._on_block, kind=kind)
        self.cap.start()

    def _on_block(self, block, rate):
        try:
            self.blocks += 1
            b = np.asarray(block, np.float32)
            if b.ndim > 1:
                b = b.mean(axis=1)
            self.peak = max(self.peak, float(np.max(np.abs(b)) if b.size else 0.0))
            self.seg.feed(block, rate)
        except Exception as e:                      # noqa: BLE001
            self.err = repr(e)

    def _on_seg(self, ev):
        if ev.get("final"):
            self.segs.append(ev)

    def stop(self):
        self.cap.stop()

    def play(self, text, zh="zh", wait=9.0):
        sy = tts.Synth(self.c, netmod.Net("ali", self.c.host, self.c.key, slots=2))
        pcm, first, _ = sy.synth(text, self.c.get("voice", ""), zh)
        n0 = len(self.segs)
        self.player.push_tts_pcm(pcm, 24000)
        t_end = time.time() + wait
        while time.time() < t_end and len(self.segs) == n0:
            time.sleep(0.05)
        time.sleep(0.3)
        return self.segs[-1] if len(self.segs) > n0 else None

    def match(self, ev, t_override=None):
        y = np.asarray(ev["audio"])
        y = y.astype(np.float32) / 32768.0 if y.dtype == np.int16 else y.astype(np.float32)
        y8 = util.resample(y, 16000, 8000)
        t0, t1 = float(t_override or ev["t0"]), float(ev["t1"])
        pt0, x = self.player.hist_window(t0 - 1.2, t1 + 0.2)
        if len(x) < len(y8) or len(y8) < 1600:
            return 0.0, 0.0, 0.0
        k0 = int(round((t0 - pt0) * 8000.0))
        span = int(1.2 * 8000)
        c, lag, gap = util.xcorr_norm(x, y8, k0 - span, k0 + span)
        cg, lagg, gapg = util.xcorr_norm(x, y8, -len(x), len(x))
        print(f"  [dbg] y8={len(y8)} x={len(x)} k0={k0} 窗口内 {c:+.3f}@{lag} 全局 {cg:+.3f}@{lagg}（{lagg/8.0:+.0f} ms）gap {gapg:+.0f}")
        return float(c), (lag - k0) / 8000.0, float(gap)


def main():
    c = cfgmod.Cfg()
    c.update({"out_on": True, "in_on": True, "partial_every": 0, "mon_on": True, "mon_gain": 1.0})
    pl = audio.Player(int(c.get("out_dev", 19)), mon_idx=int(c.get("mon_dev", 18)))
    pl.start()
    pl.gain_tts = 1.0

    print("\n=== 1/2 CABLE 回环 #26 当麦克风（电学回路，验证管线）===")
    rig = Rig(26, "loop", pl, thr=-55.0)
    e1 = rig.play(S1)
    print(f"[debug] 回环块 {rig.blocks} 峰值 {rig.peak:.3f} 段 {len(rig.segs)} err={rig.err}")
    if not e1:
        print(f"[debug] seg.buf_n={rig.seg.buf_n} in_speech={rig.seg.in_speech} db={rig.seg.db:.1f} thr={rig.seg.thr}")
    if e1:
        cc, off, gap = rig.match(e1)
        print(f"正例（同一段音频）: 相关 {cc:+.3f}  偏移 {off*1000:+.0f} ms  能量差 {gap:+.0f} dB")
        e2 = rig.play(S2)
        if e2:
            cc2, off2, gap2 = rig.match(e1, t_override=e2["t0"])
            print(f"反例（另一句话窗口）: 相关 {cc2:+.3f}  偏移 {off2*1000:+.0f} ms  能量差 {gap2:+.0f} dB")
            c3, _, _ = rig.match(e2)
            print(f"正例 2（第二句自己）: 相关 {c3:+.3f}")
    rig.stop()

    print("\n=== 3 真麦克风 #23（耳机摆桌上，耦合弱，看量级）===")
    rig2 = Rig(int(c.get("mic_dev", 23)), "mic", pl, thr=-46.0)
    e3 = rig2.play(S1)
    if e3:
        cc, off, gap = rig2.match(e3)
        print(f"声学漏音: 相关 {cc:+.3f}  偏移 {off*1000:+.0f} ms  能量差 {gap:+.0f} dB")
    else:
        print("麦克风没收到可分段的声音（耳机没戴，正常）")
    rig2.stop()
    pl.stop()


if __name__ == "__main__":
    main()