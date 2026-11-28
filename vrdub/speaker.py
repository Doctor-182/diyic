# -*- coding: utf-8 -*-
r"""段级说话人分离：给每一段算一个「像谁」的向量，再在线聚类成 1/2/3…（可起名、可存档）。

为什么不用 Sortformer：transcribe.cpp 的 diarize 通道要 stt.sortformer.* 命名的
GGUF（那是 Openresearchtools-Engine 的 fork），社区能下的 471 MB 版本装不进我们
的运行库（missing KV stt.sortformer.max_speakers）。而我们的音频本来就被 VAD 切成
一段一段，不需要「流式切人」——只要「这段像谁」。

做法：
  · 说话人向量：speechbrain/spkrec-ecapa-voxceleb 的 ONNX 导出（84 MB，CPU）。
    输入是原始 16 kHz 波形 [1, N]，前处理折在图里，输出 192 维，L2 归一后余弦即相似度。
  · 在线聚类：与各簇中心比余弦，最高且 >= spk_th 就归这一簇（中心 EMA 更新），否则开新簇。
  · 人来人往：簇满（spk_max）时不硬塞给最像的人，而是**淘汰「没起名字且最久没出现」的那一位**，
    把位置让给新声音。起过名字的人永不被淘汰，走了再回来（余弦够）还是原来那个号。
  · 存档：%LOCALAPPDATA%\VRDub\config\speakers.json（中心向量 + 编号 + 名字 + 段数 + 最后出现）。
    重启程序、重启电脑都还在；「清空」才会真的忘掉。
  · 跑在独立线程里、按段排队：B 链的字幕不等它，算完 bus.upd(seg, spk=编号, spk_name=名字)，
    弹幕/浮窗本来就是「同 id 原地换字」，所以看到的是标号晚到 100–450 ms。

实测（本机，VRChat + qwen17 同时跑，负载很高）：
  1.5 s 输入 / 2 线程 ≈ 94 ms，全段（4–9 s）/ 4 线程 ≈ 115 ms；机器空闲时更快。
  向量与时长关系：截到 1.5 s 与全段的相邻段相似度 p50 0.705 vs 0.751，几乎等价。
"""

import atexit
import json
import os
import re
import threading
import time

import numpy as np

from . import paths as _paths
from . import util

MODEL_REL = os.path.join("models", "spk", "voxceleb.onnx")
STORE_NAME = "speakers.json"
SR = 16000
NAME_MAX = 16
MAX_CAP = 24                      # 硬上限：再多也没人记得住，且中心越多误配越多


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


def store_path():
    r"""名单存哪：用户目录 config\speakers.json（不跟程序走，重装也还在）。"""
    try:
        return os.path.join(_paths.CONFIG_DIR, STORE_NAME)
    except Exception:                                 # noqa: BLE001
        return os.path.join(_paths.ROOT, "data", STORE_NAME)


def clean_name(s):
    """名字：去首尾空白、压空格、最多 16 字。"""
    return re.sub(r"\s+", " ", str(s or "")).strip()[:NAME_MAX]


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
    """说话人向量 + 在线聚类 + 名单存档。线程安全（submit 只入队，assign 在 worker 里）。"""

    def __init__(self, cfg, log=None, on_result=None, persist=True):
        self.cfg = cfg
        self.log = log or _log_default
        self.on_result = on_result        # (seg, 编号, 相似度, 名字) → 由调用方 bus.upd
        self._sess = None
        self._load_err = ""
        self._q = None
        self._lock = threading.Lock()
        self._cent = []                   # [ndarray]，L2 归一
        self._count = []                  # 每簇段数
        self._last = []                   # 每簇最后出现的 time.time()
        self._names = []                  # 每簇名字（'' = 没起名）
        self._ids = []                    # 每簇稳定编号（从 1 起，存档后不重排）
        self._next_id = 1
        self._n_ok = 0
        self._n_new = 0
        self._n_err = 0
        self._evicted = 0
        self._ms = []
        self._dropped = 0
        self._persist = bool(persist)
        self._save_t = 0.0
        self._dirty = False
        self._load_store()
        if self._persist:
            atexit.register(self._flush)

    # ---------------------------------------------------------------- 名单存档
    def _flush(self):
        try:
            if self._dirty:
                self.save(force=True)
        except Exception:                              # noqa: BLE001
            pass

    def _load_store(self):
        if not self._persist:
            return
        p = store_path()
        if not os.path.exists(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
            ents = list(d.get("clusters") or [])[:MAX_CAP]
            named = 0
            for e in ents:
                c = np.asarray(e.get("c") or [], dtype=np.float32).reshape(-1)
                n = float(np.linalg.norm(c)) if c.size else 0.0
                if c.size < 32 or n <= 0:
                    continue
                self._cent.append((c / n).astype(np.float32))
                self._count.append(int(e.get("hits") or 0))
                self._last.append(float(e.get("last") or 0.0))
                nm = clean_name(e.get("name"))
                self._names.append(nm)
                named += 1 if nm else 0
                sid = int(e.get("id") or 0)
                if sid <= 0 or sid in self._ids:        # 旧存档没编号 / 编号撞了
                    sid = self._next_id
                self._ids.append(sid)
                self._next_id = max(self._next_id, sid + 1)
            if self._cent:
                self.log("说话人名单载入 %d 人（%d 个有名字）· 存在 %s"
                         % (len(self._cent), named, p), "info", "spk")
        except Exception as e:                         # noqa: BLE001
            self.log("说话人名单读取失败（按空的算）：%r" % (e,), "warn", "spk")

    def save(self, force=False):
        """落盘（默认 2 s 节流；起名/清空/退出时 force）。"""
        if not self._persist:
            return False
        now = time.time()
        if not force and (now - self._save_t) < 2.0:
            self._dirty = True
            return False
        with self._lock:
            ents = []
            for i in range(len(self._cent)):
                ents.append({
                    "id": int(self._ids[i]),
                    "name": self._names[i],
                    "hits": int(self._count[i]),
                    "last": round(float(self._last[i]), 1),
                    "c": [round(float(x), 5) for x in self._cent[i].tolist()],
                })
        p = store_path()
        try:
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": 1, "clusters": ents}, f, ensure_ascii=False)
            os.replace(tmp, p)
            self._save_t = now
            self._dirty = False
            return True
        except Exception as e:                         # noqa: BLE001
            self.log("说话人名单存盘失败：%r" % (e,), "warn", "spk")
            return False

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
    def _cap(self):
        try:
            c = int(self.cfg.get("spk_max", 12) or 12)
        except Exception:                              # noqa: BLE001
            c = 12
        return max(2, min(MAX_CAP, c))

    def _dim_ok(self, vec):
        """存档可能是别的模型（维度不同）导出的：第一次对不上就整份作废。"""
        if self._cent and len(self._cent[0]) != len(vec):
            self.log("说话人名单维度不符（存档 %d 维，模型 %d 维）：清空重来"
                     % (len(self._cent[0]), len(vec)), "warn", "spk")
            self._cent, self._count, self._last, self._names, self._ids = [], [], [], [], []
            self._next_id = 1

    def assign(self, vec):
        """返回 (列表下标从 0 起, 相似度, 是否新簇)。"""
        th = float(self.cfg.get("spk_th", 0.72) or 0.72)
        cap = self._cap()
        now = time.time()
        with self._lock:
            self._dim_ok(vec)
            if not self._cent:
                self._append(vec, now)
                self._dirty = True
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
                self._last[j] = now
                self._dirty = True
                return j, best, False
            if len(self._cent) < cap:
                self._append(vec, now)
                self._dirty = True
                return len(self._cent) - 1, best, True
            # 满了：让位给新声音。优先淘汰「没起名 + 最久没出现」的那一位；
            # 起过名字的人永远留着（走了再回来还能认出是同一个号）。
            k = None
            for i in range(len(self._cent)):
                if self._names[i]:
                    continue
                if k is None or self._last[i] < self._last[k]:
                    k = i
            if k is not None:
                old = self._ids[k]
                self._cent[k] = vec.copy()
                self._count[k] = 1
                self._last[k] = now
                self._names[k] = ""
                self._ids[k] = self._next_id
                self._next_id += 1
                self._n_new += 1
                self._evicted += 1
                self._dirty = True
                self.log("%d 号很久没出现（没起名），位置让给新声音（新号 %d）"
                         % (old, self._ids[k]), "info", "spk")
                return k, best, True
            self._count[j] += 1
            self._last[j] = now
            self._dirty = True
            return j, best, False

    def _append(self, vec, now):
        self._cent.append(vec.copy())
        self._count.append(1)
        self._last.append(now)
        self._names.append("")
        self._ids.append(self._next_id)
        self._next_id += 1
        self._n_new += 1

    def label(self, pos):
        """(编号, 名字) —— 名字空着就返回 ''，调用方退回编号。"""
        with self._lock:
            try:
                return int(self._ids[pos]), self._names[pos]
            except Exception:                          # noqa: BLE001
                return 0, ""

    def entries(self):
        """名单（给界面/接口用）：[{id, name, hits, ago}]，按编号升序。"""
        now = time.time()
        with self._lock:
            out = []
            for i in range(len(self._cent)):
                out.append({"id": int(self._ids[i]), "name": self._names[i],
                            "hits": int(self._count[i]),
                            "ago": (round(now - self._last[i], 1) if self._last[i] else -1.0)})
        out.sort(key=lambda x: x["id"])
        return out

    def set_name(self, sid, name):
        """给某一位起名（空名字 = 取消）。返回 True/False。"""
        try:
            sid = int(sid)
        except Exception:                              # noqa: BLE001
            return False
        nm = clean_name(name)
        with self._lock:
            for i, x in enumerate(self._ids):
                if x == sid:
                    self._names[i] = nm
                    self._dirty = True
                    break
            else:
                return False
        self.save(force=True)
        self.log(("%d 号起名「%s」" % (sid, nm)) if nm else ("%d 号名字已清除" % sid),
                 "info", "spk")
        return True

    def forget(self, sid):
        """忘掉某一位（中心也删，下次出现会重新开号）。"""
        try:
            sid = int(sid)
        except Exception:                              # noqa: BLE001
            return False
        with self._lock:
            for i, x in enumerate(self._ids):
                if x == sid:
                    for arr in (self._cent, self._count, self._last, self._names, self._ids):
                        arr.pop(i)
                    self._dirty = True
                    break
            else:
                return False
        self.save(force=True)
        self.log("%d 号已从名单里删掉" % sid, "info", "spk")
        return True

    def reset(self):
        with self._lock:
            self._cent = []
            self._count = []
            self._last = []
            self._names = []
            self._ids = []
            self._next_id = 1
            self._n_new = 0
            self._dirty = True
        self.save(force=True)
        self.log("说话人聚类已清空（名单也清了，重新数人）", "info", "spk")

    def stats(self):
        now = time.time()
        with self._lock:
            n = len(self._cent)
            cnt = list(self._count)
            rows = [{"id": int(self._ids[i]), "name": self._names[i], "hits": int(self._count[i]),
                     "ago": (round(now - self._last[i], 1) if self._last[i] else -1.0)}
                    for i in range(n)]
            named = sum(1 for x in self._names if x)
        rows.sort(key=lambda x: x["id"])
        ms = self._ms[-40:]
        return {
            "ready": self.ready(),
            "err": self._load_err,
            "clusters": n,
            "counts": cnt,
            "named": named,
            "seen": self._n_ok,
            "new": self._n_new,
            "evicted": self._evicted,
            "fail": self._n_err,
            "dropped": self._dropped,
            "ms_p50": round(float(np.median(ms)), 1) if ms else 0.0,
            "th": float(self.cfg.get("spk_th", 0.72) or 0.72),
            "max": self._cap(),
            "store": (store_path() if self._persist else ""),
            "list": rows,
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
                pos, sim, new = self.assign(v)
                nid, nm = self.label(pos)
                self._n_ok += 1
                self._ms.append((time.time() - t0) * 1000.0)
                self.log("第 %d 段归到 %s（余弦 %.2f%s，%.0f ms）"
                         % (self._n_ok, (nm or ("%d 号" % nid)), sim,
                            "，新声音" if new else "", (time.time() - t0) * 1000.0),
                         "info", "spk")
                if self.on_result:
                    self.on_result(seg, nid, sim, nm)
                self.save()
            except Exception as e:            # noqa: BLE001
                self._n_err += 1
                self.log("分人出错：%r" % (e,), "warn", "spk")
            finally:
                try:
                    self._q.task_done()
                except Exception:             # noqa: BLE001
                    pass
