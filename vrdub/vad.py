# -*- coding: utf-8 -*-
"""端点检测 + 断句。目标是「说完最后一个字之后 0.5 s 内把请求发出去」，
而不是等一整句语法完成 —— 延迟的大头在这里，不在网络。

三个出口（同一个 Segmenter）：
  spec  —— 静音满 cfg.spec_silence(0.18 s)：拿当前缓冲投机提交云端识别，猜错就算了，
           它只是提前把 RTT 摊开；本地识别此时已经出字，字幕不会空着。
  final —— 静音满 cfg.end_silence(0.5 s)：正常收尾，走完整链路。
  cut   —— 连续说话超过 cfg.max_speech：强制切一刀，防止一句话憋 20 秒。
"""
from __future__ import annotations

import time

import numpy as np

from . import util


class Decimator:
    """48k → 16k 的整数 3 倍抽取（带宽平均），跨块连续无接缝。"""

    def __init__(self, factor=3):
        self.f = factor
        self.acc = np.zeros(0, np.float32)

    def push(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float32)
        if x.ndim > 1:
            x = x.mean(axis=1)
        if self.f == 1:
            return x
        self.acc = np.concatenate([self.acc, x]) if self.acc.size else x
        n = (self.acc.size // self.f) * self.f
        if n <= 0:
            return np.zeros(0, np.float32)
        body = self.acc[:n].reshape(-1, self.f).mean(axis=1)
        self.acc = self.acc[n:]
        return body.astype(np.float32)


class Segmenter:
    def __init__(self, cfg, on_seg, log=util.log, name="A"):
        self.cfg = cfg
        self.on_seg = on_seg
        self.log = log
        self.name = name            # "A"/"B"：允许 max_speech_b 之类按链覆盖的键
        self.dec = Decimator(3)
        self.sr = 16000
        self.hop = 160                      # 10 ms
        self.pre = []                       # 预滚（16k 块）
        self.pre_ms = 250
        self.buf = []                       # 当前段的 16k 块
        self.buf_db = []                    # 与 buf 一一对应的块电平（找谷用）
        self.buf_n = 0
        self.in_speech = False
        self.t_on = 0.0
        self.last_voice = 0.0
        self.spec_sent = False
        self.last_partial = 0.0
        self.floor = -70.0
        self.db = -90.0
        self.thr = -32.0
        self.on_level = None
        self.dropped = 0

    # ------------------------------------------------------------------ 参数
    def _p(self, key, default):
        """按链覆盖地取参数：先看 <key>_a / <key>_b，没有就用全局 <key>。"""
        v = self.cfg.get(f"{key}_{str(self.name).lower()}", None)
        if v is None or v == "":
            v = self.cfg.get(key, default)
        return v

    def cfg_changed(self):
        self.thr = float(self.cfg.get("mic_db", -32))

    # ------------------------------------------------------------------ 主入口
    def feed(self, block, rate=48000):
        x16 = self.dec.push(block if rate == 48000 else util.resample(block, rate, 16000))
        if x16.size == 0:
            return
        now = time.time()
        rms = float(np.sqrt(np.mean(x16.astype(np.float64) ** 2)) + 1e-9)
        db = 20.0 * np.log10(rms)
        self.db = db
        if self.on_level:
            self.on_level(db)
        thr = self.thr
        loud = db > thr
        keep = db > (thr - 7.0)

        if not self.in_speech:
            # 噪声底噪跟踪（只在非语音时往下走）
            if db < self.floor + 3:
                self.floor = 0.97 * self.floor + 0.03 * db
            self.pre.append(x16)
            keep_n = int(self.pre_ms / 10)
            if len(self.pre) > keep_n:
                self.pre = self.pre[-keep_n:]
            if loud:
                self.in_speech = True
                self.t_on = now
                self.last_voice = now
                self.buf = list(self.pre)
                self.buf_n = sum(b.size for b in self.buf)
                self.pre = []
                self.spec_sent = False
                self.last_partial = now
                self.voice_run = 1
            return

        self.buf.append(x16)
        self.buf_db.append(db)
        self.buf_n += x16.size
        if keep:
            self.last_voice = now
        dur = (now - self.t_on)
        silent = now - self.last_voice

        # 流式部分结果：只要还在说话，就按 partial_every 节奏把当前缓冲送一次，
        # 让「子句一说出口」就能被提交翻译（同传靠的就是这个）。
        every = float(self.cfg.get("partial_every", 0.7) or 0)
        if every > 0 and dur >= 0.45 and now - self.last_partial >= every:
            self.last_partial = now
            self._emit(now, "spec", dur, final=False)
            return

        max_dur = float(self._p("max_speech", 12))
        if silent >= float(self._p("end_silence", 0.5)):
            self._emit(now, "final", dur, final=True)
            self._reset()
            return
        if dur >= max_dur:
            # 硬到上限时别按时间一刀切：回看最近 valley_win 秒找电平最低处切，
            # 前半段收走，后半段留在缓冲里接着用（避免把词切成两半）。
            if bool(self.cfg.get("valley_cut", False)) and self._cut_at_valley(now, dur):
                return
            self._emit(now, "final", dur, final=True)
            self._reset()
            return

    def _emit(self, now, kind, dur, final):
        audio = np.concatenate(self.buf) if self.buf else np.zeros(0, np.float32)
        audio = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")   # 交给识别层的统一是 int16
        if audio.size < int(self.sr * float(self._p("min_speech", 0.35))):
            if final:
                self.dropped += 1
            return
        # 能量闸：整段几乎没声音（数字静音 / 底噪）就别送识别 ——
        # 实测静音送云端会「听出」一句根本不存在的中文，比漏一段更糟。
        f = audio.astype(np.float32) / 32768.0
        peak = float(np.percentile(np.abs(f), 95)) if f.size else 0.0
        lvl = 20.0 * np.log10(peak + 1e-9)
        # 占空比闸：单个咔哒/敲键盘也能把 95 分位峰值顶过门限，但它只有一两帧响，
        # 真正的说话会持续几十帧。实测「明明没声音却听出个 うん」都栽在这。
        act_ms = int(self.cfg.get("active_ms", 120))
        need_ratio = float(self.cfg.get("speech_ratio", 0.12))
        nfr = audio.size // 160
        act = 0
        if nfr > 0:
            a = audio[:nfr * 160].astype(np.float32).reshape(nfr, 160) / 32768.0
            fdb = 20.0 * np.log10(np.sqrt((a ** 2).mean(axis=1)) + 1e-9)
            act = int((fdb > self.thr).sum())
        total_ms = audio.size / 16.0
        if act * 10 < act_ms or (total_ms > 0 and act * 10.0 / total_ms < need_ratio):
            if final:
                self.dropped += 1
                self._thin = getattr(self, "_thin", 0) + 1
                if self._thin <= 3 or self._thin % 20 == 0:
                    self.log(f"弃段：有效发声只有 {act * 10} ms / {total_ms:.0f} ms"
                             f"（门限 {act_ms} ms、占空 {need_ratio:.0%}，已弃 {self._thin} 段）",
                             "info", "vad")
            return
        if lvl < self.thr + float(self.cfg.get("mic_gate_db", 6.0)):
            if final:
                self.dropped += 1
                self._quiet = getattr(self, "_quiet", 0) + 1
                # 静音期每个门限抖动都会弃一段，头 3 次说清楚就够，之后每 20 次提一次，
                # 不然日志把真正有用的识别/翻译信息冲出屏幕
                if self._quiet <= 3 or self._quiet % 20 == 0:
                    self.log(f"太安静弃段（{lvl:.0f} dBFS < {self.thr + 6.0:.0f}，"
                             f"已弃 {self._quiet} 段）", "info", "vad")
            return
        try:
            self.on_seg({"kind": kind, "t0": self.t_on, "t1": now, "dur": dur,
                         "audio": audio, "rms": self.db, "lvl": lvl, "final": final})
        except Exception as e:                    # noqa: BLE001
            self.log(f"分段回调异常：{e!r}", "err", "vad")

    # ---------------------------------------------------------------- 谷切
    def _cut_at_valley(self, now, dur):
        """在最近 valley_win 秒里找一个「谷」把段切开。

        判据：谷的电平至少比该窗口的中位数低 valley_gap dB，且切点之后还剩
        ≥ min_speech 秒（否则不如干脆整段收掉）。找到就：前半段 emit，后半段
        留在 buf 里当新段的开头。
        """
        if not self.buf or not self.buf_db:
            return False
        win = float(self.cfg.get("valley_win", 2.5))
        gap = float(self.cfg.get("valley_gap", 6.0))
        need = float(self._p("min_speech", 0.35))
        # 每块时长（秒）与累计时间轴
        total = self.buf_n / float(self.sr)
        if total <= need * 2:
            return False
        start_t = total - win
        med = float(np.median(self.buf_db))
        best_i, best_db, acc = -1, 1e9, 0.0
        for i, b in enumerate(self.buf):
            seg_t = b.size / float(self.sr)
            mid = acc + seg_t * 0.5
            acc += seg_t
            if mid < start_t:
                continue
            if acc + 1e-9 >= total - need and best_i >= 0:
                break                      # 尾巴不够 min_speech 了，别再往右找
            if self.buf_db[i] < best_db:
                best_i, best_db = i, self.buf_db[i]
        if best_i <= 0 or best_db > med - gap:
            return False
        head = self.buf[:best_i + 1]
        tail = self.buf[best_i + 1:]
        if not tail:
            return False
        f = (np.concatenate(head) if head else np.zeros(0, np.float32))
        f = (np.clip(f, -1.0, 1.0) * 32767.0).astype("<i2")
        if f.size < int(self.sr * need):
            return False
        peak = float(np.percentile(np.abs(f.astype(np.float32) / 32768.0), 95))
        lvl = 20.0 * np.log10(peak + 1e-9)
        hdur = f.size / float(self.sr)          # 前半段自己的时长（含预滚）
        if lvl >= self.thr + float(self.cfg.get("mic_gate_db", 6.0)):
            try:
                self.on_seg({"kind": "final", "t0": self.t_on, "t1": now, "dur": hdur,
                             "audio": f, "rms": self.db, "lvl": lvl, "final": True})
            except Exception as e:                 # noqa: BLE001
                self.log(f"分段回调异常（谷切）：{e!r}", "err", "vad")
        self.buf = list(tail)
        self.buf_db = list(self.buf_db[best_i + 1:])
        self.buf_n = sum(b.size for b in self.buf)
        self.t_on = now - self.buf_n / float(self.sr)
        self.last_voice = now
        self.log(f"谷切：{dur:.1f}s 处切在 {best_db:.0f} dB（中位 {med:.0f} dB），"
                 f"留下 {self.buf_n / 16000.0:.2f}s 接着用", "info", "vad")
        return True

    def _reset(self):
        self.in_speech = False
        self.buf = []
        self.buf_db = []
        self.buf_n = 0
        self.pre = []
        self.spec_sent = False

    def reset(self):
        """外部把这一段作废（关链路 / 关总闸 / 换设备）：清段内缓冲与预滚。

        只调 _reset 不够：_reset 是「说完一句」的正常收尾，语义上不清预滚也行；
        但关链路时不能让半截声音留在 pre 里，下一句会被拼到旧内容上。
        """
        self._reset()
        self.pre = []

    def flush(self):
        if self.in_speech and self.buf_n > int(self.sr * 0.3):
            self._emit(time.time(), "final", time.time() - self.t_on, final=True)
        self._reset()