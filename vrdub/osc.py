# -*- coding: utf-8 -*-
"""VRChat chatbox 输出（OSC over UDP 9000）。

实测边界（交接文档 docs/00-选材与技术路线 v0.3 §D6、docs/02）：
  /chatbox/input   sTT   只写，144 字符上限，5 条 / 5 秒，超了冷却 30 秒
所以这里做「滚动快照 + 令牌桶」：同一句只保留最新版本，限速发，超长截断。

只用标准库 socket，不依赖 python-osc。
"""
from __future__ import annotations

import socket
import struct
import threading
import time

MAX_CHARS = 144
BUCKET_N = 5
BUCKET_WIN = 5.0


# ---------------------------------------------------------------- OSC 编码
def _pad(b: bytes) -> bytes:
    return b + b"\x00" * (4 - len(b) % 4 if len(b) % 4 else 4)


def osc_message(addr: str, text: str = "", bools=()) -> bytes:
    """最小 OSC 编码：一个可选字符串 + 若干布尔（T/F，VRChat 认的就是这个）。"""
    tag_list = ","
    if text:
        tag_list += "s"
    for b in bools:
        tag_list += "T" if b else "F"
    out = _pad(addr.encode("utf-8")) + _pad(tag_list.encode("ascii"))
    if text:
        out += _pad(text.encode("utf-8"))
    return out


class OscChatbox:
    """限速 chatbox 发送器：latest-wins 快照 + 5/5s 令牌桶。"""

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda *a, **k: None)
        self.host = str(cfg.get("osc_host", "127.0.0.1"))
        self.port = int(cfg.get("osc_port", 9000))
        self.on = bool(cfg.get("chatbox_on", True))
        self.notify = bool(cfg.get("chatbox_notify", False))
        self._lock = threading.Lock()
        self._pending = None          # 等待发的最新文本
        self._sent = []               # 发送时间戳（令牌桶）
        self._stop = False
        self._thr = None
        self.stat = {"sent": 0, "drop": 0, "err": 0, "last": ""}

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self._thr is not None:
            return
        self._thr = threading.Thread(target=self._loop, name="osc-chatbox", daemon=True)
        self._thr.start()

    def stop(self):
        self._stop = True

    def refresh(self):
        """cfg 改了以后同步开关。"""
        self.on = bool(self.cfg.get("chatbox_on", True))
        self.notify = bool(self.cfg.get("chatbox_notify", False))

    # ------------------------------------------------------------ 对外
    def send(self, text: str, notify=None):
        """排一条 chatbox（同一时刻只保留最新一条，旧的丢掉）。"""
        if not self.on:
            return
        t = (text or "").strip()
        if not t:
            return
        if len(t) > MAX_CHARS:
            t = t[:MAX_CHARS - 1] + "…"
        if t == self.stat.get("last"):
            return
        with self._lock:
            if self._pending is not None:
                self.stat["drop"] += 1
            self._pending = t
        self.start()

    def typing(self, on: bool):
        if not self.on:
            return
        try:
            self._raw("/chatbox/typing", "", (bool(on),))
        except Exception:
            pass

    # ------------------------------------------------------------ 内部
    def _raw(self, addr, text, bools):
        data = osc_message(addr, text, bools)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(data, (self.host, self.port))
            self.stat["sent"] += 1
        finally:
            s.close()

    def _allow(self) -> bool:
        now = time.time()
        self._sent = [t for t in self._sent if now - t < BUCKET_WIN]
        if len(self._sent) >= BUCKET_N:
            return False
        self._sent.append(now)
        return True

    def _loop(self):
        while not self._stop:
            with self._lock:
                t = self._pending
                self._pending = None
            if t is None:
                time.sleep(0.05)
                continue
            if not self.on:
                continue
            if not self._allow():
                # 限速：留到下一轮（用最新的覆盖旧的，旧文本自然过期）
                with self._lock:
                    if self._pending is None:
                        self._pending = t
                time.sleep(0.25)
                continue
            try:
                self._raw("/chatbox/input", t, (True, self.notify))
                self.stat["last"] = t
                self.log("chatbox ← %s" % t[:40], "info", "osc")
            except Exception as e:                    # noqa: BLE001
                self.stat["err"] += 1
                self.log("chatbox 发送失败：%r" % (e,), "warn", "osc")
            time.sleep(0.02)
