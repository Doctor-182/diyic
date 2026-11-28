# -*- coding: utf-8 -*-
r"""显存读数：一条 nvidia-smi 查询，带缓存，永不抛异常。

为什么单独一个模块：这套系统里显存是唯一的硬约束（VRChat 常驻 3.5 GB、
GGUF 档 0.8–1.5 GB、再加上运行库与上下文各 0.4 GB），三处都要用它——
状态页显示、切档前的「看门」、载入后的复核。

两个设计决定：
  1) **缓存 2 秒**：nvidia-smi 一次 60–120 ms，串在 /api/state 里会把 300 ms 的
     响应拖成 400 ms，而界面每 1.5 s 就拉一次状态。2 秒对「看门」足够准
     （真正决定切档时还会再问一次 force=True）。
  2) **读不到就说读不到**：返回 ok=False + err，绝不编数字。没有 N 卡、
    驱动没装、nvidia-smi 不在 PATH 都是正常情形，此时调用方按「未知」处理
     （照装，但日志里写清），而不是一律拒绝。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

# 判定「装得下」时给驱动/碎片留的余量。实测：8151 MiB 的卡报 free 1007 MiB 时，
# 再塞 1051 MiB 的模型不会立刻失败，但 VRChat 会开始掉帧 —— 所以不是「刚好够」就放行。
RESERVE_MB = 256

_CANDIDATES = (
    r"%ProgramFiles%\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    r"%SystemRoot%\System32\nvidia-smi.exe",
)


def exe() -> str:
    r"""nvidia-smi 的路径（找不到返回空串）。"""
    p = shutil.which("nvidia-smi")
    if p:
        return p
    for c in _CANDIDATES:
        c = os.path.expandvars(c)
        if os.path.isfile(c):
            return c
    return ""


def _no_window():
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def _query(timeout=4.0):
    """(total, used, free) MB；失败抛异常，由 probe() 收成 ok=False。"""
    p = exe()
    if not p:
        raise RuntimeError("找不到 nvidia-smi")
    r = subprocess.run([p, "--query-gpu=memory.total,memory.used,memory.free",
                        "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=timeout,
                       startupinfo=_no_window())
    if r.returncode != 0:
        raise RuntimeError("nvidia-smi 退出码 %d：%s" % (r.returncode, (r.stderr or "").strip()[:120]))
    line = (r.stdout or "").strip().splitlines()
    if not line:
        raise RuntimeError("nvidia-smi 没输出")
    parts = [x.strip() for x in line[0].split(",")]
    if len(parts) < 3:
        raise RuntimeError("看不懂 nvidia-smi 输出：%r" % line[0][:80])
    return int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))


def probe(force=False, cache_s=2.0) -> dict:
    """{ok, total_mb, used_mb, free_mb, ts, err}。缓存 cache_s 秒。"""
    c = getattr(probe, "_c", None)
    if c and not force and (time.time() - c["ts"]) < cache_s:
        return c
    t0 = time.time()
    try:
        total, used, free = _query()
        d = {"ok": True, "total_mb": total, "used_mb": used, "free_mb": free,
             "ts": time.time(), "err": "", "ms": round((time.time() - t0) * 1000.0, 1)}
    except Exception as e:                          # noqa: BLE001
        d = {"ok": False, "total_mb": 0, "used_mb": 0, "free_mb": -1,
             "ts": time.time(), "err": str(e)[:160],
             "ms": round((time.time() - t0) * 1000.0, 1)}
    probe._c = d
    return d


def free_mb() -> int:
    """空闲显存 MB；读不到返回 -1（调用方必须按「未知」处理，不能当 0）。"""
    return int(probe().get("free_mb", -1))


def brief(p=None) -> str:
    """给人看的一句话：'已用 6.7 / 8.0 GB · 空 1.0 GB'。"""
    p = p or probe()
    if not p.get("ok"):
        return "显存读数不可用（%s）" % (p.get("err") or "未知")
    return "已用 %.1f / %.1f GB · 空 %.2f GB" % (
        p.get("used_mb", 0) / 1024.0, p.get("total_mb", 0) / 1024.0,
        p.get("free_mb", 0) / 1024.0)
