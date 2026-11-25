#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VRDUB 浮窗子进程 —— 协议见 docs/PROTOCOL.md §4

    stdin  : 一行一个 JSON（后端 → 浮窗）
    stdout : 一个字节都不输出（后端会读，别污染）
    stderr : 调试信息

启动:
    python hud.py

自测（Qt 自己截图，不依赖系统截屏）:
    python tools/hud_feed.py | python hud.py --shot out/hud_shot.png --shot-after 5 --quit-after-shot

约定:
    * 无边框 / 置顶 / 半透明圆角；**默认可拖动**（左键按住任意位置拖，位置存进 QSettings）
    * 右键单击 = 切到鼠标穿透（穿透时窗口不可点，靠右上角小把手拖回/取消）
    * cfg.click=true 只是「强制可拖动」（老语义反过来用），不会把窗口切成穿透
    * stdin EOF 立刻退出（后端死了子进程必须自杀）
    * 20 s 收不到 ping 自己退出
    * done 后停 20 s，用 QPropertyAnimation 淡出
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation,
                            QRectF, QSettings, QSize, Qt, QTimer)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QMouseEvent, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QSizePolicy, QVBoxLayout, QWidget)

# ---------------------------------------------------------------- 常量
CHAIN_COLOR = {"A": "#22d3ee", "B": "#a78bfa", "C": "#fb923c"}
COL_SRC = "#93a4bb"          # 原文：暗
COL_DST = "#f2f7ff"          # 译文：亮
COL_META = "#7b8da4"         # 右侧小字：灰
COL_CHAIN = {"A": CHAIN_COLOR["A"], "B": CHAIN_COLOR["B"], "C": CHAIN_COLOR["C"]}
BG_RGB = (10, 14, 21)
DONE_HOLD_S = 20.0           # done 后停多久再淡出
PING_TIMEOUT_S = 20.0        # 收不到 ping 的存活上限
SET_ORG, SET_APP = "VRDUB", "hud"   # 窗口位置/穿透状态存在注册表（HKCU\Software\VRDUB\hud）


def _settings() -> QSettings:
    return QSettings(SET_ORG, SET_APP)


def _as_bool(v, d=False) -> bool:
    if v is None:
        return d
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def dbg(*a):
    try:
        sys.stderr.write("[hud] " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def num(v):
    """数值 → 短字符串，空值给 “–”"""
    if v is None or v == "":
        return "–"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == 0:
        return "–"
    if abs(f - round(f)) < 0.05:
        return str(int(round(f)))
    return ("%.2f" % f).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- 省略号标签
class ElidedLabel(QLabel):
    """单行、自动省略、用 palette 之外的颜色自己画（省得 QSS 打架）"""

    def __init__(self, text: str, color: str, px: int, weight: int = 400, parent=None):
        super().__init__(parent)
        self._full = text or ""
        self._color = QColor(color)
        f = QFont()
        f.setPixelSize(max(7, int(px)))
        f.setWeight(QFont.Weight.DemiBold if weight >= 600 else QFont.Weight.Normal)
        self.setFont(f)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(round(px * 1.42))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_full(self, text: str):
        text = text or ""
        if text != self._full:
            self._full = text
            self.update()

    def set_color(self, color: str):
        c = QColor(color)
        if c != self._color:
            self._color = c
            self.update()

    def set_px(self, px: int, weight: int = 400):
        f = self.font()
        f.setPixelSize(max(7, int(px)))
        f.setWeight(QFont.Weight.DemiBold if weight >= 600 else QFont.Weight.Normal)
        self.setFont(f)
        self.setFixedHeight(round(px * 1.42))
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:            # 宽度不参与撑窗口
        return QSize(80, self.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.height())

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setFont(self.font())
        p.setPen(self._color)
        w = max(0, self.width())
        txt = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, w)
        p.drawText(0, 0, w, self.height(),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), txt)


# ---------------------------------------------------------------- 折行标签
class WrapLabel(QWidget):
    """自动折行、最多 max_lines 行、超出打省略号。宽度由外部给定（不参与撑窗口）。"""

    def __init__(self, text: str, color: str, px: int, weight: int = 400,
                 max_lines: int = 2, parent=None):
        super().__init__(parent)
        self._full = text or ""
        self._color = QColor(color)
        self._px = max(9, int(px))
        self._weight = weight
        self._max_lines = max(1, int(max_lines))
        self._font = QFont()
        self._font.setPixelSize(self._px)
        self._font.setWeight(QFont.Weight.DemiBold if weight >= 600 else QFont.Weight.Normal)
        self.setFont(self._font)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self._calc_h())

    def _calc_h(self) -> int:
        fm = QFontMetrics(self._font)
        return int(fm.lineSpacing() * self._max_lines + 2)

    def set_full(self, text: str):
        text = text or ""
        if text != self._full:
            self._full = text
            self.update()

    def set_color(self, color: str):
        c = QColor(color)
        if c != self._color:
            self._color = c
            self.update()

    def set_px(self, px: int, weight: int = None, max_lines: int = None):
        self._px = max(9, int(px))
        if weight is not None:
            self._weight = int(weight)
        if max_lines is not None:
            self._max_lines = max(1, int(max_lines))
        self._font.setPixelSize(self._px)
        self._font.setWeight(QFont.Weight.DemiBold if self._weight >= 600 else QFont.Weight.Normal)
        self.setFont(self._font)
        self.setFixedHeight(self._calc_h())
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(120, self.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.height())

    def _wrap(self, fm, width: float):
        """按字符贪心折行（CJK 逐字、拉丁按词），返回 (行列表, 是否截断)"""
        text = self._full
        if not text:
            return [], False
        lines, cur, i, n = [], "", 0, len(text)
        while i < n:
            ch = text[i]
            if ch.isspace() or ord(ch) > 0x2E80:
                j = i + 1
            else:
                j = i + 1
                while j < n and ord(text[j]) <= 0x2E80 and not text[j].isspace():
                    j += 1
            tok = text[i:j]
            if cur and fm.horizontalAdvance(cur + tok) > width:
                lines.append(cur)
                cur = ""
                if len(lines) >= self._max_lines:
                    break
                continue                      # 不推进 i：这一 token 落到下一行
            cur += tok
            i = j
        if len(lines) < self._max_lines and cur:
            lines.append(cur)
        truncated = i < n
        if truncated and lines:
            tail = lines[-1] + "…"
            if fm.horizontalAdvance(tail) > width:
                tail = fm.elidedText(lines[-1], Qt.TextElideMode.ElideRight,
                                     int(width - fm.horizontalAdvance("…"))) + "…"
            lines[-1] = tail
        return lines, truncated

    def paintEvent(self, _ev):
        if not self._full:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setFont(self.font())
        p.setPen(self._color)
        fm = self.fontMetrics()
        w = max(10, self.width())
        lines, _ = self._wrap(fm, float(w))
        ls = fm.lineSpacing()
        y = 0.0
        for ln in lines[:self._max_lines]:
            p.drawText(QRectF(0, y, w, ls),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), ln)
            y += ls

# ---------------------------------------------------------------- 一行
class RowWidget(QWidget):
    """[链] ▎原文（暗） / 译文（亮） ·  右侧 128 / 320 / 1020 ms · 0.72"""

    def __init__(self, seg: dict, s: float, text_w: int = 0, meta_w: int = 0, parent=None):
        super().__init__(parent)
        self.text_w = max(80, int(text_w or 460))
        self.meta_w = max(90, int(meta_w or 172))
        self.seg = dict(seg)
        self.done_at = None
        self.anim = None
        self.effect = None
        self.s = s
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(max(4, int(round(7 * s))))

        chain = str(seg.get("chain") or "?")
        self.lbl_chain = QLabel(chain, self)
        f = QFont()
        f.setPixelSize(max(8, int(round(10 * s))))
        f.setWeight(QFont.Weight.Bold)
        f.setFamilies(["Cascadia Mono", "Consolas", "monospace"])
        self.lbl_chain.setFont(f)
        self.lbl_chain.setStyleSheet("color:%s;background:transparent;" % COL_CHAIN.get(chain, COL_META))
        self.lbl_chain.setFixedWidth(max(10, int(round(13 * s))))
        self.lbl_chain.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.bar = QFrame(self)
        self.bar.setFixedWidth(max(2, int(round(3 * s))))
        self.bar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.bar.setStyleSheet("background:%s;border-radius:%dpx;" %
                               (COL_CHAIN.get(chain, COL_META), max(1, int(round(1.5 * s)))))

        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(max(0, int(round(1 * s))))
        px_dst = max(10, int(round(15 * s)))
        px_src = max(8, int(round(12 * s)))
        # 译文最多折两行（长句不溢出），原文单行省略
        self.lbl_dst = WrapLabel(seg.get("dst") or "", COL_DST, px_dst, 600, 2, self)
        self.lbl_src = ElidedLabel(seg.get("src") or "", COL_SRC, px_src, 400, self)
        self.lbl_dst.setFixedWidth(self.text_w)
        self.lbl_src.setFixedWidth(self.text_w)
        box.addWidget(self.lbl_dst)
        box.addWidget(self.lbl_src)

        self.lbl_meta = QLabel(self)
        fm = QFont()
        fm.setPixelSize(max(8, int(round(10.5 * s))))
        fm.setFamilies(["Cascadia Mono", "Consolas", "monospace"])
        self.lbl_meta.setFont(fm)
        self.lbl_meta.setStyleSheet("color:%s;background:transparent;" % COL_META)
        self.lbl_meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_meta.setFixedWidth(self.meta_w)

        lay.addWidget(self.lbl_chain, 0)
        lay.addWidget(self.bar, 0)
        lay.addLayout(box, 1)
        lay.addWidget(self.lbl_meta, 0)

        self.apply(seg)

    # -- 数据 → 界面
    def apply(self, seg: dict):
        self.seg.update({k: v for k, v in seg.items() if v is not None})
        d = self.seg
        self.lbl_dst.set_full(d.get("dst") or "")
        note = d.get("note")
        src = d.get("src") or ""
        # note（字幕上屏 / 云端校正 / 回声）挂到原文行，不挤右侧数字列
        self.lbl_src.set_full(("[" + str(note)[:12] + "] " + src) if note else src)

        state = d.get("state") or ""
        if state == "err":
            self.lbl_dst.set_color("#ffb4b4")
        elif state == "drop":
            self.lbl_dst.set_color("#8a9bb2")
        else:
            self.lbl_dst.set_color(COL_DST)

        meta = "%s / %s / %s ms · %s" % (num(d.get("asr_ms")), num(d.get("mt_ms")),
                                         num(d.get("tts_ms")), num(d.get("rtf")))
        if self.lbl_meta.text() != meta:
            self.lbl_meta.setText(meta)

        if state in ("done", "refined", "drop", "err"):
            if self.done_at is None:
                self.done_at = time.monotonic()
        else:
            self.done_at = None

    def set_metrics(self, s: float, text_w: int = 0, meta_w: int = 0):
        self.s = s
        self.lbl_dst.set_px(max(10, int(round(15 * s))), 600, 2)
        self.lbl_src.set_px(max(8, int(round(12 * s))), 400, 1)
        if text_w:
            self.text_w = max(80, int(text_w))
        if meta_w:
            self.meta_w = max(90, int(meta_w))
        self.lbl_dst.setFixedWidth(self.text_w)
        self.lbl_src.setFixedWidth(self.text_w)
        f = QFont()
        f.setPixelSize(max(8, int(round(10.5 * s))))
        f.setFamilies(["Cascadia Mono", "Consolas", "monospace"])
        self.lbl_meta.setFont(f)
        self.lbl_meta.setFixedWidth(self.meta_w)
        fc = QFont()
        fc.setPixelSize(max(8, int(round(10 * s))))
        fc.setWeight(QFont.Weight.Bold)
        fc.setFamilies(["Cascadia Mono", "Consolas", "monospace"])
        self.lbl_chain.setFont(fc)
        self.lbl_chain.setFixedWidth(max(10, int(round(13 * s))))
        self.updateGeometry()
        self.update()


# ---------------------------------------------------------------- 穿透把手
class PassthroughHandle(QWidget):
    """穿透模式下唯一还能点到的窗口：按住拖 HUD，右键（或点一下）取消穿透。

    穿透窗口本身收不到任何鼠标事件（WindowTransparentForInput 是整窗生效的），
    所以必须留一个独立的小窗，否则用户进了穿透就再也点不回来了。
    """

    SIZE = 30

    def __init__(self, hud):
        super().__init__(None)
        self.hud = hud
        self.setFixedSize(PassthroughHandle.SIZE, PassthroughHandle.SIZE)
        self.setWindowTitle("VRDUB 穿透把手")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool |
                            Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("穿透中：按住这里拖 HUD；右键（或点一下）取消穿透")
        self._press = None
        self._origin = None
        self._moved = False

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect().adjusted(0, 0, -1, -1)), 8.0, 8.0)
        p.fillPath(path, QColor(10, 14, 21, 224))
        p.setPen(QPen(QColor(34, 211, 238, 190), 1.0))
        p.drawPath(path)
        p.setPen(QPen(QColor(196, 226, 246, 215), 1.6,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        for dy in (-5.0, 0.0, 5.0):
            p.drawLine(QPointF(cx - 7.0, cy + dy), QPointF(cx + 7.0, cy + dy))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self.hud.set_passthrough(False, "右键把手")
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            gp = ev.globalPosition().toPoint()
            self._press = gp - self.hud.frameGeometry().topLeft()
            self._origin = gp
            self._moved = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._press is not None:
            gp = ev.globalPosition().toPoint()
            if self._origin is not None and (gp - self._origin).manhattanLength() > 3:
                self._moved = True
            self.hud.move(gp - self._press)
            self.hud.user_pos = (self.hud.x(), self.hud.y())
            self.hud._sync_handle()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._press is not None:
            self._press = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if not self._moved:                 # 点一下＝取消穿透
                self.hud.set_passthrough(False, "点把手")
            else:
                self.hud._save_state()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)


# ---------------------------------------------------------------- 主窗口
class HudWindow(QWidget):
    def __init__(self, args):
        super().__init__(None)
        self.args = args
        self.cfg = {"alpha": 92, "scale": 100, "rows": 8, "click": True, "x": -1, "y": -1}
        self.rows: list[RowWidget] = []
        self.rows_map: dict[str, RowWidget] = {}
        self.user_pos = None
        self.last_ping = time.monotonic()
        self.margin = 22
        self._drag_pos = None
        self._backend_status = ""
        self.handle = None                  # 穿透时浮在窗外的小把手
        self.settings = _settings()
        self.click_through = _as_bool(self.settings.value("passthrough", False), False)
        self._load_pos()
        self.win_opacity_anim = None
        self.window_visible = True
        self.quit_on_eof = not getattr(args, "demo", False)
        # 电平：协议里没有 level 事件，就用「有新 seg = 在说话」驱动一条细条
        self.level = 0.0
        self.level_target = 0.0
        self.last_activity = 0.0

        self.setWindowTitle("VRDUB HUD")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._apply_flags(self.click_through)

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)

        # 闭麦徽标（独立一行，默认隐藏）
        self.microw = QWidget(self)
        mh = QHBoxLayout(self.microw)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.addStretch(1)
        self.badge = QLabel("麦已闭", self.microw)
        self.badge.setStyleSheet(
            "color:#ffe3e3;background:rgba(198,44,58,235);"
            "border-radius:%dpx;padding:%dpx %dpx;font-weight:700;"
            % (9, 1, 8))
        f = QFont()
        f.setPixelSize(11)
        self.badge.setFont(f)
        mh.addWidget(self.badge, 0)
        self.microw.setVisible(False)

        # 行容器
        self.content = QWidget(self)
        self.rows_vbox = QVBoxLayout(self.content)
        self.rows_vbox.setContentsMargins(0, 0, 0, 0)
        self.rows_vbox.setSpacing(4)

        # 状态行（op=status）
        self.status = QLabel("", self)
        self.status.setStyleSheet("color:#6d8098;background:transparent;")
        self.status.setVisible(False)

        self.root.addWidget(self.microw)
        self.root.addWidget(self.content)
        self.root.addWidget(self.status)
        self._apply_scale_metrics()
        self._relayout()

        # 定时器
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._stdin_reader, args=(self.q,), daemon=True).start()

        self.t_drain = QTimer(self)
        self.t_drain.timeout.connect(self._drain)
        self.t_drain.start(30)

        self.t_watch = QTimer(self)
        self.t_watch.timeout.connect(self._watchdog)
        self.t_watch.start(1000)

        self.t_fade = QTimer(self)
        self.t_fade.timeout.connect(self._scan_fade)
        self.t_fade.start(500)

        self.t_level = QTimer(self)
        self.t_level.timeout.connect(self._tick_level)
        self.t_level.start(60)

        if args.shot or args.shot_b64:
            QTimer.singleShot(int(max(0.2, args.shot_after) * 1000), self._take_shot)

    # ---------------- 窗口标志
    def _apply_flags(self, passthrough: bool = None):
        """passthrough=False（默认）= 可拖动、能收鼠标；True = 整窗鼠标穿透。

        setWindowFlags 会重建原生窗口并把窗口藏起来，所以要自己再 show 一次；
        重建后位置也得重新放一遍（否则会跳回 0,0）。
        """
        if passthrough is None:
            passthrough = self.click_through
        was_visible = self.isVisible()
        flags = (Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowStaysOnTopHint |
                 Qt.WindowType.Tool |
                 Qt.WindowType.WindowDoesNotAcceptFocus)
        if passthrough:
            flags |= Qt.WindowType.WindowTransparentForInput
        # 顺序要紧：Qt 会把 WA_TransparentForMouseEvents 同步进原生 flags，
        # 先设属性再设 flags，否则退出穿透后 WindowTransparentForInput 清不掉（实测过）。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, bool(passthrough))
        self.setWindowFlags(flags)
        if (not passthrough) and (self.windowFlags() & Qt.WindowType.WindowTransparentForInput):
            self.setWindowFlags(flags)          # 原生窗口重建前再压一次
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.ArrowCursor if passthrough else Qt.CursorShape.OpenHandCursor)
        if was_visible:
            self._place_window(force=True)
            self.show()
            self.raise_()
        self._sync_handle()

    # ---------------- 位置持久化（QSettings：HKCU\Software\VRDUB\hud）
    def _load_pos(self):
        try:
            raw = self.settings.value("pos", None)
            if not raw:
                return
            sx, sy = str(raw).split(",")
            x, y = int(float(sx)), int(float(sy))
            self.user_pos = (x, y)
            self.cfg["x"], self.cfg["y"] = x, y
        except Exception as e:                       # noqa: BLE001
            dbg("读取位置失败:", e)
            self.user_pos = None

    def _save_state(self):
        try:
            if self.user_pos is not None:
                self.settings.setValue("pos", "%d,%d" % (int(self.user_pos[0]), int(self.user_pos[1])))
            self.settings.setValue("passthrough", bool(self.click_through))
            self.settings.sync()
        except Exception as e:                       # noqa: BLE001
            dbg("保存状态失败:", e)

    # ---------------- 穿透开关
    def set_passthrough(self, on: bool, why: str = ""):
        on = bool(on)
        if on == self.click_through:
            return
        self.click_through = on
        self._drag_pos = None
        self._apply_flags(on)
        self._refresh_status()
        self._save_state()
        self.update()
        dbg("穿透:", "开（点右上把手取消）" if on else "关（可拖动）", ("· " + why) if why else "")

    def _sync_handle(self):
        if not self.click_through:
            if self.handle is not None:
                self.handle.hide()
            return
        if self.handle is None:
            self.handle = PassthroughHandle(self)
        scr = self.screen() or QApplication.primaryScreen()
        g = scr.availableGeometry() if scr else None
        size = PassthroughHandle.SIZE
        x = self.x() + self.width() - size
        y = self.y() - size - 6
        if g is not None:
            x = max(g.x(), min(x, g.x() + g.width() - size))
            if y < g.y():
                y = self.y() + self.height() + 6
        self.handle.move(int(x), int(y))
        if not self.handle.isVisible():
            self.handle.show()
        self.handle.raise_()

    def _refresh_status(self):
        parts = []
        if self._backend_status:
            parts.append(self._backend_status)
        if self.click_through:
            parts.append("穿透中 · 点右上角把手取消")
        txt = "  ·  ".join(parts)
        self.status.setText(txt)
        self.status.setVisible(bool(txt))

    # ---------------- 尺寸 / 字体
    def _s(self) -> float:
        return max(0.5, self.cfg["scale"] / 100.0)

    def _apply_scale_metrics(self):
        s = self._s()
        self.pad = max(6, int(round(11 * s)))
        self.gap = max(2, int(round(5 * s)))
        self.win_w = max(360, int(round(720 * s)))
        self.root.setContentsMargins(self.pad, self.pad, self.pad, self.pad)
        self.root.setSpacing(self.gap)
        self.rows_vbox.setSpacing(max(2, int(round(5 * s))))
        f = QFont()
        f.setPixelSize(max(8, int(round(10.5 * s))))
        f.setFamilies(["Cascadia Mono", "Consolas", "monospace"])
        self.status.setFont(f)
        fb = QFont()
        fb.setPixelSize(max(9, int(round(11 * s))))
        fb.setWeight(QFont.Weight.Bold)
        self.badge.setFont(fb)
        # 右侧数字列按最长模板实测宽度给，永远不裁字（之前用固定 168*s 会截断）
        tpl = "0000 / 0000 / 0000 ms · 0.00"
        self.meta_w = max(96, QFontMetrics(f).horizontalAdvance(tpl) + max(4, int(round(6 * s))))
        chrome = int(round((13 + 3 + 7 * 3) * s)) + self.meta_w
        self.text_w = max(140, self.win_w - 2 * self.pad - chrome)

    def _sync_row_metrics(self):
        s = self._s()
        for r in self.rows:
            if (r.text_w != self.text_w or r.meta_w != self.meta_w
                    or abs(r.s - s) > 1e-6):
                r.set_metrics(s, self.text_w, self.meta_w)

    def _relayout(self):
        self._sync_row_metrics()
        self.setFixedWidth(self.win_w)
        self.layout().activate()
        self.adjustSize()
        self._place_window()
        if self.user_pos is None and self._drag_pos is None:
            QTimer.singleShot(0, lambda: self._place_window(silent=True))
        self._sync_handle()

    def _place_window(self, force: bool = False, silent: bool = False):
        """拖过就钉在拖到的位置，没拖过就贴底部居中；位置永远夹在屏幕内。"""
        # 窗口还没 show / 布局未生效时 height() 只有 22px，会把窗口顶到屏幕外，
        # 用 sizeHint 兜一次底
        h = max(self.height(), self.sizeHint().height())
        scr = self.screen() or QApplication.primaryScreen()
        g = scr.availableGeometry() if scr else None
        if self.user_pos is not None:
            x, y = int(self.user_pos[0]), int(self.user_pos[1])
        else:
            x = (g.x() if g else 0) + int(((g.width() if g else 1600) - self.win_w) / 2)
            y = (g.y() if g else 0) + ((g.height() if g else 1000) - h - self.margin)
        if g is not None:                       # 别让窗口被拖到看不见的地方
            x = max(g.x() - 40, min(x, g.x() + g.width() - self.win_w + 40))
            y = max(g.y() - 4, min(y, g.y() + g.height() - h + 4))
        if self._drag_pos is not None and not force:
            return                              # 正在拖：别抢位置
        if self.pos().x() != x or self.pos().y() != y:
            self.move(x, y)
            if not silent:
                dbg("窗口位置: %d,%d %dx%d (可用区 %s)" % (
                    x, y, self.win_w, h,
                    "none" if g is None else "%d,%d %dx%d" % (g.x(), g.y(), g.width(), g.height())))

    # ---------------- stdin
    def _stdin_reader(self, q: queue.Queue):
        """os.read 直读 fd 0：不碰 sys.stdin 的 BufferedReader 锁。
        守护线程退出时解释器不会在 buffered-io 上炸（否则会 Fatal Python error:
        _enter_buffered_busy 并以 0xC0000005 收场）。"""
        buf = b""
        try:
            while True:
                try:
                    chunk = os.read(0, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = buf.find(b"\n")
                    if i < 0:
                        break
                    line = buf[:i].strip()
                    buf = buf[i + 1:]
                    if line:
                        q.put(line)
            tail = buf.strip()
            if tail:
                q.put(tail)
        except Exception as e:                      # noqa: BLE001
            dbg("stdin 读取异常:", e)
        finally:
            q.put(None)                              # EOF 哨兵

    def _drain(self):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            if item is None:
                if self.quit_on_eof:
                    self.quit_now("stdin EOF")
                return
            try:
                obj = json.loads(item.decode("utf-8", "replace"))
            except Exception as e:                   # noqa: BLE001
                dbg("JSON 解析失败:", e, item[:120])
                continue
            try:
                self.dispatch(obj)
            except Exception as e:                   # noqa: BLE001
                dbg("处理失败:", e, item[:120])

    def dispatch(self, ev: dict):
        op = ev.get("op")
        if op == "cfg":
            self.on_cfg(ev)
        elif op == "seg":
            seg = ev.get("seg") or {}
            if isinstance(seg, dict):
                self.on_seg(seg)
        elif op == "mic":
            self.on_mic(ev.get("on"))
        elif op == "clear":
            self.clear_rows()
        elif op == "status":
            self.on_status(ev.get("text") or "")
        elif op == "level":
            # 协议 §4 没有 level；后端若将来推（mic 为 dBFS），直接吃
            try:
                db = float(ev.get("mic") or -60.0)
            except (TypeError, ValueError):
                db = -60.0
            self.level_target = max(0.0, min(1.0, (db + 60.0) / 45.0))
            self.last_activity = time.monotonic()
        elif op == "ping":
            self.last_ping = time.monotonic()
        elif op == "quit":
            self.quit_now("收到 quit")
        else:
            dbg("未知 op:", op)

    # ---------------- 协议 ops
    def on_cfg(self, ev: dict):
        old_scale = self.cfg["scale"]
        for k in ("alpha", "scale", "rows", "click"):
            if k in ev and ev[k] is not None:
                self.cfg[k] = ev[k]
        self.cfg["alpha"] = max(0, min(100, int(self.cfg["alpha"])))
        self.cfg["scale"] = max(50, min(300, int(self.cfg["scale"])))
        self.cfg["rows"] = max(1, min(40, int(self.cfg["rows"])))
        if ev.get("click") is not None:
            click = _as_bool(ev.get("click"), True)
            self.cfg["click"] = click
            # 老语义（click=可拖动）反过来用：click=true 只保证可交互，
            # click=false 不再把窗口切成穿透（默认就必须能拖，用户被这个坑过）
            if click and self.click_through:
                self.set_passthrough(False, "cfg.click=true")
        if ev.get("passthrough") is not None:
            self.set_passthrough(_as_bool(ev.get("passthrough"), False), "cfg.passthrough")
        if ev.get("x") is not None and ev.get("y") is not None:
            try:
                x, y = int(ev["x"]), int(ev["y"])
                self.cfg["x"], self.cfg["y"] = x, y
                self.user_pos = None if (x < 0 or y < 0) else (x, y)
            except (TypeError, ValueError):
                pass
        if self.cfg["scale"] != old_scale:
            self._apply_scale_metrics()
            for r in self.rows:
                r.set_metrics(self._s(), self.text_w, self.meta_w)
        self._trim_rows()
        self.update()
        self._relayout()

    def on_mic(self, on):
        self.microw.setVisible(not bool(on))
        self._relayout()

    def on_status(self, text: str):
        self._backend_status = text or ""
        self._refresh_status()
        self._relayout()

    def on_seg(self, seg: dict):
        sid = seg.get("id")
        if not sid:
            return
        sid = str(sid)
        row = self.rows_map.get(sid)
        if row is None:
            row = RowWidget(seg, self._s(), self.text_w, self.meta_w, self.content)
            self.rows_vbox.addWidget(row, 0)
            self.rows_map[sid] = row
            self.rows.append(row)
            self._trim_rows()
        else:
            row.apply(seg)
            if row.effect is not None and row.effect.opacity() < 1.0 and row.anim is None:
                row.effect.setOpacity(1.0)
        if not self.isVisible():
            self.show()
        if self.windowOpacity() < 1.0 and self.win_opacity_anim is None:
            self.setWindowOpacity(1.0)
        # 有新 seg / 正在识别 → 电平条动起来
        st = str(seg.get("state") or "")
        self.last_activity = time.monotonic()
        if st in ("asr", "mt", "tts", "play"):
            self.level_target = 0.92
        else:
            self.level_target = max(0.34, self.level_target * 0.55)
        self._relayout()

    def _trim_rows(self):
        limit = int(self.cfg["rows"])
        while len(self.rows) > limit:
            row = self.rows.pop(0)
            self.rows_map.pop(str(row.seg.get("id")), None)
            self.rows_vbox.removeWidget(row)
            row.setParent(None)
            row.deleteLater()

    def clear_rows(self):
        for row in self.rows:
            self.rows_map.pop(str(row.seg.get("id")), None)
            self.rows_vbox.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        self.rows_map = {}
        self._relayout()

    # ---------------- 电平
    def _tick_level(self):
        if time.monotonic() - self.last_activity > 0.9:
            self.level_target = 0.0
        cur, tgt = self.level, self.level_target
        nxt = cur + (tgt - cur) * (0.40 if tgt > cur else 0.10)
        if abs(nxt - tgt) < 0.002:
            nxt = tgt
        if abs(nxt - cur) > 1e-4:
            self.level = nxt
            self.update()

    # ---------------- 淡出
    def _scan_fade(self):
        now = time.monotonic()
        for row in list(self.rows):
            if row.done_at is None or row.anim is not None:
                continue
            if now - row.done_at >= DONE_HOLD_S:
                self._fade_row(row)
        if not self.rows and not self.status.isVisible() and self.windowOpacity() > 0.02:
            self._fade_window(0.0)

    def _fade_row(self, row: RowWidget):
        eff = QGraphicsOpacityEffect(row)
        eff.setOpacity(1.0)
        row.setGraphicsEffect(eff)
        row.effect = eff
        anim = QPropertyAnimation(eff, b"opacity", row)
        anim.setDuration(900)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.finished.connect(lambda r=row: self._remove_row(r))
        row.anim = anim
        anim.start()

    def _remove_row(self, row: RowWidget):
        self.rows_map.pop(str(row.seg.get("id")), None)
        if row in self.rows:
            self.rows.remove(row)
        self.rows_vbox.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self._relayout()

    def _fade_window(self, to: float):
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(900)
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(float(to))
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.win_opacity_anim = anim
        anim.finished.connect(lambda: setattr(self, "win_opacity_anim", None))
        anim.start()

    # ---------------- watchdog
    def _watchdog(self):
        if time.monotonic() - self.last_ping > PING_TIMEOUT_S:
            self.quit_now("20 s 未收到 ping")

    def quit_now(self, why: str):
        dbg("退出:", why)
        self.t_drain.stop()
        self.t_watch.stop()
        self.t_fade.stop()
        QApplication.quit()

    # ---------------- 绘制
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        a = int(round(255 * (self.cfg["alpha"] / 100.0)))
        r = max(6.0, 13.0 * self._s())
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0), r, r)
        p.fillPath(path, QColor(BG_RGB[0], BG_RGB[1], BG_RGB[2], a))
        p.setPen(QPen(QColor(255, 255, 255, 20), 1.0))
        p.drawPath(path)
        # 底部电平条（说话时涨）
        lvl = max(0.0, min(1.0, self.level))
        if lvl > 0.005:
            bh = max(2.0, 2.0 * self._s())
            bw = (self.width() - 2 * self.pad) * lvl
            p.fillRect(QRectF(self.pad, self.height() - bh - 1.0, bw, bh),
                       QColor(34, 211, 238, 190))

    # ---------------- 拖动 / 右键切穿透
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self.set_passthrough(True, "右键 HUD")
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            # 记「鼠标在窗口内的偏移」，之后每个 move 事件都把窗口放到 光标-偏移，
            # 窗口跟手、不累积误差（不是每帧 += delta）
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_pos is not None and not self.click_through:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            self.user_pos = (self.x(), self.y())
            self._sync_handle()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._drag_pos is not None:
            self._drag_pos = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.user_pos = (self.x(), self.y())
            self._save_state()
            dbg("拖到:", self.x(), self.y())
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 高度变了（第一行进来 / 行被裁掉）必须重贴底部，否则窗口会贴到屏幕外
        if self.user_pos is None and self._drag_pos is None:
            self._place_window(silent=True)
        if self.handle is not None and self.handle.isVisible():
            self._sync_handle()

    def moveEvent(self, ev):
        super().moveEvent(ev)
        if self.handle is not None and self.handle.isVisible():
            self._sync_handle()

    # ---------------- 自测：演示数据 / 拖动 / 穿透
    DEMO_ROWS = [
        {"id": "demo-a", "chain": "A", "state": "done",
         "dst": "收到，我马上过去。", "src": "わかった、すぐ行くね。",
         "asr_ms": 210, "mt_ms": 198, "tts_ms": 402, "rtf": 0.92},
        {"id": "demo-b", "chain": "B", "state": "done",
         "dst": "这个副本我们上周打过了。", "src": "このダンジョン先週もうクリアしたよ。",
         "asr_ms": 128, "mt_ms": 320, "tts_ms": 1020, "rtf": 0.72},
        {"id": "demo-c", "chain": "C", "state": "done",
         "dst": "别走传送门，先在集合点等。", "src": "ポータルはやめて、集合場所で待って。",
         "asr_ms": 96, "mt_ms": 260, "tts_ms": None, "rtf": 0.61, "note": "字幕上屏"},
    ]

    def seed_demo(self):
        """--demo：不依赖 stdin 也不依赖后端，直接铺三条假句子（自测/截图用）。"""
        self.last_ping = time.monotonic()
        for seg in self.DEMO_ROWS:
            self.on_seg(dict(seg))

    def _mk_mouse(self, kind, local, glob, button, buttons):
        loc, gpt = QPointF(*local), QPointF(*glob)
        try:
            return QMouseEvent(kind, loc, loc, gpt, button, buttons,
                               Qt.KeyboardModifier.NoModifier)
        except TypeError:                      # 老签名（Qt6 早期只有 6 参）
            return QMouseEvent(kind, loc, gpt, button, buttons,
                               Qt.KeyboardModifier.NoModifier)

    def _desktop_pixmap(self, rect):
        """真·桌面截图（GDI BitBlt + CAPTUREBLT）。

        QScreen.grabWindow() 走的是不带 CAPTUREBLT 的 BitBlt，抓不到 WS_EX_LAYERED 的窗口
        （本浮窗就是 layered，之前截出来只有背后的浏览器），所以这里自己调 GDI。
        """
        import ctypes
        from ctypes import wintypes
        x, y, w, h = [int(v) for v in rect]
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hdesk = user32.GetDC(0)
        memdc = gdi32.CreateCompatibleDC(hdesk)
        hbmp = gdi32.CreateCompatibleBitmap(hdesk, w, h)
        gdi32.SelectObject(memdc, hbmp)
        gdi32.BitBlt(memdc, 0, 0, w, h, hdesk, x, y, 0x00CC0020 | 0x40000000)

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]
        bi = BMIH()
        bi.biSize = ctypes.sizeof(BMIH)
        bi.biWidth, bi.biHeight = w, -h            # 负高度 = 自上而下
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, hbmp, 0, h, buf, ctypes.byref(bi), 0)
        from PySide6.QtGui import QImage
        img = QImage(buf, w, h, QImage.Format.Format_RGB32).copy()
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdesk)
        from PySide6.QtGui import QPixmap
        return QPixmap.fromImage(img)

    def _take_screen_b64(self, rect):
        """屏幕截图（不是窗口自绘）：拖完要能看出窗口真的搬到了别的位置。"""
        from PySide6.QtCore import QBuffer, QByteArray
        import base64
        try:
            try:
                pix = self._desktop_pixmap(rect)
                pix.setDevicePixelRatio(1.0)
            except Exception as gdi_err:          # noqa: BLE001
                dbg("GDI 截图失败，退回 Qt:", gdi_err)
                scr = self.screen() or QApplication.primaryScreen()
                pix = scr.grabWindow(0)
                if rect is not None:
                    pix = pix.copy(QRect(*rect))
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QBuffer.OpenModeFlag.WriteOnly)
            pix.save(buf, "PNG")
            buf.close()
            raw = bytes(ba)
            sys.stderr.write("HUD_SHOT_B64 %dx%d %d %s\n" % (
                pix.width(), pix.height(), len(raw),
                base64.b64encode(raw).decode("ascii")))
            sys.stderr.flush()
            dbg("屏幕截图: %dx%d %d bytes" % (pix.width(), pix.height(), len(raw)))
        except Exception as e:                 # noqa: BLE001
            dbg("屏幕截图失败:", e)
        finally:
            if getattr(self.args, "quit_after_shot", False):
                QTimer.singleShot(150, QApplication.quit)

    def selftest_drag(self):
        """①能不能拖 ②右键能不能切穿透 ③位置有没有落盘 —— 全用合成鼠标事件走真代码路径。"""
        def rep(*a):
            dbg("[selftest]", *a)

        # 先停到一个屏幕内的确定位置，免得初始位置在屏幕边缘时被夹住、位移对不上
        self.user_pos = (420, 360)
        self._place_window(force=True)
        QApplication.processEvents()
        g0 = self.frameGeometry()
        rep("初始位置", g0.x(), g0.y(), "尺寸", g0.width(), g0.height(),
            "passthrough=", self.click_through)
        # ① 左键按住 (80,20) 拖 -360/-240
        gp = (g0.x() + 80, g0.y() + 20)
        self.mousePressEvent(self._mk_mouse(QEvent.Type.MouseButtonPress, (80, 20), gp,
                                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        rep("press 后 _drag_pos =", self._drag_pos)
        gp2 = (gp[0] - 360, gp[1] - 240)
        self.mouseMoveEvent(self._mk_mouse(QEvent.Type.MouseMove, (80, 20), gp2,
                                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
        self.mouseReleaseEvent(self._mk_mouse(QEvent.Type.MouseButtonRelease, (80, 20), gp2,
                                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
        g1 = self.frameGeometry()
        moved = (g1.x() - g0.x(), g1.y() - g0.y())
        rep("拖完位置", g1.x(), g1.y(), "位移", moved, "OK" if moved == (-360, -240) else "FAIL")
        saved = self.settings.value("pos", None)
        rep("落盘 pos =", saved, "OK" if str(saved) == "%d,%d" % (g1.x(), g1.y()) else "FAIL")

        # ② 右键切穿透
        self.mousePressEvent(self._mk_mouse(QEvent.Type.MouseButtonPress, (30, 30),
                                            (g1.x() + 30, g1.y() + 30),
                                            Qt.MouseButton.RightButton, Qt.MouseButton.RightButton))
        f = self.windowFlags()
        has_ti = bool(f & Qt.WindowType.WindowTransparentForInput)
        has_attr = self.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        handle_on = bool(self.handle and self.handle.isVisible())
        status_txt = self.status.text()
        rep("右键后 passthrough =", self.click_through, "flags透明 =", has_ti,
            "attr =", has_attr, "把手 =", handle_on, "状态栏 =", status_txt)
        rep("穿透检查", "OK" if (self.click_through and has_ti and has_attr and handle_on
                                  and "穿透中" in status_txt) else "FAIL")

        # ③ 再点回可拖动（模拟点把手）
        self.set_passthrough(False, "selftest")
        f2 = self.windowFlags()
        rep("取消穿透", "flags透明 =", bool(f2 & Qt.WindowType.WindowTransparentForInput),
            "attr =", self.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents),
            "把手 =", bool(self.handle and self.handle.isVisible()),
            "位置 =", self.x(), self.y())

        # 拖到的新位置有没有被 setWindowFlags 重建原生窗口搞丢
        rep("位置是否保持", "OK" if (self.x(), self.y()) == (g1.x(), g1.y()) else
            "FAIL(%d,%d)" % (self.x(), self.y()))

        # ④ 截一张桌面（含窗口外框 + 40px 背景），证明窗口确实在别处
        if getattr(self.args, "selftest_shot", True):
            self.show()
            self.raise_()
            gg = self.frameGeometry()
            dpr = float(self.devicePixelRatioF()) or 1.0
            rect = (int((gg.x() - 40) * dpr), int((gg.y() - 40) * dpr),
                    int((gg.width() + 80) * dpr), int((gg.height() + 80) * dpr))
            # 延后 600 ms：setWindowFlags 重建原生窗口 / DWM 合成都要一点时间
            def _do():
                self.raise_()
                QApplication.processEvents()
                self._take_screen_b64(rect)
            QTimer.singleShot(600, _do)
        self.last_ping = time.monotonic()
        # 退出交给 _take_screen_b64 末尾（它是延后 600 ms 才截的，这里立刻退会截不到）

    # ---------------- 自测截图
    def _take_shot_b64(self):
        """把 PNG 以 base64 打到 stderr。子进程常常没有写盘权限（沙箱/只读目录），
        这条通道让自测截图仍能交出去：\n   HUD_SHOT_B64 <w>x<h> <bytes> <base64>"""
        from PySide6.QtCore import QBuffer, QByteArray
        import base64
        try:
            pix = self.grab()
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QBuffer.OpenModeFlag.WriteOnly)
            pix.save(buf, "PNG")
            buf.close()
            raw = bytes(ba)
            sys.stderr.write("HUD_SHOT_B64 %dx%d %d %s\n" % (
                pix.width(), pix.height(), len(raw),
                base64.b64encode(raw).decode("ascii")))
            sys.stderr.flush()
            dbg("b64 截图: %dx%d %d bytes rows=%d 位置=%d,%d 窗口=%dx%d" % (
                pix.width(), pix.height(), len(raw), len(self.rows),
                self.x(), self.y(), self.width(), self.height()))
        except Exception as e:                      # noqa: BLE001
            dbg("b64 截图失败:", e)

    def _take_shot(self):
        if getattr(self.args, "shot_b64", False):
            self._take_shot_b64()
            if self.args.quit_after_shot:
                QTimer.singleShot(150, QApplication.quit)
            return
        try:
            path = os.path.abspath(self.args.shot or "")
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            pix = self.grab()
            ok = pix.save(path, "PNG")
            dbg("截图:", path, "ok=" + str(ok), "%dx%d" % (pix.width(), pix.height()),
                "rows=%d" % len(self.rows))
        except Exception as e:                      # noqa: BLE001
            dbg("截图失败:", e)
        if self.args.quit_after_shot:
            QTimer.singleShot(150, QApplication.quit)


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--shot", default=None, help="自测：Qt 自截图保存到这个 png")
    ap.add_argument("--shot-after", type=float, default=5.0, help="启动多少秒后截图")
    ap.add_argument("--shot-b64", action="store_true", help="自测：PNG 以 base64 打到 stderr（HUD_SHOT_B64 行）")
    ap.add_argument("--quit-after-shot", action="store_true", help="截图后退出")
    ap.add_argument("--demo", action="store_true", help="自测：铺三条假句子，不读 stdin、不因 EOF 退出")
    ap.add_argument("--selftest-drag", action="store_true",
                    help="自测：合成鼠标事件验证拖动 / 右键穿透 / 位置落盘，并截桌面")
    ap.add_argument("--selftest-after", type=float, default=1.6,
                    help="--selftest-drag 启动多少秒后开始（等布局稳定）")
    args, _unknown = ap.parse_known_args()

    # stdout 保证一个字节都不输出
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

    app = QApplication(sys.argv[:1])
    app.setApplicationName("VRDUB HUD")
    app.setQuitOnLastWindowClosed(False)

    if args.demo:
        args.quit_on_eof = False
    w = HudWindow(args)
    w.show()
    try:                                # 浮窗也要从抓屏里排除，否则 C 链会读自己翻出来的中文
        from . import util as _util
        _util.hide_from_capture(w)
    except Exception:                   # noqa: BLE001
        pass
    dbg("启动 pid=%d shot=%s b64=%s demo=%s 可拖动=%s 穿透=%s pos=%s" % (
        os.getpid(), args.shot, args.shot_b64, args.demo,
        not w.click_through, w.click_through, w.user_pos))
    if args.demo:
        QTimer.singleShot(120, w.seed_demo)
    if args.selftest_drag:
        QTimer.singleShot(int(max(0.4, args.selftest_after) * 1000), w.selftest_drag)
    rc = app.exec()
    # 读线程可能还堵在 fd 0 上：跳过解释器收尾，保证 stdin EOF / quit 后进程立刻干净退出
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(int(rc or 0))


if __name__ == "__main__":
    sys.exit(main())
