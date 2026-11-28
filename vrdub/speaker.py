# -*- coding: utf-8 -*-
r"""段级说话人分离：给每一段算一个「像谁」的向量，再在线聚类成 1/2/3…。

为什么不用 Sortformer：transcribe.cpp 的 diarize 通道要 stt.sortformer.* 命名的
GGUF（那是 Openresearchtools-Engine 的 fork），社区能下的 471 MB 版本装不进我们
的运行库（missing KV stt.sortformer.max_speakers）。而我们的音频本来就被 VAD 切成
一段一段，不需要「流式切人」——只要「这段像谁」。

做法：
  · 说话人向量：speechbrain/spkrec-ecapa-voxceleb 的 ONNX 导出（84 MB，CPU）。
    输入是原始 16 kHz 波形 [1, N]，前处理（mel/fbank/均值归一）折在图里，
    输出 192 维，L2 归一后余弦就是相似度。
  · 在线聚类：与各簇中心比余弦，最高且 >= spk_th 就归这一簇（中心做 EMA 更新），
    否则开新簇；簇满（spk_max）就退回最像的那个。
  · 跑在独立线程里、按段排队：B 链的字幕不等它，算完再用 bus.upd(seg, spk=n)
    把编号补上，弹幕/浮窗本来就是「同 id 原地换字」，所以看到的是编号晚到 100–200 ms。

实测（本机，VRChat + qwen17 同时跑，负载很高）：
  1.5 s 输入 / 2 线程 ≈ 94 ms，全段（4–9 s）/ 4 线程 ≈ 115 ms；机器空闲时更快。
  向量与时长关系：截到 1.5 s 与全段的相邻段相似度 p50 0.705 vs 0.751，几乎等价。
"""

import os
import threading
import time

import numpy as np

from . import paths as _paths
from . import util

MODEL_REL = os.path.join("models", "spk", "voxceleb.onnx")
SR = 16000


def _log_default(msg, level="info", tag="spk"):
    util.log(msg, level, tag)


def model_path(cfg=None):
    """模型在哪：cfg.spk_model（绝对或相对项目根）> 项目根 models/spk/voxceleb.onnx。"""
    p = str((cfg or {}).get("spk_model") or "").strip() if hasattr(cfg, "get") else ""
    if p:
        return p if os.path.isabs(p) else os.path.join(_paths.ROOT, p)
    for base in (_paths.ROOT, getattr(_paths, "BASE", _paths.ROOT)):
        q = os.path.join(base, MODEL_REL)
        if os.path.exists(q):
            return q
    return os.path.join(_paths.ROOT, MODEL_REL)


def _resample16(a, sr_in):
    if sr_in == SR or len(a) == 0:
        return a.astype(np.float32, copy=False)
    n = max(1, int(round(len(a) * SR / float(sr_in))))
    x = np.linspace(0.0, len(a) - 1.0, n, dtype=np.float32)
    return np.interp(x, np.arange(len(a), dtype=np.float32), a).astype(np.float32)


def prepare(pcm, sr_in=SR, secs=1.5):
    """把一段音频整成模型要的波形：float32、单声道、16 kHz、限长。"""
    a = np.asarray(pcm)
    if a.ndim > 1:
        a = a.reshape(a.shape[0], -1).mean(axis=1)
    if a.dtype == np.int16:
        a = a.astype(np.float32) / 32768.0
    else:
        a = a.astype(np.float32)
    a = _resample16(a, sr_in)
    if secs and secs > 0:
        a = a[: int(SR * float(secs))]
    peak = float(np.max(np.abs(a))) if len(a) else 0.0
    if peak > 1.0:                      # int16 忘了除、或者录进来削顶
        a = a / peak
    return a


class Speaker:
    """说话人向量 + 在线聚类。线程安全（submit 只入队，assign 在 worker 里）。"""

    def __init__(self, cfg, log=None, on_result=None):
        self.cfg = cfg
        self.log = log or _log_default
        self.on_result = on_result        # (seg, n, sim) → 由调用方 bus.upd
        self._sess = None
        self._load_err = ""
        self._q = None
        self._th = None
        self._lock = threading.Lock()
        self._cent = []                   # [ndarray]，L2 归一
        self._count = []                  # 每簇段数
        self._n_ok = 0
        self._n_new = 0
        self._n_err = 0
        self._ms = []
        self._dropped = 0

    # ---------------------------------------------------------------- 模型
    def ready(self):
        return self._sess is not None

    def load(self):
        if self._sess is not None or self._load_err:
            return self._sess is not None
        p = model_path(self.cfg)
        if not os.path.exists(p):
            self._load_err = "模型不在：" + p
            self.log("说话人模型不在（%s）：分人跳过，字幕照常" % p, "warn", "spk")
            return False
        try:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2   # 实测 2 线程比 4 快（机器本来就满载）
            so.log_severity_level = 3
            t0 = time.time()
            self._sess = ort.InferenceSession(p, so, providers=["CPUExecutionProvider"])
            self.log("说话人模型载入 %d ms（%s）" % ((time.time() - t0) * 1000, os.path.basename(p)),
                     "info", "spk")
            return True
        except Exception as e:                # noqa: BLE001
            self._load_err = repr(e)
            self.log("说话人模型载入失败：%r" % (e,), "warn", "spk")
            return False

    def embed(self, pcm, sr_in=SR):
        """一段音频 → 192 维单位向量；失败返回 None。"""
        if not self.ready() and not self.load():
            return None
        secs = float(self.cfg.get("spk_secs", 1.5) or 1.5)
        a = prepare(pcm, sr_in, secs)
        if len(a) < int(SR * 0.4) or float(np.sqrt((a ** 2).mean())) < 1e-4:
            return None
        try:
            v = self._sess.run(None, {"audio_input": a[None, :]})[0].reshape(-1)
        except Exception as e:                # noqa: BLE001
            self._n_err += 1
            self.log("说话人向量算失败：%r" % (e,), "warn", "spk")
            return None
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None

    # ---------------------------------------------------------------- 聚类
    def assign(self, vec):
        """返回 (簇号从 0 起, 相似度, 是否新簇)。"""
        th = float(self.cfg.get("spk_th", 0.72) or 0.72)
        cap = int(self.cfg.get("spk_max", 6) or 6)
        with self._lock:
            if not self._cent:
                self._cent.append(vec.copy())
                self._count.append(1)
                self._n_new += 1
                return 0, 1.0, True
            sims = [float(vec @ c) for c in self._cent]
            j = int(np.argmax(sims))
            best = sims[j]
            if best >= th:
                a = 0.3
                c = self._cent[j] * (1.0 - a) + vec * a
                nn = float(np.linalg.norm(c))
                self._cent[j] = c / nn if nn > 0 else c
                self._count[j] += 1
                return j, best, False
            if len(self._cent) < max(2, cap):
                self._cent.append(vec.copy())
                self._count.append(1)
                self._n_new += 1
                return len(self._cent) - 1, best, True
            self._count[j] += 1
            return j, best, False

    def reset(self):
        with self._lock:
            self._cent = []
            self._count = []
            self._n_new = 0
        self.log("说话人聚类已清空（重新数人）", "info", "spk")

    def stats(self):
        with self._lock:
            n = len(self._cent)
            cnt = list(self._count)
        ms = self._ms[-40:]
        return {
            "ready": self.ready(),
            "err": self._load_err,
            "clusters": n,
            "counts": cnt,
            "seen": self._n_ok,
            "new": self._n_new,
            "fail": self._n_err,
            "dropped": self._dropped,
            "ms_p50": round(float(np.median(ms)), 1) if ms else 0.0,
            "th": float(self.cfg.get("spk_th", 0.72) or 0.72),
            "max": int(self.cfg.get("spk_max", 6) or 6),
        }

    # ---------------------------------------------------------------- 队列
    def submit(self, seg, pcm, sr_in=SR):
        """排队算这一段的说话人；满队/关掉/太短直接放弃（绝不阻塞识别线程）。"""
        if not self.cfg.get("spk_on", True):
            return False
        if len(pcm) < int(SR * 0.6):
            return False
        if not self.ready() and not self.load():
            return False
        if self._q is None:
            self._q = __import__("queue").Queue(maxsize=8)
            self._th = threading.Thread(target=self._worker, name="spk", daemon=True)
            self._th.start()
        # 存的是总线里那个 seg 对象本身（不是副本）：算完要原地 upd 才能让
        # 弹幕/浮窗/网页都看到编号，副本改了外面看不见。
        item = (str(seg.get("id") or ""), seg, pcm, sr_in)
        try:
            self._q.put_nowait(item)
            return True
        except Exception:                     # noqa: BLE001
            self._dropped += 1
            return False

    def _worker(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            sid, seg, pcm, sr_in = item
            t0 = time.time()
            try:
                v = self.embed(pcm, sr_in)
                if v is None:
                    continue
                n, sim, new = self.assign(v)
                self._n_ok += 1
                self._ms.append((time.time() - t0) * 1000.0)
                self.log("第 %d 段归到 %d 号（余弦 %.2f%s，%.0f ms）"
                         % (self._n_ok, n + 1, sim, "，新声音" if new else "",
                            (time.time() - t0) * 1000.0), "info", "spk")
                if self.on_result:
                    self.on_result(seg, n, sim)
            except Exception as e:            # noqa: BLE001
                self._n_err += 1
                self.log("分人出错：%r" % (e,), "warn", "spk")
            finally:
                try:
                    self._q.task_done()
                except Exception:             # noqa: BLE001
                    pass
