import sys, time, traceback
sys.path.insert(0,"D:/vrc无言势")
from vrdub import audio
def t(label, fn):
    try:
        fn(); print("  ok", label)
    except Exception as e:
        print("  FAIL", label, repr(e))

a = audio.Capture(23, lambda b,r: None, kind="a"); t("open23", a.start); time.sleep(0.3)
b = audio.Capture(25, lambda b,r: None, kind="b"); t("open25(concurrent)", b.start); time.sleep(0.3)
c = audio.Capture(24, lambda b,r: None, kind="c"); t("open24(concurrent)", c.start); time.sleep(0.3)
print("--- 关掉重开 24 ---")
t("close23", a.stop)
d = audio.Capture(24, lambda b,r: None, kind="d"); t("open24 after close23", d.start)
print("--- 重建 PyAudio 实例 ---")
for x in (b,c,d):
    try: x.stop()
    except Exception: pass
audio.reset_pa()
e = audio.Capture(24, lambda b,r: None, kind="e"); t("open24 after reset_pa", e.start); time.sleep(0.3); e.stop()
