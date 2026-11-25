# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, os.getcwd())
from vrdub import cfg as cfgmod, tts, net as netmod
c = cfgmod.Cfg()
n = netmod.Net("ali", c.host, c.key, slots=3)
t = tts.Synth(c, n)
tests = [
    ("あ、そうだ。明日の夜、九時くらいに集合でいい？ちょっと遅れるかもしれないけど、先に始めてて。", "ja", None),
    ("明日の夜、九時くらいに集合でいい？", "ja", None),
    ("あ、そうだ。", "ja", None),
    ("こんにちは、聞こえてますか。こちらはテストです。", "ja", None),
    ("あ、そうだ。明日の夜、九時くらいに集合でいい？", None, None),
    ("あ、そうだ。明日の夜、九時くらいに集合でいい？ちょっと遅れるかもしれないけど、先に始めてて。", "ja", 1.0),
]
for txt, lang, rate in tests:
    t0 = time.time()
    try:
        pcm, first, total = t.synth(txt, c.get("voice"), lang, rate=rate)
        print("OK lang=%s rate=%s dur=%.2fs first=%dms wall=%dms" % (lang, rate, len(pcm)//2/24000, first, (time.time()-t0)*1000))
    except Exception as e:
        print("ERR lang=%s rate=%s %s" % (lang, rate, repr(e)[:280]))
