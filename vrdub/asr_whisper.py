# -*- coding: utf-8 -*-
"""日语精修识别：anime-whisper（kotoba-whisper-v2.0 基座 + 5300 h 动画语料微调）int8 CT2。

为什么加它：
  SenseVoice-Small 强在「快」和中文，日语是它的弱项（对唱歌/远场/多人尤其）；
  云端日语识别抖动 0.34-13.2 s，尾部长到没法当主路径。
  anime-whisper 在 galgame/动画语音 CER 13.0%（whisper-large-v3 16.5 / kotoba-v2.0 18.8），
  域最贴 VRChat 里那种夸张演技日语。

算力实测（同一 4 s 素材，本机 14650HX + RTX 5060 Laptop）：
  CPU int8 10.5-26.5 s —— 完全不能用（ct2 在这台机器上只跑到 ~55 GFLOPS 有效算力，
  ct2 4.5.0 / 4.6.1 / 4.9.0、AVX2/AVX512 强制、float32/int8_float32 都一样慢）。
  GPU int8_float16 0.2-0.4 s / 约 1.1 GB 显存 —— 可用，但显存是稀缺资源（VRChat 要 5-8 GB）。

所以定位是「有空就精修，没空就退回云端」：
  1. device=auto 时先查显存，够（>= vram_min_mb）才上 GPU，否则直接用云端；
  2. 首次识别后自适应：一次超过 slow_ms 就自我降级，之后不再占用；
  3. 空闲 idle_s 自动卸载，把显存还给 VRChat。
依赖放在 _pylibs / _pylibs_cuda，不污染系统环境。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, "models", "anime-whisper-ct2-int8")
LIB_DIR = os.path.join(ROOT, "_pylibs")
CUDA_LIB = os.path.join(ROOT, "_pylibs_cuda")


def free_vram_mb() -> int:
    """nvidia-smi 查空闲显存（MB），没有卡返回 -1。约 60-120 ms，只在载入前调一次。"""
    try:
        si = subprocess.STARTUPINFO() if hasattr(subprocess, "STARTUPINFO") else None
        if si is not None:                        # 别弹黑框
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=6, startupinfo=si)
        if p.returncode != 0:
            return -1
        return int(p.stdout.strip().splitlines()[0].strip())
    except Exception:                             # noqa: BLE001
        return -1


class WhisperASR:
    """faster-whisper + CT2 int8。首次用到才载入（省常驻内存/显存）。"""

    def __init__(self, log=None, model_dir=None, lib_dir=None, cuda_lib=None,
                 device="auto", vram_min_mb=1300, threads=6, beam=1, slow_ms=2500.0):
        from . import util
        self.log = log or util.log
        self.dir = model_dir or DEFAULT_DIR
        self.lib = lib_dir or LIB_DIR
        self.cuda = cuda_lib or CUDA_LIB
        self.want = str(device or "auto").lower()
        self.vram_min = int(vram_min_mb)
        self.threads = int(threads)
        self.beam = int(beam)
        self.slow_ms = float(slow_ms)
        self._m = None
        self._lk = threading.Lock()
        self._last = 0.0
        self._dll = []
        self.device = "?"                              # 实际用的设备
        self.note = "未加载"
        self.slow = False                              # 一次跑太慢就自我降级
        self.calls = 0
        self.ms_total = 0.0
        self._grace = 1                                # 头一次偏慢不算数（可能在和 VRChat 抢卡）

    # ---------------------------------------------------------------- 生命周期
    def _ensure_path(self, device):
        for d in (self.lib, self.cuda):
            if os.path.isdir(d) and d not in sys.path:
                sys.path.insert(0, d)
        if device == "cuda" and os.path.isdir(self.cuda):
            # ct2 需要 cublas/cudnn 的 DLL；add_dll_directory 的句柄要留着
            for sub in (("nvidia", "cublas", "bin"), ("nvidia", "cudnn", "bin"),
                        ("nvidia", "cuda_nvrtc", "bin")):
                p = os.path.join(self.cuda, *sub)
                if os.path.isdir(p):
                    try:
                        self._dll.append(os.add_dll_directory(p))
                    except Exception:              # noqa: BLE001
                        pass
                    os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")

    def _open(self, device, ctype):
        from faster_whisper import WhisperModel
        kw = dict(compute_type=ctype, num_workers=1, local_files_only=True)
        if device == "cuda":
            kw["device_index"] = 0
        else:
            kw["cpu_threads"] = max(1, self.threads)
        return WhisperModel(self.dir, device=device, **kw)

    def _load(self):
        if self._m is not None:
            return
        with self._lk:
            if self._m is not None:
                return
            if not os.path.exists(os.path.join(self.dir, "model.bin")):
                self.note = "模型缺失：%s" % self.dir
                return
            plan = []
            if self.want in ("auto", "cuda", "gpu"):
                free = free_vram_mb()
                if free < 0 and self.want == "auto":
                    pass                           # 没卡，直接走 CPU 计划
                elif free >= 0 and free < self.vram_min and self.want == "auto":
                    self.note = "显存只剩 %d MB（< %d），不占 VRChat 的份额" % (free, self.vram_min)
                    self.log(self.note, "info", "asr.whisper")
                    plan = [("cpu", "int8")]
                else:
                    plan = [("cuda", "int8_float16"), ("cpu", "int8")]
            else:
                plan = [("cpu", "int8")]
            for dev, ctype in plan:
                self._ensure_path(dev)
                t0 = time.time()
                try:
                    self._m = self._open(dev, ctype)
                except Exception as e:                # noqa: BLE001
                    self.log("whisper %s/%s 载入失败：%r" % (dev, ctype, e), "warn", "asr.whisper")
                    self._m = None
                    continue
                self.device = dev
                self.note = "ok %s/%s 载入 %.0fms" % (dev, ctype, (time.time() - t0) * 1000)
                self.log(self.note, "info", "asr.whisper")
                # 第一次推理要建 CUDA 上下文/编 kernel（本机实测 13.9 s → 稳态 0.35 s），
                # 所以先拿 1 s 静音热身，别让这段开销落到用户第一句话上、也别让它触发降级。
                try:
                    self._m.transcribe(np.zeros(16000, dtype=np.float32), language="ja",
                                       beam_size=1, condition_on_previous_text=False,
                                       without_timestamps=True, vad_filter=False,
                                       temperature=0.0)
                except Exception:                 # noqa: BLE001
                    pass
                return
            if self._m is None:
                self.note = self.note or "载入失败"

    @property
    def loaded(self) -> bool:
        """已经占着显存了？（不触发载入）"""
        return self._m is not None and not self.slow

    def ready(self) -> bool:
        if self.slow:
            return False
        self._load()
        return self._m is not None and not self.slow

    def unload(self) -> bool:
        with self._lk:
            if self._m is None:
                return False
            self._m = None
            self.note = "已卸载"
        try:
            import gc
            gc.collect()
        except Exception:                             # noqa: BLE001
            pass
        return True

    def idle_unload(self, sec=180.0) -> bool:
        """长时间没人说话就把显存/内存还回去（VRChat 那边要用）。"""
        if self._m is None or not self._last:
            return False
        if time.time() - self._last >= float(sec):
            return self.unload()
        return False

    def stat(self) -> dict:
        return {"device": self.device, "note": self.note, "slow": self.slow,
                "calls": self.calls,
                "ms": round(self.ms_total / self.calls, 1) if self.calls else 0.0}

    # ---------------------------------------------------------------- 识别
    def transcribe(self, pcm16: np.ndarray, lang: str = "ja", beam=None):
        """返回 (text, ms)。pcm16 必须是 16 kHz 单声道 int16。"""
        from . import util
        if not self.ready():
            return "", 0.0
        a = np.asarray(pcm16).reshape(-1)
        if a.dtype != np.float32:
            a = a.astype(np.float32) / 32768.0
        if len(a) < int(0.2 * 16000):
            return "", 0.0
        t0 = time.time()
        try:
            segs, _info = self._m.transcribe(
                a, language=("ja" if lang in ("ja", "auto") else lang),
                beam_size=int(beam or self.beam), best_of=max(1, int(beam or self.beam)),
                condition_on_previous_text=False, without_timestamps=True,
                vad_filter=False, temperature=0.0,
                no_speech_threshold=0.6, compression_ratio_threshold=2.4,
            )
            txt = "".join(s.text for s in segs).strip()
        except Exception as e:                        # noqa: BLE001
            self.log("日语精修识别失败：%r" % (e,), "warn", "asr.whisper")
            if "cuda" in self.device or "CUBLAS" in str(e) or "out of memory" in str(e).lower():
                self.log("GPU 精修不可用，永久退回云端精修", "warn", "asr.whisper")
                self.slow = True
                self.unload()
            return "", 0.0
        ms = (time.time() - t0) * 1000.0
        self._last = time.time()
        self.calls += 1
        self.ms_total += ms
        if ms > self.slow_ms:
            if self._grace > 0:
                self._grace -= 1
                return util.clean_text(txt), ms
            self.slow = True
            self.log("精修单次 %.0f ms 超过 %.0f ms 门槛，本机不适合本地大模型，退回云端精修"
                     % (ms, self.slow_ms), "warn", "asr.whisper")
            self.unload()
        return util.clean_text(txt), ms
