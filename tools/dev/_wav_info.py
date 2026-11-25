# -*- coding: utf-8 -*-
import os, wave, contextlib
root = r"D:\vrc无言势\out"
for name in ["clips/ja0.wav","clips/zh0.wav","live_rec.wav"]:
    p = os.path.join(root, name.replace("/", os.sep))
    with contextlib.closing(wave.open(p, "rb")) as w:
        print(name, "ch", w.getnchannels(), "rate", w.getframerate(), "width", w.getsampwidth(),
              "dur %.2f s" % (w.getnframes() / w.getframerate()), "bytes", os.path.getsize(p))
