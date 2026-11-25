# -*- coding: utf-8 -*-
r"""C 链验证台：PySide6 造一个 700x300 黑底「VRChat 气泡」窗口，用真·GDI 抓屏跑 OCR。

跑法（必须带 PYTHONPATH / PYTHONIOENCODING）：

    $env:PYTHONPATH="<仓库>\; <仓库>\.pylibs"
    $env:PYTHONIOENCODING="utf-8"
    Set-Location "<仓库>"
    py -3.12 tools\ocr_probe.py

它做四件事：
  1) 造窗：黑底、13px（模拟小气泡）、4 行日文（含假名）+ 1 行中文、固定坐标、置顶不被遮。
  2) set_region 到窗口矩形（物理像素），capture() 存 out\ocr_probe_shot.png。
  3) read() 三次 + test() 一次：打印每行 text/score/lang 与单帧耗时、假名行是否 100% 命中。
  4) poll() 两次证明不重复上报；再改一行字，poll() 只回那一行。
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# DPI 感知必须早于 QApplication：否则 150% 缩放下 Qt 逻辑坐标 ≠ 物理像素，抓屏会错位
from vrdub._ocr_capture import ensure_dpi_aware  # noqa: E402

DPI = ensure_dpi_aware()

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from vrdub.ocr import OcrChain  # noqa: E402

WIN_X, WIN_Y, WIN_W, WIN_H = 120, 160, 700, 300

LINES = [
    ("ja", "こんにちは！このワールドきれいだね"),
    ("ja", "ちょっと待っててね、いま準備する"),
    ("ja", "ここはどこですか？"),
    ("ja", "はじめまして、よろしくおねがいします"),
    ("ch", "今天也很热闹啊"),
]
SWAP = "ありがとう、またね！"


def build_window() -> QWidget:
    win = QWidget()
    win.setWindowTitle("ocr-probe-bubble")
    win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
    # 注意：**不要**开 WA_OpaquePaintEvent —— QWidget + stylesheet 黑底时 Qt 不会重绘背景，
    # setText 后旧文字会留在原位，抓屏抓到「ありがとう、またね」和「ここはどこですか」叠在一起
    # 的鬼影图层，OCR 于是读出「あがどろでまかね!」。这是探针自己的坑，不是 OCR 的。
    win.setAutoFillBackground(True)
    win.setStyleSheet("background:#000000;")
    win.setGeometry(WIN_X, WIN_Y, WIN_W, WIN_H)

    box = QVBoxLayout(win)
    box.setContentsMargins(12, 10, 12, 10)
    box.setSpacing(12)
    labels = []
    for lang, text in LINES:
        lab = QLabel(text, win)
        f = QFont("Yu Gothic UI" if lang == "ja" else "Microsoft YaHei")
        f.setPixelSize(13)                      # 13px：上一代实测的真机小气泡字高
        f.setStyleStrategy(QFont.PreferAntialias)
        lab.setFont(f)
        lab.setStyleSheet("color:#ffffff;background:transparent;")
        lab.setFixedHeight(18)
        box.addWidget(lab)
        labels.append(lab)
    box.addStretch(1)
    win._labels = labels                     # type: ignore[attr-defined]
    return win


def physical_rect(win: QWidget):
    """Qt 逻辑坐标 → 屏幕物理像素（per-monitor v2 下乘 devicePixelRatio）。"""
    g = win.frameGeometry()
    r = float(win.devicePixelRatioF() or 1.0)
    return (int(round(g.x() * r)), int(round(g.y() * r)),
            int(round(g.width() * r)), int(round(g.height() * r)))


def main() -> int:
    app = QApplication([])
    win = build_window()
    win.show()
    win.raise_()
    win.activateWindow()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    time.sleep(0.35)
    app.processEvents()

    rect = physical_rect(win)
    print("dpi=%s  qt_logical=%s  physical=%s  dpr=%.2f"
          % (DPI, list(win.frameGeometry().getRect()), list(rect), win.devicePixelRatioF()))

    cfg = {"ocr_region": list(rect), "ocr_interval": 0.3, "ocr_crop_scale": 3.0}
    chain = OcrChain(cfg, log=print)          # 无 host/key → 不走 MT，只验 OCR
    chain.set_region(*rect)

    # --- 1) 冷启一次（含模型加载 + warmup）
    t0 = time.perf_counter()
    r = chain.test(shot=os.path.join(_ROOT, "out", "ocr_probe_shot.png"))
    cold = (time.perf_counter() - t0) * 1000.0
    st = chain.stats()
    eng = st.get("engine") or {}
    print("test ok=%s  total=%.0fms  ocr=%.0fms  cap=%.0fms  det=%.0f  rec=%.0f  ja=%.0f  boxes=%d"
          % (r.get("ok"), cold, float(r.get("ocr_ms") or 0.0), float(r.get("cap_ms") or 0.0),
             float(r.get("det_ms") or 0.0), float(r.get("rec_ms") or 0.0),
             float(r.get("ja_ms") or 0.0), int(r.get("boxes") or 0)))
    if eng:
        print("engine load=%.0fms warmup=%.0fms crop_scale=%.1f primary=%s"
              % (float(eng.get("load_ms") or 0.0), float(eng.get("warmup_ms") or 0.0),
                 float(eng.get("crop_scale") or 0.0), eng.get("primary", "")))
    if not r.get("ok"):
        print("FAIL: %s" % r.get("err"))

    got = [ln["text"] for ln in (r.get("lines") or [])]
    for ln in r.get("lines") or []:
        print("  [%s] %.3f %-6s %s" % (ln["lang"], float(ln["score"]), ln["lang"], ln["text"]))
    print("  原文: %s" % r.get("src", "").replace("\n", " | "))
    print("  译文: %s" % r.get("dst", "").replace("\n", " | "))

    # --- 2) 假名正确率（逐行子串匹配：OCR 可能多/少标点）
    import re as _re

    def _norm(s: str) -> str:
        # ja rec 会把全角 ！？、。 输出成半角，比较时一律去掉标点
        return _re.sub(r"[！!？?、。，,\.\s　・…]", "", s or "")

    def _hit(target: str, pool) -> bool:
        core = _norm(target)
        for g in pool:
            if core and core in _norm(g):
                return True
        return False

    ja_targets = [t for lang, t in LINES if lang == "ja"]
    hits = [_hit(t, got) for t in ja_targets]
    print("假名/日文行命中 %d/%d = %.0f%%  %s"
          % (sum(hits), len(hits), 100.0 * sum(hits) / max(1, len(hits)),
             " ".join("%s:%s" % (t[:8], "OK" if h else "MISS") for t, h in zip(ja_targets, hits))))
    ch_hit = any("今天也很热闹" in g for g in got)
    print("中文行 = %s" % ("OK" if ch_hit else "MISS"))

    # --- 3) 稳态 read 计时（force 绕过帧差）
    times = []
    for i in range(5):
        t1 = time.perf_counter()
        lines = chain.read(force=True)
        dt = (time.perf_counter() - t1) * 1000.0
        times.append(dt)
        st2 = chain.stats()
        print("read #%d  %.0fms  lines=%d  det=%.0f rec=%.0f ja=%.0f reuse=%s"
              % (i + 1, dt, len(lines), float(st2.get("last_det_ms") or 0.0),
                 float(st2.get("last_rec_ms") or 0.0), float(st2.get("last_ja_ms") or 0.0),
                 st2.get("rec_reuse")))
    times.sort()
    print("read 稳态 median=%.0fms min=%.0f max=%.0f（同帧复用 rec 缓存）"
          % (times[len(times) // 2], times[0], times[-1]))

    # --- 4) poll 去重
    p1 = chain.poll()
    p2 = chain.poll()
    print("poll #1 -> %d 行；poll #2 -> %d 行（应 0）" % (len(p1), len(p2)))

    # 改一行字 → 只应回这一行
    win._labels[2].setText(SWAP)             # type: ignore[attr-defined]
    win._labels[2].repaint()                 # type: ignore[attr-defined]
    win.repaint()
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    time.sleep(0.25)
    app.processEvents()
    p3 = chain.poll()
    st3 = chain.stats()
    print("改字后 poll -> %d 行 %s  (帧差 mean=%s cell=%s)"
          % (len(p3), [x["text"] for x in p3], st3.get("last_diff"), st3.get("last_cell")))

    # --- 5) 后台线程端到端（start/stop + on_text 回调契约）
    win._labels[2].setText(LINES[2][1])      # type: ignore[attr-defined] 复原第 3 行
    win._labels[2].repaint()                 # type: ignore[attr-defined]
    win.repaint()
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    print("  label3 实际文字 = %r" % win._labels[2].text())   # type: ignore[attr-defined]
    events = []
    chain.reset_dedupe()                     # 前面的 poll() 已经把这些行标成「播报过」
    chain.on_text = lambda *a: events.append(a)
    chain.on_clear = lambda: events.append(("clear",))
    chain.interval = 0.3
    started = chain.start()
    t1 = time.perf_counter()
    while time.perf_counter() - t1 < 3.0:
        app.processEvents()
        time.sleep(0.05)
    chain.stop()
    got = [e for e in events if len(e) == 5]
    clears = len(events) - len(got)
    print("start()=%s  3s 内 on_text=%d 次 on_clear=%d 次（应 5 行各 1 次、无重复）"
          % (started, len(got), clears))
    for e in got[:6]:
        print("  key=%-28s %.0fms box=%s  %s -> %s" % (e[0], float(e[4]), tuple(e[3]), e[1], e[2]))
    print("  watch 快照 = %s" % chain.capture())
    keys = {e[0] for e in got}
    print("  去重校验: 唯一 key=%d（5 行 → 5 个不同位置 key）" % len(keys))

    chain.close()
    win.close()
    print("stats=%s" % {k: v for k, v in chain.stats().items() if k != "engine"})
    return 0 if (r.get("ok") and ch_hit) else 1


if __name__ == "__main__":
    raise SystemExit(main())