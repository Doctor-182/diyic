# -*- coding: utf-8 -*-
r"""扒数据：把 /api/metrics 与 /api/state 的逐段明细摊开，按链 + 引擎分组报数。

用法（在 D:\vrc无言势 下）：
    python tools\lat_report.py                # 打一次快照
    python tools\lat_report.py --watch 10     # 每 10 s 打一行增量 + 跑完再打总表
    python tools\lat_report.py --csv out\lat.csv   # 每次快照追加一行（方便丢进 Excel）
    python tools\lat_report.py --segs 12      # 顺便列最近 12 段的逐段账

口径说明：
    vad   段尾(最后一个字) → 定稿，含停顿确认阀 end_silence
    asr   定稿 → 识别文本
    mt    识别 → 译文
    tts   译文 → 首包音频（A 链出声的关键地板）
    e2e   D 段起点 → 这一句真的出声（负值 = 抢跑，靠预合成/切句提前）
    open  说话停下 → 第一声出来（正数才是人能感觉到的等待）
    cb    定稿 → 聊天框发出（云端预算内）
"""
import json, os, sys, time, urllib.request

BASE = os.environ.get("VRDUB_BASE") or "http://127.0.0.1:8899"
# e2e = 说完最后一个字 → 听见第一声（这才是人能感觉到的等待）
# open = 开口 → 听见第一声（说话本身也含在里面，所以只会比 e2e 大）
STAGES = ("vad", "asr", "mt", "tts", "e2e", "open", "cb")
NAMES = {"vad": "停顿等", "asr": "识别", "mt": "翻译", "tts": "合成首包",
         "e2e": "说完→出声", "open": "开口→出声", "cb": "聊天框"}


def get(path, timeout=6.0):
    req = urllib.request.Request(BASE + path, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def pct(v, p):
    if not v:
        return 0.0
    s = sorted(v)
    if len(s) == 1:
        return round(s[0], 1)
    i = (len(s) - 1) * p / 100.0
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (i - lo), 1)


def stats(rows, key):
    v = [float(s[key]) for s in rows if s.get(key) not in (None, "")]
    if not v:
        return None
    return {"n": len(v), "avg": round(sum(v) / len(v), 1),
            "p50": pct(v, 50), "p90": pct(v, 90), "max": round(max(v), 1),
            "min": round(min(v), 1)}


def table(rows, title):
    print("\n== %s ==  样本 %d 段（定稿 %d / 出声 %d）" %
          (title, len(rows), sum(1 for s in rows if s.get("src")),
           sum(1 for s in rows if s.get("stage") == "done")))
    if not rows:
        return
    hdr = "%-9s %3s %8s %8s %8s %8s %8s" % ("阶段", "n", "均值", "p50", "p90", "max", "min")
    print(hdr)
    print("-" * len(hdr))
    for k in STAGES:
        st = stats(rows, k + "_ms")
        if not st:
            continue
        print("%-9s %3d %8.1f %8.1f %8.1f %8.1f %8.1f" %
              (NAMES.get(k, k), st["n"], st["avg"], st["p50"], st["p90"], st["max"], st["min"]))
    # 等满阀值的段：asr 时间落在 a_cloud_wait 上（±40 ms）＝ 云端没赶上，白等到点
    try:
        cfg = (get("/api/state").get("cfg") or {})
        valve = float(cfg.get("a_cloud_wait", 0) or 0) * 1000.0
    except Exception:
        valve = 0.0
    if valve > 0:
        hit = [s for s in rows if s.get("asr_ms") and abs(float(s["asr_ms"]) - valve) <= 40]
        if hit:
            print("白等云端 %d/%d 段（asr 卡在 %.0f ms 的预算阀上，云端没回来就走本地）"
                  % (len(hit), len(rows), valve))
    # 引擎分布：直接影响平均值的解释力
    eng = {}
    for s in rows:
        e = s.get("engine") or "-"
        eng[e] = eng.get(e, 0) + 1
    print("引擎 " + "  ".join("%s=%d" % (k, v) for k, v in sorted(eng.items())))
    # 丢段原因 top
    drops = {}
    for s in rows:
        if s.get("state") == "drop":
            note = (s.get("note") or "?")[:28]
            drops[note] = drops.get(note, 0) + 1
    if drops:
        print("丢弃 " + "  ".join("%s×%d" % (k, v) for k, v in
                                  sorted(drops.items(), key=lambda x: -x[1])[:6]))


def per_seg(rows, n):
    sel = [s for s in rows if s.get("src")][-n:]
    print("\n== 最近 %d 段逐段 ===" % len(sel))
    print("%-7s %-5s %-6s %6s %6s %6s %6s %7s %6s  %s" %
          ("段", "链", "引擎", "vad", "asr", "mt", "tts", "e2e", "cb", "文本"))
    for s in sel:
        def g(k):
            v = s.get(k + "_ms")
            return ("%6.0f" % float(v)) if v not in (None, "") else "     -"
        txt = (s.get("src") or "")[:26].replace("\n", " ")
        print("%-7s %-5s %-6s %s %s %s %s %7s %6s  %s" %
              (str(s.get("id"))[-7:], s.get("chain", "?"), (s.get("engine") or "-")[:6],
               g("vad"), g("asr"), g("mt"), g("tts"),
               ("%7.0f" % float(s["e2e_ms"])) if s.get("e2e_ms") is not None else "      -",
               g("cb"), txt))


def one(verbose=True, nseg=0, csv=None):
    d = get("/api/state")
    rows = d.get("last") or []
    m = get("/api/metrics")
    if verbose:
        cfg = d.get("cfg") or {}
        print("时刻 %s  |  阀值 end_silence=%.2fs  a_cloud_wait=%.2fs  chatbox_budget=%.1fs  "
              "引擎 A=%s B=%s" %
              (time.strftime("%H:%M:%S"), float(cfg.get("end_silence", 0) or 0),
               float(cfg.get("a_cloud_wait", 0) or 0), float(cfg.get("chatbox_budget", 0) or 0),
               (d.get("asr") or {}).get("a", "?"), (d.get("asr") or {}).get("b", "?")))
        for ch in ("A", "B", "C"):
            table([s for s in rows if s.get("chain") == ch], ch + " 链")
        if nseg:
            per_seg(rows, nseg)
    if csv:
        os.makedirs(os.path.dirname(csv) or ".", exist_ok=True)
        head = not os.path.exists(csv)
        with open(csv, "a", encoding="utf-8-sig", newline="") as f:
            if head:
                f.write("t,chain,stage,vad,asr,mt,tts,e2e,open,cb,engine\n")
            for s in rows:
                if not s.get("src"):
                    continue
                f.write(",".join([time.strftime("%H:%M:%S"), str(s.get("chain")),
                                  str(s.get("stage")), str(s.get("vad_ms", "")),
                                  str(s.get("asr_ms", "")), str(s.get("mt_ms", "")),
                                  str(s.get("tts_ms", "")), str(s.get("e2e_ms", "")),
                                  str(s.get("open_ms", "")), str(s.get("cb_ms", "")),
                                  str(s.get("engine", ""))]) + "\n")
        print("已追加 %s" % csv)
    return m


def main():
    a = sys.argv[1:]
    watch = 0.0
    nseg = 0
    csv = None
    i = 0
    while i < len(a):
        if a[i] == "--watch" and i + 1 < len(a):
            watch = float(a[i + 1]); i += 2
        elif a[i] == "--csv" and i + 1 < len(a):
            csv = a[i + 1]; i += 2
        elif a[i] == "--segs" and i + 1 < len(a):
            nseg = int(a[i + 1]); i += 2
        else:
            i += 1
    if watch <= 0:
        one(True, nseg, csv)
        return
    print("每 %.0f s 采一次，Ctrl-C 停。跑测试去吧，我在这儿攒数。" % watch)
    seen = None
    try:
        while True:
            d = get("/api/state")
            rows = [s for s in (d.get("last") or []) if s.get("src")]
            if seen is None:
                seen = len(rows)
                print("基线 %d 段" % seen)
            else:
                new = len(rows) - seen
                if new > 0:
                    tail = rows[-new:]
                    def q(k):
                        st = stats(tail, k + "_ms")
                        return ("%s %.0f" % (k, st["p50"])) if st else (k + " -")
                    print("%s  +%d 段 | %s | %s | %s" %
                          (time.strftime("%H:%M:%S"), new, q("asr"), q("tts"), q("e2e")))
                    seen = len(rows)
            if csv:
                one(False, 0, csv)
            time.sleep(watch)
    except KeyboardInterrupt:
        print("\n收工，出总表：")
        one(True, nseg, None)


if __name__ == "__main__":
    main()
