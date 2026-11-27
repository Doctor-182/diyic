# -*- coding: utf-8 -*-
r"""离线验证「语种探测 → 换提示重听」这条路由。

不碰显卡、不载模型、不花钱：asr_gguf.transcribe 与 SenseVoice 全换成假的。

  1. 探测不出语种（空）→ 只跑一次，用链上的语种
  2. 探测出 zh、当前 ja、重听出中文 → 用中文结果，engine 不变
  3. 探测出 zh 但重听没出合语种的字 → 保留原结果（绝不变空字幕）
  4. 探测出 en、模式 zh → 不切（en 有误报，默认只信 zh）
  5. 探测出 en、模式 any → 切 en
  6. 模式 off → 根本不起探测线程
  7. 链上语种是 auto → 不探（没有「正确答案」可比）
  8. 探测抛异常 → 只记日志，照常返回原结果
  9. asr_gguf_lid 的 clamp：乱写的值回落 zh

用法：python -m tests.test_gguf_lid
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrdub import asr as asrmod, cfg as cfgmod, util as utilmod
from vrdub.asr import Transcriber

CALLS = []
STATE = {"lid": "", "boom": None, "texts": {}}


class FakeLocal:
    def __init__(self, lid=""):
        self._lid = lid
        self.n = 0

    def ready(self):
        return True

    def lid(self, pcm16):
        self.n += 1
        if STATE["boom"]:
            raise STATE["boom"]
        return self._lid

    def transcribe(self, pcm16, lang="auto"):
        return STATE["texts"].get("local", "ローカルの字")


def fake_transcribe(eid, pcm16, lang="auto", cfg=None, hint="auto"):
    CALLS.append((eid, lang, hint))
    txt = STATE["texts"].get(lang, "テストです。")
    return txt, {"ms": 100.0, "err": "", "lang": lang, "hint": lang}


def mk(cfg=None, lid=""):
    t = Transcriber.__new__(Transcriber)
    t.cfg = dict(cfgmod.DEFAULTS if cfg is None else cfg)
    t.local = FakeLocal(lid)
    t.cloud = None
    t.log = lambda *a, **k: None
    return t


def run(name, cfg, lid, texts, want_lang, expect_calls, expect_text, mode=None):
    global CALLS
    CALLS = []
    STATE.update({"lid": lid, "boom": None, "texts": texts})
    c = dict(cfgmod.DEFAULTS)
    c.update(cfg or {})
    if mode:
        c["asr_gguf_lid"] = mode
    t = mk(c, lid)
    txt, eng, ms = t.transcribe_fast(b"\x00\x00" * 16000, want_lang, e="qwen17")
    ok = (len(CALLS) == expect_calls and txt == expect_text)
    print("  %-42s calls=%d %s  %r" % (name, len(CALLS), "PASS" if ok else "FAIL", txt[:36]))
    return ok, CALLS


old = asrmod.asr_gguf.transcribe
asrmod.asr_gguf.transcribe = fake_transcribe
try:
    print("== 语种探测路由")
    res = []
    res.append(run("1 探测空 → 只跑一次", {}, "", {}, "ja", 1, "テストです。")[0])
    res.append(run("2 探测 zh → 换提示重听", {}, "zh",
                   {"ja": "テストです。", "zh": "这里有点吵，等一下。"}, "ja", 2,
                   "这里有点吵，等一下。")[0])
    res.append(run("3 探测 zh 但重听没中文 → 保留", {}, "zh",
                   {"ja": "テストです。", "zh": "test test"}, "ja", 2, "テストです。")[0])
    res.append(run("4 探测 en、模式 zh → 不切", {"asr_gguf_lid": "zh"}, "en", {}, "ja", 1, "テストです。")[0])
    res.append(run("5 探测 en、模式 any → 切 en", {"asr_gguf_lid": "any"}, "en",
                   {"ja": "テストです。", "en": "this is a test"}, "ja", 2, "this is a test")[0])
    res.append(run("6 模式 off → 不起探测", {"asr_gguf_lid": "off"}, "zh", {}, "ja", 1, "テストです。")[0])
    res.append(run("7 链上语种 auto → 不探", {}, "zh", {}, "auto", 1, "テストです。")[0])

    # 8 探测抛异常：线程里吞掉，只记日志
    CALLS = []
    STATE.update({"lid": "zh", "boom": RuntimeError("onnx 炸了"), "texts": {}})
    t = mk(dict(cfgmod.DEFAULTS), "zh")
    txt, eng, ms = t.transcribe_fast(b"\x00\x00" * 16000, "ja", e="qwen17")
    ok8 = (txt == "テストです。" and len(CALLS) == 1)
    print("  %-42s calls=%d %s  %r" % ("8 探测抛异常 → 照常返回", len(CALLS), "PASS" if ok8 else "FAIL", txt[:30]))
    res.append(ok8)
    STATE["boom"] = None

    # 9 clamp
    c = dict(cfgmod.DEFAULTS)
    c["asr_gguf_lid"] = "wrong"
    c = cfgmod.clamp_cfg(c)
    ok9 = c["asr_gguf_lid"] == "zh"
    print("  %-42s %s" % ("9 clamp 乱写值 → zh", "PASS" if ok9 else "FAIL"))
    res.append(ok9)

    # 10 钉 ja 但结果是纯拉丁 → 按 en 重听
    res.append(run("10 钉 ja 却是纯拉丁、模式 any → 换 en", {"asr_gguf_lid": "any"}, "",
                   {"ja": "Yes, I am going now.", "en": "Yes, I am going now."},
                   "ja", 2, "Yes, I am going now.")[0])
    # 11 钉 ja、结果是纯中文（中文被漏探）→ 不当英语，保留原文
    #    （本地探一遍也没合 ja 语种的字 → 漂移闸放行原结果）
    res.append(run("11 钉 ja 结果是中文 → 不换 en", {}, "",
                   {"ja": "这里有人吗？", "local": "这里有人吗？"}, "ja", 1, "这里有人吗？")[0])
finally:
    asrmod.asr_gguf.transcribe = old

print("RESULT:", "ALL PASS" if all(res) else "FAILED %d/%d" % (res.count(False), len(res)))
sys.exit(0 if all(res) else 1)
