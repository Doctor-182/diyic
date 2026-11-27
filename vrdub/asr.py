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
from . import asr_gguf
from . import paths as _paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _models_dir() -> str:
    r"""模型根：装成 exe 之后是 exe 目录\models，源码跑是项目根\models。

    为什么不能只用 __file__：冻结后 __file__ 落在 _internal\vrdub\asr.py，
    往上两级是 _internal，而模型是跟 exe 并排放的（方便用户自己换/看体积）。
    """
    for cand in (os.path.join(_paths.ROOT, "models"),
                 os.path.join(ROOT, "models")):
        if os.path.isdir(cand):
            return cand
    return os.path.join(_paths.ROOT, "models")


SV_DIR = os.path.join(_models_dir(), "sensevoice")
_LID_TAG = re.compile(r"^<\|([a-z]{2,3})\|>$")
_LID_KEEP = ("zh", "en", "ja", "ko", "yue")
_KANA_CH = re.compile(r"[\u3040-\u30ff]")
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

    def __init__(self, log=util.log, sv_dir=None):
        self.log = log
        self.sv_dir = sv_dir or SV_DIR      # 可以指向别的 SenseVoice 权重目录（衍生微调版）
        self._sess = None
        self._tok = {}
        self._meta = {}
        self._mel = None
        self._lk = threading.Lock()
        self.note = "未加载"

    def set_dir(self, path: str) -> bool:
        """换一套权重（衍生微调版）。换完立刻卸载，下次用到自动按新目录载入。"""
        path = (path or "").strip() or SV_DIR
        if path == self.sv_dir:
            return False
        had = self.unload()
        self.sv_dir = path
        self._tok = {}
        self._meta = {}
        return True

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
            m = os.path.join(self.sv_dir, "model.int8.onnx")
            t = os.path.join(self.sv_dir, "tokens.txt")
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

    def lid(self, pcm16: np.ndarray) -> str:
        """这段话像什么语种：跑一遍前向，读第一个 <|xx|> 标签（language=auto）。

        实测（20261126 社交实录 200 段，对上云端判定的语种）：
          · 判 zh 精确率 100%、召回 80%（漏的全是 8 s 以上、一段里好几个话轮的）
          · 判 en 有误报（3 段日语被说成 en），所以只敢信 zh/yue
          · 吐标签的 128 段里一致率 96%，不吐标签（62 段）就当「不确定」
        因此它只用来决定「要不要换提示」，绝不当硬门控——判错代价不对称：
        把中文当日语会整句被翻成日语，把日语当中文只是那一段更差。
        """
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
            "language": np.array([0], dtype=np.int32),
            "text_norm": np.array([self._meta["noitn"]], dtype=np.int32),
        })[0]
        ids = out[0].argmax(axis=1)
        prev = -1
        for i in ids:
            i = int(i)
            if i == prev or i == 0:
                prev = i
                continue
            prev = i
            m = _LID_TAG.match(self._tok.get(i, ""))
            if m and m.group(1) in _LID_KEEP:
                return m.group(1)
        return ""


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
        self.local = LocalASR(log, (cfg.get("sv_dir") or "").strip() or None)
        self.cloud = CloudASR(net, log)
        self.ja = None                      # 日语精修（用得着才载）
        self._pool = None

    def warm(self):
        if self.local.ready():
            self.log("本地识别就绪：" + self.local.note + "（" + self.local.sv_dir + "）", "info", "asr")

    def reload_local(self) -> str:
        """按 cfg.sv_dir 重新指向本地权重并热载入。返回给人看的一句话。"""
        want = (self.cfg.get("sv_dir") or "").strip() or None
        self.local.set_dir(want or "")
        if self.local.ready():
            msg = "本地识别已换权重：" + self.local.note + "（" + self.local.sv_dir + "）"
            self.log(msg, "info", "asr")
            return msg
        msg = "本地识别不可用：" + str(self.local.note)
        self.log(msg, "warn", "asr")
        return msg

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

    def _lid_start(self, pcm16, lang):
        """开一条旁路线判语种；关掉 / 没载入 / 语种不在白名单就返回 (None, {})。"""
        mode = str(self.cfg.get("asr_gguf_lid", "zh") or "off").lower()
        if mode == "off" or lang not in ("ja", "zh", "en"):
            return None, {}
        if not self.local.ready():
            return None, {}
        box = {}

        def run():
            try:
                box["lid"] = self.local.lid(pcm16)
            except Exception as exc:                 # noqa: BLE001
                box["lid"] = ""
                box["err"] = repr(exc)

        th = threading.Thread(target=run, name="lid-probe", daemon=True)
        th.start()
        return th, box

    def _lid_wait(self, th, box, want="", txt=""):
        """等旁路线，返回该换的语种（""=不换）。

        分级信任（200 段真实语料实测）：
          zh/yue —— 探测头精确率 100%、召回 75%，误报 0：默认就切，救回被 ja 提示
                    硬翻成日语的中文段（zh 段 CER 50.0% → 3.1%）。
          ja     —— 精确率 99%：它说 ja 就信 ja，别再折腾。
          en/ko  —— 探测头对 en 不可信（精确率 0–6%）：只有 mode=any 才认，
                    且「探测没吭声 + 结果里一个假名都没有」时也按英语试一次。
        """
        if th is None:
            return ""
        th.join(3.0)
        if box.get("err"):
            self.log("语种探测失败：%s" % box["err"][:80], "warn", "asr")
        lid = str(box.get("lid") or "")
        if lid in ("zh", "yue"):
            return "zh"
        mode = str(self.cfg.get("asr_gguf_lid", "zh") or "zh").lower()
        if mode != "any" or lid in ("ja", "zh", "yue"):
            return ""                      # 默认只信 zh；探测说 ja 就别换
        if lid in ("en", "ko"):
            return lid
        if want == "ja" and txt:
            # 钉了 ja 却一个假名都没有、汉字也没有、拉丁成片 —— 那就是英语。
            _lat = sum(1 for ch in txt if ch.isascii() and ch.isalpha())
            _han = sum(1 for ch in txt if "一" <= ch <= "鿿")
            if _lat >= 3 and _han <= 1 and not _KANA_CH.search(txt):
                return "en"
        return ""

    def transcribe_fast(self, pcm16, lang="auto", e=None) -> tuple[str, str, float]:
        """只要最快的结果：(text, engine, ms)。hybrid 时本地优先，失败退云端。

        e 由调用方按链传（A 链 asr_engine_a / B 链 asr_engine_b），不传就用全局 asr_engine。
        """
        e = e or self.engine()
        t0 = time.time()
        if e in asr_gguf.ENGINES:
            # GGUF 档（qwen17 / qwen06 / fun）：一次只驻留一个模型，换档由 asr_gguf 负责卸载旧的。
            # 语种：qwen 的输出协议是「先写一句 language X<asr_text> 再写正文」，
            # 这一句就是它的语种判定，而且没有概率、不许反悔——写错整句就换语种。
            # 所以用外部廉价 LID（SenseVoice 的 <|xx|>）决定钉哪个提示，
            # 只对「明确是中文」的段做单向切换（判错代价不对称，见 lid() 注释）。
            want = lang
            th, box = self._lid_start(pcm16, lang)
            txt, meta = asr_gguf.transcribe(e, pcm16, want, self.cfg,
                                            hint=self.cfg.get("asr_gguf_hint", "auto"))
            txt = util.clean_text(txt or "")
            ms = float(meta.get("ms") or 0.0)
            pick = self._lid_wait(th, box, want, txt)
            if pick and pick != want:
                t2 = time.time()
                txt2, meta2 = asr_gguf.transcribe(e, pcm16, pick, self.cfg, hint=pick)
                txt2 = util.clean_text(txt2 or "")
                if txt2 and util.lang_ok(txt2, pick):
                    self.log("语种探测：这段像 %s，按它重听一遍（%.0f ms）%r"
                             % (pick, (time.time() - t2) * 1000.0, txt2[:36]), "info", "asr")
                    txt, meta, ms, want = txt2, meta2, float(meta2.get("ms") or 0.0), pick
                else:
                    self.log("语种探测提示 %s，但重听没出合语种的字，保留原结果" % pick, "warn", "asr")
            if txt:
                # 语种漂移守卫：20261127 01:00–02:00 KTV 实录里，qwen17 在唱段 6/10 段
                # 把日语写成英文（"I finally found you…"）或罗马字，字幕和翻译全废；
                # 0.6B 会漂韩文、Fun 会漂英文+瞎补。判据直接用 lang_ok（ja 要假名≥15%），
                # 命中就用 SenseVoice 重听一遍——它 0 复读 0 漂移，宁可字差也不要整句外语。
                why = util.lang_why(txt, want) if want in ("ja", "zh", "en") else ""
                if why:
                    self.log("%s 输出语种不对（想要 %s，实际 %s）：%r → 退回 SenseVoice"
                             % (e, lang, why, txt[:48]), "warn", "asr")
                    if self.local.ready():
                        lt = util.clean_text(self.local.transcribe(pcm16, want) or "")
                        if lt and util.lang_ok(lt, want):
                            return lt, "local(漂移)", (time.time() - t0) * 1000.0
                    return txt, e, ms          # 本地也漂：保留原结果，别变成空字幕
                return txt, e, ms
            # 这个档一个字都没出（解码自重复/模型缺失/显存不够）：退回 SenseVoice，
            # B 链宁可字差一点，也不要整段变成空字幕（那会被当成静音丢掉）。
            why = str(meta.get("err") or "空结果")[:80]
            self.log("%s 没出字（%s），退回本地 SenseVoice" % (e, why), "warn", "asr")
            if self.local.ready():
                lt = util.clean_text(self.local.transcribe(pcm16, want) or "")
                if lt:
                    return lt, "local(兜底)", (time.time() - t0) * 1000.0
            return "", e, ms
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

    # ------------------------------------------------------------ GGUF 驻留管理
    def gguf_release(self, why="") -> str:
        """主动放掉 GGUF 显存（切回本地/云端引擎、或显存不够时用）。"""
        return asr_gguf.RES.release(why)

    def gguf_warm(self, eid) -> bool:
        """预热某个 GGUF 档（换档时后台载入，别让第一句等 2–4 s）。"""
        if eid not in asr_gguf.ENGINES:
            return False
        sess, err = asr_gguf.RES.acquire(eid, self.cfg)
        if sess is None:
            self.log("GGUF 预热失败 %s：%s" % (eid, err), "warn", "asr")
            return False
        return True

    def idle(self, sec=None) -> bool:
        """空闲回收显存（VRChat 要用）。"""
        if self.ja is None:
            return False
        if sec is None:
            sec = float(self.cfg.get("whisper_idle_s", 180))
        return self.ja.idle_unload(sec)

    def stat(self) -> dict:
        d = {"refine": str(self.cfg.get("asr_refine", "auto")),
             "gguf": asr_gguf.RES.info(),
             "gguf_models": asr_gguf.catalog(self.cfg)}
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