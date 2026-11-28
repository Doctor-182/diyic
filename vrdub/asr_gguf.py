# -*- coding: utf-8 -*-
r"""GGUF 系识别引擎（transcribe.cpp）：Qwen3-ASR 1.7B / 0.6B、Fun-ASR-Nano。

为什么单独一个模块：这三家都是 GGUF 单文件 + CUDA，走同一套 C 库；而 SenseVoice 是
onnxruntime（见 asr.py 的 LocalASR），两套运行时互不相干。

**同一时刻只允许一个 GGUF 模型驻留显存**（实测三引擎同时驻留时出现 12–20 s 离群延迟，
显存压力下换出换入比慢一点更致命）。所以这里只有一个 Resident 单例：要换模型必须先
release 掉旧的，_lock 保证两个链线程不会同时各自载入一份。

模型目录：cfg.asr_gguf_dir（空 = <程序目录>\models\asr）。
CUDA 运行库：transcribe_cpp_native_cu12\_native 必须在 PATH 里（ggml-cuda.dll 用裸名
LoadLibraryA("cublas64_12.dll") 找库，只认 PATH，不认 dll 自己所在目录）。
"""
from __future__ import annotations

import gc
import os
import re
import sys
import threading
import time

import numpy as np

from . import paths as _paths
from . import util
from . import vram

ROOT = _paths.ROOT

ENGINES = {
    "qwen17": {"name": "Qwen3-ASR-1.7B-Q5", "file": "Qwen3-ASR-1.7B-Q5_K_M.gguf",
               "backend": "cuda", "vram_mb": 1441, "overhead_mb": 420, "detect": True,
               "note": "带标点、自报语种；真日语实录里唯一整句全对的档"},
    "qwen06": {"name": "Qwen3-ASR-0.6B-Q8", "file": "Qwen3-ASR-0.6B-Q8_0.gguf",
               "backend": "cuda", "vram_mb": 805, "overhead_mb": 420, "detect": True,
               "note": "1.7B 的一半显存，质量掉一档；缺显存时用"},
    "fun": {"name": "Fun-ASR-Nano-Q8", "file": "Fun-ASR-Nano-2512-Q8_0.gguf",
            "backend": "cuda", "vram_mb": 844, "overhead_mb": 420, "detect": False,
            "note": "最快但不出标点、只有 3 个语种，日语偶尔听成中文"},
}

_LANG_HINT = {"ja": "ja", "zh": "zh", "en": "en", "ko": "ko", "yue": "yue"}


def model_dir(cfg=None) -> str:
    r"""模型目录：cfg.asr_gguf_dir 优先，空则 <程序目录>\models\asr。"""
    want = ""
    if cfg is not None:
        try:
            want = str(cfg.get("asr_gguf_dir") or "").strip()
        except Exception:                        # noqa: BLE001
            want = ""
    return want or os.path.join(_paths.ROOT, "models", "asr")


def path_of(eid, cfg=None) -> str:
    e = ENGINES.get(eid) or {}
    return os.path.join(model_dir(cfg), e.get("file") or "")


def available(eid, cfg=None) -> bool:
    p = path_of(eid, cfg)
    return bool(eid in ENGINES and os.path.isfile(p))


# ---------------------------------------------------------------- 显存看门
# 为什么要看门：换到一个装不下的档，后果不是「慢一点」，是 CUDA 分配失败 +
# VRChat 一起掉帧/闪退。而换档失败还会把旧模型先卸掉（_drop 在载入之前），
# 于是两条链一起哑掉。所以在真正碰模型之前先算清楚，装不下就不动。
# 420 MiB 是实测出来的：qwen17 权重 1441 MiB 驻留时，进程显存 1836 MiB
# （cublas Lt 工作区 + CUDA context）。宁可多留，不要刚好。

def need_mb(eid) -> int:
    """这个档真正要占多少显存：权重 + 运行库/上下文。"""
    e = ENGINES.get(eid) or {}
    return int(e.get("vram_mb") or 0) + int(e.get("overhead_mb") or 0)


def free_after_switch(eid) -> int:
    """切到 eid 之后能用的显存：现在空闲 + 换档会卸掉的那一份。读不到返回 -1。"""
    free = vram.free_mb()
    if free < 0:
        return -1
    cur = RES.key
    if cur and cur != eid and cur in ENGINES:
        free += need_mb(cur)
    return free


def fits(eid, free_mb=None):
    """(装得下?, 理由)。free_mb 传 None 就用实时读数（含换档释放）。"""
    e = ENGINES.get(eid)
    if e is None:
        return False, "没有这个档：%s" % eid
    if RES.key == eid and RES.session is not None:
        return True, "已经在显存里（%d MiB）" % e["vram_mb"]
    free = free_after_switch(eid) if free_mb is None else int(free_mb)
    need = need_mb(eid)
    if free < 0:
        return True, "查不到显存读数，照装（装完日志会写实际占用）"
    if free < need + vram.RESERVE_MB:
        return False, ("装不下 %s：要 %d MiB（模型 %d + 运行库/上下文 %d），加 %d MiB 余量共 %d MiB；"
                       "现在只空 %d MiB。先关 VRChat/壁纸，或改选 sv（0 显存）%s。"
                       % (eid, need, e["vram_mb"], e.get("overhead_mb", 0), vram.RESERVE_MB,
                          need + vram.RESERVE_MB, free,
                          "／qwen06（%d MiB）" % need_mb("qwen06") if eid == "qwen17" else ""))
    return True, "装得下（需 %d MiB，空 %d MiB）" % (need, free)


def native_dir() -> str:
    r"""transcribe_cpp_native_cu12\_native 的位置（找到就返回，找不到返回空串）。"""
    cands = [os.path.join(_paths.ROOT, ".pylibs"), os.path.join(_paths.ROOT, "_pylibs"),
             getattr(sys, "_MEIPASS", ""),          # PyInstaller onedir：_internal 就是它
             _paths.ROOT]                           # 冻结时也可能直接摆在 exe 旁边
    for base in cands:
        if not base:
            continue
        p = os.path.join(base, "transcribe_cpp_native_cu12", "_native")
        if os.path.isdir(p):
            return p
    try:
        import transcribe_cpp_native_cu12 as nat      # noqa: F401
        p = os.path.join(os.path.dirname(os.path.abspath(nat.__file__)), "_native")
        return p if os.path.isdir(p) else ""
    except Exception:                            # noqa: BLE001
        return ""


_ready_dir = ""


def _env_setup() -> bool:
    r"""把 native 目录塞进 PATH/DLL 搜索路径，并直接告诉 transcribe_cpp 库在哪（只需一次）。

    两个坑，都真机踩过：
      1. ggml-cuda.dll 用裸名 LoadLibraryA("cublas64_12.dll") 找库——**只认 PATH**，
         不认它自己所在的目录。所以必须把 native 目录前置进 PATH，否则报
         BackendError: backend error (status 8)。
      2. 官方包靠 entry point（transcribe_cpp.native 组）发现 native 提供者，
         而我们是把轮子直接摊进 .pylibs 的，没有 dist-info 就没有 entry point，
         于是报 'could not locate the native transcribe library'。
         用 TRANSCRIBE_LIBRARY 直接点名 dll 最稳，开发态与冻结态一个写法。
      3. 载入贵在「一个进程里的第一次」：1.7B 实测 52 s（从磁盘读 1.4 GB + 建上下文），
         同一进程内换走再换回来只要 4.5 s（页缓存热了）。驱动侧没有生成
         ComputeCache 目录（试过 CUDA_CACHE_PATH 也不落盘），所以真正管用的是
         「启动时就预热」——见 App.start() 里的 _apply_gguf(set())。
    """
    global _ready_dir
    nd = native_dir()
    if not nd:
        return False
    if _ready_dir == nd:
        return True
    os.environ["PATH"] = nd + os.pathsep + os.environ.get("PATH", "")
    lib = os.path.join(nd, "transcribe.dll")
    if os.path.isfile(lib) and not os.environ.get("TRANSCRIBE_LIBRARY"):
        os.environ["TRANSCRIBE_LIBRARY"] = lib
    try:
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(nd)
    except OSError:
        pass
    _ready_dir = nd
    return True


def _to_f32(pcm) -> np.ndarray:
    a = np.asarray(pcm)
    if a.dtype == np.float32:
        return a.reshape(-1)
    if a.dtype == np.int16:
        return (a.astype(np.float32) / 32768.0).reshape(-1)
    if a.dtype.kind == "f":
        return a.astype(np.float32).reshape(-1)
    return (a.astype(np.float32) / 32768.0).reshape(-1)


class Resident:
    """全局唯一驻留模型：acquire(新) 会先释放(旧)，绝不并存。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.key = ""
        self.backend = ""
        self.session = None
        self.model = None
        self.load_ms = 0.0
        self.err = ""
        self.loaded_at = 0.0
        self.n_runs = 0
        self._snap = {}          # 上一次拿得到锁时的快照（info 拿不到锁就用它，绝不阻塞）

    # ---------------------------------------------------------------- 内部
    def _drop(self):
        had = self.key
        self.session = None
        self.model = None
        self.key = ""
        self.backend = ""
        self.load_ms = 0.0
        self.loaded_at = 0.0
        gc.collect()
        return had

    def release(self, why="") -> str:
        with self._lock:
            had = self._drop()
        if had:
            util.log("GGUF 识别引擎卸载 %s%s" % (ENGINES.get(had, {}).get("name", had),
                                            ("（%s）" % why) if why else ""), "info", "asr.gguf")
        return had

    def acquire(self, eid, cfg=None):
        """确保 eid 驻留；换模型自动卸载旧的。返回 (session, err)。"""
        with self._lock:
            if self.key == eid and self.session is not None:
                return self.session, ""
            old = self.key
            # 看门：装不下就别动。注意必须在 _drop() 之前判——旧模型一卸，
            # 新模型又装不下，两条链就一起哑了（宁可继续用旧档跑差的，也不要没得跑）。
            if eid in ENGINES and not (old == eid and self.session is not None):
                _free = vram.free_mb()
                if _free >= 0 and old and old != eid and old in ENGINES:
                    _free += need_mb(old)          # 换档会先卸旧的，这份空间马上能复用
                _ok, _why = fits(eid, free_mb=(_free if _free >= 0 else None))
                if not _ok:
                    self.err = _why
                    util.log("拒绝载入 GGUF %s：%s" % (eid, _why), "warn", "asr.gguf")
                    return None, _why
            if old and old != eid:
                self._drop()
                util.log("GGUF 切换：卸载 %s → 载入 %s" % (
                    ENGINES.get(old, {}).get("name", old), ENGINES.get(eid, {}).get("name", eid)),
                    "info", "asr.gguf")
            if self.session is not None:
                return self.session, ""
            if eid not in ENGINES:
                return None, "未知引擎 %s" % eid
            if not _env_setup():
                return None, "找不到 transcribe_cpp 运行库（native 目录）"
            p = path_of(eid, cfg)
            if not os.path.isfile(p):
                return None, "模型文件不存在：%s" % p
            try:
                import transcribe_cpp as tc
            except Exception as e:               # noqa: BLE001
                return None, "transcribe_cpp 不可用：%r" % (e,)
            t0 = time.time()
            try:
                m = tc.Model(p, backend=str((cfg.get("asr_gguf_backend") if cfg else "") or
                                            ENGINES[eid]["backend"]))
                s = m.session(n_threads=0, kv_type="auto")
            except Exception as e:               # noqa: BLE001
                self.err = repr(e)
                return None, "载入失败：%r" % (e,)
            self.key, self.model, self.session = eid, m, s
            self.err = ""
            self.load_ms = round((time.time() - t0) * 1000.0, 1)
            self.loaded_at = time.time()
            self.n_runs = 0
            util.log("GGUF 识别引擎就绪 %s（%s，载入 %.0f ms，权重 %.0f MiB，整机%s）" % (
                ENGINES[eid]["name"], ENGINES[eid]["backend"], self.load_ms,
                ENGINES[eid]["vram_mb"], vram.brief()), "info", "asr.gguf")
            return self.session, ""

    def _snap_now(self) -> dict:
        e = ENGINES.get(self.key) or {}
        return {"key": self.key, "name": e.get("name", ""),
                "backend": self.backend or e.get("backend", ""),
                "load_ms": self.load_ms, "runs": self.n_runs,
                "vram_mb": e.get("vram_mb", 0), "err": self.err,
                "secs": round(time.time() - self.loaded_at, 1) if self.loaded_at else 0}

    def info(self) -> dict:
        r"""报「现在驻留谁」。**绝不阻塞**。

        acquire() 载模型时是一直握着这把锁的（CUDA 首次 JIT 实测能握 60 s），
        早先 info() 直接 with self._lock 会跟着一起卡住 —— 表现为整个控制台
        /api/state 无响应、网页每 3 秒重连、子窗口以为后端死了自杀重启。
        现在拿不到锁就回上次的快照 + busy=True，让界面显示「载入中」。
        """
        got = self._lock.acquire(blocking=False)
        if got:
            try:
                self._snap = self._snap_now()
            finally:
                self._lock.release()
        snap = dict(self._snap or {})
        snap["busy"] = not got
        return snap


RES = Resident()


# Fun-ASR 会把 `/sil`、`<sil>` 这类标记当普通词吐出来（实测 012032 → "/sil"），
# 它们既不是内容也不是标点，进字幕会被当成真话翻译，先清掉。
_MARK = re.compile(r"(?i)[/<\[]?\bsil\b[/>\]]?")


def tidy(text: str) -> str:
    """去掉模型吐出的非语言标记，压掉多余空格。"""
    s = _MARK.sub(" ", text or "")
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    return s


def _vocab_of(cfg):
    """专名表：只给 qwen 系（fun 不认 vocabulary）。cfg 可能是 Cfg 对象也可能是 dict。"""
    try:
        raw = cfg.get("asr_gguf_vocab") if hasattr(cfg, "get") else (cfg or {}).get("asr_gguf_vocab")
    except Exception:                              # noqa: BLE001
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for it in raw:
        w = str(it or "").strip()
        if w and len(w) <= 24:
            out.append(w)
    return out[:8]


def transcribe(eid, pcm16, lang="auto", cfg=None, hint="auto"):
    """跑一次识别，返回 (text, meta)。异常一律吞掉并把原因放 meta["err"]（不能让一段音频打断链路）。"""
    t0 = time.time()
    sess, err = RES.acquire(eid, cfg)
    if sess is None:
        return "", {"engine": eid, "ms": (time.time() - t0) * 1000.0, "err": err, "lang": ""}
    x = _to_f32(pcm16)
    if x.size < 1600:
        return "", {"engine": eid, "ms": (time.time() - t0) * 1000.0, "err": "太短", "lang": ""}
    want_hint = str(hint or "auto").lower() != "off"
    code = _LANG_HINT.get(str(lang or "").lower(), "")
    tries = []
    if want_hint and code:
        tries.append(code)
    tries.append(None)          # 兜底：语种提示不被这个模型接受时，退回自动判定
    vocab = _vocab_of(cfg) if eid in ("qwen17", "qwen06") else []
    last = ""
    for lg in tries:
        for use_vocab in ((True, False) if vocab else (False,)):
            try:
                kw = {"task": "transcribe", "timestamps": "none"}
                if lg:
                    kw["language"] = lg
                if use_vocab:
                    kw["vocabulary"] = vocab
                res = sess.run(x, **kw)
                txt = tidy(util.clean_text((res.text or "").strip()))
                got = (getattr(res, "language", "") or "").strip()
                with RES._lock:
                    RES.n_runs += 1
                return txt, {"engine": eid, "ms": round((time.time() - t0) * 1000.0, 1),
                             "err": "", "lang": got, "hint": lg or "auto",
                             "vocab": len(vocab) if use_vocab else 0}
            except Exception as e:                # noqa: BLE001
                last = type(e).__name__
                continue
    # 解码自重复（OutputRepetition）等：transcribe.cpp 主动停下，一段音频不值得重试
    return "", {"engine": eid, "ms": round((time.time() - t0) * 1000.0, 1),
                "err": last or "失败", "lang": ""}


def catalog(cfg=None) -> list:
    """给控制台用：每个引擎的可用性/体积/说明。"""
    out = []
    for eid, e in ENGINES.items():
        p = path_of(eid, cfg)
        # present 必须按「文件在不在」判，不能按四舍五入后的 MB 判：小于 0.05 MB 的
        # 文件会被 round 成 0.0，然后被当成「没下载」（模型都是几百 MB，但别留这种坑）。
        have = os.path.isfile(p)
        size = round(os.path.getsize(p) / (1024.0 * 1024.0), 1) if have else 0
        ok, why = fits(eid) if have else (False, "模型文件没下载")
        out.append({"id": eid, "name": e["name"], "present": bool(have), "mb": size,
                    "vram_mb": e["vram_mb"], "need_mb": need_mb(eid),
                    "backend": e["backend"], "detect": e["detect"], "note": e["note"],
                    "fits": bool(ok), "why": why})
    return out
