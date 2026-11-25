import sys, time
sys.path.insert(0, "D:/vrc无言势")
from vrdub import cfg as cm, mt, net
c = cm.Cfg(); n = net.Net("ali", c.host, c.key, slots=4)
t = mt.Translator(c, n)
ctxs = [
    ("无上文", ""),
    ("长上文", "今天有点迟到了，路上堵车。今天的回环自测说的稍微长一点点。你那边能听清吗？我刚把麦克风换了个位置。"),
    ("短上文", "你那边能听清吗？我刚把麦克风换了个位置。"),
]
text = "这个模型的声音我已经换成你的音色了。"
for name, ctx in ctxs:
    for card in ("natural", "polite"):
        out = "".join(piece for piece, *_ in t.stream_clauses(text, src="zh", dst="ja", card=card, ctx=ctx, timeout=15))
        print("[%s|%s] %s" % (name, card, out))
