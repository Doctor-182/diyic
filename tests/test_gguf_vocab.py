# -*- coding: utf-8 -*-
r"""离线验证专名表（vocabulary）这条支路。

不碰显卡、不载模型：RES.acquire 与 Session.run 全换成假的。

  1. 没配专名表 → 不带 vocabulary 参数
  2. 配了 2 条 → 带上，且 meta["vocab"]=2
  3. 模型不认 vocabulary（抛异常）→ 自动退回不带词表的那次，仍然出字
  4. fun 档不传（它不解 vocabulary）
  5. 词表超过 8 条 / 单条 >24 字 → clamp 截断
  6. cfg 传字符串（前端直接存逗号串）→ 也能解析

用法：python -m tests.test_gguf_vocab
"""
import os
import sys
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrdub import asr_gguf, cfg as cfgmod

CALLS = []
STATE = {"reject_vocab": False}


class FakeSess:
    def run(self, x, **kw):
        CALLS.append(dict(kw))
        if STATE["reject_vocab"] and "vocabulary" in kw:
            raise RuntimeError("vocabulary not supported")
        return types.SimpleNamespace(text="テストです。", language="ja")


class FakeRes:
    _lock = __import__("threading").Lock()
    n_runs = 0

    def acquire(self, eid, cfg):
        return FakeSess(), ""


def run(name, cfg, eid="qwen17", lang="ja", expect_kw=None, expect_text="テストです。"):
    CALLS.clear()
    old_res = asr_gguf.RES
    asr_gguf.RES = FakeRes()
    try:
        pcm = np.zeros(16000, dtype=np.int16)
        txt, meta = asr_gguf.transcribe(eid, pcm, lang, cfg)
    finally:
        asr_gguf.RES = old_res
    ok = txt == expect_text and (expect_kw is None or expect_kw in CALLS[0])
    print("  %-38s calls=%d %s" % (name, len(CALLS), "PASS" if ok else "FAIL"))
    if not ok:
        print("     calls=%r text=%r" % (CALLS, txt))
    return ok


res = []
print("== 专名表")
res.append(run("1 没有词表 → 不带 vocabulary", {}, expect_kw=None))
ok1 = "vocabulary" not in CALLS[0]
print("  %-38s %s" % ("1b 确实没这个参数", "PASS" if ok1 else "FAIL"))
res.append(ok1)

c = {"asr_gguf_vocab": ["ゴーストブロー", "グラセフ"]}
res.append(run("2 两条 → 带上", c, expect_kw="vocabulary"))
ok2 = CALLS[0]["vocabulary"] == ["ゴーストブロー", "グラセフ"]
print("  %-38s %s" % ("2b 内容一致", "PASS" if ok2 else "FAIL"))
res.append(ok2)

STATE["reject_vocab"] = True
res.append(run("3 模型不认词表 → 退回不带", c, expect_text="テストです。"))
ok3 = len(CALLS) == 2 and "vocabulary" in CALLS[0] and "vocabulary" not in CALLS[1]
print("  %-38s %s" % ("3b 第二次没带词表", "PASS" if ok3 else "FAIL"))
res.append(ok3)
STATE["reject_vocab"] = False

res.append(run("4 fun 档不传词表", c, eid="fun", expect_kw=None))
ok4 = "vocabulary" not in CALLS[0]
print("  %-38s %s" % ("4b fun 确实没带", "PASS" if ok4 else "FAIL"))
res.append(ok4)

d = dict(cfgmod.DEFAULTS)
d["asr_gguf_vocab"] = ["x" * 40] + ["v%d" % i for i in range(12)]
d = cfgmod.clamp_cfg(d)
ok5 = len(d["asr_gguf_vocab"]) == 8 and len(d["asr_gguf_vocab"][0]) == 24
print("  %-38s %s  %r" % ("5 超长/超条数 clamp", "PASS" if ok5 else "FAIL", d["asr_gguf_vocab"][:3]))
res.append(ok5)

e = dict(cfgmod.DEFAULTS)
e["asr_gguf_vocab"] = "ゴーストブロー, グラセフ"
e = cfgmod.clamp_cfg(e)
ok6 = e["asr_gguf_vocab"] == ["ゴーストブロー", "グラセフ"]
print("  %-38s %s" % ("6 逗号串 → 列表", "PASS" if ok6 else "FAIL"))
res.append(ok6)

# 7 列表类型：字符串表不能被 int() 吞掉（曾经专名表/音色表保存后全空）
ok7 = (cfgmod._coerce("asr_gguf_vocab", ["a", "b"], []) == ["a", "b"]
       and cfgmod._coerce("ocr_region", ["5", "6", "7", "8"], [0, 0, 0, 0]) == [5, 6, 7, 8])
print("  %-38s %s" % ("7 列表强转分类型", "PASS" if ok7 else "FAIL"))
res.append(ok7)

v = dict(cfgmod.DEFAULTS)
v["voices_custom"] = [{"id": "qwen-audio-3.0-tts-plus-t9-abc", "name": "我的", "model": "m", "ts": 1}]
v = cfgmod.clamp_cfg(v)
ok8 = len(v["voices_custom"]) == 1 and v["voices_custom"][0]["id"].endswith("-abc")
print("  %-38s %s" % ("8 音色表 dict 不被吞", "PASS" if ok8 else "FAIL"))
res.append(ok8)

print("RESULT:", "ALL PASS" if all(res) else "FAILED %d/%d" % (res.count(False), len(res)))
sys.exit(0 if all(res) else 1)
