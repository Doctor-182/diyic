import sys
sys.path.insert(0, "D:/vrc无言势")
from vrdub import cfg as cm, mt, net
c = cm.Cfg(); n = net.Net("ali", c.host, c.key, slots=4)
t = mt.Translator(c, n)
cases = ["下次把浮窗放到屏幕上方。", "这个模型的声音我已经换成你的音色了。",
         "收到语音和字幕都正常", "我的网络有点卡，可能会断一下。"]
for s in cases:
    out = "".join(p for p, *_ in t.stream_clauses(s, src="zh", dst="ja", card="natural", timeout=15))
    print(s, "=>", out)
