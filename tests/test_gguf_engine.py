# -*- coding: utf-8 -*-
r"""离线验证 GGUF 三档引擎（qwen17 / qwen06 / fun）的驻留规则与兜底。

不碰显卡、不载真模型、不花钱：transcribe_cpp 整套换成假的，只验逻辑。

  1. 换档：qwen17 → fun，旧的先被卸载再载新的（绝不并存）
  2. 同一档再 acquire：不重载，只累加 runs
  3. 模型文件不存在：返回 (None, 原因)，不装作成功
  4. 解码自重复（OutputRepetition）：transcribe 返回空文本 + 错误类型名
  5. GGUF 没出字 → Transcriber 退回本地 SenseVoice，engine 记 local(兜底)
  6. GGUF 出字 → 原样返回，engine 就是那个档
  7. chatbox 继承 B 链：B 链用 GGUF 档时不再花云端钱重听
  8. cfg 白名单：qwen17/fun 不被 clamp 掉，乱写的值回落默认

用法：python -m tests.test_gguf_engine
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrdub import asr_gguf, cfg as cfgmod, util as utilmod
from vrdub.asr import Transcriber
from vrdub.chains import StreamChain as Chain

HERE = os.path.abspath(__file__)

# ---------------------------------------------------------------- 假的运行库
LOADS = []          # [(模型路径, 后端)]，用来验「换档时旧的被卸载」
STATE = {"text": "テスト、これは字幕です。", "lang": "ja", "boom": None}


class OutputRepetition(Exception):
    """transcribe.cpp 自重复时抛的就是它（这里只需要类名对得上）。"""


class _FakeSession:
    def __init__(self, model):
        self.model = model
        self.runs = 0

    def run(self, x, **kw):
        if STATE["boom"]:
            raise STATE["boom"]
        self.runs += 1
        return types.SimpleNamespace(text=STATE["text"], language=STATE["lang"])


class _FakeModel:
    def __init__(self, path, backend="auto"):
        LOADS.append((os.path.basename(path), backend))
        self.path, self.backend = path, backend
        self.dropped = False

    def session(self, n_threads=0, kv_type="auto"):
        return _FakeSession(self)


sys.modules["transcribe_cpp"] = types.SimpleNamespace(Model=_FakeModel)

LOGS = []


class _U:
    @staticmethod
    def log(msg, lvl="info", who=""):
        LOGS.append(str(msg))

    clean_text = staticmethod(utilmod.clean_text)


asr_gguf.util = _U
asr_gguf._env_setup = lambda: True                      # 不碰真实 DLL 搜索路径
asr_gguf.path_of = lambda eid, cfg=None: HERE           # 模型文件「存在」
asr_gguf.RES = asr_gguf.Resident()                      # 干净的驻留器

ok = True


def chk(name, cond, extra=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + extra if extra else ""))
    ok = ok and bool(cond)


def reset():
    LOADS.clear()
    LOGS.clear()
    STATE.update({"text": "テスト、これは字幕です。", "lang": "ja", "boom": None})
    asr_gguf.RES = asr_gguf.Resident()
    asr_gguf.RES.__dict__  # noqa: B018  （保持对象，别让 GC）

print("[1] 换档：旧模型先卸载，新的再载入")
reset()
s1, e1 = asr_gguf.RES.acquire("qwen17", {})
first = asr_gguf.RES.session
chk("qwen17 载入成功", s1 is not None and not e1, e1)
s2, e2 = asr_gguf.RES.acquire("fun", {})
chk("fun 载入成功", s2 is not None and not e2, e2)
chk("两次载入（没有并存）", len(LOADS) == 2, str(LOADS))
chk("驻留的是 fun", asr_gguf.RES.key == "fun", str(asr_gguf.RES.info()))
chk("旧 session 被丢掉", asr_gguf.RES.session is not first)
chk("日志里有『GGUF 切换：卸载 … → 载入 …』",
    any("GGUF 切换" in m and "→" in m for m in LOGS), str(LOGS))

print("[2] 同一档再 acquire：不重载")
reset()
asr_gguf.RES.acquire("qwen06", {})
asr_gguf.RES.acquire("qwen06", {})
chk("只载入一次", len(LOADS) == 1, str(LOADS))

print("[3] 模型文件不存在 → 明确失败")
reset()
asr_gguf.path_of = lambda eid, cfg=None: os.path.join(HERE, "不存在.gguf")
s3, e3 = asr_gguf.RES.acquire("qwen17", {})
asr_gguf.path_of = lambda eid, cfg=None: HERE
chk("session=None", s3 is None)
chk("原因里写着文件不存在", "不存在" in e3 or "文件" in e3, e3)
chk("没记成驻留", not asr_gguf.RES.info().get("key"))

print("[4] 解码自重复 → 空文本 + 错误类型名")
reset()
STATE["boom"] = OutputRepetition("repeated")
txt, meta = asr_gguf.transcribe("qwen17", [0] * 32000, "ja", {})
chk("文本为空", txt == "")
chk("错误记成 OutputRepetition", meta.get("err") == "OutputRepetition", str(meta))

print("[5] GGUF 没出字 → 退回本地 SenseVoice")
reset()
STATE["boom"] = OutputRepetition("repeated")


class FakeLocal:
    loaded = True
    note = "假 SenseVoice"

    def ready(self):
        return True

    def transcribe(self, pcm, lang="auto"):
        return "本地兜回来的话"


t = Transcriber.__new__(Transcriber)
t.cfg = {"asr_engine_a": "qwen17", "asr_gguf_hint": "auto"}
t.log = lambda m, lvl="info", who="": LOGS.append(str(m))
t.local = FakeLocal()
t.cloud = types.SimpleNamespace(transcribe=lambda pcm, timeout=12.0: {"text": "云", "ms": 1.0})
txt, eng, _ms = t.transcribe_fast([0] * 32000, "ja", e="qwen17")
chk("文本=本地兜回", txt == "本地兜回来的话", txt)
chk("engine=local(兜底)", eng == "local(兜底)", eng)
chk("有 warn 说明", any("退回本地 SenseVoice" in m for m in LOGS), str(LOGS[-3:]))

print("[6] GGUF 出字 → 原样返回，engine 就是那个档")
reset()
txt, eng, ms = t.transcribe_fast([0] * 32000, "ja", e="qwen17")
chk("文本=模型输出", txt.startswith("テスト"), txt)
chk("engine=qwen17", eng == "qwen17", eng)
chk("ms 有数", ms >= 0)

print("[7] chatbox 继承 B 链：GGUF 档用那个档重听，不碰云端")
import time                                    # noqa: E402


class SpyASR:
    def __init__(self, txt="これは qwen が聞き直した文です"):
        self.txt = txt
        self.calls = []

    def transcribe_fast(self, audio, lang="auto", e=None):
        self.calls.append(e)
        return self.txt, str(e), 120.0

    def transcribe_cloud(self, *a, **k):
        raise AssertionError("GGUF 档不该再调云端识别")


class FakeMT:
    def __init__(self):
        self.got = []

    def translate(self, text, src=None, dst=None, card=None, timeout=8.0, custom=""):
        self.got.append(text)
        return {"text": "訳「" + text[:10] + "」"}


def mkcb(eng_b, asr, speak=True):
    c = Chain.__new__(Chain)
    c.name, c.src_key, c.speak = "B", "loop", speak
    c.cfg = {"asr_engine_b": eng_b, "chatbox_cloud": True, "chatbox_budget": 5.0,
             "chatbox_on": True, "out_on": True, "card": "natural", "echo_guard": False}
    c.asr = asr
    c.mt = FakeMT()
    c._logs = []
    c.log = lambda m, lvl="info", who="": c._logs.append(str(m))
    c._persona = lambda: ""
    c.sent = []
    c.osc = types.SimpleNamespace(send=lambda s: c.sent.append(s))
    c.bus = types.SimpleNamespace(upd=lambda *a, **k: None)
    return c


def drain(c, n=1, timeout=4.0):
    t0 = time.time()
    while time.time() - t0 < timeout and len(c.sent) < n:
        time.sleep(0.02)
    time.sleep(0.1)


c = mkcb("qwen17", SpyASR())
st = {"src_lang": "ja", "dst_lang": "zh"}
c._chatbox_async({"id": 7, "state": "mt", "dst": "B 链已有的译文"},
                 st, b"" * 100, "ja", "これは字幕です")
drain(c)
chk("用 qwen17 重听了一次", c.asr.calls == ["qwen17"], str(c.asr.calls))
chk("发了 1 条", len(c.sent) == 1, str(c.sent))
chk("内容来自 qwen17 的文本", bool(c.sent) and "qwen" in c.sent[0], str(c.sent))
chk("cb_done", st.get("cb_done") is True)

print("[7b] chatbox：B 链是纯本地 → 不重听，直接发已有译文")
c = mkcb("local", SpyASR())
st = {"src_lang": "ja", "dst_lang": "zh"}
c._chatbox_async({"id": 8, "state": "mt", "dst": "B 链已有的译文"},
                 st, b"" * 100, "ja", "これは字幕です")
drain(c)
chk("没重听", not c.asr.calls, str(c.asr.calls))
chk("内容是已有译文", bool(c.sent) and c.sent[0] == "B 链已有的译文", str(c.sent))

print("[8] cfg 白名单")
base = dict(cfgmod.DEFAULTS)
base.update({"asr_engine_a": "qwen17", "asr_engine_b": "fun",
             "asr_gguf_backend": "vulkan", "asr_gguf_hint": "off",
             "asr_gguf_dir": "D:\\models"})
cl = cfgmod.clamp_cfg(base)
chk("A=qwen17 保留", cl.get("asr_engine_a") == "qwen17", str(cl.get("asr_engine_a")))
chk("B=fun 保留", cl.get("asr_engine_b") == "fun", str(cl.get("asr_engine_b")))
chk("backend=vulkan 保留", cl.get("asr_gguf_backend") == "vulkan")
bad = dict(cfgmod.DEFAULTS)
bad.update({"asr_engine_a": "gpt9", "asr_gguf_backend": "tensorcore"})
cl2 = cfgmod.clamp_cfg(bad)
chk("乱写的引擎回落默认", cl2.get("asr_engine_a") == cfgmod.DEFAULTS["asr_engine_a"],
    str(cl2.get("asr_engine_a")))
chk("乱写的后端回落 cuda", cl2.get("asr_gguf_backend") == "cuda",
    str(cl2.get("asr_gguf_backend")))

print("\nRESULT: " + ("ALL PASS" if ok else "HAS FAIL"))
sys.exit(0 if ok else 1)
