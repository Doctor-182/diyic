# -*- coding: utf-8 -*-
"""翻译：qwen3.6-flash 流式（首 token 250-450 ms）+ 子句切分 + 回声拦截。

上一代实测的两个真问题，这里都在应用层兜住：
1) 对个别日语句子模型会**原样返回原文**（改 prompt / temperature 都无效）。
   这里做两件事：检测到「输出≈输入且原文含假名」→ 换一个更硬的提示重试一次；
   仍然回声就标 echo=True，字幕打「疑似回声」，A 链不进 TTS（不能把日语念给日本人）。
2) 整句等翻译完再合成会白等 0.4-0.7 s。这里流式出子句，出一个送一个。
"""
from __future__ import annotations

import time

from . import persona, util

PATH = "/compatible-mode/v1/chat/completions"
_CLAUSE_END = "。！？!?…；;，,、：:"
_HARD = 44          # 一直没有标点时硬切，别让 TTS 干等


def split_clause(buf: str, hard: int = _HARD):
    """返回 buf 里第一个可切出的子句长度（含标点），没有就 None。"""
    for i, ch in enumerate(buf):
        if ch not in _CLAUSE_END:
            continue
        if i + 1 < 3:                 # 「嗯，」太短，等下一个
            continue
        return i + 1
    j = buf.find(".", 3)
    if 0 < j < hard and (j + 1 >= len(buf) or buf[j + 1] in " \n"):
        return j + 1
    if len(buf) >= hard:
        return hard
    return None


class Translator:
    def __init__(self, cfg, net, log=util.log):
        self.cfg = cfg
        self.net = net
        self.log = log

    def _body(self, text, src, dst, card, stream, temperature=0.25, ctx=""):
        sysp = persona.system_prompt(dst, card, src)
        if not sysp:
            return None
        return {
            "model": str(self.cfg.get("mt_model", "qwen3.6-flash")),
            "messages": [{"role": "system", "content": sysp},
                         {"role": "user", "content": persona.ctx_prompt(text, ctx, dst)}],
            "enable_thinking": False,
            "stream": bool(stream),
            "temperature": temperature,
        }

    # ---------------------------------------------------------------- 非流式
    def translate(self, text: str, src="auto", dst="zh", card="natural", timeout=10.0) -> dict:
        t0 = time.time()
        if dst in ("none", "", None) or not (text or "").strip():
            return {"text": text, "ms": 0.0, "echo": False}
        if (src and dst and src == dst and src != "auto"
                and util.lang_ok(text, dst)):
            # 源语=目标语（比如「中文模式」）：直通，别让模型把中文再「翻译」一遍。
            # 必须同时确认文本**真的**是目标语言——语向配错时（曾经 B 链写成 zh→zh）
            # 这里会把日语原样本地吐出去，等于整条链不翻译。
            return {"text": text, "ms": 0.0, "echo": False, "same_lang": True}
        if (src in ("auto", "", None) and util.lang_ok(text, dst)):
            # auto 模式下原文本来就是目标语言（A 链中文模式最常见）→ 直通，别过模型
            return {"text": text, "ms": 0.0, "echo": False, "same_lang": True}
        body = self._body(text, src, dst, card, False)
        j = self.net.post_json(PATH, body, timeout=timeout)
        out = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        out = util.clean_translation(out)
        echo = self._is_echo(text, out, src, dst)
        if echo:
            out2 = self._retry_echo(text, src, dst, card, timeout)
            if out2 and not self._is_echo(text, out2, src, dst):
                out, echo = out2, False
            else:
                out = out2 or out
        # 目标语校验：中文串进日语、英文串进中文字幕、原样吐回原文，都在这儿拦一次
        if out and not util.lang_ok(out, dst):
            alt = self._strict(text, src, dst, card, timeout)
            if alt and util.lang_ok(alt, dst):
                self.log(f"译文语言不对（{dst}）→ 已重翻：{out[:18]!r} → {alt[:18]!r}",
                         "warn", "mt")
                out = alt
            else:
                self.log(f"译文语言不对（{dst}）且重翻没救回来 → 这句不出：{out[:40]!r}",
                         "warn", "mt")
                return {"text": "", "ms": (time.time() - t0) * 1000.0,
                        "echo": False, "lang_fail": True, "raw": out}
        return {"text": out, "ms": (time.time() - t0) * 1000.0, "echo": echo}

    def _retry_echo(self, text, src, dst, card, timeout):
        """回声重试：提示词里点名「不要照抄原文」，temperature 提到 0.6 换条路径。"""
        sysp = persona.system_prompt(dst, card, src)
        if not sysp:
            return ""
        body = {
            "model": str(self.cfg.get("mt_model", "qwen3.6-flash")),
            "messages": [
                {"role": "system", "content": sysp + "\n注意：不要照抄原文，必须用目标语言重写。"},
                {"role": "user", "content": "请翻译成目标语言（不要原样返回）：\n" + text},
            ],
            "enable_thinking": False, "stream": False, "temperature": 0.6,
        }
        try:
            j = self.net.post_json(PATH, body, timeout=timeout)
            return util.clean_translation(((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        except Exception:
            return ""

    def _strict(self, text, src, dst, card, timeout=10.0):
        """目标语错了以后的一次强约束重翻：点名语言、禁英文、禁标签。"""
        sysp = persona.system_prompt(dst, card, src)
        if not sysp:
            return ""
        lang = persona.LANG_NAME.get(dst, dst)
        body = {
            "model": str(self.cfg.get("mt_model", "qwen3.6-flash")),
            "messages": [
                {"role": "system", "content": sysp + (
                    f"\n硬要求：输出必须 100% 是{lang}；不许保留原文里的词，"
                    f"不许夹英文，不许输出任何括号、标注、情绪标签或解释。")},
                {"role": "user", "content": persona.ctx_prompt(text, "", dst)},
            ],
            "enable_thinking": False, "stream": False, "temperature": 0.3,
        }
        try:
            j = self.net.post_json(PATH, body, timeout=timeout)
            return util.clean_translation(
                ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        except Exception:                       # noqa: BLE001
            return ""

    def _is_echo(self, src: str, dst: str, src_lang: str, dst_lang: str) -> bool:
        if not self.cfg.get("echo_guard", True):
            return False
        if not src or not dst:
            return False
        sim = util.similarity(src, dst)
        if sim < 0.90:
            return False
        if dst_lang == "zh" and util.has_kana(dst):
            return True                         # 日语原样回来了
        if dst_lang == "ja" and not util.has_kana(dst) and util.has_kana(src) is False:
            return sim > 0.92                   # 中文原样回来了
        return sim > 0.95

    # ---------------------------------------------------------------- 流式
    def stream_clauses(self, text: str, src="auto", dst="zh", card="natural",
                       timeout=12.0, want_rate=False, ctx=""):
        """逐个子句 yield (子句, 首包ms, 是否回声, 语速提示)。调用方拿到就送 TTS。"""
        if dst in ("none", "", None) or not (text or "").strip():
            if (text or "").strip():
                yield text, 0.0, False, None
            return
        if (src and dst and src == dst and src != "auto"
                and util.lang_ok(text, dst)):
            yield text, 0.0, False, None          # 同语言直通，不走模型
            return
        if src in ("auto", "", None) and util.lang_ok(text, dst):
            yield text, 0.0, False, None          # 原文已是目标语言 → 直通
            return
        body = self._body(text, src, dst, card, True, ctx=ctx)
        buf = ""
        t0 = time.time()
        first_ms = None
        rate_hint = self._rate_hint(text) if want_rate else None
        for j in _deltas(self.net.stream(PATH, body, timeout=timeout)):
            if first_ms is None:
                first_ms = (time.time() - t0) * 1000.0
            buf += j
            while True:
                k = split_clause(buf)
                if k is None:
                    break
                piece, buf = buf[:k], buf[k:]
                piece = util.clean_translation(piece)
                if piece and not util.lang_ok(piece, dst):
                    # 流式片段语言不对（中英日串台）：强约束重翻这一片，救不回来就不念
                    alt = self._strict(piece, src, dst, card, timeout)
                    if alt and util.lang_ok(alt, dst):
                        piece = alt
                    else:
                        self.log(f"片段语言不对（{dst}）→ 跳过不念：{piece[:24]!r}",
                                 "warn", "mt")
                        continue
                if piece:
                    yield piece, first_ms, self._is_echo(text, piece, src, dst), rate_hint
        if buf.strip():
            tail = util.clean_translation(buf)
            if tail and not util.lang_ok(tail, dst):
                alt = self._strict(tail, src, dst, card, timeout)
                if alt and util.lang_ok(alt, dst):
                    tail = alt
                else:
                    self.log(f"尾片语言不对（{dst}）→ 跳过不念：{tail[:24]!r}", "warn", "mt")
                    return
            if tail:
                yield tail, (first_ms or (time.time() - t0) * 1000.0), \
                    self._is_echo(text, tail, src, dst), rate_hint

    @staticmethod
    def _rate_hint(text: str):
        """源语速（字/秒）→ 给 TTS 的 rate 倍率。说得急就别念得慢吞吞。"""
        n = util.char_count(text)
        return n

    @staticmethod
    def speech_rate(chars: int, dur: float) -> float:
        if dur <= 0.2 or chars <= 0:
            return 0.0
        return chars / dur


def _deltas(chunks):
    buf = b""
    for ch in chunks:
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
            if s == "[DONE]":
                return
            if not s:
                continue
            try:
                import json
                j = json.loads(s)
            except Exception:
                continue
            for c in (j.get("choices") or []):
                d = (c.get("delta") or {}).get("content")
                if d:
                    yield d


def map_rate(chars_per_s: float, lo=0.92, hi=1.14) -> float:
    """4 字/秒 ≈ 正常；更快就加速，最多 hi。"""
    if not chars_per_s:
        return 1.0
    r = 1.0 + (chars_per_s - 4.2) * 0.06
    return max(lo, min(hi, r))