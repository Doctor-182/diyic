import sys
sys.path.insert(0,"D:/vrc无言势")
from vrdub import cfg as cm, mt, net
c=cm.Cfg(); n=net.Net("ali",c.host,c.key,slots=4)
t=mt.Translator(c,n)
src="这个模型的声音听起来比我预想的自然多了。"
for i in range(3):
    out="".join(p for p,f,e,r in t.stream_clauses(src,src="zh",dst="ja",card="natural",timeout=12))
    print(i,out)
