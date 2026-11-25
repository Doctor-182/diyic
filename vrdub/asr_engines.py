# -*- coding: utf-8 -*-
"""识别引擎注册表 + 机器条件探测。

这个项目是要能搬到别人机器上的：朋友的机子内存更大、也许没独显、也许 12 GB 显存。
把「用哪个模型」写死在代码里 = 每换一台机器就得改代码。所以这里只做三件事：

  1) SPECS：登记每个引擎的规格——支持哪些语言、多大、要多少显存/内存/几核、
     实测延迟区间、实测质量分、能不能离线；
  2) probe()：读当前机器的条件（有没有独显、空闲显存、总/空闲内存、核数、有没有网）；
  3) pick()：按条件给某个语种选一档，并给出「为什么选它 / 为什么排除别人」的理由，
     理由是给人看的——换机器时第一眼就知道这台机器会跑什么。

规矩：
  · 只在「有实测数字」时写 quality，没测过就写 None（别用猜的数字骗自己）；
  · 选档永远可被手动覆盖（cfg.asr_engine_map / asr_engine_a/b），自动只负责默认值；
  · 不 import 任何模型库（这里只做决策，载入由 asr.py / asr_whisper.py 负责），
    这样在没装依赖的机器上也能算出「该用谁」。
"""
from __future__ import annotations

import os
import time

# ---------------------------------------------------------------- 引擎规格
# lat：本机实测的 p50 区间（ms）。别人的机器会不同，但量级关系不会翻。
# quality：与「干净素材的云端文本」比的相似度（0–1），None = 没测过。
SPECS = [
    {
        "id": "sensevoice",
        "name": "SenseVoice-Small int8",
        "kind": "local",
        "langs": ["zh", "ja", "en", "yue", "auto"],
        "size_mb": 228,
        "vram_mb": 0,          # 纯 CPU
        "ram_mb": 900,
        "cpu_min": 4,
        "lat": (80, 350),
        "quality": {"zh": 0.99, "ja": 0.99, "en": None, "yue": None},
        "offline": True,
        "note": "非自回归，CPU 就够；中文/日语的性价比档，没标点",
    },
    {
        "id": "whisper-anime",
        "name": "anime-whisper CT2 int8",
        "kind": "local",
        "langs": ["ja"],
        "size_mb": 736,
        "vram_mb": 1100,
        "ram_mb": 600,
        "cpu_min": 6,
        "lat": (310, 360),
        "quality": {"ja": 0.968},
        "offline": True,
        "note": "日语专精、带标点；CUDA 才有意义（CPU 10–26 s），质量并不比 SenseVoice 高",
    },
    {
        "id": "cloud",
        "name": "qwen-audio-3.0-asr-flash",
        "kind": "cloud",
        "langs": ["auto", "zh", "ja", "en", "ko", "yue"],
        "size_mb": 0,
        "vram_mb": 0,
        "ram_mb": 0,
        "cpu_min": 0,
        "lat": (340, 5200),
        "quality": {"zh": 1.0, "ja": 1.0},
        "offline": False,
        "note": "最准，抖动最大（实测 0.34–13.2 s），要网、要套餐额度",
    },
]

BY_ID = {s["id"]: s for s in SPECS}


def get(eid: str):
    return BY_ID.get(str(eid or "").strip())


def langs_of(eid: str):
    s = get(eid)
    return list(s["langs"]) if s else []


# ---------------------------------------------------------------- 机器条件
def _ram_mb():
    """(总, 空闲) MB。ctypes 读，不依赖 psutil。"""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return int(m.ullTotalPhys / 1048576), int(m.ullAvailPhys / 1048576)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        avail = os.sysconf("SC_AVPHYS_PAGES")
        ps = os.sysconf("SC_PAGE_SIZE")
        return int(pages * ps / 1048576), int(avail * ps / 1048576)
    except Exception:                                    # noqa: BLE001
        return 0, 0


def probe(refresh=False, cache_s=30.0):
    """当前机器条件。结果缓存 30 s（显存会被 VRChat 吃掉又还回来，不必每句都问）。"""
    g = getattr(probe, "_c", None)
    if g and not refresh and (time.time() - g["ts"]) < cache_s:
        return g
    vram = -1
    try:
        from .asr_whisper import free_vram_mb
        vram = free_vram_mb()
    except Exception:                                    # noqa: BLE001
        vram = -1
    total, free = _ram_mb()
    g = {
        "ts": time.time(),
        "gpu": vram >= 0,
        "vram_free_mb": vram,
        "ram_total_mb": total,
        "ram_free_mb": free,
        "cores": os.cpu_count() or 1,
    }
    probe._c = g
    return g


# ---------------------------------------------------------------- 选档
def fit(eid: str, cond=None):
    """(能不能装得下, 理由)。装得下不等于值得跑——那部分交给 pick()。"""
    s = get(eid)
    c = cond or probe()
    if s is None:
        return False, "没有这个引擎"
    if s["kind"] == "cloud":
        return True, "云端，不占本机资源"
    need_vram = int(s.get("vram_mb") or 0)
    if need_vram > 0:
        if not c.get("gpu"):
            return False, "要显卡，但这台机器没有可用的 NVIDIA 卡"
        if c.get("vram_free_mb", 0) < need_vram:
            return False, ("要 %d MB 显存，现在只空 %d MB（VRChat 占着就先别抢）"
                           % (need_vram, c.get("vram_free_mb", 0)))
    # 内存看「总量」不看「空闲」：空闲量随 VRChat / 浏览器每秒都在变，
    # 拿它做判据会让同一台机器同一分钟里反复「装得下 / 装不下」，
    # 选档结果跟着抖（实测：B 链一会儿走本地、一会儿退回云端，端到端 0.4 s ↔ 1.7 s）。
    # 装不下是机器能力问题，涨落不是。
    need_ram = int(s.get("ram_mb") or 0)
    if need_ram > 0 and c.get("ram_total_mb", 0) and c["ram_total_mb"] < need_ram:
        return False, "要 %d MB 内存，这台一共才 %d MB" % (need_ram, c["ram_total_mb"])
    if s.get("cpu_min") and (c.get("cores") or 1) < s["cpu_min"]:
        return False, "要 %d 核，这台只有 %d 核" % (s["cpu_min"], c.get("cores") or 1)
    return True, "装得下"


def pick(lang="auto", prefer=None, cond=None):
    """给某个语种挑一个本地引擎：(spec | None, 理由)。云端不在这里选。

    prefer：cfg 里手写的引擎 id，能装下就用它（人说了算）；装不下会说明原因再回落。
    """
    c = cond or probe()
    lang = str(lang or "auto").strip().lower()
    if prefer:
        s = get(prefer)
        if s is None:
            return None, "配置里的引擎 '{}' 不认识，按自动选".format(prefer)
        if s["kind"] == "cloud":
            return None, "云端引擎不参与本地选档"
        if lang not in s["langs"] and "auto" not in s["langs"]:
            return None, "{} 不支持 {}（它只做 {}），按自动选".format(
                s["id"], lang, "/".join(s["langs"]))
        ok, why = fit(s["id"], c)
        if ok:
            return s, "手写指定：{}（{}）".format(s["id"], why)
        return None, "手写的 {} 装不下：{}，按自动选".format(s["id"], why)
    cands, why_not = [], []
    for s in SPECS:
        if s["kind"] != "local":
            continue
        if lang not in s["langs"] and "auto" not in s["langs"]:
            continue
        ok, why = fit(s["id"], c)
        if not ok:
            why_not.append("{}：{}".format(s["id"], why))
            continue
        q = (s["quality"] or {}).get(lang)
        lat = s["lat"][1] if s.get("lat") else 9999
        cands.append((-(q if q is not None else 0.0), lat, s))
    if not cands:
        return None, "没有可用的本地引擎（" + "；".join(why_not or ["该语种没有本地引擎"]) + "）"
    cands.sort(key=lambda x: (x[0], x[1]))
    s = cands[0][2]
    return s, "自动选 {}：{} 语实测质量 {} / 延迟 {}–{} ms（{}）".format(
        s["id"], lang, (s["quality"] or {}).get(lang),
        s["lat"][0], s["lat"][1], s["note"])


def describe(cond=None):
    """给控制台/日志看的一段话：这台机器会跑什么，为什么不是别的。"""
    c = cond or probe()
    lines = ["机器条件：{} 核 / 内存 {} GB（空 {} MB）/ 显存空闲 {} MB".format(
        c.get("cores"), round((c.get("ram_total_mb") or 0) / 1024.0, 1),
        c.get("ram_free_mb"), c.get("vram_free_mb"))]
    for lang in ("zh", "ja"):
        s, why = pick(lang, cond=c)
        lines.append("  {} -> {}".format(lang, why if s else why))
    return lines
