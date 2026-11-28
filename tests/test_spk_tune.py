# -*- coding: utf-8 -*-
r"""真值表：分人提准的三件套（掐静音 / 双窗平均 / 碎簇合并）。

对应 vrdub/speaker.py 里 trim_silence、embed 的双窗、_merge_pass。
都是纯 numpy 逻辑，不载模型、不联网。

跑法：python -m tests.test_spk_tune
"""
import sys

import numpy as np

from vrdub import speaker as spkmod


class FCfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


BAD = []


def ok(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        BAD.append(msg)


def voice(sec=3.0, sr=16000, amp=0.2, lead=0.6, tail=0.5):
    """一段「先静音、再说话、再静音」的假音频。"""
    n = int(sec * sr)
    a = np.zeros(n, dtype=np.float32)
    a[int(lead * sr):int((sec - tail) * sr)] = (amp * np.sin(
        2 * np.pi * 220 * np.arange(int((sec - lead - tail) * sr)) / sr)).astype(np.float32)
    return a


def main():
    print("[1] 掐静音：首尾底噪被切掉，长度变短，中间内容还在")
    a = voice(3.0)
    b = spkmod.trim_silence(a)
    ok(len(b) < len(a), "长度从 %d 减到 %d" % (len(a), len(b)))
    ok(0.5 < len(b) / 16000.0 < 2.5, "剩下的接近真正说话的长度（%.2f s）" % (len(b) / 16000.0))
    ok(float(np.abs(b).max()) > 0.15, "中间的声音没被切掉")

    print("[2] 全静音 / 极短输入：原样返回，不炸")
    z = np.zeros(16000, dtype=np.float32)
    ok(len(spkmod.trim_silence(z)) == 16000, "全静音原样返回")
    ok(len(spkmod.trim_silence(np.zeros(10, dtype=np.float32))) == 10, "10 个样本也不炸")

    print("[3] 双窗平均：段够长时 embed 会算两次，结果仍是单位向量")
    calls = {"n": 0}

    class Sess:
        def run(self, out, feeds):          # Speaker._run 调的就是 ort 的这个形状
            calls["n"] += 1
            v = np.ones(192, dtype=np.float32)
            return [v / np.linalg.norm(v)]

    s = spkmod.Speaker(FCfg({"spk_tta": True, "spk_secs": 6.0}), log=lambda *a, **k: None,
                       persist=False)
    s._sess = Sess()
    s._load_err = ""
    v = s.embed(voice(4.0, amp=0.3, lead=0.3, tail=0.3), 16000)
    ok(v is not None and abs(float(np.linalg.norm(v)) - 1.0) < 1e-4, "输出是单位向量")
    ok(calls["n"] == 2, "掐完静音还剩 3.4 s，走了双窗（算 %d 次）" % calls["n"])

    calls["n"] = 0
    s.embed(voice(1.2, amp=0.3, lead=0.2, tail=0.2), 16000)
    ok(calls["n"] == 1, "只剩 0.8 s 的短段不双窗（算 %d 次）" % calls["n"])

    calls["n"] = 0
    s2 = spkmod.Speaker(FCfg({"spk_tta": False, "spk_secs": 6.0}), log=lambda *a, **k: None,
                        persist=False)
    s2._sess = Sess()
    s2._load_err = ""
    s2.embed(voice(4.0, amp=0.3, lead=0.3, tail=0.3), 16000)
    ok(calls["n"] == 1, "关掉双窗只算一次")

    print("[4] 碎簇合并：两个很像的小簇并成一个号，各留一个不一样的人")
    s3 = spkmod.Speaker(FCfg({"spk_th": 0.9, "spk_max": 8, "spk_merge": 0.82}),
                        log=lambda *a, **k: None, persist=False)
    s3._load_err = ""
    base = np.zeros(192, dtype=np.float32)
    base[0] = 1.0
    near = base.copy()
    near[0] = 0.99
    near[1] = 0.14            # 与 base 余弦 ≈ 0.99，但门限 0.9 也会归进去
    near /= np.linalg.norm(near)
    other = np.zeros(192, dtype=np.float32)
    other[5] = 1.0
    p1, _, new1 = s3.assign(base)
    ok(new1 and len(s3._cent) == 1, "第一段开 1 号")
    p2, _, _ = s3.assign(other)
    ok(len(s3._cent) == 2, "完全不同的声音开 2 号")
    # 造一个「很像 1 号但门限没进」的第三簇，然后手工合并
    s3._cent.append(near.copy())
    s3._ex.append([near.copy()])
    s3._count.append(1)
    s3._last.append(0.0)
    s3._names.append("")
    s3._ids.append(99)
    ok(len(s3._cent) == 3, "手工加了一个 99 号碎簇")
    merged = s3._merge_pass(keep_id=s3._ids[p2])
    ok(bool(merged), "合并发生了：%r" % (merged,))
    ok(99 not in s3._ids, "99 号被并掉了")
    ok(len(s3._cent) == 2, "剩下两个人")

    print("[5] keep_id 保护：刚归好的号不许被并走")
    s4 = spkmod.Speaker(FCfg({"spk_th": 0.9, "spk_max": 8, "spk_merge": 0.5}),
                        log=lambda *a, **k: None, persist=False)
    s4._load_err = ""
    a1 = np.zeros(192, dtype=np.float32)
    a1[0] = 1.0
    a2 = np.zeros(192, dtype=np.float32)
    a2[0] = 0.9
    a2[1] = 0.43
    a2 /= np.linalg.norm(a2)          # 与 a1 余弦 ≈ 0.9
    s4.assign(a1)
    pos, _, _ = s4.assign(a2)
    keep_id = s4._ids[pos]
    s4._merge_pass(keep_id=keep_id)
    ok(keep_id in s4._ids, "keep_id=%d 还在" % keep_id)

    print("[6] spk_merge=0 等于关掉")
    s5 = spkmod.Speaker(FCfg({"spk_th": 0.9, "spk_max": 8, "spk_merge": 0.0}),
                        log=lambda *a, **k: None, persist=False)
    s5._load_err = ""
    s5.assign(a1)
    s5.assign(a2)
    ok(s5._merge_pass() == [], "关掉之后一次都不并")

    print()
    if BAD:
        print("RESULT: %d FAIL" % len(BAD))
        for m in BAD:
            print(" - " + m)
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
