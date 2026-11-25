"""单句复现：看一段的合成明细，定位「一句念了两遍」的音频时长问题。"""
import os, sys, time
sys.path.insert(0, "D:/vrc无言势")
from vrdub import bench, cfg as cm, util

c = cm.Cfg()
c.update({"dbg_timing": True, "echo_guard": True})
rig = bench.Rig(c)
try:
    clips = bench.ensure_clips(rig.synth, c)
    zh = [(l, t, p) for (l, t, p) in clips if l == "zh"]
    print("clips:", [(t, p) for (l, t, p) in zh])
    for (l, t, p) in zh[2:4]:
        r = bench.lat_once(rig, "A", t, p, c)
        print("RESULT", r)
        seg = rig.bus.recent(10)
        for s in seg:
            print("  ", s["id"], s["state"], "tts", s.get("tts_ms"), "audio", s.get("audio_ms"), "e2e", s.get("e2e_ms"), "note", s.get("note"))
finally:
    rig.stop()
