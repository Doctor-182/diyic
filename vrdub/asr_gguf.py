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
import sys
import threading
import time

import numpy as np

from . import paths as _paths
from . import util

ROOT = _paths.ROOT

ENGINES = {
    "qwen17": {"name": "Qwen3-ASR-1.7B-Q5", "file": "Qwen3-ASR-1.7B-Q5_K_M.gguf",
               "backend": "cuda", "vram_mb": 1441, "detect": True,
               "note": "带标点、自报语种；真日语实录里唯一整句全对的档"},
    "qwen06": {"name": "Qwen3-ASR-0.6B-Q8", "file": "Qwen3-ASR-0.6B-Q8_0.gguf",
               "backend": "cuda", "vram_mb": 805, "detect": True,
               "note": "1.7B 的一半显存，质量掉一档；缺显存时用"},
    "fun": {"name": "Fun-ASR-Nano-Q8", "file": "Fun-ASR-Nano-2512-Q8_0.gguf",
            "backend": "cuda", "vram_mb": 844, "detect": False,
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
            util.log("GGUF 识别引擎就绪 %s（%s，载入 %.0f ms，显存 %.0f MiB）" % (
                ENGINES[eid]["name"], ENGINES[eid]["backend"], self.load_ms,
                ENGINES[eid]["vram_mb"]), "info", "asr.gguf")
            return self.session, ""

    def info(self) -> dict:
        with self._lock:
            e = ENGINES.get(self.key) or {}
            return {"key": self.key, "name": e.get("name", ""), "backend": self.backend or e.get("backend", ""),
                    "load_ms": self.load_ms, "runs": self.n_runs,
                    "vram_mb": e.get("vram_mb", 0), "err": self.err,
                    "secs": round(time.time() - self.loaded_at, 1) if self.loaded_at else 0}


RES = Resident()


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
    last = ""
    for lg in tries:
        try:
            kw = {"task": "transcribe", "timestamps": "none"}
            if lg:
                kw["language"] = lg
            res = sess.run(x, **kw)
            txt = util.clean_text((res.text or "").strip())
            got = (getattr(res, "language", "") or "").strip()
            with RES._lock:
                RES.n_runs += 1
            return txt, {"engine": eid, "ms": round((time.time() - t0) * 1000.0, 1),
                         "err": "", "lang": got, "hint": lg or "auto"}
        except Exception as e:                    # noqa: BLE001
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
        size = round(os.path.getsize(p) / (1024.0 * 1024.0), 1) if os.path.isfile(p) else 0
        out.append({"id": eid, "name": e["name"], "present": bool(size), "mb": size,
                    "vram_mb": e["vram_mb"], "backend": e["backend"],
                    "detect": e["detect"], "note": e["note"]})
    return out
