# -*- coding: utf-8 -*-
"""离线验证 A 链两条新通路（不碰麦克风、不碰 VRChat、不花钱）：

  1. _asr_first：云端在预算内回来 → 用云端的字；超预算 → 立刻用本地（不被拖住）；
  2. 云端把中文听成日语 → 拒收，用本地；
  3. _chatbox_async：云端文本进 chatbox（整句重译），打 cb_done；
  4. 云端超预算 → 发本地译文；
  5. chatbox_on 关 → 一个字都不发；
  6. 连续两句：云端一句慢一句快 → 发送顺序仍按说话顺序（队列串行）。

用法：python tools/_cloudfirst_test.py
"""
import sys, time
sys.path.insert(0, ".")
from vrdub.chains import StreamChain as Chain


class FakeASR:
    def __init__(self, d_local=0.12, d_cloud=0.4, cloud=None):
        self.dl, self.dc = d_local, d_cloud
        self.cloud = cloud or {}          # audio → (延迟, 文本)
    def transcribe_fast(self, audio, lang, e="hybrid"):
        time.sleep(self.dl)
        return "本地识别的话", "local", None
    def transcribe_cloud(self, audio, lang):
        c = self.cloud.get(audio)
        if c is None:
            time.sleep(self.dc)
            return {"text": "云端识别出来的话"}
        time.sleep(c[0])
        if c[1] is None:
            raise RuntimeError("cloud down")
        return {"text": c[1]}


class FakeMT:
    # custom= 是后来加的人设口吻参数，真 MT 有；这里必须跟着收，否则 TypeError 会被
    # _chatbox_one 的兜底吞掉，看起来像「云端文本没被用上」。
    def translate(self, text, src=None, dst=None, card=None, timeout=8.0, custom=""):
        time.sleep(0.05)
        return {"text": "訳「" + text[:12] + "」です"}


class FakeOsc:
    def __init__(self):
        self.sent = []
    def send(self, t):
        self.sent.append(t)


class FakeBus:
    def upd(self, *a, **k):
        pass


def mk(cloud_first=True, cloud_wait=1.0, chatbox_on=True, chatbox_cloud=True,
       budget=5.0, asr=None):
    c = Chain.__new__(Chain)
    c.name, c.src_key, c.speak = "A", "mic", True
    c.cfg = {"asr_engine_a": "hybrid", "a_cloud_first": cloud_first,
             "a_cloud_wait": cloud_wait, "chatbox_on": chatbox_on,
             "chatbox_cloud": chatbox_cloud, "chatbox_budget": budget,
             "out_on": True, "card": "natural", "dbg_timing": True,
             "echo_guard": False}
    c.asr, c.mt, c.osc, c.bus = asr or FakeASR(), FakeMT(), FakeOsc(), FakeBus()
    c._logs = []
    c.log = lambda msg, lvl="info", who="": c._logs.append(msg)
    return c


ok = True
def chk(name, cond, extra=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + extra if extra else ""))
    ok = ok and bool(cond)


def wait_for(c, n=1, timeout=8.0):
    t = time.time()
    while time.time() - t < timeout and len(c.osc.sent) < n:
        time.sleep(0.02)
    time.sleep(0.15)


print("[1] 云端在预算内回来 → 用云端的字")
c = mk(cloud_wait=1.0)
t = time.time(); txt, eng, ms = c._asr_first(None, "zh", 1.0); dt = (time.time() - t) * 1000
chk("engine=cloud", eng == "cloud", f"{txt} / {dt:.0f} ms")
chk("文本=云端", txt == "云端识别出来的话")

print("[2] 云端超预算 → 立刻用本地")
c = mk(cloud_wait=1.0, asr=FakeASR(d_cloud=3.0))
t = time.time(); txt, eng, ms = c._asr_first(None, "zh", 1.0); dt = (time.time() - t) * 1000
chk("engine=local", eng == "local", f"{txt} / {dt:.0f} ms")
chk("没被拖住（<1.4s）", dt < 1400, f"{dt:.0f} ms")

print("[3] 云端听成日语 → 拒收")
c = mk(cloud_wait=1.0, asr=FakeASR(cloud={None: (0.1, "こんにちは、きょうはいいてんきですね")}))
txt, eng, _ = c._asr_first(None, "zh", 1.0)
chk("engine=local", eng == "local", txt)

print("[4] chatbox：云端文本 + 整句重译 → 发云端版")
c = mk(budget=5.0)
st = {"src_lang": "zh", "dst_lang": "ja"}
c._chatbox_async({"id": 1, "state": "mt", "dst": "ローカルの訳"}, st, None, "zh", "本地识别的话")
wait_for(c, 1)
chk("发了 1 条", len(c.osc.sent) == 1, str(c.osc.sent))
chk("云端文本", bool(c.osc.sent) and "云端识别" in c.osc.sent[0], str(c.osc.sent))
chk("cb_done", st.get("cb_done") is True)

print("[5] chatbox：云端超预算 → 发本地译文")
c = mk(budget=1.0, asr=FakeASR(d_cloud=3.0))
st = {"src_lang": "zh", "dst_lang": "ja"}
t0 = time.time()
c._chatbox_async({"id": 2, "state": "mt", "dst": "ローカルの訳"}, st, None, "zh", "本地识别的话")
wait_for(c, 1, timeout=4.0)
chk("发了 1 条", len(c.osc.sent) == 1, str(c.osc.sent))
chk("本地译文", bool(c.osc.sent) and c.osc.sent[0] == "ローカルの訳", str(c.osc.sent))
chk("约 1 s 就发（budget）", (time.time() - t0) < 2.0, f"{time.time() - t0:.2f}s")

print("[6] chatbox_on=False → 不发")
c = mk(chatbox_on=False)
c._chatbox_async({"id": 3, "state": "mt", "dst": "x"}, {"src_lang": "zh", "dst_lang": "ja"},
                 None, "zh", "本地识别的话")
time.sleep(0.6)
chk("没发", not c.osc.sent, str(c.osc.sent))

print("[7] 连续两句：先慢后快 → 顺序不乱")
asr = FakeASR(cloud={"slow": (0.6, "第一句云端"), "fast": (0.05, "第二句云端")})
c = mk(budget=5.0, asr=asr)
s1 = {"src_lang": "zh", "dst_lang": "ja"}
s2 = {"src_lang": "zh", "dst_lang": "ja"}
c._chatbox_async({"id": 11, "state": "mt", "dst": "第一句本地"}, s1, "slow", "zh", "本地甲")
c._chatbox_async({"id": 12, "state": "mt", "dst": "第二句本地"}, s2, "fast", "zh", "本地乙")
wait_for(c, 2, timeout=8.0)
chk("发了两条", len(c.osc.sent) == 2, str(c.osc.sent))
chk("按说话顺序", bool(c.osc.sent) and "第一句" in c.osc.sent[0] and "第二句" in c.osc.sent[1],
    str(c.osc.sent))

print("\nRESULT: " + ("ALL PASS" if ok else "HAS FAIL"))
sys.exit(0 if ok else 1)
