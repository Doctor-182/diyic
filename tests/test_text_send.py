# -*- coding: utf-8 -*-
r"""真值表：悬浮球里「打一句中文」这条路（api_text_send）。

四个开关组合必须真的生效（tts/chatbox 各开各关），翻译失败绝不拿中文顶上去发聊天框，
dst=zh 时不该调翻译。全部用假 MT/假合成器，不联网不花钱。

跑法：python -m tests.test_text_send
"""
import sys

from vrdub.app import App


class FakeCfg(dict):
    def all(self):
        return dict(self)


class FakeBus:
    def __init__(self):
        self.segs = []

    def new_seg(self, chain="A", **kw):
        seg = {"id": "s%d" % (len(self.segs) + 1), "chain": chain}
        seg.update(kw)
        self.segs.append(seg)
        return seg

    def upd(self, seg, **kw):
        seg.update(kw)
        return seg


class FakeMT:
    def __init__(self, out="よろしくね", fail=False):
        self.calls = []
        self.out = out
        self.fail = fail

    def translate(self, text, src="auto", dst="zh", card=None, timeout=10.0, custom=None):
        self.calls.append({"text": text, "src": src, "dst": dst, "custom": custom})
        if self.fail:
            raise RuntimeError("mt boom")
        return {"text": self.out, "ms": 12.0, "echo": ""}


class FakeSynth:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def synth(self, text, voice, lang="ja", rate=None, tags=None, timeout=30):
        self.calls.append({"text": text, "voice": voice, "lang": lang, "rate": rate})
        if self.fail:
            raise RuntimeError("tts boom")
        return b"\x00\x01" * 24000, 88.0, 200.0


class FakePlayer:
    def __init__(self):
        self.pushed = []

    def push_tts_pcm(self, pcm, sr=24000):
        self.pushed.append((len(pcm), sr))
        return True


class FakeOsc:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def build(cfg=None, mt=None, synth=None, osc=None, player=None):
    a = App.__new__(App)
    a.cfg = FakeCfg({"dst_lang": "ja", "voice": "v-t3", "chatbox_on": True,
                     "speed": 1.0, "card": "natural", "persona_custom": "",
                     "out_dev": 19, "gain": 1.0})
    a.cfg.update(cfg or {})
    a.bus = FakeBus()
    a.mt = mt or FakeMT()
    a.synth = synth or FakeSynth()
    a.osc = osc if osc is not None else FakeOsc()
    a.player = player if player is not None else FakePlayer()
    return a


BAD = []


def ok(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        BAD.append(msg)


def main():
    print("[1] dst=zh：不翻译、不出声、不发框")
    a = build()
    r = a.api_text_send({"text": "  你好  ", "lang": "zh", "tts": False, "chatbox": False})
    ok(r.get("ok") and r["dst"] == "你好", "原文压空白后原样返回")
    ok(not a.mt.calls, "dst=zh 不调翻译")
    ok(not a.synth.calls and not a.osc.sent, "不出声也不发框")

    print("[2] 三个开关组合")
    for tts, cb, want_mt, want_tts, want_cb in (
            (False, False, True, False, False),
            (True, False, True, True, False),
            (False, True, True, False, True),
            (True, True, True, True, True)):
        a = build()
        r = a.api_text_send({"text": "你好", "lang": "ja", "tts": tts, "chatbox": cb})
        ok(r["ok"] and bool(a.mt.calls) == want_mt, "翻译调用 tts=%s cb=%s" % (tts, cb))
        ok(bool(a.synth.calls) == want_tts, "合成调用 tts=%s cb=%s" % (tts, cb))
        ok(bool(a.osc.sent) == want_cb, "聊天框发送 tts=%s cb=%s" % (tts, cb))
        ok(r["spoke"] == want_tts and r["sent"] == want_cb, "返回值与动作一致")
        ok(r["tts_ms"] > 0 if want_tts else r["tts_ms"] == 0.0, "首包耗时只在出声时出现")

    print("[3] 译文进了聊天框、音频进了播放器")
    a = build(mt=FakeMT(out="よろしく"))
    r = a.api_text_send({"text": "请多关照", "lang": "ja", "tts": True, "chatbox": True})
    ok(a.osc.sent == ["よろしく"], "聊天框收到的是译文而不是中文")
    ok(a.player.pushed and a.player.pushed[0][1] == 24000, "PCM 走播放器而不是另起一条")
    ok(r["audio_s"] > 0, "报了音频时长")

    print("[4] 翻译失败：不发聊天框、段标 drop")
    a = build(mt=FakeMT(fail=True))
    r = a.api_text_send({"text": "你好", "lang": "ja", "tts": True, "chatbox": True})
    ok(r["ok"] is False and "翻译失败" in r["err"], "返回失败原因")
    ok(not a.osc.sent, "绝不拿中文顶上去发框")
    ok(a.bus.segs[-1]["state"] == "drop", "段被标 drop")
    ok(not a.synth.calls, "翻译失败不浪费合成")

    print("[5] 聊天框关着：只出声，并说明原因")
    a = build(cfg={"chatbox_on": False})
    r = a.api_text_send({"text": "你好", "lang": "ja", "tts": True, "chatbox": True})
    ok(r["ok"] and r["spoke"] and not r["sent"], "出声但没发框")
    ok("聊天框" in r["cb_note"], "说明了为什么没发")

    print("[6] 边界：空文本 / 非法语种 / 合成炸了")
    a = build()
    ok(a.api_text_send({"text": "   "})["ok"] is False, "空文本直接拒")
    r = a.api_text_send({"text": "你好", "lang": "xx", "tts": False, "chatbox": False})
    ok(r["lang"] == "ja", "非法语种回落到 A 链目标语言")
    a = build(synth=FakeSynth(fail=True))
    r = a.api_text_send({"text": "你好", "lang": "ja", "tts": True, "chatbox": True})
    ok(r["ok"] is False and "合成失败" in r["err"], "合成失败报出来")
    ok(r["sent"] is True, "合成失败不影响发文字框")

    print()
    if BAD:
        print("RESULT: %d FAIL" % len(BAD))
        for m in BAD:
            print(" - " + m)
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
