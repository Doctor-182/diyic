# -*- coding: utf-8 -*-
"""识别：云端 qwen-audio-3.0-asr-flash（准）+ 本地 SenseVoice-Small int8（快）。

为什么要两套：
  云端 p50 ≈ 0.7-1.3 s、p90 ≈ 2-3 s，尾部长，而且抖动跟网络无关（是服务端排队）。
  本地 SenseVoice 在干净近讲上 ≈ 60-120 ms，没有尾部。
  于是「hybrid」：本地先出结果把链路推起来，云端回来若明显不同再校正（
  字幕可以改；已经出声的 A 链不追改，只在 HUD 标注）。
"""
from __future__ import annotations

import io
import os
import re
import threading
import time
import wave

import numpy as np

from . import util
from .asr_whisper import WhisperASR
from . import asr_engines

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SV_DIR = os.path.join(ROOT, "models", "sensevoice")
ASR_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
ASR_MODEL = "qwen-audio-3.0-asr-flash"


def _loud_enough(pcm16, min_sec=0.45, min_peak=1000) -> bool:
    """这段值不值得送去精修：太短或几乎没振幅的直接放过。

    为什么：B 链在安静期会被 VAD 切出 0.2–0.5 s 的空段，云端对这些统一回
    HTTP 400（实测日志「云端识别跳过非语音片段」），而我们还会为它把 1.1 GB 的
    本地 whisper 拉起来、跑完再释放——纯浪费显存和一次 RTT。
    """
    try:
        x = np.frombuffer(pcm16, np.int16) if isinstance(pcm16, (bytes, bytearray)) else np.asarray(pcm16)
        if x.size < int(16000 * float(min_sec)):
            return False
        return int(np.abs(x.astype(np.int32)).max()) >= int(min_peak)
    except Exception:                                  # noqa: BLE001
        return True


# ---------------------------------------------------------------- 本地
_LANG = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12}


class LocalASR:
    """SenseVoice-Small int8 / onnxruntime。前端逐项对齐 Kaldi fbank：

    16k / 25ms 窗 / 10ms 移 / 去直流 / 预加重 0.97 / povey 窗 / 512 FFT /
    80 mel（low 20Hz）/ 功率谱 / ln → LFR(7,6) → CMVN（模型 metadata）。
    任何一项改动都要重新对云端结果验证。
    """

    def __init__(self, log=util.log):
        self.log = log
        self._sess = None
        self._tok = {}
        self._meta = {}
        self._mel = None
        self._lk = threading.Lock()
        self.note = "未加载"

    def ready(self) -> bool:
        self._load()
        return self._sess is not None

    @property
    def loaded(self) -> bool:
        """已经载入内存了吗（只看状态，不触发加载）。"""
        return self._sess is not None

    def unload(self) -> bool:
        """放掉 228MB 的本地模型（内存紧张或纯云端模式下用；下次用到会自动重载）。"""
        with self._lk:
            if self._sess is None:
                return False
            self._sess = None
            self._mel = None
            self.note = "已卸载"
        try:
            import gc
            gc.collect()
        except Exception:                       # noqa: BLE001
            pass
        return True

    def _load(self):
        if self._sess is not None:
            return
        with self._lk:
            if self._sess is not None:
                return
            try:
                import onnxruntime as ort
            except Exception as e:          # noqa: BLE001
                self.note = f"onnxruntime 不可用：{e!r}"
                return
            m = os.path.join(SV_DIR, "model.int8.onnx")
            t = os.path.join(SV_DIR, "tokens.txt")
            if not (os.path.exists(m) and os.path.exists(t)):
                self.note = f"模型缺失：{m}"
                return
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(2, min(6, (os.cpu_count() or 4) - 2))
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            t0 = time.time()
            self._sess = ort.InferenceSession(m, sess_options=so, providers=["CPUExecutionProvider"])
            md = self._sess.get_modelmeta().custom_metadata_map
            self._meta = {
                "neg_mean": np.fromstring(md["neg_mean"].strip("[]"), sep=",", dtype=np.float32),
                "inv_stddev": np.fromstring(md["inv_stddev"].strip("[]"), sep=",", dtype=np.float32),
                "lfr_m": int(md.get("lfr_window_size", 7)),
                "lfr_n": int(md.get("lfr_window_shift", 6)),
                "itn": int(md.get("with_itn", 14)),
                "noitn": int(md.get("without_itn", 15)),
            }
            with open(t, encoding="utf-8") as fh:
                for line in fh:
                    p = line.rstrip("\n").split(" ")
                    if len(p) == 2:
                        self._tok[int(p[1])] = p[0]
            self._mel = _mel_fb(80, 16000, 512, 20.0)
            self.note = f"ok 载入 {int((time.time() - t0) * 1000)}ms"
            self.log(self.note, "info", "asr.local")

    def transcribe(self, pcm16: np.ndarray, lang: str = "auto") -> str:
        if not self.ready():
            return ""
        feats = _fbank(pcm16, self._mel)
        if feats is None:
            return ""
        f = _lfr(feats, self._meta["lfr_m"], self._meta["lfr_n"])
        f = (f + self._meta["neg_mean"][: f.shape[1]]) * self._meta["inv_stddev"][: f.shape[1]]
        out = self._sess.run(["logits"], {
            "x": f[None, :, :].astype(np.float32),
            "x_length": np.array([f.shape[0]], dtype=np.int32),
            "language": np.array([_LANG.get(lang, 0)], dtype=np.int32),
            "text_norm": np.array([self._meta["noitn"]], dtype=np.int32),
        })[0]
        ids = out[0].argmax(axis=1)
        keep, prev = [], -1
        for i in ids:
            i = int(i)
            if i != prev and i != 0:
                keep.append(i)
            prev = i
        if not keep:
            return ""
        txt = "".join(self._tok.get(i, "") for i in keep)
        txt = re.sub(r"<\|[^|]*\|>", "", txt)          # 语言/情感/事件标签
        txt = txt.replace("▁", " ").replace("<unk>", "")
        txt = re.sub(r"(?<=[\u3040-\u30ff\u4e00-\u9fff\u3005-\u3007])\s+"
                     r"(?=[\u3040-\u30ff\u4e00-\u9fff\u3005-\u3007])", "", txt)
        return util.clean_text(txt)


def _mel_fb(n_mels=80, sr=16000, nfft=512, low=20.0, high=None):
    high = high or sr / 2.0
    m2f = lambda m: 700.0 * (np.exp(m / 1127.0) - 1.0)          # noqa: E731
    f2m = lambda f: 1127.0 * np.log(1.0 + f / 700.0)             # noqa: E731
    pts = m2f(np.linspace(f2m(low), f2m(high), n_mels + 2))
    bins = np.floor((nfft + 1) * pts / sr).astype(int)
    fb = np.zeros((n_mels, nfft // 2 + 1), np.float32)
    for i in range(n_mels):
        l, c, r = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        if r <= l:
            r = l + 1
        for j in range(l, c):
            fb[i, j] = (j - l) / max(1, c - l)
        for j in range(c, r):
            fb[i, j] = (r - j) / max(1, r - c)
    return fb


def _fbank(pcm16, mel, win=400, hop=160, nfft=512):
    x = np.asarray(pcm16, dtype=np.float32) / 32768.0
    if x.size < win + hop * 10:
        return None
    n = 1 + (x.size - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.hstack([fr[:, :1], fr[:, 1:] - 0.97 * fr[:, :-1]])
    w = np.power(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(win) / (win - 1)), 0.85)
    sp = np.abs(np.fft.rfft(fr * w[None, :], nfft)) ** 2
    return np.log(np.maximum(sp @ mel.T, 1e-10)).astype(np.float32)


def _lfr(feats, m=7, n=6):
    T = feats.shape[0]
    if T == 0:
        return feats
    left = (m - 1) // 2
    f = np.pad(feats, ((left, m), (0, 0)), mode="edge")
    rows = [f[i:i + m].reshape(-1) for i in range(0, T, n)]
    return np.asarray(rows, dtype=np.float32)


# ---------------------------------------------------------------- 云端
def as_i16(pcm) -> np.ndarray:
    """统一成 int16：浮点（-1..1）自动放大，已是整数原样不动。"""
    a = np.asarray(pcm)
    if a.dtype == np.int16:
        return a
    if a.dtype.kind == "f":
        return (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2")
    return a.astype("<i2")


def wav_bytes(pcm16: np.ndarray, sr=16000) -> bytes:
    pcm16 = as_i16(pcm16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.asarray(pcm16, dtype="<i2").tobytes())
    return buf.getvalue()


class CloudASR:
    def __init__(self, net, log=util.log, name=""):
        self.net = net
        self.log = log
        self.name = name

    # 太短的片段云端直接 400（实测 0.1 s 音频 → HTTP 400 空包），本地也没必要送
    MIN_SEC = 0.22
    MIN_PEAK = 400        # int16 峰值下限（≈ -38 dBFS），低于它不可能有语音

    def transcribe(self, pcm16: np.ndarray, timeout=12.0) -> dict:
        import base64
        pcm16 = as_i16(np.asarray(pcm16).reshape(-1))
        dur = len(pcm16) / 16000.0
        if dur < self.MIN_SEC:
            return {"text": "", "ms": 0.0, "words": [], "dur": dur, "raw": {}, "skip": "short"}
        # 静音/纯噪声送上去必定 400（实测 silence/noise/tone 全部 400 空包），
        # 既浪费 0.4-1.8 s 又刷错误日志，本地先卡一道电平门。
        peak = int(np.abs(pcm16).max()) if len(pcm16) else 0
        if peak < self.MIN_PEAK:
            return {"text": "", "ms": 0.0, "words": [], "dur": dur, "raw": {},
                    "skip": "quiet", "peak": peak}
        b64 = base64.b64encode(wav_bytes(pcm16)).decode()
        body = {
            "model": ASR_MODEL,
            "input": {"messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + b64}},
            ]}]},
            "parameters": {"format": "wav", "sample_rate": "16000"},
        }
        t0 = time.time()
        try:
            j = self.net.post_json(ASR_PATH, body, timeout=timeout)
        except Exception as e:                       # noqa: BLE001
            # 云端识别失败不能把整段链路带走：这一次当没识别到（本地草稿已经在字幕上了）
            msg = str(e)
            if "400" in msg:            # 非语音片段被服务端拒收：不算链路故障
                self._nonspeech = getattr(self, "_nonspeech", 0) + 1
                if self._nonspeech <= 3:
                    self.log("云端识别跳过非语音片段（第 %d 次）：%s"
                             % (self._nonspeech, msg[:100]), "info", "asr")
            else:
                self._errs = getattr(self, "_errs", 0) + 1
                if self._errs <= 5 or self._errs % 20 == 0:
                    self.log("云端识别失败（第 %d 次，已降级为只用本地字幕）：%s"
                             % (self._errs, msg[:120]), "warn", "asr")
            return {"text": "", "ms": (time.time() - t0) * 1000.0, "words": [],
                    "dur": dur, "raw": {}, "err": str(e)[:200]}
        dt = (time.time() - t0) * 1000.0
        out = j.get("output") or {}
        sen = out.get("sentence") or {}
        text = (sen.get("text") or j.get("text") or "").strip()
        return {"text": util.clean_text(text), "ms": dt,
                "words": sen.get("words") or [], "dur": dur, "raw": j}


# ---------------------------------------------------------------- 路由
class Transcriber:
    """对上层只暴露一个 transcribe()：按 cfg.asr_engine 决定本地/云端/混合。"""

    def __init__(self, cfg, net, log=util.log):
        self.cfg = cfg
        self.net = net
        self.log = log
        self.local = LocalASR(log)
        self.cloud = CloudASR(net, log)
        self.ja = None                      # 日语精修（用得着才载）
        self._pool = None

    def warm(self):
        if self.local.ready():
            self.log("本地识别就绪：" + self.local.note, "info", "asr")

    def engine(self) -> str:
        return str(self.cfg.get("asr_engine", "hybrid"))

    # ------------------------------------------------------------ 引擎选档
    def pick_local(self, lang="auto"):
        """这个语种该用哪个本地引擎：(engine_id | None, 理由)。

        决定权在 vrdub/asr_engines.py（按机器条件 + 实测数据），这里只负责
        把配置里的手写覆盖（asr_engine_map / asr_local_engine）喂进去。
        换到朋友那台机器上，不用改代码，注册表自己会给出不同的答案。
        """
        lang = str(lang or "auto").strip().lower()
        prefer = None
        m = self.cfg.get("asr_engine_map")
        if isinstance(m, dict):
            prefer = m.get(lang) or m.get("auto")
        prefer = prefer or self.cfg.get("asr_local_engine") or None

        # 已经载入的引擎不再拿「空闲内存/显存」重新裁决：那些数字每秒都在变，
        # 重选会让同一句话一会儿走本地、一会儿退回云端（实测端到端 0.4 s ↔ 1.7 s 来回跳）。
        # 手写配置 or 引擎换成不兼容语种时才重选。
        if not prefer and self.local.loaded:
            eid, why = "sensevoice", "已载入内存，这个语种沿用（%s）" % lang
        else:
            try:
                spec, why = asr_engines.pick(lang, prefer=prefer)
            except Exception as e:                   # noqa: BLE001
                return None, "选档失败（%r），沿用 SenseVoice" % (e,)
            eid = (spec or {}).get("id")

        # 只在「引擎换了」时打一行；理由串会随条件措辞变化，拿它当判据等于没判据。
        notes = getattr(self, "_pick_notes", None)
        if notes is None:
            notes = self._pick_notes = {}
        if notes.get(lang) != eid:
            notes[lang] = eid
            self.log("本地引擎选档（%s）：%s" % (lang, why or (eid or "无")), "info", "asr")
        return eid, why

    def transcribe_fast(self, pcm16, lang="auto", e=None) -> tuple[str, str, float]:
        """只要最快的结果：(text, engine, ms)。hybrid 时本地优先，失败退云端。

        e 由调用方按链传（A 链 asr_engine_a / B 链 asr_engine_b），不传就用全局 asr_engine。
        """
        e = e or self.engine()
        t0 = time.time()
        if e in ("local", "hybrid"):
            # 走哪条本地引擎由注册表按「语种 + 这台机器的条件」决定，不写死。
            eid, _why = self.pick_local(lang)
            txt = ""
            if eid == "whisper-anime":
                j0 = self._ja()
                if j0 is not None and j0.ready():
                    try:
                        txt, _ms = j0.transcribe(pcm16, lang)
                    except Exception:                # noqa: BLE001
                        txt = ""
            if not txt and self.local.ready():
                txt = self.local.transcribe(pcm16, lang)
            if txt and util.char_count(txt) >= 1:
                return txt, "local", (time.time() - t0) * 1000.0
            if e == "local":
                return "", "local", (time.time() - t0) * 1000.0
        t0 = time.time()
        r = self.cloud.transcribe(pcm16)
        return r["text"], "cloud", r["ms"]

    def transcribe_cloud(self, pcm16, lang="auto", timeout=12.0):
        return self.cloud.transcribe(pcm16, timeout=timeout)

    # ------------------------------------------------------------ 精修（日语）
    def _ja(self):
        """按需造 WhisperASR（cfg 改了就按新参数重建，除非已经载着）。"""
        if self.ja is not None:
            return self.ja
        self.ja = WhisperASR(
            self.log,
            device=str(self.cfg.get("whisper_device", "auto")),
            vram_min_mb=int(self.cfg.get("whisper_vram_min_mb", 1300)),
            beam=int(self.cfg.get("whisper_beam", 1)),
            threads=int(self.cfg.get("whisper_threads", 6)),
            slow_ms=float(self.cfg.get("whisper_slow_ms", 2500)),
        )
        return self.ja

    def refiner(self) -> str:
        """精修引擎：auto（云端优先，失败退本地显卡）/ cloud / whisper / off。

        为什么 auto 是「云端优先」而不是「本地优先」：
          同一批 5 条日语素材（含噪/混响/BGM/远场四种劣化，共 20 组）实测，
          与干净素材的云端文本比相似度——云端 1.000、SenseVoice 0.992、anime-whisper 0.968
          （anime-whisper 会写错同音汉字：文→分、区切り→縊り）。所以「准」这一格是云端，
          本地那套只在没网/云端挂了的时候顶上，也顺手省下 1.1 GB 显存。
        """
        want = str(self.cfg.get("asr_refine", "auto")).lower()
        if want in ("off", "none", "0", ""):
            return "off"
        if want == "whisper":
            j = self._ja()
            if j.ready():
                return "whisper"
            self.log("本地精修不可用（%s），改走云端" % j.note, "warn", "asr")
            return "cloud"
        if want == "cloud":
            return "cloud"
        return "auto"

    def refine(self, pcm16, lang="ja", timeout=12.0):
        """定稿精修：返回 (text, engine, ms)。engine = cloud | whisper | off。"""
        k = self.refiner()
        if k == "off":
            return "", "off", 0.0
        if not _loud_enough(pcm16):
            return "", "off", 0.0
        if k in ("auto", "cloud"):
            try:
                r = self.cloud.transcribe(pcm16, timeout=timeout)
            except Exception as e:                    # noqa: BLE001
                r = {"text": "", "ms": 0.0, "err": repr(e)}
            if r.get("text"):
                return r["text"], "cloud", float(r.get("ms") or 0.0)
            if k == "cloud":
                return "", "cloud", float(r.get("ms") or 0.0)
            # 本地只当「已经载入」时的兜底：云端抖动一次就去占 1.1 GB 显存，
            # 等于为了精修把 VRChat 挤掉帧（实测云端超时 13.2 s，本地载入 1.4–1.8 s + 0.35 s）
            j0 = self.ja
            if j0 is None or not j0.loaded:
                self.log("云端精修没出字（%s），本地模型没载入就不为它开显卡" %
                         str(r.get("err") or "空结果")[:60], "warn", "asr")
                return "", "cloud", float(r.get("ms") or 0.0)
            self.log("云端精修没出字（%s），改用已载入的本地显卡" %
                     str(r.get("err") or "空结果")[:60], "warn", "asr")
        j = self._ja()
        if j.ready():
            txt, ms = j.transcribe(pcm16, lang)
            if txt:
                return txt, "whisper", ms
        return "", "off", 0.0

    def idle(self, sec=None) -> bool:
        """空闲回收显存（VRChat 要用）。"""
        if self.ja is None:
            return False
        if sec is None:
            sec = float(self.cfg.get("whisper_idle_s", 180))
        return self.ja.idle_unload(sec)

    def stat(self) -> dict:
        d = {"refine": str(self.cfg.get("asr_refine", "auto"))}
        if self.ja is not None:
            d.update(self.ja.stat())
        else:
            d["device"] = "-"
        # 选档结果一起报出去：换机器/换配置时，控制台第一眼就能看到「这台机器跑什么、为什么」
        try:
            cond = asr_engines.probe()
            d["host"] = {"cores": cond.get("cores"), "ram_gb": round((cond.get("ram_total_mb") or 0) / 1024.0, 1),
                         "vram_free_mb": cond.get("vram_free_mb"), "gpu": cond.get("gpu")}
            d["pick"] = {lg: (asr_engines.pick(lg, prefer=(self.cfg.get("asr_engine_map") or {}).get(lg)
                                               if isinstance(self.cfg.get("asr_engine_map"), dict) else None)[0] or {}
                            ).get("id") for lg in ("zh", "ja")}
        except Exception:                            # noqa: BLE001
            pass
        return d