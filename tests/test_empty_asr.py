# -*- coding: utf-8 -*-
r"""空段真值表（离线，不碰麦克风、不花钱）：识别没出字时不能再假装 done。

线上现场（2026-11-25 14:35）：B 链连续 12 段 state=done，但 src/dst/asr_ms 全是空的，
日志里只有 "[asr] 云端识别跳过非语音片段：ali HTTP 400: {}"。
原因：_on_final 里 full 为空时没有任何分支，直接走到段落收口，于是空段落被标成 done。

现在两条规矩：
  1. 云端（engine=cloud）没出字 → 先用本地兜一次，救得回来就救；
  2. 本地也没有 → 段落标 drop 并写清原因（不是静默 done）。

用法：call scripts\_env.bat 后跑  %PY% -m tests.test_empty_asr
"""
import queue
import sys

sys.path.insert(0, ".")
from vrdub.chains import StreamChain as Chain     # noqa: E402


class FakeASR:
    """cloud_text / local_text 为空串就模拟「没出字」。"""

    def __init__(self, cloud_text="", local_text="", cloud_ms=900.0, local_ms=120.0):
        self.cloud_text, self.local_text = cloud_text, local_text
        self.cloud_ms, self.local_ms = cloud_ms, local_ms
        self.calls = []

    def transcribe_fast(self, pcm, lang="auto", e=None):
        self.calls.append(e)
        if e in ("local",):
            return self.local_text, "local", self.local_ms
        return self.cloud_text, "cloud", self.cloud_ms


class FakeBus:
    def __init__(self):
        self.upds = []

    def upd(self, seg, **kw):
        self.upds.append((seg, kw))

    def recent(self, n=50):
        return []

    def new_seg(self, *a, **k):
        return {"id": "A-1"}


def mk(cloud_text="", local_text="", **cfg):
    c = Chain.__new__(Chain)
    c.name = "A"
    c.src_key, c.speak = "mic", True
    base = {"out_on": True, "in_on": True, "asr_on": True, "mt_on": True,
            "mode": "trans", "card": "natural", "voice": "", "speak_q_max": 1,
            "echo_guard": False, "dbg_timing": False, "asr_engine_a": "cloud",
            "asr_refine": "off", "asr_capture": False}
    base.update(cfg)
    c.cfg = base
    c.asr = FakeASR(cloud_text, local_text)
    c.mt, c.bus = None, FakeBus()
    c._in_q = queue.Queue(maxsize=8)
    c._speak_q = queue.Queue()
    c._pend, c._t1, c._fa, c._t0, c._stmap = {}, {}, {}, {}, {}
    c.ctx, c.seg, c.dropped, c._run = "", None, 0, True
    c._logs = []
    c.log = lambda msg, lvl="info", who="": c._logs.append((lvl, msg))
    c.engine = lambda: "cloud"
    c._final_tail = lambda *a, **k: None
    c._chatbox_async = lambda *a, **k: None
    c._refine_async = lambda *a, **k: None
    c._finish = lambda *a, **k: None
    c._is_echo = lambda *a, **k: False
    c._bleed = lambda *a, **k: False
    c._overlap_bleed = lambda *a, **k: False
    return c


ok = True


def chk(name, cond, extra=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + extra if extra else ""))
    ok = ok and bool(cond)


def run(c):
    st = {"src_lang": "zh", "dst_lang": "ja", "last_text": "", "dst_acc": "",
          "spoken_acc": "", "clauses": 0, "ctx": "", "bus": {"id": "A-7"},
          "committed": "", "first_audio": None, "t0": 0.0}
    seg = {"id": "A-7", "state": "vad"}
    c.seg = st
    c._cur = lambda ev: st
    c._ensure_seg = lambda s: seg
    c._on_final({"audio": b"", "t1": 0.0, "dur": 1.5, "lvl": -30.0})
    return seg, st


print("[1] 云端没出字 → 本地兜回一句话（不再是空段）")
c = mk(cloud_text="", local_text="我今天有点累")
seg, st = run(c)
chk("云端之后又调了本地", c.asr.calls == ["cloud", "local"], str(c.asr.calls))
chk("字幕落的是本地文本", any(k.get("src") == "我今天有点累" for _s, k in c.bus.upds),
    str([k.get("src") for _s, k in c.bus.upds]))
chk("引擎标了兜底", any(k.get("engine") == "local(兜底)" for _s, k in c.bus.upds),
    str([k.get("engine") for _s, k in c.bus.upds]))
chk("没有标成 drop", not any(k.get("state") == "drop" for _s, k in c.bus.upds))

print("[2] 云端和本地都没字 → drop + 原因（不是静默 done）")
c = mk(cloud_text="", local_text="")
seg, st = run(c)
drops = [k for _s, k in c.bus.upds if k.get("state") == "drop"]
chk("标成 drop", bool(drops))
chk("原因写在 note 里", bool(drops) and "没识别出字" in str(drops[0].get("note")),
    str(drops[0].get("note"))[:40] if drops else "")
chk("没走收尾（_finish 没被调）", st.get("fin") is not True)
chk("留了 warn 日志", any(lv == "warn" for lv, _m in c._logs), str(c._logs[-1:]))

print("[3] 本地引擎本来就没字（engine=local）→ 不重复兜，直接 drop")
c = mk(cloud_text="", local_text="")
c.engine = lambda: "local"
seg, st = run(c)
chk("只识别了一次", c.asr.calls == ["local"], str(c.asr.calls))
chk("也标了 drop", any(k.get("state") == "drop" for _s, k in c.bus.upds))

print("[4] 云端正常出字 → 不动（没有多余的本地调用）")
c = mk(cloud_text="今天有点晚了", local_text="不该被用到")
seg, st = run(c)
chk("只识别一次", c.asr.calls == ["cloud"], str(c.asr.calls))
chk("字幕是云端文本", any(k.get("src") == "今天有点晚了" for _s, k in c.bus.upds))
chk("没有 drop", not any(k.get("state") == "drop" for _s, k in c.bus.upds))

print("\n" + ("ALL PASS" if ok else "有失败项"))
sys.exit(0 if ok else 1)
