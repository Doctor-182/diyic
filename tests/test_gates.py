# -*- coding: utf-8 -*-
r"""总闸真值表（离线，不碰麦克风、不花钱）：验证「关了就是关了」。

用户 m05780 的现场：按「全部停止」之后，字幕还在翻，只是不出声。
根因不是 UI，是采集侧没有闸、在途的段不清、mode=pass 从来没被读过。
这张表把四种情况钉死：

  1. A 链关着 → 采集侧喂进来也不入队（on_seg 直接丢）；
  2. 识别总闸关着 → A/B 两条链都不干活；
  3. 直通模式 → A 链不翻译不出声，B 链照常（对方还是要中文字幕）；
  4. 翻译总闸关着 → 只出原文，不调 MT、不排 TTS；
  5. cancel_all → 输入队列/当前段/待播队列全清，并留下 drop 原因。

用法：call scripts\_env.bat 后跑  %PY% -m tests.test_gates
"""
import queue
import sys
import types

sys.path.insert(0, ".")
from vrdub.chains import StreamChain as Chain     # noqa: E402


class FakeMT:
    def __init__(self):
        self.calls = 0

    def stream_clauses(self, *a, **k):
        self.calls += 1
        yield ("訳です", 30.0, False, None)

    def translate(self, *a, **k):
        self.calls += 1
        return {"text": "訳"}


class FakeBus:
    def __init__(self):
        self.upds = []

    def upd(self, seg, **kw):
        self.upds.append((seg, kw))

    def recent(self, n=50):
        return []


def mk(src_key="mic", **cfg):
    c = Chain.__new__(Chain)
    c.name = "A" if src_key == "mic" else "B"
    c.src_key, c.speak = src_key, True
    base = {"out_on": True, "in_on": True, "asr_on": True, "mt_on": True,
            "mode": "trans", "card": "natural", "voice": "", "speak_q_max": 1,
            "echo_guard": False, "dbg_timing": False}
    base.update(cfg)
    c.cfg = base
    c.mt, c.bus = FakeMT(), FakeBus()
    c._in_q = queue.Queue(maxsize=64)
    c._speak_q = queue.Queue()
    c._pend = {}
    c.seg, c.dropped, c._run = None, 0, True
    c._logs = []
    c.log = lambda msg, lvl="info", who="": c._logs.append((lvl, msg))
    return c


def st(**kw):
    d = {"src_lang": "zh", "dst_lang": "ja", "last_text": "", "dst_acc": "",
         "spoken_acc": "", "clauses": 0, "ctx": "", "bus": {"id": "A-1"},
         "first_audio": None}
    d.update(kw)
    return d


ok = True


def chk(name, cond, extra=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + extra if extra else ""))
    ok = ok and bool(cond)


print("[1] A 链关着 → 采集侧送进来也不入队")
c = mk("mic", out_on=False)
c.on_seg({"kind": "final", "audio": b"", "t1": 0})
chk("_link_off 报原因", c._link_off() == "A 链关着", c._link_off())
chk("队列仍是空的", c._in_q.empty())
chk("留了日志", bool(c._logs))

print("[2] 识别总闸关着 → 两条链都停")
chk("A 停", mk("mic", asr_on=False)._link_off() == "识别总闸关着")
chk("B 停", mk("loop", asr_on=False)._link_off() == "识别总闸关着")

print("[3] 直通模式 → A 链不翻译，B 链照常")
ca, cb = mk("mic", mode="pass"), mk("loop", mode="pass")
chk("A 链停", "直通" in ca._link_off(), ca._link_off())
chk("A 链 _mt_off 也停", "直通" in ca._mt_off())
chk("B 链不受影响", cb._link_off() == "", cb._link_off())

print("[4] 翻译总闸关着 → 只出原文，不调 MT")
c = mk("mic", mt_on=False)
s = st()
bseg = {"id": "A-2"}
c.seg = s
c._translate_speak(s, bseg, "我今天要晚点睡", final=True)
chk("MT 一次没调", c.mt.calls == 0, f"calls={c.mt.calls}")
chk("没排 TTS", c._speak_q.empty())
chk("字幕出的是原文", s.get("dst_acc") == "" and bseg.get("id") == "A-2")
got = [kw for seg, kw in c.bus.upds if seg is bseg]
chk("bus 标了只出原文", bool(got) and "只出原文" in str(got[-1].get("note", "")), str(got[-1:] if got else []))

print("[5] 翻译总闸关着 → 收尾不补念")
c = mk("mic", mt_on=False)
c._speak_only(st(), {"id": "A-3"}, "尾巴")
chk("MT 一次没调", c.mt.calls == 0)
chk("没排 TTS", c._speak_q.empty())

print("[6] cancel_all → 三层全清并留痕")
c = mk("mic")
s = st()
c.seg = s
c._in_q.put(("final", {}))
c._in_q.put(("partial", {}))
c._speak_q.put(types.SimpleNamespace(seg={"id": "A-4"}))
c._pend = {"A-1": 2}
n = c.cancel_all("A 链停：测试")
chk("输入队列清空", c._in_q.empty())
chk("待播队列清空", c._speak_q.empty())
chk("当前段已置空", c.seg is None)
chk("_pend 清空", c._pend == {})
chk("返回作废条数=4", n == 4, f"n={n}")
drops = [kw.get("note") for seg, kw in c.bus.upds if kw.get("state") == "drop"]
chk("留了 drop 原因", len(drops) == 2 and all("测试" in d for d in drops), str(drops))

print("[7] 开关关掉之后队列里的块才被取出 → 作废而不是硬跑")
c = mk("mic", out_on=False)
c.seg = st()
c._off_drop("A 链关着")
chk("当前段置空", c.seg is None)
chk("标了 drop", any(kw.get("state") == "drop" for _, kw in c.bus.upds))

print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
