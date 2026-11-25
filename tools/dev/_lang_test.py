import sys; sys.path.insert(0,'.')
from vrdub import util
for s,d in [("进行一些ERP","zh"),("It is getting chaotic, I will turn it off.","zh"),("ok ok","zh"),("我被你秒掉了","zh"),("这个PC配置不错","zh"),("これは日本語です","zh"),("今日はいい天気だね","ja"),("今天天气不错啊","ja")]:
    print(repr(s), d, util.lang_ok(s,d))