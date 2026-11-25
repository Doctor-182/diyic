import sys, traceback, time
sys.path.insert(0,"D:/vrc无言势")
import numpy as np
from vrdub import audio, cfg as cm
c=cm.Cfg()
print("devs:", {k:[ (d["i"],d["name"][:28],d["ch"]) for d in v] for k,v in audio.list_devices().items()})
def trycap(idx):
    try:
        cp=audio.Capture(idx, lambda b,r: None, log=print, kind="t")
        cp.start(); time.sleep(0.4); cp.stop(); print("  ok", idx)
        return True
    except Exception as e:
        print("  FAIL", idx, repr(e)); traceback.print_exc()
        return False
for i in (23,22,24,25,27):
    trycap(i)
