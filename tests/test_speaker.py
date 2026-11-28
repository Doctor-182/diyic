# -*- coding: utf-8 -*-
r"""分人（说话人分离）真值表：不花钱、不碰麦克风，只验聚类、淘汰、起名与存档。

跑法：
  $env:PYTHONPATH="D:\vrc无言势;D:\vrc无言势\.pylibs"
  python -m tests.test_speaker
"""
import os
import sys
import tempfile

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


def mk(**kw):
    """不落盘的 Speaker（测试绝不碰用户目录里的真名单）。"""
    base = dict(spk_on=True, spk_th=0.72, spk_max=12, spk_secs=1.5, spk_model="")
    base.update(kw)
    return spkmod.Speaker(Cfg(**base), persist=False)


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
    sp = mk()
    sp._sess = object()                 # 假装载入好了，下面只测前置判据
    chk("embed: 太短（<0.4 s）返回 None", sp.embed(np.zeros(1000, dtype=np.int16), 16000) is None)
    chk("embed: 全静音返回 None", sp.embed(np.zeros(16000, dtype=np.int16), 16000) is None)

    # 3) 聚类：同人（抖动 0.06）归一类，异人开新簇
    sp = mk()
    v_a, v_b = vec(1), vec(2)
    n0, s0, new0 = sp.assign(v_a)
    n1, s1, new1 = sp.assign(vec(1, 0.25))
    n2, s2, new2 = sp.assign(v_b)
    chk("聚类: 第 1 段是 0 号且标为新人", (n0, new0) == (0, True))
    chk("聚类: 同一人的第二段归 0 号", (n1, new1) == (0, False), "sim=%.2f" % s1)
    chk("聚类: 另一个人开 1 号簇", (n2, new2) == (1, True), "sim=%.2f" % s2)
    chk("stats: 记了两个簇和两段计数", sp.stats()["clusters"] == 2 and sp.stats()["counts"] == [2, 1],
        sp.stats()["counts"])
    chk("编号: 从 1 起、连续", [x["id"] for x in sp.entries()] == [1, 2], sp.entries())

    # 4) 门限：调高到 0.95 时，抖动的同人不再合并（会开新簇）
    sp2 = mk(spk_th=0.95)
    sp2.assign(v_a)
    n, s, new = sp2.assign(vec(1, 0.35))
    chk("门限 0.95：余弦 0.94 的同人不再合并（0.72 时会合并）", new is True and n == 1, "sim=%.2f" % s)

    # 5) 簇满：淘汰「没起名且最久没出现」的那一位，簇数不涨
    sp3 = mk(spk_th=0.99, spk_max=2)
    sp3.assign(vec(11)); sp3.assign(vec(12))
    n, s, new = sp3.assign(vec(13))
    chk("簇满（max=2）：第 3 个人顶掉最久没出现的那位，簇数还是 2",
        new is True and sp3.stats()["clusters"] == 2, "n=%d sim=%.2f" % (n, s))
    chk("簇满：淘汰次数记上了", sp3.stats()["evicted"] == 1, sp3.stats()["evicted"])

    # 6) 起过名字的人永不被淘汰
    sp6 = mk(spk_th=0.99, spk_max=2)
    sp6.assign(vec(21))
    sp6.assign(vec(22))
    sp6.set_name(1, "阿明")
    n, s, new = sp6.assign(vec(23))
    ids = [x["id"] for x in sp6.entries()]
    names = {x["id"]: x["name"] for x in sp6.entries()}
    chk("起过名的 1 号不会被新声音顶掉", 1 in names and names[1] == "阿明", names)
    chk("没起名的 2 号让位给新声音（新号 3），簇数还是 2",
        new is True and ids == [1, 3] and sp6.stats()["clusters"] == 2, ids)

    # 7) 起名 / 清名 / 删掉
    chk("set_name: 起名成功", sp6.set_name(1, "  阿 明 ") is True)
    chk("set_name: 空白压成一个、首尾去掉", {x["id"]: x["name"] for x in sp6.entries()}[1] == "阿 明",
        sp6.entries())
    sp6.set_name(1, "一二三四五六七八九十一二三四五六七八")
    got = {x["id"]: x["name"] for x in sp6.entries()}[1]
    chk("set_name: 名字最多 16 字", len(got) == 16, "len=%d" % len(got))
    chk("set_name: 没有这个编号返回 False", sp6.set_name(99, "x") is False)
    chk("forget: 删掉 1 号之后名单里没有它", sp6.forget(1) is True and
        all(x["id"] != 1 for x in sp6.entries()))

    # 8) 存档：存盘 → 新实例读回 → 同一个声音还是原来那个号（名字也在）
    tmpdir = tempfile.mkdtemp(prefix="vrdub-spk-")
    tmp = os.path.join(tmpdir, "speakers.json")
    orig = spkmod.store_path
    spkmod.store_path = lambda: tmp
    try:
        s1 = spkmod.Speaker(Cfg(spk_on=True, spk_th=0.72, spk_max=12, spk_secs=1.5, spk_model=""),
                            persist=True)
        s1.assign(vec(31))
        s1.assign(vec(32))
        s1.set_name(1, "酒吧老板")
        s1.save(force=True)
        chk("存档: 文件写出来了", os.path.exists(tmp))
        s2 = spkmod.Speaker(Cfg(spk_on=True, spk_th=0.72, spk_max=12, spk_secs=1.5, spk_model=""),
                            persist=True)
        ents = {x["id"]: x["name"] for x in s2.entries()}
        chk("存档: 名字读回来了", ents.get(1) == "酒吧老板", ents)
        pos, sim, new = s2.assign(vec(31))
        nid, nm = s2.label(pos)
        chk("存档: 同一个人回来还是 1 号、名字还在", (nid, nm, new) == (1, "酒吧老板", False),
            "id=%s name=%s new=%s sim=%.2f" % (nid, nm, new, sim))
        chk("存档: 声明了存放路径（界面会显示）", s2.stats()["store"] == tmp, s2.stats()["store"])
        s2.reset()
        s3 = spkmod.Speaker(Cfg(spk_on=True, spk_th=0.72, spk_max=12, spk_secs=1.5, spk_model=""),
                            persist=True)
        chk("存档: reset 之后新实例是空的", s3.stats()["clusters"] == 0, s3.stats()["clusters"])
    finally:
        spkmod.store_path = orig

    # 9) 关掉开关 / 太短的段不进队
    sp4 = mk(spk_on=False)
    sp4._sess = object()
    chk("submit: spk_on=False 直接拒绝", sp4.submit({"id": "B-1"}, np.zeros(16000, np.int16)) is False)
    sp5 = mk()
    sp5._sess = object()
    chk("submit: 0.3 s 的段直接拒绝",
        sp5.submit({"id": "B-2"}, np.zeros(4800, np.int16)) is False)

    # 10) reset 清空簇
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
