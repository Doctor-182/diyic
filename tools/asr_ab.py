# -*- coding: utf-8 -*-
"""在自己的真实语音上 A/B 识别引擎。

前置：cfg.asr_capture = true，然后正常说话（A 链/ B 链都会把定稿段落存到 out/corpus/）。
用法：
    python tools/asr_ab.py sensevoice whisper-anime          # 比这两条
    python tools/asr_ab.py sensevoice cloud 20261120         # 指定日期
    python tools/asr_ab.py --list                            # 看引擎清单与注册表规格

输出：每条的耗时 + 互相相似度（谁跟谁一致），以及「跟当时线上那条引擎的差异」。
质量分不是重点——不一致的那几句才是要人耳判断的，脚本会把它们单独列出来。
"""
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from vrdub import util
from vrdub import corpus
from vrdub import asr_engines
from vrdub import cfg as cfgmod
from vrdub import net as netmod
from vrdub.asr import Transcriber


def load_wav(path):
    with wave.open(path, "rb") as z:
        raw = z.readframes(z.getnframes())
        sr = z.getframerate()
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    y = util.resample(x, sr, 16000) if sr != 16000 else x
    return (np.clip(y, -1, 1) * 32767).astype(np.int16)


def main(argv):
    if "--list" in argv or not argv:
        print("注册表（这台机器：%s）" % asr_engines.probe())
        for s in asr_engines.SPECS:
            ok, why = asr_engines.fit(s["id"])
            print("  %-14s %-6s %-16s %5d MB  vram %4d MB  %-6s  %s"
                  % (s["id"], s["kind"], "/".join(s["langs"])[:16], s["size_mb"],
                     s.get("vram_mb", 0), "可用" if ok else "不可用", why))
        print("\n".join("  " + l for l in asr_engines.describe()))
        return 0
    ids = [a for a in argv if a in asr_engines.BY_ID]
    day = next((a for a in argv if a.isdigit() and len(a) == 8), None)
    if len(ids) < 1:
        print("给 1–2 个引擎 id，或用 --list 看清单")
        return 2
    items = corpus.scan(day)
    if not items:
        print("没有语料。先把 cfg.asr_capture 设 true，正常说话攒一批（out/corpus/）。")
        return 1
    c = cfgmod.Cfg()
    net = netmod.Net("ali", c.host, c.key, slots=6)
    t = Transcriber(c, net, log=lambda *a, **k: None)
    if "whisper-anime" in ids:
        t.refiner()
    print("语料 %d 条%s\n" % (len(items), ("（%s）" % day) if day else ""))
    tot = {i: [] for i in ids}
    tot_ok = {i: 0 for i in ids}
    rows = []
    for wav, meta in items:
        a = load_wav(wav)
        lang = (meta.get("lang") or "zh").lower()
        if lang in ("auto", ""):
            lang = "zh"
        got = {}
        for eid in ids:
            t0 = time.time()
            try:
                if eid == "cloud":
                    txt = (t.cloud.transcribe(a) or {}).get("text") or ""
                elif eid == "whisper-anime":
                    txt, _ = t._ja().transcribe(a, lang)
                else:
                    txt = t.local.transcribe(a, lang)
            except Exception as e:                       # noqa: BLE001
                txt = "<%r>" % (e,)
            got[eid] = (txt, (time.time() - t0) * 1000.0)
            tot[eid].append(got[eid][1])
            if meta.get("text"):
                tot_ok[eid] += (util.similarity(txt, meta["text"]) >= 0.9)
        rows.append((wav, meta, got))
        print("%s  [%s/%.2fs/线上 %s]" % (os.path.basename(wav), lang, meta.get("dur", 0),
                                          meta.get("engine")))
        print("   线上: %s" % meta.get("text"))
        for eid in ids:
            print("   %-14s %5.0fms  %s" % (eid, got[eid][1], got[eid][0]))
        if len(ids) == 2:
            s = util.similarity(got[ids[0]][0], got[ids[1]][0])
            flag = "  ← 不一致，值得人耳听" if s < 0.9 else ""
            print("   两者相似度 %.3f%s" % (s, flag))
    print("\n== 汇总 ==")
    for eid in ids:
        ms = np.array(tot[eid])
        print("  %-14s 均值 %5.0f ms  p90 %5.0f ms  与线上一致 %d/%d"
              % (eid, ms.mean(), np.percentile(ms, 90), tot_ok[eid], len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
