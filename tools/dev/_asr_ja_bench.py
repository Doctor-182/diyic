# -*- coding: utf-8 -*-
"""日语 ASR 强化对比：SenseVoice vs anime-whisper int8 vs 云端。
变体：clean / mix（音乐床 SNR8）/ far（低通7k+混响+低电平）- 外加长段拼接。
用法: python tools/_asr_ja_bench.py [beam] [threads]
"""
import sys, os, time, wave, glob
sys.path.insert(0, r"D:\vrc无言势")
import numpy as np
from vrdub import cfg as cfgmod, net as netmod, asr as asrmod, util

BEAM = int(sys.argv[1]) if len(sys.argv) > 1 else 1
THREADS = int(sys.argv[2]) if len(sys.argv) > 2 else 4


def load16(f):
    with wave.open(f, "rb") as w:
        raw = w.readframes(w.getnframes()); sr = w.getframerate(); ch = w.getnchannels()
    a = np.frombuffer(raw, "<i2")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype("<i2")
    if sr != 16000:
        x = a.astype(np.float32) / 32768.0
        x = util.resample(x, sr, 16000)
        a = (np.clip(x, -1, 1) * 32767).astype("<i2")
    return a


def to_f(a):
    return a.astype(np.float32) / 32768.0


def to_i(x):
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


def rms_db(x):
    return 20 * np.log10(np.sqrt((x.astype(np.float64) ** 2).mean()) + 1e-9)


def norm_lvl(x, db=-20.0):
    return x * (10 ** ((db - rms_db(x)) / 20.0))


def reverb(x, rt60=0.35, sr=16000, seed=0):
    n = int(rt60 * sr)
    rng = np.random.default_rng(seed)
    ir = rng.standard_normal(n) * np.exp(-6.9 * np.arange(n) / n)
    ir[0] = 1.0
    ir = ir / np.abs(ir).sum() * 1.2
    y = np.convolve(x, ir)[:len(x)]
    return y / (np.abs(y).max() + 1e-9) * (np.abs(x).max() + 1e-9)


def music_bed(n, sr=16000, seed=1):
    """近似 VRChat 背景音乐：三个正弦和声 + 弱噪声鼓点。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / sr
    base = 220.0 * (2 ** (int(rng.integers(0, 5)) / 12.0))
    m = (0.5 * np.sin(2 * np.pi * base * t) + 0.3 * np.sin(2 * np.pi * base * 1.5 * t)
         + 0.2 * np.sin(2 * np.pi * base * 2.0 * t))
    beat = np.zeros(n)
    hop = int(0.5 * sr)
    for i in range(0, n, hop):
        ln = min(int(0.08 * sr), n - i)
        beat[i:i + ln] = rng.standard_normal(ln) * np.exp(-30 * np.arange(ln) / sr)
    return 0.7 * m + 0.3 * beat


def lowpass(x, fc=7000.0, sr=16000, taps=101):
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * fc / sr * n) * np.hamming(taps)
    h = h / h.sum()
    return np.convolve(x, h, mode="same")


def variant(x, kind):
    if kind == "clean":
        return x
    if kind == "mix":
        bed = music_bed(len(x))
        snr = rms_db(x) - rms_db(bed)
        return x + bed * (10 ** (-(8.0 + snr) / 20.0))
    if kind == "far":
        y = lowpass(reverb(x), 7000.0)
        y = norm_lvl(y, -22.0)
        y = y + np.random.default_rng(3).standard_normal(len(y)) * (10 ** (-55 / 20.0))
        return y
    raise ValueError(kind)


c = cfgmod.Cfg()
net = netmod.Net("ali", c.host, c.key, slots=4)
T = asrmod.Transcriber(c, net, util.log)

from faster_whisper import WhisperModel
print("载入 anime-whisper int8（threads=%d, beam=%d）…" % (THREADS, BEAM), flush=True)
t0 = time.time()
W = WhisperModel(os.path.join(r"D:\vrc无言势", "models", "anime-whisper-ct2-int8"),
                 device="cpu", compute_type="int8", cpu_threads=THREADS)
print("  载入 %.0f ms" % ((time.time() - t0) * 1000), flush=True)


def aw(x_f32, beam=None):
    beam = beam or BEAM
    t = time.time()
    segs, _ = W.transcribe(x_f32, language="ja", beam_size=beam, best_of=beam,
                           condition_on_previous_text=False, without_timestamps=True,
                           vad_filter=False, temperature=0.0, no_speech_threshold=0.6,
                           compression_ratio_threshold=2.4)
    txt = "".join(s.text for s in segs).strip()
    return util.clean_text(txt), (time.time() - t) * 1000.0


def cer(ref, hyp):
    ref = "".join(ref.split()); hyp = "".join(hyp.split())
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, hc in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1] / len(ref)


files = sorted(glob.glob(os.path.join(r"D:\vrc无言势", "out", "clips", "ja*.wav")))
base = [(os.path.basename(f), load16(f)) for f in files]
# 长段：两两拼接（模拟对方连说一句半以上）
longs = [("long%d" % i, np.concatenate([base[i][1], base[i + 1][1]])) for i in range(len(base) - 1)]
items = []
for name, a in base + longs:
    for kind in ("clean", "mix", "far"):
        items.append((name, kind, to_i(norm_lvl(variant(to_f(a), kind), -20.0))))

aw(to_f(base[0][1]))          # 预热
T.local.transcribe(base[0][1], "ja")

print("\n素材 %d 段（5 短 + 4 长）× 3 变体" % len(base + longs), flush=True)
res = {"sv": [], "aw": [], "cl": []}
per_variant = {}
for name, kind, a in items:
    x = to_f(a)
    dur = len(x) / 16000.0
    t = time.time(); sv = T.local.transcribe(a, "ja"); t_sv = (time.time() - t) * 1000
    awt, t_aw = aw(x)
    r = T.transcribe_cloud(a, "ja"); cl = r.get("text", ""); t_cl = r.get("ms") or 0.0
    per_variant.setdefault((name, kind), {})["ref"] = cl
    per_variant[(name, kind)].update(sv=(sv, t_sv), aw=(awt, t_aw), cl=(cl, t_cl, dur))
    res["sv"].append(t_sv); res["aw"].append(t_aw); res["cl"].append(t_cl)
    print("\n%s/%s %.2fs" % (name, kind, dur))
    print("  SV %5.0fms %r" % (t_sv, sv))
    print("  AW %5.0fms %r" % (t_aw, awt))
    print("  CL %5.0fms %r" % (t_cl, cl))
    sys.stdout.flush()

# 以同一条素材 clean 变体的云端结果为参照，算 CER
print("\n=== CER（参照=同素材 clean 云端文本）===")
agg = {"sv": [], "aw": []}
for name, _ in base + longs:
    ref = per_variant[(name, "clean")]["cl"][0]
    for kind in ("clean", "mix", "far"):
        p = per_variant[(name, kind)]
        c_sv = cer(ref, p["sv"][0]); c_aw = cer(ref, p["aw"][0])
        agg["sv"].append(c_sv); agg["aw"].append(c_aw)
        print("  %-6s %-5s SV %.3f  AW %.3f  %s" % (name, kind, c_sv, c_aw,
              "" if c_sv == c_aw else ("← AW 更好" if c_aw < c_sv else "← SV 更好")))

import statistics as st
print("\n=== 汇总 ===")
print("耗时 p50/p90: SV %.0f/%.0f | AW %.0f/%.0f | CL %.0f/%.0f"
      % (st.median(res["sv"]), np.percentile(res["sv"], 90),
         st.median(res["aw"]), np.percentile(res["aw"], 90),
         st.median(res["cl"]), np.percentile(res["cl"], 90)))
print("平均 CER: SV %.3f | AW %.3f" % (sum(agg["sv"]) / len(agg["sv"]), sum(agg["aw"]) / len(agg["aw"])))
durs = [per_variant[k]["cl"][2] for k in per_variant]
print("RTF(AW) p50 %.3f" % (st.median([per_variant[k]["aw"][1] / 1000.0 / per_variant[k]["cl"][2] for k in per_variant])))
