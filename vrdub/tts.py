# -*- coding: utf-8 -*-
"""合成：qwen-audio-3.0-tts-plus 流式（SSE，base64 PCM 24 kHz mono int16）。

首包 0.7–1.4 s 是服务端固定开销，切短句救不回来 —— 所以策略是
「越早发出第一个请求越好」，而不是把句子切碎。首包一到就写进播放队列。
"""
from __future__ import annotations

import base64
import time

from . import util

PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"
MODEL = "qwen-audio-3.0-tts-plus"


class Synth:
    def __init__(self, cfg, net, log=util.log):
        self.cfg = cfg
        self.net = net
        self.log = log

    def _body(self, text, voice, lang, rate=None, tags=None):
        # 最后一道闸：标注/情绪标签（(happy)…(end)、[笑]、*叹气*）一律不许进合成请求，
        # 否则 TTS 会把标签当正文念出来（实测念成 happy / end）。
        text = util.clean_translation(util.strip_anno(text or ""))
        if not text.strip():
            raise ValueError("空文本不给合成")
        spoken = text if not tags else (" ".join(tags) + " " + text)
        inp = {"text": spoken, "voice": voice, "format": "pcm", "sample_rate": 24000,
               "language_hints": [lang]}
        if rate is not None:
            inp["rate"] = float(rate)
        return {"model": MODEL, "input": inp}

    def stream(self, text: str, voice: str, lang: str = "ja", rate=None, tags=None,
               timeout: float = 25.0):
        """yield PCM bytes（24k int16 mono）。首个 chunk 到达即 yield。"""
        body = self._body(text, voice, lang, rate, tags)
        buf = b""
        for ch in self.net.stream(PATH, body, timeout=timeout,
                                  extra={"X-DashScope-SSE": "enable",
                                         "Accept": "text/event-stream"}):
            buf += ch
            while True:
                k = buf.find(b"\n")
                if k < 0:
                    break
                line, buf = buf[:k], buf[k + 1:]
                s = line.decode("utf-8", "replace").strip()
                if not s.startswith("data:"):
                    continue
                s = s[5:].strip()
                if not s or s == "[DONE]":
                    continue
                try:
                    import json
                    j = json.loads(s)
                except Exception:
                    continue
                a = (j.get("output") or {}).get("audio") or {}
                b64 = a.get("data")
                if b64:
                    yield base64.b64decode(b64)

    def synth(self, text: str, voice: str, lang: str = "ja", rate=None, tags=None,
              timeout: float = 30.0):
        """整段合成，返回 (pcm_bytes, 首包ms, 总ms)。试听/评测用。"""
        t0 = time.time()
        first = None
        out = bytearray()
        for b in self.stream(text, voice, lang, rate, tags, timeout):
            if first is None:
                first = (time.time() - t0) * 1000.0
            out += b
        return bytes(out), (first or (time.time() - t0) * 1000.0), (time.time() - t0) * 1000.0
