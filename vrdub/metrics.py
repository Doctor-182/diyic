# -*- coding: utf-8 -*-
"""报数：p50/p90/均值 + 实时率 + 回声率 + 失败段。质量要能看见，不能靠感觉。"""
from __future__ import annotations

import statistics


def pct(vals, p, keep_neg=False):
    """分位数。默认丢掉负值（各环节耗时不可能为负）；e2e 例外——负数代表「还没说完就开口」，
    是这套系统最想看到的数，必须进来，否则 p50 会被系统性高估。"""
    vals = [v for v in vals if v is not None and (keep_neg or v >= 0)]
    if not vals:
        return 0
    vals = sorted(vals)
    k = min(len(vals) - 1, max(0, int(round((p / 100.0) * (len(vals) - 1)))))
    return round(vals[k], 1)


def summarize(segs, chain=None):
    rows = [s for s in segs if (chain is None or s.get("chain") == chain)]
    done = [s for s in rows if s.get("state") in ("done", "refined")]
    e2e = [s["e2e_ms"] for s in done if s.get("e2e_ms")]
    asr = [s["asr_ms"] for s in rows if s.get("asr_ms")]
    mt = [s["mt_ms"] for s in rows if s.get("mt_ms")]
    tts = [s["tts_ms"] for s in rows if s.get("tts_ms")]
    aud = [s["audio_ms"] for s in rows if s.get("audio_ms")]
    vad = [s["vad_ms"] for s in rows if s.get("vad_ms")]
    cb = [s["cb_ms"] for s in rows if s.get("cb_ms")]
    opn = [s["open_ms"] for s in done if s.get("open_ms")]

    def avg(v):
        return round(sum(v) / len(v), 1) if v else 0

    chars = sum(len(s.get("dst") or "") for s in done)
    gen = sum((s.get("audio_ms") or 0) for s in done)
    pos = sum(max(0.0, v) for v in e2e)
    return {
        "n": len(rows),
        "done": len(done),
        "err": len([s for s in rows if s.get("state") == "err"]),
        "drop": len([s for s in rows if s.get("state") == "drop"]),
        "echo": len([s for s in rows if s.get("echo")]),
        "e2e_p50": pct(e2e, 50, keep_neg=True), "e2e_p90": pct(e2e, 90, keep_neg=True),
        "e2e_min": round(min(e2e), 1) if e2e else 0, "e2e_max": round(max(e2e), 1) if e2e else 0,
        "asr_p50": pct(asr, 50), "asr_p90": pct(asr, 90),
        "mt_p50": pct(mt, 50), "mt_p90": pct(mt, 90),
        "tts_p50": pct(tts, 50), "tts_p90": pct(tts, 90),
        # 分段阀本身的固定开销（段尾 → 定稿）与首声（起话 → 第一声）：
        # 这两个数才说明「延迟阀拧到哪里」，前面的 asr/mt/tts 只是零件。
        "vad_p50": pct(vad, 50), "vad_p90": pct(vad, 90),
        "cb_p50": pct(cb, 50), "cb_p90": pct(cb, 90),
        "open_p50": pct(opn, 50, keep_neg=True), "open_p90": pct(opn, 90, keep_neg=True),
        "asr_avg": avg(asr), "mt_avg": avg(mt), "tts_avg": avg(tts),
        "e2e_avg": avg(e2e), "vad_avg": avg(vad), "cb_avg": avg(cb),
        "open_avg": avg(opn),
        "audio_p50": pct(aud, 50),
        "chars": chars, "audio_s": round(gen / 1000.0, 1),
        # 负 e2e 段（说完之前就开口）不参与这两项：它们算的是「为产出 1 s 音频花了多少延迟」
        "rtf": round((pos / 1000.0) / (gen / 1000.0), 3) if (pos and gen) else 0.0,
        "cps": round(chars / (pos / 1000.0), 2) if (chars and pos) else 0.0,
    }
