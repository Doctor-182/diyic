# -*- coding: utf-8 -*-
r"""离线验证 GGUF 档的语种漂移守卫与 /sil 标记清理。

背景：20261127 01:00–02:00 的 KTV 实录（562 段，其中唱歌/BGM 178 段）四模型对照里，
qwen17 有 6/10 段把日语写成英文（"I finally found you, and I'm here now."）或罗马字，
0.6B 会漂韩文，Fun 会漂英文 + 瞎补汉字，还会把 "/sil" 当词吐出来。
这份真值表只验逻辑：不载模型、不碰显卡、不花钱。

  1. 漂移（qwen17 吐英文）→ 退回 SenseVoice，engine 记 local(漂移)
  2. 正常日语 → 原样返回，不去白跑一遍本地
  3. 本地也漂 → 保留原结果，不变成空字幕
  4. 漂成韩文 → 同样回退
  5. lang=auto → 不判语种，原样返回
  6. 中文链漂成英文 → 回退（A 链用户自己说话的场景）
  7. /sil 之类的标记被清掉，不当作正文往下传

用法：python -m tests.test_drift_guard
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrdub import asr_gguf, util as utilmod
from vrdub.asr import Transcriber

LOGS = []
ok = True


def chk(name, cond, extra=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + extra if extra else ""))
    ok = ok and bool(cond)


# ---------------------------------------------------------------- 假的 GGUF 档
SAY = {"text": "", "err": "", "left": 0}


def fake_gguf(eid, pcm16, lang="auto", cfg=None, hint="auto"):
    SAY["left"] -= 1
    return SAY["text"], {"engine": eid, "ms": 137.0, "err": SAY["err"], "lang": lang}


asr_gguf.transcribe = fake_gguf


class FakeLocal:
    def __init__(self, text=""):
        self.text = text
        self.calls = 0

    def ready(self):
        return True

    def transcribe(self, pcm, lang="auto"):
        self.calls += 1
        return self.text


def mk(local_text=""):
    t = Transcriber.__new__(Transcriber)
    t.cfg = {"asr_gguf_hint": "auto"}
    t.log = lambda m, lvl="info", who="": LOGS.append(str(m))
    t.local = FakeLocal(local_text)
    t.cloud = types.SimpleNamespace(transcribe=lambda pcm, timeout=12.0: {"text": "云", "ms": 1.0})
    return t


print("[1] qwen17 吐英文（唱段漂移）→ 退回 SenseVoice")
LOGS.clear()
SAY.update({"text": "I finally found you, and I'm here now.", "err": "", "left": 9})
t = mk("みつけた、ここにいたんだ")
txt, eng, ms = t.transcribe_fast([0] * 32000, "ja", e="qwen17")
chk("文本=本地重听结果", txt == "みつけた、ここにいたんだ", txt)
chk("engine=local(漂移)", eng == "local(漂移)", eng)
chk("本地只被叫了一次", t.local.calls == 1, str(t.local.calls))
chk("日志说明是语种不对", any("输出语种不对" in m for m in LOGS), str(LOGS[-2:]))
chk("日志带假名/拉丁比例", any("拉丁" in m or "假名" in m for m in LOGS), str(LOGS[-2:]))
chk("ms 有数", ms >= 0, str(ms))

print("[2] 正常日语 → 原样返回，不白跑本地")
LOGS.clear()
SAY.update({"text": "ありえない。ねえ、これねプヨプヨするね。", "err": "", "left": 9})
t = mk("不该被调用")
txt, eng, _ms = t.transcribe_fast([0] * 32000, "ja", e="qwen17")
chk("文本=模型输出", txt.startswith("ありえない"), txt)
chk("engine=qwen17", eng == "qwen17", eng)
chk("本地没被调用", t.local.calls == 0, str(t.local.calls))
chk("没有 warn", not any("语种不对" in m for m in LOGS), str(LOGS))

print("[3] 本地也漂 → 保留原结果（不要变成空字幕）")
LOGS.clear()
SAY.update({"text": "The world will it ever reach?", "err": "", "left": 9})
t = mk("also english here")
txt, eng, _ms = t.transcribe_fast([0] * 32000, "ja", e="qwen17")
chk("保留模型原文", txt == "The world will it ever reach?", txt)
chk("engine 仍是 qwen17", eng == "qwen17", eng)
chk("本地试过一次", t.local.calls == 1, str(t.local.calls))

print("[4] 漂成韩文 → 同样回退")
LOGS.clear()
SAY.update({"text": "나들이 소파 태우시나까.", "err": "", "left": 9})
t = mk("そふぁにすわった")
txt, eng, _ms = t.transcribe_fast([0] * 32000, "ja", e="qwen06")
chk("文本=本地结果", txt == "そふぁにすわった", txt)
chk("engine=local(漂移)", eng == "local(漂移)", eng)
chk("原因里提到韩文", any("韩文" in m for m in LOGS), str(LOGS[-1:]))

print("[5] lang=auto → 不判语种")
LOGS.clear()
SAY.update({"text": "Sweet memories.", "err": "", "left": 9})
t = mk("不该被调用")
txt, eng, _ms = t.transcribe_fast([0] * 32000, "auto", e="qwen17")
chk("原样返回", txt == "Sweet memories.", txt)
chk("engine=qwen17", eng == "qwen17", eng)
chk("本地没被调用", t.local.calls == 0, str(t.local.calls))

print("[6] 中文链漂成英文 → 回退")
LOGS.clear()
SAY.update({"text": "I'm going to eat first.", "err": "", "left": 9})
t = mk("我操！我前期的工作呢？")
txt, eng, _ms = t.transcribe_fast([0] * 32000, "zh", e="qwen17")
chk("文本=本地中文", txt == "我操！我前期的工作呢？", txt)
chk("engine=local(漂移)", eng == "local(漂移)", eng)

print("[7] /sil 之类标记被清掉")
chk("纯标记清成空", asr_gguf.tidy("/sil") == "", repr(asr_gguf.tidy("/sil")))
chk("<sil> 也清", asr_gguf.tidy("<sil>") == "", repr(asr_gguf.tidy("<sil>")))
got = asr_gguf.tidy("あ/sil 嘟嘟嘟嘟")
chk("标记没了、正文留着", "sil" not in got.lower() and "嘟嘟嘟嘟" in got, repr(got))
chk("真词不误伤（silly / すし）",
    asr_gguf.tidy("silly") == "silly" and asr_gguf.tidy("すし") == "すし",
    repr((asr_gguf.tidy("silly"), asr_gguf.tidy("すし"))))

print()
print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
sys.exit(0 if ok else 1)
