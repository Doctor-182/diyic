import time,sys
sys.path.insert(0,"D:/vrc无言势")
from vrdub import cfg as cm, tts, net
c=cm.Cfg()
t=tts.Synth(c, net.Net("ali", c.host, c.key, slots=4))
for i,txt in enumerate(["うん、わかった。","今日ちょっと遅いじゃん。","今日はちょっと遅れるかも、先に始めててね。"]*2):
    t0=time.time(); first=None; n=0
    for pcm in t.stream(txt, c.get("voice",""), "ja"):
        if first is None: first=(time.time()-t0)*1000
        n+=len(pcm)
    print(f"{i} len={len(txt)} first={first:.0f}ms total={(time.time()-t0)*1000:.0f}ms audio={n/2/24000:.2f}s")