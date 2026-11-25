import sys
sys.path.insert(0, "D:/vrc无言势")
from vrdub import chains
cases = [("俺这边也可以了よ", "ja", True), ("このダンジョン、先週もう周回したよ", "ja", False),
         ("いいよ", "ja", False), ("こちらも準備できたよ", "ja", False), ("俺们这边也可以了", "ja", True)]
for s, d, want in cases:
    got = chains.lang_leak(s, d)
    print(("OK " if got == want else "BAD"), repr(s), "leak=", got, "kana=%.2f" % chains.kana_ratio(s))
