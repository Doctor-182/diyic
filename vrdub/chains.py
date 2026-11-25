# -*- coding: utf-8 -*-
"""三条链的调度器。

A 链（我说话 → 对方听日语）
    mic → 分段 → 边说边切：整段缓冲每 0.6 s 送一次本地识别，识别文本里出现子句
    边界就立刻「提交」这一句 → MT 流式 → TTS 首包一到就推进播放环。
    也就是说不用等我说完，日语已经开始放了 —— 这才是同传，不是录音后翻译。
    云端识别在收尾时补一次，只用来校正字幕（已经念出去的不追改）。

B 链（对方说话 → 我看中文）
    扬声器 Loopback → 同一套流式机制，只是不出声：字幕/浮窗更新即止。
    目的地的音频设备不会被写一个字，所以日语永远不会回灌给对方。

C 链（对方打字 → 中文浮在原位）交给 ocr.OcrChain，这里只做事件桥。

所有段状态都进 bus，网页/浮窗/统计共用一个真相。
"""
from __future__ import annotations

import queue
import re
import threading
import time

import numpy as np

from . import util

_RE_END = "。！？!?…；;，,、：:"
# 没有标点时也不能一直憋着：语气词/句末助词后面是天然的气口
_SOFT_END = "了的吗呢啊呀吧哦嘛咯ねよかわぞなー、 "


def find_cut(text: str, done: int, min_chars: int) -> int:
    """在 text 里找一个可以立刻开口的位置（返回切点下标，-1 表示还不到时候）。

    优先级：真标点 → 语气/句末助词 → 够长就整段提交（宁可早说半句，也不让人等）。
    """
    for i, ch in enumerate(text):
        if ch in _RE_END and i + 1 > done and i >= 2:
            return i + 1
    if util.char_count(text[done:]) < max(6, min_chars + 3):
        return -1
    for i in range(len(text) - 1, done, -1):
        if text[i] in _SOFT_END and util.char_count(text[done:i + 1]) >= min_chars:
            return i + 1
    # 兜底：没有气口又已经够长才硬切；短句宁可等 final（碎片翻错比慢半秒糟糕得多）
    if util.char_count(text[done:]) >= max(16, min_chars + 8):
        return len(text)
    return -1


_CJK_SP = re.compile(r"(?<=[\u4e00-\u9fff\u3040-\u30ff])\s+(?=[\u4e00-\u9fff\u3040-\u30ff])")
_MIX = re.compile(r"[A-Za-z]")


def _norm(s: str) -> str:
    """统一清理：本地 ASR 会在汉字中间插空格（「这个 酒馆」），不折掉 MT 会翻歪。"""
    return _CJK_SP.sub("", (s or "").strip())


def _suspect(s: str) -> bool:
    """识别结果里中英混排 = 多半是把词听错了（实测「测试」→「测a 恋」），
    这种句子直接送去翻译只会把错误放大，值得多等一次云端。"""
    return bool(s) and bool(_MIX.search(s)) and bool(re.search(r"[\u4e00-\u9fff]", s))


def common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class SpeakTask:
    __slots__ = ("seg", "text", "lang", "voice", "rate", "tag", "queued_at")

    def __init__(self, seg, text, lang, voice, rate=1.0, tag=""):
        self.seg = seg
        self.text = text
        self.lang = lang
        self.voice = voice
        self.rate = rate
        self.tag = tag
        self.queued_at = time.time()


# ====================================================================== 回声登记
# 我合成出来的每一句话都登记在这里，所有链共享 —— 因为 A 链的日语会从耳机漏进
# B 链的回环、也可能被麦克风吃回去，只有跨链共享才拦得住（实测：B 链会把我自己
# 说的日语重新翻译一遍）。
_SPOKEN: list = []
_SPOKEN_LK = threading.Lock()
_SPOKEN_TTL = 30.0
_SPOKEN_MAX = 24


def note_spoken(text: str):
    t = (text or "").strip()
    if not _norm(t):
        return
    with _SPOKEN_LK:
        _SPOKEN.append((time.time(), t))
        del _SPOKEN[:-_SPOKEN_MAX]


def recent_spoken(ttl: float = _SPOKEN_TTL):
    now = time.time()
    with _SPOKEN_LK:
        keep = [(ts, t) for ts, t in _SPOKEN if now - ts < ttl]
        _SPOKEN[:] = keep
    return keep


def saying_echo(text: str, ttl: float = _SPOKEN_TTL):
    """返回 (是否回声, 相似度, 对应那句)。"""
    norm = _norm(text)
    if not norm:
        return False, 0.0, ""
    best, who = 0.0, ""
    for ts, t in recent_spoken(ttl):
        nt = _norm(t)
        if not nt:
            continue
        if nt == norm or (len(norm) >= 4 and norm in nt) or (len(nt) >= 4 and nt in norm):
            return True, 1.0, t
        sim = util.similarity(norm, nt)
        if sim > best:
            best, who = sim, t
    return (best > 0.60), best, who


def kana_ratio(s: str) -> float:
    k = sum(1 for ch in s if "\u3040" <= ch <= "\u30ff")
    n = max(1, util.char_count(s))
    return k / n


# 目标语是日语时，正常的日语译文不可能几乎没有假名。实测模型偶发「俺这边也可以了よ」
# 这种半中半日的串台输出——假名占比 <12%（且长度 ≥6）就当串台，重翻一次。
_LEAK_MIN_KANA = 0.18


def lang_leak(text: str, dst_lang: str) -> bool:
    """译文看起来不是目标语言（只查中文漏进日语这一种真实故障）。"""
    if dst_lang != "ja":
        return False
    s = (text or "").strip()
    if util.char_count(s) < 6:
        return False
    return kana_ratio(s) < _LEAK_MIN_KANA


class StreamChain:
    """A / B 共用：一边收音频一边识别翻译，出不出声由 speak 决定。"""

    def __init__(self, name, speak, cfg, bus, asr, mt, tts, player=None, log=util.log,
                 src_key="mic"):
        self.name = name
        self.speak = speak
        self.cfg = cfg
        self.bus = bus
        self.asr = asr
        self.mt = mt
        self.tts = tts
        self.player = player
        self.log = log
        self.src_key = src_key
        self._in_q = queue.Queue(maxsize=64)
        self._speak_q = queue.Queue()
        self._thr = None
        self._tthr = None
        self._run = False
        self.seg = None            # 当前段状态
        self.recent_tts = []       # [(text, ts)]，回声检测
        self.ctx = ""              # 跨段上文（最近 200 字原文），给收尾整句重译当参考
        self._stmap = {}           # seg id → 段状态 dict（_utter 收尾时要用）
        self._t1 = {}              # seg id → 段尾时间戳（说完最后一个字）
        self._t0 = {}              # seg id → 开口时间戳
        self._fa = {}              # seg id → 第一声译文/音频落地的绝对时间
        self._pend = {}            # seg id → 还没播完的片段数（全播完才算 done）
        self._refine_slots = threading.Semaphore(2)
        self.osc = None            # chatbox 发送器（只有 A 链挂）
        self.tts_busy = False
        self.dropped = 0
        self.speaking_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def start(self):
        if self._run:
            return
        self._run = True
        self._thr = threading.Thread(target=self._loop, name=f"chain-{self.name}", daemon=True)
        self._thr.start()
        if self.speak:
            self._tthr = threading.Thread(target=self._speak_loop, name=f"tts-{self.name}",
                                          daemon=True)
            self._tthr.start()

    def stop(self):
        self._run = False
        try:
            self._in_q.put_nowait(None)
        except Exception:
            pass
        if self.speak:
            try:
                self._speak_q.put_nowait(None)
            except Exception:
                pass

    def wait_idle(self, timeout: float = 15.0) -> bool:
        """等这一路把队列里的片段都合成完（评测/切换设备前用）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._speak_q.empty() and not self.tts_busy:
                try:
                    if self.player is not None:
                        self.player.wait_drain(0.4)
                except Exception:
                    pass
                return True
            time.sleep(0.03)
        return False

    # ------------------------------------------------------------------ 入口
    # ------------------------------------------------------------------ 总闸
    def _link_off(self) -> str:
        """这条链现在该不该干活？返回停摆原因（干活时返回 ""）。

        链开关（out_on/in_on）与识别总闸（asr_on）是两个维度：链开关是「界面上关了
        这一条」，asr_on 是「整机先别识别」。两者任一关掉，采集侧就不该再把音频喂
        进来；这里是第二道保险，防止已经排进队列的块、或绕过采集回调的路径继续跑。
        """
        if not bool(self.cfg.get("asr_on", True)):
            return "识别总闸关着"
        key = "out_on" if self.src_key == "mic" else "in_on"
        if not bool(self.cfg.get(key, True)):
            return "A 链关着" if self.src_key == "mic" else "B 链关着"
        if self.src_key == "mic" and str(self.cfg.get("mode", "trans")) == "pass":
            # 直通 = 原声过去、不变声。老代码里 mode 根本没被读过：
            # 选了直通照样翻译、照样把日语念出去（用户 m05780 现场踩到）。
            return "直通模式（原声直通，不翻译）"
        return ""

    def _mt_off(self) -> str:
        """该不该翻译？关着返回原因，开着返回 ""。"""
        if not bool(self.cfg.get("mt_on", True)):
            return "翻译总闸关着"
        if self.src_key == "mic" and str(self.cfg.get("mode", "trans")) == "pass":
            return "直通模式（不翻译）"
        return ""

    def cancel_all(self, reason="链路已关") -> int:
        """把在途的东西全作废：输入队列、当前段、待播 TTS 队列。返回作废条数。

        为什么三层都要清：用户按「停止」那一刻，已经进识别队列的块、已经拿到文本
        排队等翻译的句子、已经排进播放器的音频，都不会自己消失 —— 观感就是
        「关掉了还在动、还在响」。未完成的段统一留 drop 记录，事后能查到原因。
        """
        n = 0
        try:
            while True:
                self._in_q.get_nowait()
                n += 1
        except queue.Empty:
            pass
        st = self.seg
        if st is not None and st.get("bus") is not None:
            self.bus.upd(st["bus"], state="drop", note=reason)
            n += 1
        self.seg = None
        try:
            while True:
                task = self._speak_q.get_nowait()
                if task is None:
                    continue
                n += 1
                if getattr(task, "seg", None) is not None:
                    self.bus.upd(task.seg, state="drop", note=reason)
        except queue.Empty:
            pass
        self._pend.clear()
        return n

    def _off_drop(self, off: str):
        """队列里的块在开关关掉之后才被取出来：作废当前段并留痕。"""
        st = self.seg
        if st is not None and st.get("bus") is not None:
            self.bus.upd(st["bus"], state="drop", note=off + "：作废")
        self.seg = None

    def on_seg(self, ev):
        """Segmenter 回调：只入队，绝不阻塞音频线程。"""
        if not self._run:
            return
        off = self._link_off()
        if off:
            self._off_n = getattr(self, "_off_n", 0) + 1
            if self._off_n <= 3 or self._off_n % 20 == 0:
                self.log(f"{self.name} {off}：丢掉一段音频（累计 {self._off_n} 段；"
                         f"采集侧本不该再送，这条是保险）", "info", self.name)
            return
        try:
            if ev["kind"] == "spec":
                self._in_q.put_nowait(("partial", ev))
            else:
                self._in_q.put_nowait(("final", ev))
        except queue.Full:
            self.dropped += 1

    def _loop(self):
        while self._run:
            try:
                item = self._in_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                continue
            kind, ev = item
            off = self._link_off()
            if off:
                self._off_drop(off)
                continue
            try:
                if kind == "partial":
                    self._on_partial(ev)
                else:
                    self._on_final(ev)
            except Exception as e:                    # noqa: BLE001
                self.log(f"{self.name} 处理异常：{e!r}", "err", self.name)

    # ------------------------------------------------------------------ 段状态
    def _cur(self, ev):
        """拿当前段状态；没有就按链的语向新建一个（自上一段继承上文）。"""
        if self.seg is not None:
            return self.seg
        if self.src_key == "mic":                     # A 链：我说中文 → 对方语种
            sl, dl = self.cfg.get("src_lang", "zh"), self.cfg.get("dst_lang", "ja")
        else:                                         # B 链：对方语种 → 中文
            # 注意：源语是「对方说的语言」in_lang，不是译文语言 sub_lang。
            # （这里曾经误用 sub_lang="zh"，一旦开启同语言直通，B 链就整条不翻译了）
            sl, dl = self.cfg.get("in_lang", "auto"), self.cfg.get("sub_lang", "zh")
        self.seg = {"committed": "", "last_text": "", "bus": None, "ctx": self.ctx, "dst_acc": "",
                    "src_lang": sl, "dst_lang": dl, "t0": ev["t0"],
                    "t1": ev.get("t1") or time.time(), "dur": ev.get("dur") or 0.0,
                    "lvl": float(ev.get("lvl", -99.0)),
                    "tts_ms": 0, "first_audio": None, "clauses": 0, "note": ""}
        return self.seg

    # ------------------------------------------------------------------ 引擎
    def engine(self) -> str:
        """这条链用哪个识别引擎：A 链看 asr_engine_a，B 链看 asr_engine_b，
        没设就回落全局 asr_engine。引擎是即时读的，控制台改完下一段就生效，不用重启。"""
        key = "asr_engine_a" if self.src_key == "mic" else "asr_engine_b"
        v = str(self.cfg.get(key) or "")
        if v in ("hybrid", "local", "cloud"):
            return v
        return str(self.cfg.get("asr_engine", "hybrid"))

    def _ensure_seg(self, st):
        if st["bus"] is None:
            st["bus"] = self.bus.new_seg(self.name, src_lang=st["src_lang"],
                                         dst_lang=st["dst_lang"],
                                         speaker="我" if self.src_key == "mic" else "对方")
            self._t1[st["bus"]["id"]] = st["t1"]
            self._t0[st["bus"]["id"]] = st["t0"]
        return st["bus"]

    # ------------------------------------------------------------------ 部分结果
    def _on_partial(self, ev):
        audio = ev["audio"]
        st = self._cur(ev)
        lang = st["src_lang"]
        e = self.engine()
        # cloud 模式的投机必须离开链线程：实测云端对 5-11 s 的长段/唱歌会飘到 6-19 s，
        # 串行排在链线程里就把之后每一段的收尾全堵住（日志里出现过「投机 19485ms」）。
        if e == "cloud":
            self._spec_cloud(st, ev)
            return
        t0 = time.time()
        text, engine, ms = self.asr.transcribe_fast(audio, lang, e=e)
        text = _norm(text)
        if self.cfg.get("dbg_timing", False):
            self.log(f"{self.name} 投机 {ev['dur']:.2f}s → {ms:.0f}ms · '{text[-20:]}'", "info", self.name)
        if not text:
            return
        bseg = self._ensure_seg(st)
        # 字幕先动起来（本地识别 100 ms 级，观感上几乎是实时的）
        self.bus.upd(bseg, src=text, asr_ms=round(ms, 1), state="asr", engine=engine,
                     note=f"{ev['dur']:.1f}s/{util.char_count(text)}字")
        if self._mt_off():
            return                              # 只留原文，不翻不念
        if not self.cfg.get("live", True):
            return
        # 找可提交的气口（标点 → 语气词 → 够长就整段）
        cut = find_cut(text, len(st["committed"]), int(self.cfg.get("commit_chars", 3)))
        if cut <= len(st["committed"]):
            return
        piece = text[len(st["committed"]):cut]
        if util.char_count(piece) < int(self.cfg.get("commit_chars", 3)):
            return
        st["committed"] = text[:cut]
        self._translate_speak(st, bseg, piece, final=False)

    def _spec_cloud(self, st, ev):
        """cloud 引擎的投机识别：后台线程 + 最多一条在飞 + 最小间隔，只更新字幕。

        云端一遍识别 0.5-3 s（长段偶尔十几秒），所以：
          · 绝不在链线程里跑，收尾永远不被投机挡住；
          · 同一时刻只允许一条在飞，避免把账号并发吃光（云端并发被占满时连收尾都会排队）；
          · 只写 src（字幕），不提交子句、不出声 —— 云端的中间结果不稳，拿它切句会切错。
        """
        now = time.time()
        gap = float(self.cfg.get("asr_spec_gap", 1.5))
        if getattr(self, "_spec_busy", False) or now - getattr(self, "_spec_last", 0.0) < gap:
            return
        self._spec_last = now
        self._spec_busy = True
        bseg = self._ensure_seg(st)
        audio = ev["audio"]
        lang = st["src_lang"]
        tmo = float(self.cfg.get("asr_spec_timeout", 6.0))

        def job():
            try:
                t0 = time.time()
                r = self.asr.transcribe_cloud(audio, lang, timeout=tmo)
                text = _norm(r.get("text") or "")
                # 段已经翻页了就别写过期的字幕
                if text and self.seg is st:
                    self.bus.upd(bseg, src=text, state="asr", engine="cloud",
                                 asr_ms=round(r.get("ms") or (time.time() - t0) * 1000.0, 1))
                    if self.cfg.get("dbg_timing", False):
                        self.log(f"{self.name} 云端投机 {ev.get('dur', 0):.2f}s → "
                                 f"{(time.time() - t0) * 1000.0:.0f}ms · '{text[-18:]}'",
                                 "info", self.name)
            except Exception as e:                    # noqa: BLE001
                self.log(f"{self.name} 云端投机失败：{e!r}", "warn", self.name)
            finally:
                self._spec_busy = False

        threading.Thread(target=job, name=f"spec-{self.name}", daemon=True).start()

    # ------------------------------------------------------------------ 收尾
    def _on_final(self, ev):
        audio = ev["audio"]
        st = self._cur(ev)
        lang = st["src_lang"]
        bseg = self._ensure_seg(st)
        text = ""
        engine = ""
        ms = 0.0
        # 1) 用这条链自己选的引擎把整段定下来（local/hybrid=本地快路，cloud=直接等云端）
        e = self.engine()
        _cf = (self.src_key == "mic" and e == "hybrid"
               and bool(self.cfg.get("a_cloud_first", False))
               and float(self.cfg.get("a_cloud_wait", 1.0)) > 0)
        if _cf:
            # 云端优先（A 卡 AQL）：本地照跑，永远有结果；云端在预算内回来就用云端的字
            text, engine, ms = self._asr_first(audio, lang, float(self.cfg.get("a_cloud_wait", 1.0)))
        else:
            t = time.time()
            text, engine, _ = self.asr.transcribe_fast(audio, lang, e=e)
            ms = (time.time() - t) * 1000.0
        # 2) 收尾句：整句重译，只把「还没说出口的尾巴」补进队列
        #    碎片单翻最容易翻错（原文 4 个字，模型只能靠猜），收尾时用整句的质量换那半秒
        full = _norm(text)
        # 本地把词听串了（中英混排的乱码）时，宁可多花一次云端的钱换正确性：
        # 这种文本送进 MT 只会把错误放大成半中半日的译文。
        if full and not _cf and e == "hybrid" and _suspect(full) \
                and self.cfg.get("cloud_wait", 2.4) > 0:
            ct = self._cloud_now(audio, lang, float(self.cfg.get("cloud_wait", 2.4)))
            if ct and util.similarity(ct, full) < 0.94:
                self.log(f"{self.name} 本地听串（'{full[:16]}'）→ 云端 '{ct[:16]}'",
                         "info", self.name)
                full, engine = ct, "cloud"
        st["src_final"] = full            # chatbox 那条通路要拿它和云端文本比
        # 延迟阀的固定开销：段尾（最后一个字）→ 现在这一行 = 停顿确认 + 投机/云端收尾
        vad_ms = round((time.time() - float(ev.get("t1") or time.time())) * 1000.0, 1)
        if full and bseg is not None:
            # 收尾识别才是准的：字幕以它为准（否则字幕会一直停在投机结果上）。
            # asr_ms/engine 也在这里落：纯云端（asr_engine_b=cloud）或整段没有投机段时，
            # 只有收尾这一次识别，不写就会让 metrics 的识别 p50 变成 0。
            self.bus.upd(bseg, src=full, state="asr",
                         asr_ms=round(ms, 1), engine=engine, vad_ms=vad_ms,
                         lvl=round(float(st.get("lvl", ev.get("lvl", -99.0))), 1))
        # 噪声幻觉闸：太短 + 字太少 = 底噪被识别成词（实测「重气端」「重启端」这种）。
        # 宁可丢一句，也不能把噪声翻成日语念给对面听。
        thr = float(self.cfg.get("mic_db" if self.src_key == "mic" else "loop_db", -32.0))
        # 门槛按实测收紧过一次：0.62s/3 字/<门限+14dB 会连「嗯，对」这种轻声真话一起丢，
        # 改成 0.55s/3 字/<门限+9dB（实测底噪幻觉「重启端」在 -41dB 仍被挡住）。
        if full and ev.get("dur", 0) < 0.55 and util.char_count(full) <= 3 \
                and float(ev.get("lvl", -99)) < thr + 9.0:
            self._noise_n = getattr(self, "_noise_n", 0) + 1
            if self._noise_n <= 5 or self._noise_n % 20 == 0:
                self.log(f"{self.name} 丢掉噪声短语（{ev.get('dur', 0):.2f}s/"
                         f"{util.char_count(full)}字/{ev.get('lvl', -99):.0f}dB）'{full[:12]}'"
                         f"，累计 {self._noise_n}", "info", self.name)
            self.bus.upd(bseg, src=full, state="drop", note="噪声短语，已丢弃")
            st["bus"] = None
            self.seg = None
            return
        # 真实语料落盘（cfg.asr_capture）：TTS 合成的干净素材测出来永远是 1.00，
        # 只有「我真麦真房间说的话」才能判引擎好坏，攒下来给 tools/asr_ab.py 用。
        if full and bool(self.cfg.get("asr_capture", False)):
            try:
                from . import corpus as _corpus
                _corpus.save(audio, lang=lang, text=full, engine=engine,
                             chain=("A" if self.src_key == "mic" else "B"),
                             asr_ms=round(ms, 1), lvl=float(ev.get("lvl", -99)))
            except Exception:                    # noqa: BLE001
                pass
        # 翻译总闸关着（或工作模式=直通）：只把原文放到句子表，不翻、不合成、不发 chatbox。
        # 用户 m05780 报的就是这个：全部停止之后字幕还在翻译，只是不出声。
        _mtoff = self._mt_off()
        if _mtoff:
            if full and bseg is not None:
                self.bus.upd(bseg, dst=full, state="done", note=_mtoff + "：只出原文")
            st["bus"] = None
            self.seg = None
            return
        rest = full[len(st["committed"]):] if full.startswith(st["committed"]) else full
        # 麦克风听到的其实是 B 链的音源（外放 / 耳机漏音 / 一起放歌）：
        # 这时 A 段的内容和 B 段几乎一样，翻出去就是把别人的歌词念给对面听。
        if full and self.src_key == "mic" and bool(self.cfg.get("leak_gate", False)) \
                and callable(getattr(self, "leak_check", None)):
            try:
                corr, mi, lo = self.leak_check(st.get("t0") or 0.0, st.get("t1") or time.time())
            except Exception:                       # noqa: BLE001
                corr, mi, lo = 0.0, -99.0, -99.0
            # 两路电平曲线高度相关且线里更响 = 麦克风听到的就是扬声器那一路（漏音）
            if corr >= 0.72 and lo > mi + 6.0:
                self._leak2_n = getattr(self, "_leak2_n", 0) + 1
                if self._leak2_n <= 5 or self._leak2_n % 10 == 0:
                    self.log(f"{self.name} 麦克风与 B 链那一路电平相关 {corr:.2f}"
                             f"（{mi:.0f} vs {lo:.0f} dB）→ 判为耳机漏音，丢弃 '{full[:14]}'，"
                             f"累计 {self._leak2_n}", "info", self.name)
                if bseg is not None:
                    self.bus.upd(bseg, src=full, state="drop",
                                 note="耳机漏音被麦克风收到（和 B 链那一路同源），已丢弃")
                st["bus"] = None
                self.seg = None
                return
        if full and bool(self.cfg.get("bleed_lvl", False)) and self._overlap_bleed(st):
            self._bleed2_n = getattr(self, "_bleed2_n", 0) + 1
            if self._bleed2_n <= 5 or self._bleed2_n % 10 == 0:
                self.log(f"{self.name} 同一时间 B 链更响（{st.get('lvl', -99):.0f}dB vs 线里）"
                         f"→ 判为耳机漏音，丢弃 '{full[:14]}'，累计 {self._bleed2_n}",
                         "info", self.name)
            if bseg is not None:
                self.bus.upd(bseg, src=full, state="drop",
                             note="B 链同一时间更响：这段是耳机漏音，不是我在说话")
            st["bus"] = None
            self.seg = None
            return
        if full and self._bleed(full):
            self._bleed_n = getattr(self, "_bleed_n", 0) + 1
            if self._bleed_n <= 5 or self._bleed_n % 10 == 0:
                self.log(f"{self.name} 麦克风收到的是 B 链那一路的声音（'"
                         f"{full[:14]}'）→ 丢弃，累计 {self._bleed_n}", "info", self.name)
            if bseg is not None:
                self.bus.upd(bseg, src=full, state="drop",
                             note="麦克风收到 B 链音源（外放/漏音），已丢弃")
            st["bus"] = None
            self.seg = None
            return
        # 我说的是中文，识别结果里却有假名 → 这段根本不是我在说话，
        # 是耳机漏出来的日语/别人的日语被麦克风收进来了（实测会当成我的话翻给对面）。
        # 注意 src_lang 常是 "auto"（交付默认），所以不能只认 "zh"；
        # 只要 A 链的目标不是日/英/韩，就说明「我这边说的是中文」。
        _sl = str(st.get("src_lang") or "auto").lower()
        _i_speak_zh = _sl in ("zh", "auto", "", "none", "chinese", "cn")
        if full and self.src_key == "mic" and _i_speak_zh \
                and util.char_count(full) >= 3 and kana_ratio(full) > 0.05:
            self._leak_n = getattr(self, "_leak_n", 0) + 1
            if self._leak_n <= 5 or self._leak_n % 10 == 0:
                self.log(f"{self.name} 收到日语（我说的是中文）'{full[:14]}' → 丢弃，"
                         f"累计 {self._leak_n}", "info", self.name)
            if bseg is not None:
                self.bus.upd(bseg, src=full, state="drop",
                             note="这段是日语，不是我说的中文（耳机漏音/回声）")
            st["bus"] = None
            self.seg = None
            return
        # 整段就是回声：到此为止（不翻、不出声、不上字幕），否则我自己的日语会被
        # 麦克风/回环重新翻译一遍，越翻越乱
        if full and self._is_echo(full, st, ev.get("t1"), audio):
            if bseg is not None:
                m = getattr(self, "_echo_last", None) or (0, 0, 0, "")
                if m[3] == "wave":
                    why = "波形与自己刚放出去的声音 %.2f 相同（偏移 %+.0f ms）" % (m[0], m[1] * 1000.0)
                elif m[3] == "kana":
                    why = "这段是日语，而我说的是中文（耳机漏音）"
                elif m[3] == "btext":
                    why = "和 VRChat 里刚放出来的声音 %.2f 重合" % m[0]
                else:
                    why = "和自己刚放出去的声音重合"
                self.bus.upd(bseg, state="drop", echo=True,
                             note=f"这是自己的声音漏回麦克风：{why}，不是我在说话")
            st["bus"] = None
            self.seg = None
            return
        if _norm(rest):
            self._final_tail(st, bseg, full, rest)
            st["committed"] = full
        # 2.5) chatbox 不等播完：段一定稿就发（云端文本 + 整句重译，预算 chatbox_budget）
        if bseg is not None:
            self._chatbox_async(bseg, st, audio, lang, full)
        # 3) 云端补一次，只校正字幕（后台跑，绝不挡住下一段）
        self._refine_async(bseg, audio, lang, text)
        # 4) 段落收口（段尾时间用 final 的真时间）
        if bseg is not None:
            sid = bseg["id"]
            st["t1"] = ev.get("t1") or time.time()
            self._t1[sid] = st["t1"]
            fa = self._fa.get(sid)
            if fa and bseg.get("state") == "done":
                t0 = self._t0.get(sid) or st["t0"]
                self.bus.upd(bseg, e2e_ms=round((fa - st["t1"]) * 1000.0, 1),
                             open_ms=round((fa - t0) * 1000.0, 1))
        st["fin"] = True
        if bseg is not None:
            self._stmap[bseg["id"]] = st        # _utter 里收尾时拿不到 st，靠这张表找回
        st["bus"] = None
        if full:
            self.ctx = (self.ctx + " " + full).strip()[-200:]
        self.seg = None
        if bseg is not None:
            if self.speak and self._pend.get(bseg["id"]):
                self.bus.upd(bseg, state="speaking")     # 还有片段没播完，别急着报 done
            else:
                self._finish(bseg, st)

    def _finish(self, bseg, st=None):
        """一段的终点：语音合成也播完了，才有资格写 e2e / rtf。

        e2e = 段尾（最后一个字）→ 第一声落地；open = 起话 → 第一声落地。
        第一声可能是负的（说完之前就已经开口）—— 那才是真同传，不夹到 0。
        """
        sid = bseg.get("id")
        if st is None:
            st = self._stmap.get(sid)
        fa = self._fa.get(sid)
        t1 = self._t1.get(sid)
        t0 = self._t0.get(sid)
        if fa is None and st and st.get("first_audio"):   # B 链不出声：以首个译文片段为准
            fa = st["first_audio"]
            t1 = st.get("t1")
            t0 = st.get("t0")
        e2e = round((fa - t1) * 1000.0, 1) if (fa and t1) else round(bseg.get("e2e_ms") or 0.0, 1)
        open_ms = round((fa - t0) * 1000.0, 1) if (fa and t0) else 0.0
        audio_ms = round(bseg.get("audio_ms") or 0.0, 0)
        # chatbox 只在整段收尾（final 已到）后发一次整句终版；半截时发会连成乱码。
        # 还要满足：① 只有 A 链（B 链 self.osc=None 天然不会发）；② 真的出声了才发
        # （out_on 关着时 chatbox 只该安静，实测把旁人歌词翻成日语也塞进了 chatbox）；
        # ③ 这段没被判回声/丢弃。
        if ((st or {}).get("fin") and not (st or {}).get("cb_done")
                and not (st or {}).get("cb_pending")     # 独立通路正在发，别抢
                and self.cfg.get("out_on", True) and self.cfg.get("chatbox_on", True)
                and bseg.get("state") not in ("drop", "err") and not bseg.get("echo")):
            if st is not None:
                st["cb_done"] = True
            self._chatbox(bseg.get("dst") or "")
        self.bus.upd(bseg, state="done", e2e_ms=e2e, open_ms=open_ms, audio_ms=audio_ms,
                     rtf=round((e2e / 1000.0) / (audio_ms / 1000.0), 3) if (e2e and audio_ms) else 0.0,
                     note=(st or {}).get("note") or bseg.get("note") or "")
        self._stmap.pop(sid, None)
        self._t1.pop(sid, None)
        self._t0.pop(sid, None)
        self._fa.pop(sid, None)
        self._pend.pop(sid, None)

    def _bleed(self, text):
        """麦克风收到的这段，和最近几秒 B 链听到的是同一路声音吗？

        真机踩过：VRChat 里在放歌 / 对方声音从外放出来，麦克风照单全收，
        A 链把人家的中文歌词当我在说话，翻成日语念给对面（chatbox 里还留着证据）。
        B 链本来就在听同一路声音，比对一下就能认出来——不需要额外的声学处理。
        """
        if self.src_key != "mic" or util.char_count(text) < 4:
            return False
        now = time.time()
        for seg in reversed(self.bus.recent(16)):
            if seg.get("chain") != "B":
                continue
            ts = float(seg.get("ts") or 0.0) / 1000.0
            if ts and now - ts > 6.0:
                continue
            b = _norm(seg.get("src") or "")
            if util.char_count(b) < 3:
                continue
            if text in b or b in text:
                return True
            sim = util.similarity(text, b)
            # 纯相似度会把「共用汉字的中日句子」判成同一句（实测中文 0.6 上下浮动），
            # 所以要么麦克风那路确实是日语（假名证据），要么相似度顶到 0.8 以上。
            if sim > 0.62 and (kana_ratio(text) > 0.12 or sim > 0.80):
                return True
        return False

    def _overlap_bleed(self, st):
        """同一时间点 B 链也在听到声音，而且比麦克风还响 → 麦克风听到的是那一路的漏音。

        真机上耳机漏音会被麦克风收到，云端/local 识别把日语听成中文乱码，
        再翻成日语发进 chatbox（实测冒出一堆「俺のPC、いいね」这种没人说过的话）。
        这里用「谁更响」判断：我自己说话时麦克风电平远高于线里那一路；
        漏音时正好反过来。
        """
        if self.src_key != "mic":
            return False
        t0 = float(st.get("t0") or 0.0)
        t1 = float(st.get("t1") or time.time())
        my = float(st.get("lvl", -99.0))
        for seg in reversed(self.bus.recent(16)):
            if seg.get("chain") != "B":
                continue
            bts = float(seg.get("ts") or 0.0) / 1000.0
            if not bts or t1 - bts > 4.0:
                continue
            bl = seg.get("lvl")
            if bl is None:
                continue
            # 原来只要「线里别比麦克风低 6 dB 以上」就算漏音，太松：我在说话、别人也在说话时
            # 会被整段吃掉（实测一天误杀 40+ 段）。漏音的物理特征是「线里那一路更响」。
            if bts >= t0 - 1.0 and float(bl) >= my + 6.0:
                return True
        return False

    def _cloud_now(self, audio, lang, timeout=2.4):
        """同步等一次云端识别，等不到就当没等到（返回空串）。"""
        box = {}

        def job():
            try:
                box["r"] = self.asr.transcribe_cloud(audio, lang)
            except Exception as e:                    # noqa: BLE001
                box["e"] = e

        th = threading.Thread(target=job, name=f"cloud-{self.name}", daemon=True)
        th.start()
        th.join(timeout)
        r = box.get("r") or {}
        return _norm(r.get("text") or "")

    def _asr_first(self, audio, lang, wait=1.0):
        """云端优先（A 链可开）：本地和云端同时跑，云端在 wait 秒内回来就用云端的字。

        返回 (text, engine, ms)。本地那条永远不被云端拖住：先起云端线程，主线程里
        立刻跑本地（实测 80–350 ms），剩下的时间才等云端。云端没回来、或者回来了
        但明显串语种（中文被听成日语），一律用本地结果。
        """
        box = {}

        def job():
            try:
                box["r"] = self.asr.transcribe_cloud(audio, lang)
            except Exception as exc:                  # noqa: BLE001
                box["e"] = exc

        th = threading.Thread(target=job, name=f"cloudeq-{self.name}", daemon=True)
        th.start()
        t0 = time.time()
        text, _eng, _ = self.asr.transcribe_fast(audio, lang, e="local")
        ltext = _norm(text)
        left = max(0.0, float(wait) - (time.time() - t0))
        if left > 0:
            th.join(left)
        ct = _norm((box.get("r") or {}).get("text") or "")
        ms = (time.time() - t0) * 1000.0
        if not ct:
            return ltext, "local", ms
        # A 链我说的是中文：整串假名只能是云端听错了（实测偶发把中文听成日语）
        if kana_ratio(ct) > 0.2 and kana_ratio(ltext) < 0.05 \
                and util.char_count(ltext) >= 3:
            self.log(f"{self.name} 云端听成日语（'{ct[:14]}'），本地是中文 → 用本地", "info", self.name)
            return ltext, "local", ms
        if ltext and util.similarity(ct, ltext) < 0.999:
            self.log(f"{self.name} 云端优先：本地 '{ltext[:14]}' → 云端 '{ct[:14]}'"
                     f"（{ms:.0f} ms）", "info", self.name)
        return ct, "cloud", ms

    def _chatbox_async(self, bseg, st, audio, lang, src_text):
        """段定稿就把这句排进 chatbox 队列 —— 不等 TTS 播完，也不抢时间。

        用户结论（m04624）：聊天框不需要在我说话说完之前就上去，所以不用省时间，
        云端识别给足预算（cfg.chatbox_budget，默认 5 s）；云端回来就用云端文本整句
        重译，等不到才退回这一句已经翻好的本地译文。
        发送走单线程队列：云端延迟每句不同，并行发会乱序（后一句先上屏）。
        """
        if self.osc is None or not self.speak:
            return
        if not (self.cfg.get("out_on", True) and self.cfg.get("chatbox_on", True)):
            return
        if bseg.get("state") in ("drop", "err") or bseg.get("echo"):
            return
        if st.get("cb_done") or st.get("cb_pending"):
            return
        st["cb_pending"] = True
        q = getattr(self, "_cb_q", None)
        if q is None:
            q = self._cb_q = queue.Queue()
        th = getattr(self, "_cb_th", None)
        if th is None or not th.is_alive():
            th = self._cb_th = threading.Thread(target=self._chatbox_worker,
                                                name=f"cbq-{self.name}", daemon=True)
            th.start()
        q.put({"bseg": bseg, "st": st, "audio": audio, "lang": lang,
               "src": _norm(src_text or ""), "t": time.time()})

    def _chatbox_worker(self):
        """chatbox 单线程队列：严格按说话顺序发；排队不占用识别/播放主线。"""
        while True:
            item = self._cb_q.get()
            try:
                self._chatbox_one(item)
            except Exception as exc:                  # noqa: BLE001
                self.log(f"{self.name} chatbox 发送失败：{exc!r}", "warn", self.name)
                (item.get("st") or {})["cb_pending"] = False
            finally:
                self._cb_q.task_done()

    def _chatbox_one(self, item):
        bseg, st = item["bseg"], item["st"]
        if st.get("cb_done"):
            st["cb_pending"] = False
            return
        budget = max(0.5, float(self.cfg.get("chatbox_budget", 5.0)))
        want_cloud = bool(self.cfg.get("chatbox_cloud", True)) \
            and self.engine() in ("hybrid", "cloud")
        src_local = item["src"]
        dst_lang = st.get("dst_lang")
        out = ""
        why = "本地"
        t0 = time.time()
        try:
            if want_cloud and src_local:
                # 预算从「这句定稿的时刻」算起，排队等待不算在里面
                left = budget - (t0 - item["t"])
                if left > 0.2:
                    ct = self._cloud_now(item["audio"], item["lang"], left)
                    left = budget - (time.time() - item["t"])
                    if ct and left > 0.2 and not (
                            kana_ratio(ct) > 0.2 and kana_ratio(src_local) < 0.05):
                        if util.similarity(ct, src_local) < 0.985:
                            r = self.mt.translate(ct, src=st.get("src_lang"), dst=dst_lang,
                                                  card=self.cfg.get("card", "natural"),
                                                  timeout=left)
                            cd = (r.get("text") or "").strip()
                            if cd and not lang_leak(cd, dst_lang):
                                out, why = cd, "云端"
        except Exception as exc:                      # noqa: BLE001
            self.log(f"{self.name} chatbox 云端文本失败：{exc!r}", "warn", self.name)
        if not out:
            out = (bseg.get("dst") or "").strip()
        if out:
            self._chatbox(out)
            self.bus.upd(bseg, cb_ms=round((time.time() - item["t"]) * 1000.0, 1))
            st["cb_done"] = True
            if why == "云端" or self.cfg.get("dbg_timing", False):
                self.log(f"{self.name} chatbox[{why}] {(time.time() - item['t']) * 1000:.0f} ms"
                         f" ← {out[:24]}", "info", self.name)
        st["cb_pending"] = False

    def _refine_async(self, bseg, audio, lang, text):
        """定稿精修：本地 anime-whisper（GPU ≈0.35 s）优先，没显存就走云端。

        只改字幕，不动已经排进 TTS 的音频；B 链（只出字幕、不出声）连中文译文一起刷新。
        """
        if not self.cfg.get("refine", True):
            return
        if self.engine() == "cloud":        # 主路径本来就是云端，收尾不用再来一遍
            return
        if self.asr.refiner() == "off":
            return
        # <0.9 s 的碎片（「喂」「嗯」「对」）精修不出新信息：实测云端要 9.1–9.5 s，
        # 而且十有八九只是把「喂」改成「喂，」。噪音闸已经挡掉更短的段，这里再兜一层。
        if len(audio) < 32000 * 0.9:
            return
        if not self._refine_slots.acquire(blocking=False):
            return

        def job():
            try:
                ct0, eng, ms = self.asr.refine(
                    audio, lang, timeout=float(self.cfg.get("refine_timeout", 6.0)))
                ct = _norm(ct0)
                if not ct:
                    return
                cur = bseg.get("src") or ""
                # 注意：精修耗时只写 refine_ms，绝不覆盖 asr_ms ——
                # asr_ms 是「快路出字用了多久」，混进 0.3-13 s 的精修耗时会把 metrics 的识别 p50 整个拉歪。
                same = util.similarity(ct, cur) >= 0.92
                fields = {"engine": eng, "refine_ms": round(ms, 1)}
                if same:
                    self.bus.upd(bseg, **fields)
                    return
                fields.update(src=ct, note=f"{util.char_count(cur)}→{util.char_count(ct)}字")
                # B 链只出字幕：精修后把中文译文一起刷新，否则字幕右边还是错的
                if self.src_key != "mic" and bool(self.cfg.get("refine_mt", True)):
                    dl = str(self.cfg.get("sub_lang", "zh"))
                    sl = str(self.cfg.get("in_lang", lang or "ja"))
                    try:
                        r = self.mt.translate(ct, src=sl, dst=dl,
                                              card=self.cfg.get("card", "natural"), timeout=8.0)
                        dt = (r.get("text") or "").strip()
                        if dt and not lang_leak(dt, dl):
                            # 译文字数差得远才改成「已修正」，避免每句都闪
                            if util.similarity(_norm(dt), _norm(bseg.get("dst") or "")) < 0.90:
                                fields["state"] = "refined"
                            fields["dst"] = dt
                    except Exception as e:            # noqa: BLE001
                        self.log(f"{self.name} 精修后重译失败：{e!r}", "warn", self.name)
                else:
                    fields["state"] = "refined"
                self.bus.upd(bseg, **fields)
            except Exception as e:                    # noqa: BLE001
                self.log(f"{self.name} 精修失败：{e!r}", "warn", self.name)
            finally:
                self._refine_slots.release()

        threading.Thread(target=job, name=f"refine-{self.name}", daemon=True).start()

    # ------------------------------------------------------------------ 翻译 + 出声
    def _final_tail(self, st, bseg, full, rest):
        """收尾：整句重译 → 和已经念过的部分求公共前缀 → 只补新增的尾巴。"""
        dst = st["dst_lang"]
        t0 = time.time()
        try:
            outs = []
            for piece, first_ms, echo, rate_hint in self.mt.stream_clauses(
                    full, src=st["src_lang"], dst=dst, card=self.cfg.get("card", "natural"),
                    timeout=12.0, want_rate=self.src_key == "mic",
                    ctx=st.get("ctx", "")):
                # 这里绝不能按 echo 丢片段：整句重译本来就会重复已经念过的词，
                # 丢了字幕就少尾巴（实测「…本当に同時通訳みたい」缺了「だね」）。
                outs.append(piece)
            full_dst = "".join(outs).strip()
        except Exception as e:                        # noqa: BLE001
            self.log(f"{self.name} 收尾整句翻译失败，退回碎翻：{e!r}", "warn", self.name)
            self._translate_speak(st, bseg, rest, final=True)
            return
        if full_dst and lang_leak(full_dst, dst):
            r = self.mt.translate(full, src=st["src_lang"], dst=dst,
                                  card=self.cfg.get("card", "natural"), timeout=12.0)
            alt = (r.get("text") or "").strip()
            if alt and not lang_leak(alt, dst):
                self.log("%s 译文串台（假名 %.2f）→ 已重翻：%s"
                         % (self.name, kana_ratio(full_dst), alt[:24]), "warn", self.name)
                full_dst = alt
            else:
                self.log("%s 译文串台但重翻也没救回来：%s" % (self.name, full_dst[:24]), "warn", self.name)
        spoken = (st.get("spoken_acc") or "").strip()
        if not full_dst:
            return
        # 字幕永远以整句译文为准（碎翻只是「先出声」的中间产物）
        st["dst_acc"] = full_dst
        if st.get("first_audio") is None:
            st["first_audio"] = time.time()
        self.bus.upd(bseg, dst=full_dst, state="mt",
                     mt_ms=round((time.time() - t0) * 1000.0, 1))
        tail = full_dst
        if spoken:
            lcp = common_prefix(full_dst, spoken)
            # lcp 太短 = 模型把前半句也重写了：前半句已经念出去了，收不回来，
            # 这时只补原文剩余部分的单翻，不重念整句（否则对方会听到两遍）。
            tail = full_dst[lcp:] if lcp >= max(2, len(spoken) - 6) else ""
            # 收尾重译常把「已念部分的结尾词」也带上（实测「予想してたより」+「よりずっと…」
            # → 听感是「より、より」打结）。去掉尾巴开头与已念结尾重复的那几个字。
            for k in range(min(6, len(tail)), 1, -1):
                if spoken.endswith(tail[:k]):
                    tail = tail[k:]
                    break
        if not tail:
            # 模型把整句重写了，但意思和已经念出去的几乎一样（相似度 >0.72）→ 不再重念，
            # 只更新字幕；否则对方会听到同一句话被念两遍（实测同一句音频 6.5s ≈ 两遍）
            if spoken and util.similarity(full_dst, spoken) > 0.72:
                if self.cfg.get("dbg_timing", False):
                    self.log(f"{self.name} 收尾：整句重写但已念过 {util.similarity(full_dst, spoken):.2f} 相似 → 不重念",
                             "info", self.name)
                return
            self._speak_only(st, bseg, rest)
            if self.cfg.get("dbg_timing", False):
                self.log(f"{self.name} 收尾：整句重写，字幕更新整句 {util.char_count(full_dst)}字，只补尾",
                         "info", self.name)
            return
        if self.speak and self.cfg.get("out_on", True) and _norm(tail):
            # 注意：tail 是「我们自己的译文」，绝不能用 _is_echo —— 它的第 2 层判据是
            # 「A 链收到整串假名 = 自己的日语漏回来了」，拿它判日语输出会把每一句都判成回声，
            # 结果就是字幕有、声音没有（实测「语音合成合不出来」就是这个）。
            # 这里只挡「这段尾巴我刚才已经念过」的重复。
            if spoken and util.similarity(tail, spoken) > 0.92:
                if self.cfg.get("dbg_timing", False):
                    self.log(f"{self.name} 收尾：尾巴和已念内容重合 {util.similarity(tail, spoken):.2f} → 不重念",
                             "info", self.name)
                return
            sid = bseg["id"]
            self._pend[sid] = self._pend.get(sid, 0) + 1
            self._speak_q.put(SpeakTask(bseg, tail, dst, self.cfg.get("voice", ""),
                                        self._rate(st)))
            st["clauses"] += 1
        if self.cfg.get("dbg_timing", False):
            self.log(f"{self.name} 收尾整句 {util.char_count(full)}字 → {util.char_count(full_dst)}字"
                     f" · 补尾 {util.char_count(tail)}字 · {(time.time() - t0) * 1000:.0f}ms",
                     "info", self.name)

    def _speak_only(self, st, bseg, text):
        """只念、不改字幕（收尾补尾用）。"""
        if not (self.speak and self.cfg.get("out_on", True)) or not _norm(text):
            return
        if self._mt_off():
            return                              # 关掉翻译就不补念尾巴
        t0 = time.time()
        try:
            for piece, first_ms, echo, rate_hint in self.mt.stream_clauses(
                    text, src=st["src_lang"], dst=st["dst_lang"],
                    card=self.cfg.get("card", "natural"), timeout=12.0,
                    want_rate=self.src_key == "mic", ctx=st.get("ctx", "")):
                if echo or not _norm(piece):
                    continue
                last = st.get("last_piece") or ""
                if last and util.similarity(piece, last) > 0.86:
                    continue
                st["last_piece"] = piece
                sid = bseg["id"]
                self._pend[sid] = self._pend.get(sid, 0) + 1
                st["spoken_acc"] = (st.get("spoken_acc") or "") + piece
                self._speak_q.put(SpeakTask(bseg, piece, st["dst_lang"],
                                            self.cfg.get("voice", ""), self._rate(st, rate_hint)))
                st["clauses"] += 1
        except Exception as e:                        # noqa: BLE001
            self.log(f"{self.name} 补尾失败：{e!r}", "warn", self.name)

    def _translate_speak(self, st, bseg, text, final=False):
        """翻译一个气口并立刻排进 TTS 队列。dst 是累计的：字幕只增不减，不会「吞字」。"""
        st["last_text"] = (st["last_text"] + text) if not final else text
        if not _norm(text):
            return
        _mtoff = self._mt_off()
        if _mtoff:
            self.bus.upd(bseg, dst=st.get("dst_acc") or text, note=_mtoff + "：只出原文")
            return
        if self._is_echo(text, st):
            self.bus.upd(bseg, state="drop", echo=True,
                         note="这段不是我在说话（自己的声音漏回来/和别人撞句），已拦")
            return
        card = self.cfg.get("card", "natural")
        dst = st["dst_lang"]
        srcl = st["src_lang"]
        t0 = time.time()
        first = None
        try:
            for piece, first_ms, echo, rate_hint in self.mt.stream_clauses(
                    text, src=srcl, dst=dst, card=card, timeout=12.0,
                    want_rate=self.src_key == "mic", ctx=st.get("ctx", "")):
                if first is None:
                    first = (time.time() - t0) * 1000.0
                if echo:
                    self.bus.upd(bseg, echo=True, note="翻译器回吐原文，已拦")
                    continue
                st["dst_acc"] = (st.get("dst_acc") or "") + piece
                if st.get("first_audio") is None:
                    st["first_audio"] = time.time()
                self.bus.upd(bseg, dst=st["dst_acc"],
                             state="mt", mt_ms=round(first or 0.0, 1))
                last = st.get("last_piece") or ""
                if last and util.similarity(piece, last) > 0.86:
                    self.log(f"{self.name} 跳过重复片段 '{piece[:12]}'", "warn", self.name)
                    continue
                st["last_piece"] = piece
                if self.speak and self.cfg.get("out_on", True):
                    say = piece
                    if lang_leak(say, dst):
                        # 半中半日的碎片绝不念出去 —— 对方听到的会是乱码。
                        # 先整句重翻一次救回来，救不回来就只留字幕、等收尾整句。
                        say = self._unleak(text, srcl, dst, card, say)
                        if not say:
                            continue
                    # 队列里已经压着碎片时不再排队：碎片晚 1.5 秒才念出去，
                    # 比等收尾整句一起念更糟（听感上就是「一句话被切成两半还错位」）。
                    if self._speak_q.qsize() >= int(self.cfg.get("speak_q_max", 1)):
                        if self.cfg.get("dbg_timing", False):
                            self.log(f"{self.name} 队列积压 {self._speak_q.qsize()} 段，跳过这片"
                                     f"（等收尾整句补）'{say[:12]}'", "info", self.name)
                    else:
                        rate = self._rate(st, rate_hint)
                        sid = bseg["id"]
                        self._pend[sid] = self._pend.get(sid, 0) + 1
                        st["spoken_acc"] = (st.get("spoken_acc") or "") + say
                        self._speak_q.put(SpeakTask(bseg, say, self.cfg.get("dst_lang", "ja"),
                                                    self.cfg.get("voice", ""), rate))
                        st["clauses"] += 1
        except Exception as e:                        # noqa: BLE001
            self.log(f"{self.name} 翻译失败：{e!r}", "err", self.name)
            self.bus.upd(bseg, state="err", note=f"翻译 {e!r}"[:80])
        finally:
            st["ctx"] = (st.get("ctx", "") + text)[-200:]
            if self.cfg.get("dbg_timing", False):
                self.log(f"{self.name} 气口 {util.char_count(text)}字 → {util.char_count(st['dst_acc'])}字"
                         f" · MT首包 {(first or 0):.0f}ms · 队列 {self._speak_q.qsize()}"
                         f" · '{text[:18]}'", "info", self.name)

    def _unleak(self, text, srcl, dst, card, piece):
        """碎片译文串台（假名少得不像目标语）时整句重翻一次；救不回来返回空串=不念。"""
        try:
            r = self.mt.translate(text, src=srcl, dst=dst, card=card, timeout=8.0)
            alt = util.clean_translation((r or {}).get("text") or "").strip()
        except Exception as e:                        # noqa: BLE001
            self.log(f"{self.name} 串台重翻失败：{e!r}", "warn", self.name)
            alt = ""
        if alt and not lang_leak(alt, dst):
            self.log(f"{self.name} 碎片串台（假名 {kana_ratio(piece):.2f}）→ 已整句重翻：{alt[:22]}",
                     "warn", self.name)
            return alt
        self.log(f"{self.name} 碎片串台且重翻无效，跳过出声（只留字幕）：{piece[:22]}",
                 "warn", self.name)
        return ""

    def _rate(self, st, chars_hint=None):
        """语速自适应：说得越快就越追；播放环积压了再加，让日语别越拉越远。"""
        from .mt import map_rate
        lo = float(self.cfg.get("rate_min", 0.92))
        hi = float(self.cfg.get("rate_max", 1.14))
        cps = None
        try:
            if chars_hint and st.get("dur"):
                cps = float(chars_hint) / max(0.4, float(st["dur"]))
        except Exception:
            cps = None
        base = map_rate(cps if cps is not None else 0, lo, hi)
        try:
            base *= float(self.cfg.get("speed", 1.0))     # 用户主观语速
        except Exception:
            pass
        try:
            b = float(self.player.backlog()) if self.player is not None else 0.0
        except Exception:
            b = 0.0
        boost = min(0.32, max(0.0, b - 0.7) * 0.14)
        return round(min(1.6, base + boost), 3)

    def _speak_loop(self):
        while self._run:
            try:
                task = self._speak_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if task is None or not self._run:
                continue
            try:
                self._utter(task)
            except Exception as e:                    # noqa: BLE001
                self.log(f"{self.name} 合成失败：{e!r}", "err", self.name)
                if task.seg:
                    self.bus.upd(task.seg, state="err", note=f"合成 {e!r}"[:80])

    def _utter(self, task: SpeakTask):
        st = None
        voice = task.voice or self.cfg.get("voice", "")
        lang = task.lang or self.cfg.get("dst_lang", "ja")
        # 语速微调：播放环积压越多念得越快（保证不越攒越慢）
        rate = task.rate
        if self.player is not None:
            backlog = self.player.tts.level() / 48000.0
            if backlog > 1.2:
                rate = max(rate, float(self.cfg.get("rate_max", 1.14)))
            elif backlog < 0.2:
                rate = min(rate, 1.0)
        t0 = time.time()
        if task.queued_at and self.cfg.get("dbg_timing", False):
            self.log(f"{self.name} 出队前排队 {(t0 - task.queued_at) * 1000:.0f}ms"
                     f" · '{task.text[:16]}'", "info", self.name)
        first = None
        total = 0
        pcm = bytearray()
        self.recent_tts.append((task.text, time.time()))
        self.recent_tts = self.recent_tts[-8:]
        note_spoken(task.text)
        seg = task.seg
        for chunk in self.tts.stream(task.text, voice, lang, rate=rate):
            if first is None:
                first = (time.time() - t0) * 1000.0
                if seg is not None:
                    sid = seg.get("id")
                    if self._fa.get(sid) is None:
                        self._fa[sid] = time.time()      # 第一声落地的绝对时间，只认第一次
                    self.bus.upd(seg, tts_ms=round(first, 1), state="speak")
                if self.player is not None:
                    self.player.push_tts_pcm(chunk, 24000)
                # chatbox 不在开口那一刻发：半截日文发出去会连成乱码，
                # 统一等整句定稿在 _finish 里发一次。
                pcm += chunk
            else:
                pcm += chunk
                if self.player is not None:
                    self.player.push_tts_pcm(chunk, 24000)
            total += len(chunk)
        dt = (time.time() - t0) * 1000.0
        if first is None:
            first = dt
        audio_ms = total / 2.0 / 24.0      # int16 24 kHz → ms
        if seg is not None:
            self.bus.upd(seg, tts_ms=round(first, 1),
                         audio_ms=round((seg.get("audio_ms") or 0) + audio_ms, 0))
            sid = seg["id"]
            if sid in self._pend:
                self._pend[sid] -= 1
                if self._pend[sid] <= 0 and seg.get("state") not in ("done",):
                    self._finish(seg)

    # ------------------------------------------------------------------ chatbox
    def _chatbox(self, text):
        """把这一句念的内容同步到 VRChat chatbox（只有 A 链有 osc）。"""
        if self.osc is None or not self.speak:
            return
        try:
            self.osc.send(text)
        except Exception:
            pass

    # ------------------------------------------------------------------ 回声
    def _is_echo(self, text, st=None, t1=None, audio=None) -> bool:
        """回声判定。原则：宁可漏判（多翻一句），也绝不误杀（用户重说一遍被吞掉）。

        1. **匹配滤波（主判据）**：把麦克风那一段的波形和我们刚放出去的音频做归一化互相关。
           真实漏音就是同一段波形（实测电学回路 0.98，阈值取 0.15），
           而「同一句话重说一遍」是另一场演奏
           （相关 <0.05）。旧版用「文本相似度 >0.6 算回声」，在中文模式下把用户的每一句
           复述都吃掉（实测「等待着下一个冬天的来临」连丢三次，连「什么叫疑似自己的回复啊」
           都被吃），这条判据已彻底删除。
        2. **假名闸**：A 链说中文、输出日语，麦克风却收到整串假名 → 只能是自己的日语漏回来。
        3. **B 链文本闸**：只听 VRChat 的那一路收到和我刚说过的句子几乎一样（≥0.85）
           → VRChat 把我自己的声音放回来了。
        """
        self._echo_last = None
        if not self.cfg.get("echo_guard", True):
            return False
        text = text or ""
        if audio is not None and st is not None \
                and callable(getattr(self, "echo_match", None)):
            try:
                corr, off, gap = self.echo_match(audio, float(st.get("t0") or 0.0),
                                                 float(t1 or time.time()))
            except Exception:                        # noqa: BLE001
                corr, off, gap = 0.0, 0.0, 0.0
            # gap = 麦克风能量 - 播放能量（dB）。纯回声时麦克风只会更小（负值）；
            # 我一开口，麦克风能量就明显超过播放能量（正值 +6 dB 以上）——
            # 那种情况哪怕波形有点像也是「我在说话 + 漏音」的混合，绝对不能丢。
            self._echo_last = (corr, off, gap, "wave")
            if corr >= 0.15 and gap < 6.0:
                self._echo_n = getattr(self, "_echo_n", 0) + 1
                if self._echo_n <= 5 or self._echo_n % 10 == 0:
                    self.log(f"{self.name} 回声（波形匹配 {corr:.2f}，偏移 {off * 1000:+.0f} ms，"
                             f"能量差 {gap:+.0f} dB）：{text[:18]}，累计 {self._echo_n}",
                             "info", self.name)
                return True
            elif corr >= 0.08 and gap < 6.0:
                # 介于「像」和「不像」之间：留证据方便按真实日志调阈值，不丢段
                self.log(f"{self.name} 回声边缘（波形匹配 {corr:.2f}，能量差 {gap:+.0f} dB）"
                         f"→ 保留：{text[:16]}", "info", self.name)
        if self.src_key == "mic" and str(self.cfg.get("dst_lang", "ja")) != "zh" \
                and util.has_kana(text) and kana_ratio(text) > 0.30 \
                and util.char_count(text) >= 2:
            self._echo_last = (0.0, 0.0, 0.0, "kana")
            self.log(f"{self.name} 回声（假名 {kana_ratio(text):.0%}，我只说中文）：{text[:18]}",
                     "info", self.name)
            return True
        if self.src_key != "mic":
            hit, sim, who = saying_echo(text)
            if hit and sim >= 0.85:
                self._echo_last = (sim, 0.0, 0.0, "btext")
                self.log(f"{self.name} 回声（{sim:.2f} 与刚才的输出重合）：{text[:18]}",
                         "info", self.name)
                return True
        return False

class OcrBridge:
    """把 ocr.OcrChain 的回调转成 C 链段。"""

    def __init__(self, cfg, bus, log=util.log):
        self.cfg = cfg
        self.bus = bus
        self.log = log
        self.seg = None
        self.key = None
        self.raw_hook = None        # 原文先浮（OCR 一出来就调，译文还没到）
        self.show_hook = None       # 译文到位后原地替换

    def on_raw(self, items):
        if callable(self.raw_hook):
            try:
                self.raw_hook(items)
            except Exception:                       # noqa: BLE001
                pass

    def on_text(self, key, src, dst, box, ms=None, mt_ms=None):
        """同一个位置文本不变就不刷屏；变了就原地更新同一段。"""
        if not src:
            return
        if callable(self.show_hook):
            try:
                self.show_hook(key, src, dst, box, mt_ms)
            except Exception:                       # noqa: BLE001
                pass
        if self.seg is not None and self.key == key:
            if self.seg.get("src") == src and self.seg.get("dst") == dst:
                return
            self.bus.upd(self.seg, src=src, dst=dst, state="done",
                         asr_ms=round(ms or 0, 1), mt_ms=round(mt_ms or 0, 1),
                         note=f"{key}")
            return
        seg = self.bus.new_seg("C", src_lang=self.cfg.get("ocr_src", "auto"), dst_lang="zh",
                               speaker="屏幕")
        self.seg = seg
        self.key = key
        self.bus.upd(seg, src=src, dst=dst, state="done", asr_ms=round(ms or 0, 1),
                     mt_ms=round(mt_ms or 0, 1), note=f"{key} {box}")

    def on_clear(self):
        self.seg = None
        self.key = None