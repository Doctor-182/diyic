# -*- coding: utf-8 -*-
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vrdub import audio as A
devs = A.list_devices()
print("keys:", list(devs.keys()))
for k in ("input", "output", "loop"):
    print("==", k)
    for d in devs.get(k, []):
        print("  #%-3d %s" % (d["i"], d["name"]))
