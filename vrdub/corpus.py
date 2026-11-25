# -*- coding: utf-8 -*-
"""真实语音语料落盘：把每条定稿音频 + 识别文本存下来。

为什么需要它：项目里的「质量」数字如果只用 TTS 合成的干净素材量，永远是 1.00——
那测的是引擎读不读得懂清晰的合成音，不是「我在 VRChat 里说话它听不听得清」。
真正的判据只能用真人真麦真房间的音频。所以这里只负责把这样的片段攒下来：

  out/corpus/<YYYYMMDD>/<HHMMSS>_<lang>_<chain>.wav  16 kHz 单声道 int16
  out/corpus/<YYYYMMDD>/<HHMMSS>_<lang>_<chain>.json {text, engine, ms, lvl, dur, ...}

打开 cfg.asr_capture 之后，正常用就行；攒够一批再跑 tools/asr_ab.py 做 A/B。
"""
from __future__ import annotations

import json
import os
import time
import wave

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "corpus")
_n = 0


def save(pcm16, lang="auto", text="", engine="", chain="", **extra):
    """存一条。返回路径（失败返回空串——语料写不进去绝不能影响说话）。"""
    global _n
    try:
        a = np.asarray(pcm16).reshape(-1)
        if a.dtype.kind == "f":
            a = np.clip(a, -1, 1)
            a = (a * 32767.0).astype("<i2")
        else:
            a = a.astype("<i2")
        if a.size < 1600:                       # <0.1 s 不算语音
            return ""
        day = time.strftime("%Y%m%d")
        d = os.path.join(ROOT, day)
        os.makedirs(d, exist_ok=True)
        _n += 1
        stem = "%s_%s_%s_%02d" % (time.strftime("%H%M%S"), str(lang or "auto"),
                                  str(chain or "x"), _n % 100)
        wav = os.path.join(d, stem + ".wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(a.tobytes())
        meta = {"ts": round(time.time(), 3), "lang": lang, "engine": engine,
                "chain": chain, "text": text, "dur": round(a.size / 16000.0, 3),
                "wav": os.path.basename(wav)}
        meta.update({k: v for k, v in extra.items() if isinstance(v, (int, float, str, bool))})
        with open(os.path.join(d, stem + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        return wav
    except Exception:                            # noqa: BLE001
        return ""


def scan(day=None):
    """列出已存的片段 [(wav, meta)]，day=None 表示全部日期。"""
    out = []
    if not os.path.isdir(ROOT):
        return out
    days = [day] if day else sorted(os.listdir(ROOT))
    for d in days:
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            continue
        for f in sorted(os.listdir(p)):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(p, f), encoding="utf-8") as fh:
                    m = json.load(fh)
            except Exception:                    # noqa: BLE001
                continue
            w = os.path.join(p, m.get("wav") or (f[:-5] + ".wav"))
            if os.path.isfile(w):
                out.append((w, m))
    return out
