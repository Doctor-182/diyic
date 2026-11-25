import sys
sys.path.insert(0,"D:/vrc无言势")
from vrdub import cfg as cm, mt, net
c=cm.Cfg(); n=net.Net("ali",c.host,c.key,slots=4)
t=mt.Translator(c,n)
cases=[("你那边能听清吗？我刚把麦克风换了个位置。","zh","ja"),
       ("这个模型的声音听起来比我预想的自然多了，延迟也还可以。","zh","ja"),
       ("明日の夜、九時くらいに集合でいい？ちょっと遅れるかもしれないけど。","ja","zh"),
       ("我今天可能要晚点到，你们先开吧，不用等我。","zh","ja")]
for src,sl,dst in cases:
    out=[];  t0=None
    import time
    for piece,first,echo,rate in t.stream_clauses(src,src=sl,dst=dst,card="natural",timeout=12):
        out.append(piece)
    print(f"[{sl}->{dst}] {src}\n  => {''.join(out)}\n")
