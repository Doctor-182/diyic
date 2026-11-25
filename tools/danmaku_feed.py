#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把字幕事件喂给弹幕浮窗（vrdub/danmaku.py）。

两个数据源：
  1) --src demo ：内置 6 条不同链的句子（不需要后端），自测/截图用；
  2) --src sse  ：连真后端 http://127.0.0.1:8899/api/events（SSE），
                  把 seg 事件原样转成弹幕进程的 stdin 行。

用法：
  python tools/danmaku_feed.py --src demo --count 6 --hold 9 | python -m vrdub.danmaku
  python tools/danmaku_feed.py --src sse  --hold 600       | python -m vrdub.danmaku

输出（stdout，一行一个 JSON，正好是协议 §4 的格式）：
  {"op":"cfg","top_ratio":0.06,"lanes":4,"max_live":6,"dur_s":7.0,"alpha":100}
  {"op":"seg","seg":{"id":"demo-1","chain":"A","state":"asr","src":"…","dst":""}}
  {"op":"seg","seg":{"id":"demo-1","chain":"A","state":"done","src":"…","dst":"…"}}
  {"op":"ping"}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

DEMO = [
    ("A", "收到，我马上过去。", "わかった、すぐ行くね。"),
    ("B", "这个副本我们上周打过了。", "このダンジョン先週もうクリアしたよ。"),
    ("C", "别走传送门，先在集合点等。", "ポータルはやめて、集合場所で待って。"),
    ("A", "我的网络有点卡，可能会断一下。", "ネットがちょっと重い、途切れるかも。"),
    ("B", "今天的队伍配置挺稳的。", "今日の編成はなかなか安定してるね。"),
    ("C", "那个世界加载完了吗？", "あのワールド、読み込み終わった？"),
    ("A", "先别冲，等坦克拉住仇恨。", "突っ込まないで、タンクのヘイト待ち。"),
    ("B", "一二三四五，六七八九十。", "いちにさんしご、ろくしちはちきゅうじゅう。"),
]


def emit(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_demo(args):
    emit({"op": "cfg", "top_ratio": args.top_ratio, "lanes": args.lanes,
          "max_live": args.max_live, "dur_s": args.dur_s, "alpha": args.alpha})
    # 先给几条已经落定的（截图时一次能看到多条），再补 live 更新
    n = max(1, min(len(DEMO), args.count))
    for i in range(n):
        chain, dst, src = DEMO[i]
        sid = "demo-%d" % (i + 1)
        # 第一次：还在识别（只有原文）→ 弹幕先飘出来
        if args.two_step:
            emit({"op": "seg", "seg": {"id": sid, "chain": chain, "state": "asr",
                                       "src": src, "dst": ""}})
            time.sleep(min(0.4, args.gap / 1000.0 / 2))
        # 第二次：同 id 带上译文 → 弹幕原地换字，不新开一条
        emit({"op": "seg", "seg": {"id": sid, "chain": chain, "state": "done",
                                   "src": src, "dst": dst}})
        emit({"op": "ping"})
        time.sleep(args.gap / 1000.0)
    t0 = time.monotonic()
    while time.monotonic() - t0 < args.hold:
        emit({"op": "ping"})
        time.sleep(2.0)


def run_sse(args):
    emit({"op": "cfg", "top_ratio": args.top_ratio, "lanes": args.lanes,
          "max_live": args.max_live, "dur_s": args.dur_s, "alpha": args.alpha})
    url = args.url.rstrip("/") + "/api/events"
    dbg = (lambda *a: sys.stderr.write("[feed] " + " ".join(map(str, a)) + "\n"))
    t0 = time.monotonic()
    last_ping = 0.0
    while time.monotonic() - t0 < args.hold:
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream",
                                                       "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as r:
                dbg("SSE 已连上", url)
                for raw in r:
                    if time.monotonic() - t0 > args.hold:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    try:
                        ev = json.loads(payload)
                    except Exception:
                        continue
                    k = ev.get("k") or ev.get("type")
                    if k == "seg":
                        seg = ev.get("seg") or {}
                        if seg.get("id"):
                            emit({"op": "seg", "seg": seg})
                    elif k == "clear":
                        emit({"op": "clear"})
                    elif k == "mic":
                        emit({"op": "mic", "on": ev.get("on")})
                    elif k in ("hb", "level", "st"):
                        pass
                    if time.monotonic() - last_ping > 4.0:
                        emit({"op": "ping"})
                        last_ping = time.monotonic()
        except Exception as e:
            dbg("SSE 断开:", e, "1.5 s 后重连")
            emit({"op": "ping"})
            time.sleep(1.5)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--src", choices=["demo", "sse"], default="demo")
    ap.add_argument("--url", default="http://127.0.0.1:8899")
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--gap", type=float, default=700.0, help="两条之间毫秒")
    ap.add_argument("--hold", type=float, default=10.0, help="喂完继续 ping 多少秒")
    ap.add_argument("--two-step", action="store_true", default=True,
                    help="每条先 asr 后 done（验证同 id 原地换字）")
    ap.add_argument("--top-ratio", type=float, default=0.06)
    ap.add_argument("--lanes", type=int, default=4)
    ap.add_argument("--max-live", type=int, default=6)
    ap.add_argument("--dur-s", type=float, default=7.0)
    ap.add_argument("--alpha", type=int, default=100)
    args = ap.parse_args()
    if args.src == "demo":
        run_demo(args)
    else:
        run_sse(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
