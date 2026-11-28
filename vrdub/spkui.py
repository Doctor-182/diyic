#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""说话人名单窗口：谁在说、叫什么名字、门限调多少。

为什么单独一个窗口：名单是个「要反复看、反复改」的东西，塞在网页控制台最底下
每次都要滚半天；而它又恰好是「一边听一边调」的活——把它做成置顶小窗，
听的时候看到「3 号」不像人话，直接点进去打名字就行。

窗口内容：
  · 分人开关、余弦门限（0.40–0.98）、最多记几个人（2–24）
  · 每人一行：#编号 / 名字（可直接编辑，回车保存）/ 段数 · 最近出现 / 删掉
  · 状态行：几人在场、算过几段、聚类耗时 p50/p90、样板向量数

数据走 HTTP：GET /api/spk（名单）、POST /api/spk（起名/删人/清空）、
POST /api/cfg（门限/上限/开关）。和网页控制台完全同一套接口，改哪边另一边都看得见。

自检（不上屏，直接存图）：

    python -m vrdub.spkui --shot out/spkui.png
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel,
                               QDoubleSpinBox, QLineEdit, QPushButton, QScrollArea,
                               QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

# 源码运行时是「pythonw vrdub/spkui.py」，没有包上下文，相对导入会直接 ImportError；
# 先把仓库根塞进 sys.path 并声明包名。冻结态（VRDub.exe --child spkui）本来就有包上下文。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "vrdub"

from . import uiapi

W = 470
ROWS_H = 296


def _qss():
    return """
#card { background: rgba(12,16,22,246); border: 1px solid #24303f; border-radius: 14px; }
QLabel { color: #E7EDF6; font-size: 12px; }
QLabel#title { font-size: 13px; font-weight: 600; color: #D6E6F6; }
QLabel#dim { color: #8A97AB; font-size: 11px; }
QLabel#bad { color: #FB7185; font-size: 11px; }
QLabel#id { color: #06202A; background: #22D3EE; border-radius: 6px; font-size: 11px;
           font-weight: 600; padding: 1px 6px; }
QLabel#idn { color: #C8D3E0; background: #263140; border-radius: 6px; font-size: 11px;
            padding: 1px 6px; }
QLineEdit { background: #151B24; border: 1px solid #24303f; border-radius: 6px;
            color: #E7EDF6; font-size: 11px; padding: 2px 6px; }
QLineEdit:focus { border-color: #2f6d84; }
QPushButton#mini { background: #151B24; color: #A8B6C8; border: 1px solid #24303f;
                   border-radius: 7px; padding: 3px 8px; font-size: 11px; }
QPushButton#mini:hover { border-color: #35506a; color: #E7EDF6; }
QPushButton#del { background: transparent; color: #6F7E92; border: none; font-size: 13px; }
QPushButton#del:hover { color: #FB7185; }
QPushButton#send { background: #A78BFA; color: #16101f; border: none; border-radius: 8px;
                   padding: 5px 12px; font-weight: 600; font-size: 11px; }
QCheckBox { color: #A8B6C8; font-size: 11px; spacing: 6px; }
QCheckBox::indicator { width: 13px; height: 13px; border-radius: 4px;
                       border: 1px solid #35506a; background: #151B24; }
QCheckBox::indicator:checked { background: #A78BFA; border-color: #A78BFA; }
QSlider::groove:horizontal { height: 4px; background: #24303f; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; margin: -4px 0; border-radius: 6px;
                             background: #A78BFA; }
QSpinBox { background: #151B24; border: 1px solid #24303f; border-radius: 6px;
           color: #E7EDF6; font-size: 11px; padding: 1px 4px; }
QScrollArea { background: transparent; border: none; }
#rows { background: transparent; }
QWidget#row { background: rgba(255,255,255,6); border-radius: 8px; }
"""


class SpkWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.caller = uiapi.Caller(self)
        self.caller.done.connect(self._on)
        self._sig = None
        self._fails = 0
        self._last = []
        self._build()
        self.setStyleSheet(_qss())
        self.setFixedWidth(W)
        self._t1 = QTimer(self)
        self._t1.setInterval(1500)
        self._t1.timeout.connect(lambda: self.caller.get("/api/spk", "spk", timeout=3.0))
        self._t1.start()
        self._t2 = QTimer(self)
        self._t2.setInterval(4000)
        self._t2.timeout.connect(lambda: self.caller.get("/api/state", "state", timeout=4.0))
        self._t2.start()
        QTimer.singleShot(80, self._poll_all)

    # ---------------------------------------------------------------- 布局
    def _build(self):
        card = QWidget(self)
        card.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        v = QVBoxLayout(card)
        v.setContentsMargins(13, 11, 13, 12)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        t = QLabel("说话人")
        t.setObjectName("title")
        head.addWidget(t)
        self.stat = QLabel("…")
        self.stat.setObjectName("dim")
        head.addWidget(self.stat, 1)
        x = QPushButton("×")
        x.setObjectName("mini")
        x.setFixedSize(20, 20)
        x.setCursor(Qt.PointingHandCursor)
        x.clicked.connect(self.hide)
        head.addWidget(x)
        v.addLayout(head)

        v.addWidget(self._hr())

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.k_on = QCheckBox("分人")
        self.k_on.setToolTip("关掉 = 不算向量、不标号（省 100 ms CPU/段）")
        self.k_on.toggled.connect(self._on_switch)
        r1.addWidget(self.k_on)
        lab = QLabel("门限")
        lab.setObjectName("dim")
        r1.addWidget(lab)
        self.sl = QSlider(Qt.Horizontal)
        self.sl.setRange(40, 98)
        self.sl.setValue(72)
        self.sl.setFixedWidth(120)
        self.sl.valueChanged.connect(self._on_slide)
        self.sl.sliderReleased.connect(self._push_th)
        r1.addWidget(self.sl)
        self.th_lab = QLabel("0.72")
        self.th_lab.setObjectName("dim")
        self.th_lab.setFixedWidth(30)
        r1.addWidget(self.th_lab)
        lab2 = QLabel("最多")
        lab2.setObjectName("dim")
        r1.addWidget(lab2)
        self.sp = QSpinBox()
        self.sp.setRange(2, 24)
        self.sp.setValue(12)
        self.sp.setFixedWidth(64)                 # 50 会把「12」截成「1」
        self.sp.valueChanged.connect(self._on_spin)
        r1.addWidget(self.sp)
        lab3 = QLabel("合并")
        lab3.setObjectName("dim")
        lab3.setToolTip("两个「没起名、只出现 1–2 次」的人余弦超过这个就并成一个号；0 = 不合并")
        r1.addWidget(lab3)
        self.mg = QDoubleSpinBox()
        self.mg.setRange(0.0, 0.98)
        self.mg.setSingleStep(0.02)
        self.mg.setDecimals(2)
        self.mg.setValue(0.70)
        self.mg.setAlignment(Qt.AlignCenter)
        self.mg.setFixedWidth(84)                  # 「0.82」连小数位带箭头，64 会只剩「0」
        self.mg.valueChanged.connect(self._on_merge)
        r1.addWidget(self.mg)
        r1.addStretch(1)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.hint = QLabel("同一句话跟已有的人比余弦，超过门限就是同一个人")
        self.hint.setObjectName("dim")
        r2.addWidget(self.hint, 1)
        b = QPushButton("全清")
        b.setObjectName("mini")
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip("忘掉所有人（重新数）")
        b.clicked.connect(self._reset)
        r2.addWidget(b)
        v.addLayout(r2)

        v.addWidget(self._hr())

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFixedHeight(ROWS_H)
        self.holder = QWidget()
        self.holder.setObjectName("rows")
        self.rows = QVBoxLayout(self.holder)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(5)
        self.rows.addStretch(1)
        self.area.setWidget(self.holder)
        v.addWidget(self.area)

        self.foot = QLabel("点名字直接改，回车保存；删掉的那位下次出现会重新开号")
        self.foot.setObjectName("dim")
        self.foot.setWordWrap(True)
        v.addWidget(self.foot)

    def _hr(self):
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet("background: #1d2734; border: none;")
        return f

    # ---------------------------------------------------------------- 数据
    def _poll_all(self):
        self.caller.get("/api/spk", "spk", timeout=3.0)
        self.caller.get("/api/state", "state", timeout=4.0)

    def _on(self, r):
        tag = str(r.get("_tag") or "")
        if tag == "state":
            if r.get("ok"):
                self._fails = 0
                c = dict(r.get("cfg") or {})
                self.k_on.blockSignals(True)
                self.k_on.setChecked(bool(c.get("spk_on", True)))
                self.k_on.blockSignals(False)
                self.sl.blockSignals(True)
                self.sl.setValue(int(round(float(c.get("spk_th", 0.72)) * 100)))
                self.sl.blockSignals(False)
                self.th_lab.setText("%.2f" % (float(c.get("spk_th", 0.72))))
                self.sp.blockSignals(True)
                self.sp.setValue(int(c.get("spk_max", 12) or 12))
                self.sp.blockSignals(False)
                self.mg.blockSignals(True)
                self.mg.setValue(float(c.get("spk_merge", 0.70) or 0.0))
                self.mg.blockSignals(False)
            else:
                self._fails += 1
                self.stat.setObjectName("bad")
                self.stat.setText("后端没起来")
                self.stat.style().unpolish(self.stat)
                self.stat.style().polish(self.stat)
                if self._fails >= 20:
                    QApplication.quit()
            return
        if tag != "spk":
            return
        if not r.get("ok"):
            return
        s = dict(r.get("spk") or {})
        rows = list(s.get("list") or [])
        self._last = rows
        ms = "%.0f" % float(s.get("ms_p50") or 0)
        p90 = "%.0f" % float(s.get("ms_p90") or 0)
        self.stat.setObjectName("dim")
        self.stat.setText("%d 人在场 · 本次 %d 段 · %s/%s ms（p50/p90）· 样板 %d"
                          % (len(rows), int(s.get("seen") or 0), ms, p90, int(s.get("ex") or 0)))
        self.stat.style().unpolish(self.stat)
        self.stat.style().polish(self.stat)
        if s.get("err"):
            self.foot.setText("模型没载入：" + str(s.get("err"))[:90])
        sig = tuple((int(x.get("id") or 0), str(x.get("name") or ""), int(x.get("hits") or 0))
                    for x in rows)
        if sig != self._sig and not self._editing():
            self._sig = sig
            self._rebuild(rows)

    def _editing(self):
        w = QApplication.focusWidget()
        return isinstance(w, QLineEdit) and w.isVisible()

    def _rebuild(self, rows):
        while self.rows.count() > 1:
            it = self.rows.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if not rows:
            empty = QLabel("还没有分出来的人——B 链收到人说话就会自动建号")
            empty.setObjectName("dim")
            empty.setWordWrap(True)
            self.rows.insertWidget(0, empty)
            return
        for i, x in enumerate(rows):
            self.rows.insertWidget(i, self._row(x))

    def _row(self, x):
        sid = int(x.get("id") or 0)
        nm = str(x.get("name") or "")
        hits = int(x.get("hits") or 0)
        ago = float(x.get("ago") or -1.0)
        w = QWidget()
        w.setObjectName("row")
        h = QHBoxLayout(w)
        h.setContentsMargins(8, 5, 6, 5)
        h.setSpacing(7)
        badge = QLabel(("#" + str(sid)) if nm else ("#" + str(sid)))
        badge.setObjectName("id" if nm else "idn")
        badge.setFixedWidth(38)                  # 三位数编号（#100）不截
        badge.setAlignment(Qt.AlignCenter)
        h.addWidget(badge)
        ed = QLineEdit(nm)
        ed.setPlaceholderText("%d 号（起个名字）" % sid)
        ed.setMaxLength(16)
        ed.setFixedWidth(150)
        ed.editingFinished.connect(lambda s=sid, e=ed: self._rename(s, e.text()))
        h.addWidget(ed)
        meta = QLabel("%d 段 · %s" % (hits, self._ago(ago)))
        meta.setToolTip("段数含以前的场次（存档累计）；上次出现在什么时候看右边")
        meta.setObjectName("dim")
        h.addWidget(meta, 1)
        d = QPushButton("×")
        d.setObjectName("del")
        d.setFixedSize(18, 18)
        d.setCursor(Qt.PointingHandCursor)
        d.setToolTip("把这位从名单里删掉")
        d.clicked.connect(lambda _c=False, s=sid: self._forget(s))
        h.addWidget(d)
        return w

    @staticmethod
    def _ago(sec):
        if sec < 0:
            return "—"
        if sec < 60:
            return "%.0f 秒前" % sec
        if sec < 3600:
            return "%.0f 分钟前" % (sec / 60.0)
        return "%.1f 小时前" % (sec / 3600.0)

    # ---------------------------------------------------------------- 动作
    def _rename(self, sid, name):
        name = (name or "").strip()[:16]
        self.caller.post("/api/spk", {"action": "name", "id": int(sid), "name": name}, "ask",
                         timeout=8.0)
        self._sig = None
        QTimer.singleShot(200, self._poll_all)

    def _forget(self, sid):
        self.caller.post("/api/spk", {"action": "forget", "id": int(sid)}, "ask", timeout=8.0)
        self._sig = None
        QTimer.singleShot(200, self._poll_all)

    def _reset(self):
        self.caller.post("/api/spk", {"action": "reset"}, "ask", timeout=10.0)
        self._sig = None
        QTimer.singleShot(200, self._poll_all)

    def _on_switch(self, on):
        self.caller.post("/api/cfg", {"spk_on": bool(on)}, "ask", timeout=8.0)

    def _on_slide(self, v):
        self.th_lab.setText("%.2f" % (v / 100.0))

    def _push_th(self):
        self.caller.post("/api/cfg", {"spk_th": round(self.sl.value() / 100.0, 2)}, "ask",
                         timeout=8.0)

    def _on_merge(self, v):
        self.caller.post("/api/cfg", {"spk_merge": round(float(v), 2)}, "cfg")

    def _on_spin(self, v):
        self.caller.post("/api/cfg", {"spk_max": int(v)}, "ask", timeout=8.0)


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--shot", default="", help="渲染成 PNG（自检用，不上屏）")
    a, _rest = ap.parse_known_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("VRDub")
    w = SpkWindow()
    scr = QGuiApplication.primaryScreen()
    if scr is not None:
        g = scr.availableGeometry()
        w.move(g.right() - W - 40, g.top() + 80)
    if a.shot:
        w.setAttribute(Qt.WA_DontShowOnScreen, True)
        w.show()
        end = time.time() + 2.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.03)
        img = w.grab()
        try:
            d = os.path.dirname(os.path.abspath(a.shot))
            if d:
                os.makedirs(d, exist_ok=True)
            img.save(a.shot)
            sys.stderr.write("SPKUI_SHOT %s %dx%d\n" % (a.shot, img.width(), img.height()))
        except Exception as e:                             # noqa: BLE001
            sys.stderr.write("SPKUI_SHOT_FAIL %r\n" % (e,))
        w.close()
        return 0
    w.show()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
