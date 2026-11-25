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

        if silent >= float(self._p("end_silence", 0.5)) \
                or dur >= float(self._p("max_speech", 12)):
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

    def _reset(self):
        self.in_speech = False
        self.buf = []
        self.buf_n = 0
        self.pre = []
        self.spec_sent = False

    def flush(self):
        if self.in_speech and self.buf_n > int(self.sr * 0.3):
            self._emit(time.time(), "final", time.time() - self.t_on, final=True)
        self._reset()