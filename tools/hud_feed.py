#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hud_feed.py —— 造 30 条假 seg 喂给 hud.py（协议 §4）

用法:
    python tools/hud_feed.py | python vrdub/hud.py
    python tools/hud_feed.py --count 30 --hold 12 --ping 3

只往 stdout 写 JSON 行，每行一条协议消息；stderr 打进度。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time

LIB = [
    ("A", "今天有点迟到了，路上堵车", "今日はちょっと遅れちゃった、道が渋滞してて", "zh", "ja"),
    ("B", "ちょっと待って、今行くから", "等一下，我马上过去", "ja", "zh"),
    ("A", "你那边现在几点", "そっちは今何時？", "zh", "ja"),
    ("C", "[Chatbox] teto: 久しぶり！元気？", "[聊天框] teto：好久不见！还好吗？", "ja", "zh"),
    ("B", "そのワールド、重くない？", "那个世界不卡吗？", "ja", "zh"),
    ("A", "这个副本我们上周打过了", "このレイド、先週やったよね", "zh", "ja"),
    ("B", "I'll be there in five minutes", "我五分钟就到", "en", "zh"),
    ("A", "等一下，我先调一下麦", "ちょっと待って、先にマイク調整する", "zh", "ja"),
    ("C", "[Chatbox] kira: 待って、ロード中…", "[聊天框] kira：等等，加载中…", "ja", "zh"),
    ("A", "你声音有点小，能大一点吗", "声が小さいかも、もう少し大きくできる？", "zh", "ja"),
    ("B", "この曲いいね、誰の？", "这歌不错，谁的？", "ja", "zh"),
    ("A", "别走传送门，我们先去集合点", "ポータル行かないで、先に集合場所に行こう", "zh", "ja"),
    # 下面两条故意写长：验证浮窗自动折行（最多两行）不溢出
    ("A", "说实话这个世界的建模比我想象中精细太多了，尤其是天空盒和灯光",
     "正直、このワールドのモデリングは思ってたよりずっと精細で、特にスカイボックスとライティングがすごい",
     "zh", "ja"),
    ("B", "So basically we have to re-run the whole calibration because the loopback device changed again",
     "简单说就是回环设备又变了，所以整套标定得重跑一遍", "en", "zh"),
]

NOTES = ["", "", "", "预合成命中", "本地草稿", "云端校正"]


def emit(obj):
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # hud 已经退出（截图后自杀），喂数据的一方安静收场
        raise SystemExit(0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--hold", type=float, default=14.0, help="总共喂多少秒")
    ap.add_argument("--ping", type=float, default=3.0, help="ping 间隔秒")
    ap.add_argument("--step", type=float, default=0.16, help="每句间隔秒")
    ap.add_argument("--mic-off-at", type=float, default=9.0, help="第几秒闭麦（<0 关掉）")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    random.seed(args.seed)

    t0 = time.time()
    emit({"op": "cfg", "alpha": 92, "scale": 100, "rows": 8, "click": False, "x": -1, "y": -1})
    emit({"op": "status", "text": "A 链 运行中 · CapsLock 闭麦 · 演示数据"})

    nxt_ping = t0
    mic_off_done = False

    for i in range(args.count):
        if time.time() - t0 > args.hold:
            break
        chain, src, dst, sl, dl = LIB[i % len(LIB)]
        sid = "%s-%d" % (chain, 100 + i)
        asr = random.randint(90, 220) if chain != "C" else random.randint(300, 420)
        mt = random.randint(240, 520)
        tts = random.randint(760, 1240) if chain == "A" else 0
        dur = random.randint(1200, 3600)
        e2e = asr + mt + tts + random.randint(60, 220)

        seg = {"id": sid, "chain": chain, "ts": time.time(), "state": "asr",
               "src_lang": sl, "dst_lang": dl, "engine": "local+cloud",
               "asr_ms": asr, "mt_ms": 0, "tts_ms": 0, "audio_ms": 0,
               "e2e_ms": 0, "rtf": 0, "note": "", "echo": False, "speaker": None}
        emit({"op": "seg", "seg": dict(seg, src=src, dst="")})

        time.sleep(0.05)
        seg2 = {k: seg[k] for k in ("id", "chain", "ts")}
        seg2.update({"state": "mt", "src": src, "dst": "", "asr_ms": asr, "mt_ms": mt})
        emit({"op": "seg", "seg": seg2})

        time.sleep(args.step * 0.5)
        if chain == "B":
            seg3 = {k: seg[k] for k in ("id", "chain", "ts")}
            seg3.update({"state": "done", "src": src, "dst": dst, "asr_ms": asr,
                         "mt_ms": mt, "audio_ms": dur, "e2e_ms": e2e,
                         "rtf": round(dur / e2e, 2), "note": "字幕上屏"})
            emit({"op": "seg", "seg": seg3})
        else:
            seg3 = {k: seg[k] for k in ("id", "chain", "ts")}
            seg3.update({"state": "play", "src": src, "dst": dst, "asr_ms": asr,
                         "mt_ms": mt, "tts_ms": tts, "audio_ms": dur, "e2e_ms": e2e,
                         "rtf": round(dur / e2e, 2), "note": ""})
            emit({"op": "seg", "seg": seg3})
            time.sleep(args.step * 0.4)
            seg4 = {k: seg[k] for k in ("id", "chain", "ts")}
            seg4.update({"state": "done", "src": src, "dst": dst, "asr_ms": asr,
                         "mt_ms": mt, "tts_ms": tts, "audio_ms": dur, "e2e_ms": e2e,
                         "rtf": round(dur / e2e, 2), "note": random.choice(NOTES)})
            emit({"op": "seg", "seg": seg4})

        now = time.time()
        if now - nxt_ping >= args.ping:
            emit({"op": "ping"})
            nxt_ping = now

        if not mic_off_done and args.mic_off_at > 0 and now - t0 >= args.mic_off_at:
            emit({"op": "mic", "on": False})
            emit({"op": "status", "text": "B 链 运行中 · 麦克风已闭"})
            mic_off_done = True

        time.sleep(max(0.01, args.step))

    # 保持管道存活，让 hud 能跑满 --hold
    while time.time() - t0 < args.hold:
        emit({"op": "ping"})
        time.sleep(min(args.ping, 1.0))

    emit({"op": "clear"})
    emit({"op": "quit"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
