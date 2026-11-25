import sys,time
sys.path.insert(0,"D:/vrc无言势")
from vrdub import cfg as cm, net, tts
c=cm.Cfg(); n=net.Net("ali",c.host,c.key,slots=4); s=tts.Synth(c,n)
txt="今日は少し遅れちゃった、ごめんね。先に始めてて。"
for r in (None,0.8,1.4):
    pcm,f,t=s.synth(txt,c.get("voice"),"ja",rate=r)
    print("rate",r,"first %.0f"%f,"audio %.2fs"%(len(pcm)/2/24000))
