# -*- coding: utf-8 -*-
"""评测台：把真实音频按真实节拍喂进真链，让系统自己报数。

    python -m vrdub.bench --dev          设备闭环：TTS 播进虚拟线，再从虚拟线录回来
    python -m vrdub.bench --lat 5        延迟实测：A 链（中→日，出声）与 B 链（日→中，只出字幕）
    python -m vrdub.bench --all          两样都做，结果写 out/bench.md

素材优先用 out/clips/*.wav；没有就用 TTS 现合成并缓存（同一批素材反复测才有可比性）。
喂音频走 10 ms 一块的真实节拍，所以 VAD / 分句 / 并发都是真的在跑，不是离线跑批。
"""
from __future__ import annotations

import os
import statistics
import time
import wave

import numpy as np

from . import audio, bus as busmod, cfg as cfgmod
from . import chains, mt as mtmod, net as netmod, tts as ttsmod, util
from .asr import Transcriber
from .vad import Segmenter

ROOT = cfgmod.ROOT
OUT = os.path.join(ROOT, "out")
CLIPS = os.path.join(OUT, "clips")

ZH = [
    "今天有点晚了，我先去吃饭，回头再聊。",
    "这个模型的声音听起来比我预想的自然多了。",
    "你那边能听清吗？我刚把麦克风换了个位置。",
    "如果延迟还能再低一点，那就真的很像同传了。",
    "我在试最后一句话，看看断句会不会切错。",
]
JA = [
    "あ、もしもし。今日ちょっと遅れちゃうかも、先に始めてて。",
    "この声、思ってたよりずっと自然だね。",
    "そっちは聞こえてる？マイクの位置を変えてみたんだけど。",
    "もう少し遅れが少なくなったら、本当に同時通訳みたいだね。",
    "最後の文を試してるんだけど、区切りはうまくいってる？",
]


# ------------------------------------------------------------------ 素材
def _wav_write(path, pcm24):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm24)


def _wav_read(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, sr


def ensure_clips(synth, cfg, log=util.log, force=False):
    os.makedirs(CLIPS, exist_ok=True)
    voice = str(cfg.get("voice", ""))
    out = []
    for lang, arr in (("zh", ZH), ("ja", JA)):
        for i, text in enumerate(arr):
            p = os.path.join(CLIPS, f"{lang}{i}.wav")
            if force or not os.path.exists(p) or os.path.getsize(p) < 4000:
                pcm, first, total = synth.synth(text, voice, "zh" if lang == "zh" else "ja")
                if not pcm:
                    log("素材合成失败：" + text[:12], "warn", "bench")
                    continue
                _wav_write(p, pcm)
                log(f"素材 {lang}{i}.wav 合成 {len(pcm)/48000:.2f}s（首包 {first:.0f}ms）",
                    "info", "bench")
            out.append((lang, text, p))
    return out


# ------------------------------------------------------------------ 设备闭环
def dev_loop(cfg, log=util.log):
    """播进虚拟线 → 从虚拟线录回来，验证「对方的声音我到底能不能听到」这条物理链路。"""
    tests = [("CABLE 19→24（虚拟线闭环）", 19, 24),
             ("CABLE 20→27（16ch 虚拟线）", 20, 27),
             ("耳机 18→25（Loopback）", 18, 25),
             ("耳机 18→23（麦克风）", 18, 23)]
    voice = str(cfg.get("voice", ""))
    net = netmod.Net("ali", cfg.host, cfg.key, slots=2)
    synth = ttsmod.Synth(cfg, net)
    pcm, _, _ = synth.synth("设备闭环测试，一二三四五。", voice, "zh")
    res = []
    for name, out_dev, dev in tests:
        try:
            cap = []
            c = audio.Capture(dev, lambda b, r: cap.append(np.asarray(b, np.float32).copy()),
                              log=log, kind="probe")
            c.start()
            time.sleep(0.05)
            audio.play_pcm(out_dev, pcm, 24000, log=log)
            time.sleep(0.25)
            c.stop()
            if not cap:
                res.append({"path": name, "ok": False, "note": "没抓到任何数据"})
                continue
            x = np.concatenate(cap)
            peak = float(np.max(np.abs(x))) if x.size else 0.0
            db = 20 * np.log10(max(peak, 1e-9))
            dur = x.size / 48000.0
            res.append({"path": name, "ok": peak > 0.002, "note":
                        f"峰值 {peak:.3f}（{db:.1f} dB）· 录到 {dur:.2f}s"})
        except Exception as e:                                    # noqa: BLE001
            res.append({"path": name, "ok": False, "note": repr(e)[:70]})
    return res


# ------------------------------------------------------------------ 延迟实测
class Rig:
    """不带播放设备的真链：TTS 照常合成、照常计时，只是不写进任何声卡。"""

    def __init__(self, cfg, log=util.log):
        self.cfg = cfg
        self.log = log
        # 评测要量「真开口」，链路的 out_on/in_on 只是界面开关；这里临时打开，stop() 还原
        self._prev_on = (cfg.get("out_on"), cfg.get("in_on"))
        cfg.update({"out_on": True, "in_on": True})
        self.bus = busmod.Bus()
        self.net = netmod.Net("ali", cfg.host, cfg.key, slots=10)
        self.synth = ttsmod.Synth(cfg, self.net)
        self.asr = Transcriber(cfg, self.net, log)
        self.mt = mtmod.Translator(cfg, self.net, log)
        self.chains = {}
        for name, speak, src in (("A", True, "mic"), ("B", False, "loop")):
            ch = chains.StreamChain(name, speak, cfg, self.bus, self.asr, self.mt,
                                    self.synth, player=None, src_key=src, log=log)
            ch.start()
            self.chains[name] = ch
        self.asr.warm()

    def stop(self):
        for ch in self.chains.values():
            ch.stop()
        try:
            o, i = self._prev_on
            self.cfg.update({"out_on": o, "in_on": i})
        except Exception:
            pass


def _feed(seg, x48, log=util.log, tail=0.9):
    """按 10 ms 节拍把 48k 波形喂进 VAD，前后各留一点静音让端点检测自然触发。"""
    blk = 480
    pad = np.zeros(int(48000 * 0.35), np.float32)
    y = np.concatenate([pad, x48, np.zeros(int(48000 * tail), np.float32)])
    t0 = time.time()
    soft_end = None
    n = int(y.size // blk)
    for i in range(n):
        seg.feed(y[i * blk:(i + 1) * blk], 48000)
        if soft_end is None and i * blk >= pad.size + int(x48.size):
            soft_end = time.time()
        target = t0 + (i + 1) * 0.010
        dt = target - time.time()
        if dt > 0:
            time.sleep(dt)
    return soft_end or time.time(), n * 0.010


def lat_once(rig, chain_name, text, path, cfg, log=util.log, wait=26.0):
    ch = rig.chains[chain_name]
    x, sr = _wav_read(path)
    x48 = util.resample(x, sr, 48000) if sr != 48000 else x
    seg = Segmenter(cfg, ch.on_seg, log, name=chain_name)
    seg.thr = float(cfg.get("mic_db", -32))
    seq0 = rig.bus.seq.__reduce__()[1][0] if hasattr(rig.bus.seq, "__reduce__") else 0
    t_feed0 = time.time()
    t_end, dur = _feed(seg, x48, log)
    # 等这一轮所有段收口（done 且 e2e 有数；TTS 播完才算真完）
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.05)
        rows = [s for s in rig.bus.recent(40) if s["chain"] == chain_name
                and s["ts"] / 1000.0 >= t_feed0 - 1.0]
        if rows and all(s["state"] in ("err", "drop") or
                        (s["state"] == "done" and (s.get("e2e_ms") or 0) > 0) for s in rows):
            break
        if rows and all(s["state"] == "done" for s in rows) and time.time() > t_end + 4.0:
            break
    rows = [s for s in rig.bus.recent(40) if s["chain"] == chain_name
            and s["ts"] / 1000.0 >= t_feed0 - 1.0]
    rows.sort(key=lambda s: s["ts"])
    if not rows:
        log(f"{chain_name} 轮空：没有产生段（{text[:12]}）", "warn", "bench")
        return None
    # 一轮里可能切出好几段：取「第一段开口」的延迟，这才是体感延迟
    good = [r for r in rows if (r.get("e2e_ms") or 0) > 0] or rows
    s = good[0]
    # 下一句必须等这一句的合成+播放全部排空：否则上一句还在生成，下一句的
    # 「说完→开口」会把排队时间算进去（实测过：一句 2.7s 的音频能把下一句顶到 3s+）
    try:
        ch.wait_idle(30.0)
        time.sleep(0.8)
    except Exception:
        pass
    # recent() 给的是拷贝：整句重译在 wait_idle 之前可能还没落到 dst 上，
    # 必须排空后按 id 重新取一次，否则报告里会印半截译文（实测「…本当に」被截）
    fresh = rig.bus.get(s.get("id")) if s.get("id") else None
    if fresh is not None:
        s = rig.bus.pub(fresh)
    return {"chain": chain_name, "src": s.get("src", ""), "dst": s.get("dst", ""),
            "asr_ms": s.get("asr_ms") or 0.0, "mt_ms": s.get("mt_ms") or 0.0,
            "tts_ms": s.get("tts_ms") or 0.0, "audio_ms": s.get("audio_ms") or 0.0,
            "e2e_ms": s.get("e2e_ms") or 0.0, "open_ms": s.get("open_ms") or 0.0,
            "state": s.get("state"),
            "wall_ms": round((time.time() - t_end) * 1000.0, 1), "n_seg": len(rows)}


def pct(v, p):
    v = sorted(v)
    if not v:
        return 0.0
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


def report(rows, dev, cfg, log=util.log):
    os.makedirs(OUT, exist_ok=True)
    lines = ["# 评测报告 " + time.strftime("%Y-%m-%d %H:%M:%S"), ""]

    lines += ["## 设备闭环（播→录）", "", "| 路径 | 结果 | 说明 |", "|---|---|---|"]
    for r in dev:
        lines.append(f"| {r['path']} | {'通' if r['ok'] else '不通'} | {r['note']} |")
    lines.append("")

    lines += ["## 链路实测（真实节拍喂音频）", "",
              "| 链 | 原文 | 译文 | 识别 | 翻译 | 合成首包 | 音频 | 起话→开口 | 说完→开口 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['chain']} | {r['src'][:26]} | {r['dst'][:26]} | "
                     f"{r['asr_ms']:.0f} | {r['mt_ms']:.0f} | {r['tts_ms']:.0f} | "
                     f"{r['audio_ms']:.0f} | {r['open_ms']:.0f} | **{r['e2e_ms']:.0f}** |")
    lines.append("")
    for name in ("A", "B"):
        v = [r["e2e_ms"] for r in rows if r["chain"] == name and r["e2e_ms"] > 0]
        if not v:
            continue
        st = (f"**{name} 链 · 说完→开口**：p50 {pct(v,50):.0f} ms · p90 {pct(v,90):.0f} ms · "
              f"最快 {min(v):.0f} · 最慢 {max(v):.0f} · n={len(v)}")
        lines.append("- " + st)
    ov = [r["open_ms"] for r in rows if r.get("open_ms")]
    if ov:
        lines.append(f"- **起话→开口**：p50 {pct(ov,50):.0f} / p90 {pct(ov,90):.0f} ms（含说话时长）")
    va = [r["asr_ms"] for r in rows if r["asr_ms"] > 0]
    vm = [r["mt_ms"] for r in rows if r["mt_ms"] > 0]
    vt = [r["tts_ms"] for r in rows if r["tts_ms"] > 0]
    if va:
        lines.append(f"- 识别 p50 {pct(va,50):.0f} / p90 {pct(va,90):.0f} ms")
    if vm:
        lines.append(f"- 翻译 p50 {pct(vm,50):.0f} / p90 {pct(vm,90):.0f} ms")
    if vt:
        lines.append(f"- 合成首包 p50 {pct(vt,50):.0f} / p90 {pct(vt,90):.0f} ms")
    lines.append("")
    txt = "\n".join(lines)
    p = os.path.join(OUT, "bench.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    log("评测报告 " + p, "info", "bench")
    return txt


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    do_dev = "--dev" in argv or "--all" in argv
    do_lat = "--lat" in argv or "--all" in argv
    n = 5
    for i, a in enumerate(argv):
        if a == "--lat" and i + 1 < len(argv) and argv[i + 1].isdigit():
            n = int(argv[i + 1])
    cfg = cfgmod.Cfg()
    # 评测按交付默认跑：A 链本地优先（低延迟），B 链云端（准）。
    # 想量别的组合就加 --eng-a=local|hybrid|cloud / --eng-b=...
    cfg.update({"echo_guard": False, "live": True,
                "asr_engine": "hybrid", "asr_engine_a": "hybrid", "asr_engine_b": "cloud"})
    for a in argv:
        if a.startswith("--eng-a="):
            cfg.update({"asr_engine_a": a.split("=", 1)[1]})
        elif a.startswith("--eng-b="):
            cfg.update({"asr_engine_b": a.split("=", 1)[1]})
    print(f"[bench] 识别引擎 A={cfg.get('asr_engine_a')} B={cfg.get('asr_engine_b')}", flush=True)
    if "--dbg" in argv:
        cfg.update({"dbg_timing": True})
    dev = []
    rows = []
    if do_dev:
        dev = dev_loop(cfg)
    if do_lat:
        rig = Rig(cfg)
        try:
            clips = ensure_clips(rig.synth, cfg)
            use = [(l, t, p) for (l, t, p) in clips if l == "zh"][:n]
            usej = [(l, t, p) for (l, t, p) in clips if l == "ja"][:n]
            for lang, text, path in use:
                rig.chains["A"].wait_idle(15.0)
                r = lat_once(rig, "A", text, path, cfg)
                if r:
                    rows.append(r)
                    util.log(f"A 端到端 {r['e2e_ms']:.0f}ms · {r['dst'][:24]}", "info", "bench")
            for lang, text, path in usej:
                rig.chains["B"].wait_idle(15.0)
                r = lat_once(rig, "B", text, path, cfg)
                if r:
                    rows.append(r)
                    util.log(f"B 端到端 {r['e2e_ms']:.0f}ms · {r['dst'][:24]}", "info", "bench")
        finally:
            rig.stop()
    if do_dev or do_lat:
        print(report(rows, dev, cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())