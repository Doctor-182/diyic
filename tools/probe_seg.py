
import json, sys, queue
sys.path.insert(0, r"D:\vrc无言势")
from vrdub import bench, cfg as cfgmod
cfg = cfgmod.Cfg()
cfg.update({"dbg_timing": True})
rig = bench.Rig(cfg)
clips = bench.ensure_clips(rig.synth, cfg)
zh = [c for c in clips if c[0] == "zh"]
print("nzh", len(zh))
sid, q = rig.bus.sub()
r = bench.lat_once(rig, "A", zh[1][1], zh[1][2], cfg)
print("RESULT", json.dumps(r, ensure_ascii=False))
seen = []
while True:
    try:
        e = q.get_nowait()
    except queue.Empty:
        break
    if "seg" in e:
        s = e["seg"]
        seen.append(s)
        print("SEG", s.get("id"), s.get("state"), "tts", s.get("tts_ms"), "audio", s.get("audio_ms"),
              "e2e", s.get("e2e_ms"), "asr", s.get("asr_ms"), "mt", s.get("mt_ms"), "|", (s.get("dst") or "")[:24])
rig.stop()
