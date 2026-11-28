#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""VRDUB 悬浮球：一个能拖的小圆球，点开就是最常用的几件事。

为什么要有它：VRChat 全屏的时候回控制台要么切窗口（切出去人就听不见了），
要么摘头显。球只有 56 px，拖到不挡视线的地方；点一下展开一张卡片：

  · A 链目标语言（我说的中文翻成哪种语言）
  · 采集 / 输出 / 监听 三个设备（换耳机不用回控制台）
  · 一个输入框：打中文 → 翻成目标语言 → 发 VRChat 聊天框（可选同时念出来）

数据全走 HTTP（/api/state、/api/cfg、/api/text_send、/api/win），和网页控制台同一套接口；
后端没起来就变灰、面板写「后端没起来」，绝不拿假数据糊弄。连不上超过 60 s 自己退出，
免得后端没了还留一个孤儿球在屏幕上。

单独看外观（不上屏，直接存图，自检用）：

    python -m vrdub.ball --shot out/ball.png
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QLinearGradient, QPainter,
                           QPen, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

# 源码运行时是「pythonw vrdub/ball.py」，没有包上下文，相对导入会直接 ImportError；
# 先把仓库根塞进 sys.path 并声明包名。冻结态（VRDub.exe --child ball）本来就有包上下文。
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "vrdub"

from . import paths as _paths
from . import uiapi

SIZE = 56                       # 球的直径
PANEL_W = 330                   # 卡片宽（设备名带「#号」也不至于被截掉）
STORE = "ball.json"             # 球的位置（用户目录 config 下）
LANGS = (("ja", "日语"), ("en", "英语"), ("ko", "韩语"), ("zh", "中文"))


def _store_path():
    try:
        return os.path.join(_paths.CONFIG_DIR, STORE)
    except Exception:                                  # noqa: BLE001
        return ""


def _load_pos():
    p = _store_path()
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        x, y = int(d.get("x", -1)), int(d.get("y", -1))
        return (x, y) if x >= 0 and y >= 0 else None
    except Exception:                                  # noqa: BLE001
        return None


def _save_pos(x, y):
    p = _store_path()
    if not p:
        return
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"x": int(x), "y": int(y)}, f)
        os.replace(tmp, p)
    except Exception:                                  # noqa: BLE001
        pass


def _qss():
    return """
#card { background: rgba(12,16,22,244); border: 1px solid #24303f; border-radius: 14px; }
QLabel { color: #E7EDF6; font-size: 12px; }
QLabel#title { font-size: 13px; font-weight: 600; color: #D6E6F6; }
QLabel#dim { color: #8A97AB; font-size: 11px; }
QLabel#good { color: #22D3EE; font-size: 11px; }
QLabel#bad { color: #FB7185; font-size: 11px; }
QLabel#lab { color: #8A97AB; font-size: 11px; }
QComboBox { background: #151B24; border: 1px solid #24303f; border-radius: 7px;
            color: #E7EDF6; padding: 3px 8px; font-size: 12px; min-height: 20px; }
QComboBox:hover { border-color: #35506a; }
QComboBox::drop-down { border: none; width: 16px; }
QComboBox QAbstractItemView { background: #151B24; color: #E7EDF6; border: 1px solid #24303f;
                              selection-background-color: #1d3646; outline: none; }
QPlainTextEdit#box { background: #151B24; border: 1px solid #24303f; border-radius: 8px;
                     color: #E7EDF6; font-size: 12px; padding: 4px; }
QPlainTextEdit#box:focus { border-color: #2f6d84; }
QPushButton#send { background: #22D3EE; color: #06202A; border: none; border-radius: 9px;
                   padding: 7px 0; font-weight: 600; font-size: 12px; }
QPushButton#send:hover { background: #3fe0f5; }
QPushButton#send:disabled { background: #263140; color: #6F7E92; }
QPushButton#mini { background: #151B24; color: #A8B6C8; border: 1px solid #24303f;
                   border-radius: 8px; padding: 4px 8px; font-size: 11px; }
QPushButton#mini:hover { border-color: #35506a; color: #E7EDF6; }
QCheckBox { color: #A8B6C8; font-size: 11px; spacing: 6px; }
QCheckBox::indicator { width: 13px; height: 13px; border-radius: 4px;
                       border: 1px solid #35506a; background: #151B24; }
QCheckBox::indicator:checked { background: #22D3EE; border-color: #22D3EE; }
"""


class Ball(QWidget):
    """那颗球：拖动挪位置，点一下开合面板。"""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(SIZE, SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("VRDub 快速面板（拖动可以挪位置）")
        self.panel = None
        self.ok = None                 # None=还没问到；True/False=后端通不通
        self._press = None
        self._w0 = None
        self._drag = False
        p = _load_pos()
        if p:
            self.move(p[0], p[1])
        else:
            scr = QGuiApplication.primaryScreen()
            g = scr.availableGeometry() if scr else None
            self.move((g.width() - SIZE - 36) if g else 40, (g.height() // 2) if g else 40)

    # ---------------------------------------------------------------- 画
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(2.0, 2.0, SIZE - 4.0, SIZE - 4.0)
        grad = QRadialGradient(r.center().x() - 6, r.center().y() - 8, SIZE)
        if self.ok is False:
            grad.setColorAt(0.0, QColor(0x4b, 0x55, 0x63))
            grad.setColorAt(1.0, QColor(0x22, 0x28, 0x31))
        else:
            grad.setColorAt(0.0, QColor(0x35, 0xE6, 0xF2))
            grad.setColorAt(0.6, QColor(0x22, 0x9B, 0xCE))
            grad.setColorAt(1.0, QColor(0x2563EB))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(r)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1.2))
        p.drawEllipse(r.adjusted(0.6, 0.6, -0.6, -0.6))
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(24)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 245))
        p.drawText(r, Qt.AlignCenter, "言")
        p.end()

    # ---------------------------------------------------------------- 拖 / 点
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.globalPosition().toPoint()
            self._w0 = self.pos()
            self._drag = False
        ev.accept()

    def mouseMoveEvent(self, ev):
        if self._press is None:
            return
        d = ev.globalPosition().toPoint() - self._press
        if not self._drag and (abs(d.x()) + abs(d.y())) < 5:
            return
        self._drag = True
        self.move(self._w0 + d)
        if self.panel is not None and self.panel.isVisible():
            self.panel.place()

    def mouseReleaseEvent(self, ev):
        was = self._drag
        self._press = None
        self._drag = False
        if not was:
            self.toggle()
        else:
            _save_pos(self.x(), self.y())
        ev.accept()

    # ---------------------------------------------------------------- 面板
    def toggle(self):
        if self.panel is not None and self.panel.isVisible():
            self.panel.hide()
            return
        self.open_panel()

    def open_panel(self):
        if self.panel is None:
            self.panel = Panel(self)
        self.panel.place()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()
        self.panel.refresh_now()

    def closeEvent(self, ev):
        if self.panel is not None:
            self.panel.close()
        ev.accept()


class Panel(QWidget):
    """展开后的那张卡：语言 / 设备 / 说一句。"""

    def __init__(self, ball):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.ball = ball
        self.caller = uiapi.Caller(self)
        self.caller.done.connect(self._on)
        self._loading = False
        self._dev_at = 0.0
        self._fails = 0
        self._cfg = {}
        self._build()
        self.setStyleSheet(_qss())
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    # ---------------------------------------------------------------- 布局
    def _row(self, text, w):
        lay = QHBoxLayout()
        lay.setSpacing(8)
        lab = QLabel(text)
        lab.setObjectName("lab")
        lab.setFixedWidth(44)
        lay.addWidget(lab)
        lay.addWidget(w, 1)
        return lay

    def _combo(self):
        c = QComboBox()
        c.setFocusPolicy(Qt.StrongFocus)
        return c

    def _build(self):
        self.setFixedWidth(PANEL_W)
        card = QWidget(self)
        card.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 11)
        v.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("快速")
        title.setObjectName("title")
        head.addWidget(title)
        head.addStretch(1)
        self.state_lab = QLabel("…")
        self.state_lab.setObjectName("dim")
        head.addWidget(self.state_lab)
        x = QPushButton("×")
        x.setObjectName("mini")
        x.setFixedSize(20, 20)
        x.setCursor(Qt.PointingHandCursor)
        x.clicked.connect(self.hide)
        head.addWidget(x)
        v.addLayout(head)

        self.c_dst = self._combo()
        for k, nm in LANGS:
            self.c_dst.addItem(nm, k)
        v.addLayout(self._row("A 链", self.c_dst))

        self.c_mic = self._combo()
        v.addLayout(self._row("麦克风", self.c_mic))
        self.c_out = self._combo()
        v.addLayout(self._row("输出", self.c_out))
        self.c_mon = self._combo()
        v.addLayout(self._row("监听", self.c_mon))

        self.box = QPlainTextEdit()
        self.box.setObjectName("box")
        self.box.setFixedHeight(54)
        self.box.setPlaceholderText("打一句中文，Ctrl+Enter 发送")
        self.box.installEventFilter(self)
        v.addWidget(self.box)

        opt = QHBoxLayout()
        opt.setSpacing(10)
        self.k_tts = QCheckBox("念出来")
        self.k_tts.setChecked(True)
        self.k_tts.setToolTip("用当前音色合成日语（或目标语）播进 CABLE Input，VRChat 里的人能听见")
        self.k_cb = QCheckBox("发聊天框")
        self.k_cb.setChecked(True)
        self.k_cb.setToolTip("把译文发进 VRChat 聊天框（OSC）")
        opt.addWidget(self.k_tts)
        opt.addWidget(self.k_cb)
        opt.addStretch(1)
        v.addLayout(opt)

        self.btn = QPushButton("发送")
        self.btn.setObjectName("send")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self._send)
        v.addWidget(self.btn)

        self.res = QLabel("")
        self.res.setObjectName("dim")
        self.res.setWordWrap(True)
        self.res.setMinimumHeight(14)
        v.addWidget(self.res)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        for text, fn in (("说话人", self._open_spk), ("控制台", self._open_web),
                         ("收起", self.hide)):
            b = QPushButton(text)
            b.setObjectName("mini")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            foot.addWidget(b)
        foot.addStretch(1)
        v.addLayout(foot)

        self.c_dst.currentIndexChanged.connect(lambda _i: self._cfg_set("dst_lang", self.c_dst.currentData()))
        self.c_mic.currentIndexChanged.connect(lambda _i: self._cfg_set("mic_dev", self.c_mic.currentData()))
        self.c_out.currentIndexChanged.connect(lambda _i: self._cfg_set("out_dev", self.c_out.currentData()))
        self.c_mon.currentIndexChanged.connect(lambda _i: self._cfg_set("mon_dev", self.c_mon.currentData()))

    # ---------------------------------------------------------------- 交互
    def eventFilter(self, obj, ev):
        if obj is self.box and ev.type() == QEvent.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and (ev.modifiers() & Qt.ControlModifier):
                self._send()
                return True
        return False

    def place(self):
        scr = QGuiApplication.primaryScreen()
        area = scr.availableGeometry() if scr else None
        b = self.ball.geometry()
        self.adjustSize()
        w, h = self.width(), self.height()
        x = b.right() + 10
        if area and x + w > area.right():
            x = b.left() - w - 10
        y = b.top() - 6
        if area:
            x = max(area.left() + 6, min(x, area.right() - w - 6))
            y = max(area.top() + 6, min(y, area.bottom() - h - 6))
        self.move(int(x), int(y))

    def refresh_now(self):
        self._poll()
        if time.time() - self._dev_at > 20.0:
            self._dev_at = time.time()
            self.caller.get("/api/devices", "dev", timeout=3.0)

    def _poll(self):
        if not self.isVisible():
            return
        self.caller.get("/api/state", "state", timeout=4.0)

    def _on(self, r):
        tag = str(r.get("_tag") or "")
        if tag == "state":
            if r.get("ok"):
                self._fails = 0
                self.ball.ok = True
                self.ball.update()
                self._apply(r)
            else:
                self._fails += 1
                self.ball.ok = False
                self.ball.update()
                self.state_lab.setObjectName("bad")
                self.state_lab.setText("后端没起来")
                self.state_lab.style().unpolish(self.state_lab)
                self.state_lab.style().polish(self.state_lab)
                self.res.setText(str(r.get("err") or "")[:120])
                self.btn.setEnabled(False)
                if self._fails >= 20:          # 约 60 s 连不上：自己退出，不留孤儿球
                    QApplication.quit()
            return
        if tag == "dev":
            if r.get("ok"):
                self._fill_devices(r.get("devices") or {})
            return
        if tag == "cfg":
            if not r.get("ok"):
                self.res.setText("改配置失败：" + str(r.get("err") or "")[:120])
            return
        if tag == "send":
            self.btn.setEnabled(True)
            if r.get("ok"):
                dst = str(r.get("dst") or "")
                bits = []
                if r.get("mt_ms"):
                    bits.append("翻译 %d ms" % int(r.get("mt_ms") or 0))
                if r.get("tts_ms"):
                    bits.append("合成 %d ms" % int(r.get("tts_ms") or 0))
                if r.get("spoke"):
                    bits.append("已出声")
                if r.get("sent"):
                    bits.append("已发聊天框")
                self.res.setText("→ " + dst[:60] + "（" + " · ".join(bits) + "）")
                self.box.clear()
            else:
                self.res.setText("没发出去：" + str(r.get("err") or "")[:140])
            return
        if tag == "win":
            if not r.get("ok"):
                self.res.setText("窗口没起来：" + str(r.get("err") or "")[:120])
            return

    def _apply(self, s):
        c = dict(s.get("cfg") or {})
        self._cfg = c
        self._loading = True
        try:
            k = str(c.get("dst_lang") or "ja")
            i = self.c_dst.findData(k)
            if i >= 0:
                self.c_dst.setCurrentIndex(i)
            for combo, key in ((self.c_mic, "mic_dev"), (self.c_out, "out_dev"),
                               (self.c_mon, "mon_dev")):
                try:
                    v = int(c.get(key))
                except Exception:                      # noqa: BLE001
                    continue
                j = combo.findData(v)
                if j >= 0:
                    combo.setCurrentIndex(j)
        finally:
            self._loading = False
        lang = dict(LANGS).get(str(c.get("dst_lang") or "ja"), str(c.get("dst_lang") or ""))
        names = c.get("dev_names") or {}
        mic = str((names or {}).get("in") or "未选麦克风")
        self.state_lab.setObjectName("good")
        self.state_lab.setText("A 链 → " + lang)
        self.state_lab.setToolTip("麦克风：" + mic)
        self.state_lab.style().unpolish(self.state_lab)
        self.state_lab.style().polish(self.state_lab)
        self.btn.setEnabled(True)
        if not self.c_mic.count():
            self.refresh_now()

    def _fill_devices(self, d):
        def fill(combo, items):
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for it in items:
                # 号放最前面：下拉框窄，Qt 从尾部省略，号在尾部就会被截掉看不见
                nm = str(it.get("name") or "设备")
                nm = nm if len(nm) <= 22 else nm[:21] + "…"
                combo.addItem("#%s %s" % (it.get("i"), nm), int(it.get("i")))
            if cur is not None:
                j = combo.findData(cur)
                if j >= 0:
                    combo.setCurrentIndex(j)
            combo.blockSignals(False)
        fill(self.c_mic, list(d.get("input") or []))
        fill(self.c_out, list(d.get("output") or []))
        fill(self.c_mon, list(d.get("output") or []))
        self._loading = True
        try:
            for combo, key in ((self.c_mic, "mic_dev"), (self.c_out, "out_dev"),
                               (self.c_mon, "mon_dev")):
                try:
                    v = int(self._cfg.get(key))
                except Exception:                      # noqa: BLE001
                    continue
                j = combo.findData(v)
                if j >= 0:
                    combo.setCurrentIndex(j)
        finally:
            self._loading = False

    def _cfg_set(self, key, val):
        if self._loading or val is None:
            return
        self.caller.post("/api/cfg", {key: val}, "cfg", timeout=8.0)

    def _send(self):
        text = self.box.toPlainText().strip()
        if not text:
            self.res.setText("先打一句中文")
            return
        self.btn.setEnabled(False)
        self.res.setText("发送中…")
        self.caller.post("/api/text_send", {"text": text,
                                            "tts": bool(self.k_tts.isChecked()),
                                            "chatbox": bool(self.k_cb.isChecked()),
                                            "lang": str(self.c_dst.currentData() or "ja")},
                         "send", timeout=40.0)

    def _open_spk(self):
        self.caller.post("/api/win", {"name": "spkui", "on": True}, "win", timeout=6.0)

    def _open_web(self):
        try:
            webbrowser.open("http://127.0.0.1:%d/" % uiapi.port())
        except Exception:                                  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--shot", default="", help="把面板渲染成 PNG（自检用，不上屏）")
    ap.add_argument("--open", action="store_true", help="起手就展开面板")
    ap.add_argument("--x", type=int, default=-1)
    ap.add_argument("--y", type=int, default=-1)
    a, _rest = ap.parse_known_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("VRDub")
    ball = Ball()
    if a.x >= 0 and a.y >= 0:
        ball.move(a.x, a.y)
    if a.shot:
        ball.setAttribute(Qt.WA_DontShowOnScreen, True)
        ball.show()
        ball.open_panel()
        ball.panel.setAttribute(Qt.WA_DontShowOnScreen, True)
        ball.panel.show()
        end = time.time() + 3.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.03)
        img = ball.panel.grab()
        try:
            d = os.path.dirname(os.path.abspath(a.shot))
            if d:
                os.makedirs(d, exist_ok=True)
            img.save(a.shot)
            sys.stderr.write("BALL_SHOT %s %dx%d\n" % (a.shot, img.width(), img.height()))
        except Exception as e:                             # noqa: BLE001
            sys.stderr.write("BALL_SHOT_FAIL %r\n" % (e,))
        ball.close()
        return 0
    ball.show()
    if a.open:
        ball.open_panel()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
