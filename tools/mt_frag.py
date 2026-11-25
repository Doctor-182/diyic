import sys
sys.path.insert(0,"D:/vrc无言势")
from vrdub import cfg as cm, mt, net
c=cm.Cfg(); n=net.Net("ali",c.host,c.key,slots=4)
t=mt.Translator(c,n)
cases=[("変えてみたんだ","そっちは聞こえてるマイクの位置を変えてみたんだ","ja","zh"),
       ("通訳みたいだね","遅れが少なくなったら本当に同時通訳みたい","ja","zh"),
       ("自然多了","这个模型的声音听起来比我预想的","zh","ja"),
       ("もう少し遅れが少なくなったら","もし言葉がもっと速くなったら本当に同時","ja","zh")]
for txt,ctx,sl,dst in cases:
    out="".join(p for p,f,e,r in t.stream_clauses(txt,src=sl,dst=dst,card="natural",timeout=12,ctx=ctx))
    print(f"{sl}->{dst} [{txt}] + ctx -> {out}")
