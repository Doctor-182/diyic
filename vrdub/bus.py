# -*- coding: utf-8 -*-
"""事件总线：段状态、日志、电平、麦克风状态 → 网页 SSE / 浮窗 / 控制台。

订阅者各自一条有界队列，谁慢谁丢（丢的是旧帧，界面永远追最新状态）。
另外保存最近 400 段，供 /api/state 回填和评测统计。
"""
from __future__ import annotations

import itertools
import queue
import threading
import time

from . import util


class Bus:
    def __init__(self, keep=400):
        self._subs = {}
        self._ids = itertools.count(1)
        self._lk = threading.Lock()
        self.segs = []
        self.keep = keep
        self._by_id = {}
        self.seq = itertools.count(1)
        self.levels = {"A": -90.0, "B": -90.0}
        self.mic = {"on": True, "live": False, "dev": None, "reason": ""}
        self.status = {"state": "idle", "since": time.time(), "text": ""}

    # ------------------------------------------------------------------ 订阅
    def sub(self):
        q = queue.Queue(maxsize=512)
        with self._lk:
            sid = next(self._ids)
            self._subs[sid] = q
        return sid, q

    def unsub(self, sid):
        with self._lk:
            self._subs.pop(sid, None)

    def emit(self, kind, data=None):
        ev = {"k": kind, "t": int(time.time() * 1000)}
        if data:
            ev.update(data)
        with self._lk:
            for q in self._subs.values():
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(ev)
                    except Exception:
                        pass
        return ev

    # ------------------------------------------------------------------ 段
    def new_seg(self, chain, src="", src_lang="", dst_lang="", speaker="", engine=""):
        sid = f"{chain}-{next(self.seq)}"
        now = int(time.time() * 1000)
        seg = {"id": sid, "chain": chain, "ts": now, "state": "asr", "src": "",
               "dst": "", "src_lang": src_lang, "dst_lang": dst_lang,
               "asr_ms": 0, "mt_ms": 0, "tts_ms": 0, "audio_ms": 0, "e2e_ms": 0,
               "rtf": 0.0, "engine": engine, "note": "", "echo": False,
               "speaker": speaker, "t_start": time.time()}
        with self._lk:
            self._by_id[sid] = seg
            self.segs.append(seg)
            if len(self.segs) > self.keep:
                old = self.segs.pop(0)
                self._by_id.pop(old["id"], None)
        self.emit("seg", {"seg": self.pub(seg)})
        return seg

    def upd(self, seg, **kw):
        with self._lk:
            seg.update(kw)
        self.emit("seg", {"seg": self.pub(seg)})
        return seg

    def pub(self, seg):
        d = {k: v for k, v in seg.items() if not k.startswith("t_")}
        return d

    def get(self, sid):
        with self._lk:
            return self._by_id.get(sid)

    def recent(self, n=60):
        with self._lk:
            return [self.pub(s) for s in self.segs[-n:]]

    def clear(self):
        with self._lk:
            self.segs = []
            self._by_id = {}
        self.emit("clear", {})

    # ------------------------------------------------------------------ 电平 / 麦克风 / 状态
    def level(self, chain, db):
        self.levels[chain] = round(float(db), 1)

    def set_mic(self, **kw):
        self.mic.update(kw)
        self.emit("mic", dict(self.mic))

    def set_status(self, state, text=""):
        if self.status["state"] != state:
            self.status = {"state": state, "since": time.time(), "text": text}
        else:
            self.status["text"] = text
