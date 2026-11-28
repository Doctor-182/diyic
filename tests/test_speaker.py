# -*- coding: utf-8 -*-
r"""分人（说话人分离）真值表：不花钱、不碰麦克风，只验聚类与前置处理。

跑法：
  $env:PYTHONPATH="D:\vrc无言势;D:\vrc无言势\.pylibs"
  python -m tests.test_speaker
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vrdub import speaker as spkmod          # noqa: E402

OK, FAIL = [], []


def chk(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + "  " + name + (("  " + str(extra)) if extra else ""))


class Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def unit(x):
    x = np.asarray(x, dtype=np.float32)
    return x / (np.linalg.norm(x) + 1e-9)


def vec(seed, jitter=0.0):
    rng = np.random.RandomState(seed)
    v = rng.normal(size=192).astype(np.float32)
    if jitter:
        v = v + rng.normal(scale=jitter, size=192).astype(np.float32)
    return unit(v)


def main():
    # 1) prepare：int16 归一、多声道下混、重采样、限长
    a = (np.sin(np.linspace(0, 200, 48000)) * 8000).astype(np.int16)
    p = spkmod.prepare(a, 48000, 1.5)
    chk("prepare: int16 → float32 且峰值 <= 1", p.dtype == np.float32 and float(np.max(np.abs(p))) <= 1.0,
        "peak=%.2f" % float(np.max(np.abs(p))))
    chk("prepare: 48k 的 1.0 s → 16k 的 16000 采样点", abs(len(p) - 16000) <= 2, "len=%d" % len(p))
    st = np.stack([a, a], axis=1)
    p2 = spkmod.prepare(st, 16000, 1.0)
    chk("prepare: 立体声下混成单声道", p2.ndim == 1 and abs(len(p2) - 16000) <= 2)
    p3 = spkmod.prepare(np.zeros(16000, dtype=np.float32), 16000, 1.0)
    chk("prepare: 全零不炸", len(p3) == 16000)

    # 2) 空向量 / 静音不产出向量（不加载模型，直接走 ready=False 分支）
    cfg = Cfg(spk_on=True, spk_th=0.72, spk_max=6, spk_secs=1.5, spk_model="")
    sp = spkmod.Speaker(cfg)
    sp._sess = object()                 # 假装载入好了，下面只测前置判据
    chk("embed: 太短（<0.4 s）返回 None", sp.embed(np.zeros(1000, dtype=np.int16), 16000) is None)
    chk("embed: 全静音返回 None", sp.embed(np.zeros(16000, dtype=np.int16), 16000) is None)

    # 3) 聚类：同人（抖动 0.06）归一类，异人开新簇
    sp = spkmod.Speaker(cfg)
    v_a, v_b = vec(1), vec(2)
    n0, s0, new0 = sp.assign(v_a)
    n1, s1, new1 = sp.assign(vec(1, 0.25))
    n2, s2, new2 = sp.assign(v_b)
    chk("聚类: 第 1 段是 0 号且标为新人", (n0, new0) == (0, True))
    chk("聚类: 同一人的第二段归 0 号", (n1, new1) == (0, False), "sim=%.2f" % s1)
    chk("聚类: 另一个人开 1 号簇", (n2, new2) == (1, True), "sim=%.2f" % s2)
    chk("stats: 记了两个簇和两段计数", sp.stats()["clusters"] == 2 and sp.stats()["counts"] == [2, 1],
        sp.stats()["counts"])

    # 4) 门限：调高到 0.95 时，抖动的同人不再合并（会开新簇）
    sp2 = spkmod.Speaker(Cfg(spk_on=True, spk_th=0.95, spk_max=6, spk_secs=1.5, spk_model=""))
    sp2.assign(v_a)
    n, s, new = sp2.assign(vec(1, 0.35))
    chk("门限 0.95：余弦 0.94 的同人不再合并（0.72 时会合并）", new is True and n == 1, "sim=%.2f" % s)

    # 5) 簇上限：满了以后都往最像的归，不再无限开新簇
    sp3 = spkmod.Speaker(Cfg(spk_on=True, spk_th=0.99, spk_max=2, spk_secs=1.5, spk_model=""))
    sp3.assign(vec(11)); sp3.assign(vec(12))
    n, s, new = sp3.assign(vec(13))
    chk("簇满（max=2）：第 3 个人归回最像的簇，不开新簇",
        new is False and sp3.stats()["clusters"] == 2, "n=%d sim=%.2f" % (n, s))

    # 6) 关掉开关 / 太短的段不进队
    sp4 = spkmod.Speaker(Cfg(spk_on=False, spk_th=0.72, spk_max=6, spk_secs=1.5, spk_model=""))
    sp4._sess = object()
    chk("submit: spk_on=False 直接拒绝", sp4.submit({"id": "B-1"}, np.zeros(16000, np.int16)) is False)
    sp5 = spkmod.Speaker(Cfg(spk_on=True, spk_th=0.72, spk_max=6, spk_secs=1.5, spk_model=""))
    sp5._sess = object()
    chk("submit: 0.3 s 的段直接拒绝",
        sp5.submit({"id": "B-2"}, np.zeros(4800, np.int16)) is False)

    # 7) reset 清空簇
    sp.reset()
    chk("reset: 簇清空、计数归零", sp.stats()["clusters"] == 0)

    print()
    if FAIL:
        print("RESULT: FAIL %d 项：%s" % (len(FAIL), FAIL))
        return 1
    print("RESULT: ALL PASS（%d 项）" % len(OK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
