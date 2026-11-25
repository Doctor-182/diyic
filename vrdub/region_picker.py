#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""region_picker.py —— 全屏框选器（C 链「聊天框」范围）。

独立 Qt 子进程：后端 POST /api/region/pick 时把它拉起来，用户拖好按回车，
它把选区以**一行 JSON** 打到 stdout 就退出；按 Esc 取消则什么都不打、退出码 0。
后端拿到的是**逻辑像素**（Qt 坐标），DPR 由后端自己乘。

用法：
    python vrdub/region_picker.py                 # 正常框选
    python vrdub/region_picker.py --demo          # 只显示不自动退出（手动看效果）
    python vrdub/region_picker.py --shot PATH     # 自动确认，并把选区截图存到 PATH
    python vrdub/region_picker.py --self-shot P   # 自测：抓一张「带遮罩的整屏」存 P，然后退出
    python vrdub/region_picker.py --region x,y,w,h  # 预设选区（逻辑像素）

交互：
    拖四边 / 四角 = 拉伸；拖中间 = 整块移动；方向键 1 px（Shift 10 px）；
    回车 / 双击 = 确认；Esc / 右键 = 取消；Tab = 复位到初始选区。

约定：stdout 只有确认时的那一行 JSON，其余一切（Qt 警告、调试）都走 stderr。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QPainter, QPen,
                           QCursor)
from PySide6.QtWidgets import QApplication, QWidget

MASK_RGBA = (4, 8, 14, 140)      # 遮罩：深色 55%
ACCENT = "#22d3ee"               # 边线 / 信息条，和网页 C 链同色
HANDLE_FILL = QColor(255, 255, 255, 245)
HANDLE_EDGE = QColor(6, 10, 18, 220)
HIT = 12                         # 手柄命中半径（px）
HANDLE = 9                       # 手柄视觉边长（px）
MIN_W, MIN_H = 24, 16            # 选区最小尺寸

CURSORS = {
    "n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
    "w": Qt.CursorShape.SizeHorCursor, "e": Qt.CursorShape.SizeHorCursor,
    "nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
    "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
    "move": Qt.CursorShape.SizeAllCursor,
}


def _font(size: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
    f.setPixelSize(size)
    if bold:
        f.setWeight(QFont.Weight.DemiBold)
    return f


class Picker(QWidget):
    def __init__(self, args):
        super().__init__(None)
        self.args = args
        self.picked = None            # 确认后的选区（逻辑像素）
        self.cancelled = False
        self._drag = None             # (kind, 起点全局坐标, 起始选区)
        self._cursor = Qt.CursorShape.SizeAllCursor

        self.setWindowTitle("VRDUB 框选范围")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.BypassWindowManagerHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        scr = QGuiApplication.primaryScreen()
        self.screen_rect = QRect(scr.geometry())          # 逻辑像素
        self.setGeometry(self.screen_rect)

        self.initial = self._initial_rect()
        self.sel = QRect(self.initial)

        if args.region:
            try:
                x, y, w, h = [int(v) for v in str(args.region).split(",")]
                self.sel = self._clamp(QRect(x, y, max(MIN_W, w), max(MIN_H, h)))
            except Exception as e:                       # noqa: BLE001
                print("[picker] --region 解析失败:", e, file=sys.stderr)

    # ---------------- 几何
    def _initial_rect(self) -> QRect:
        """以屏幕中心为原点：60% 宽 × 26% 高（VRChat 聊天框一般在中间偏上/偏下）。"""
        g = self.screen_rect
        w = max(MIN_W, int(round(g.width() * 0.60)))
        h = max(MIN_H, int(round(g.height() * 0.26)))
        x = g.x() + (g.width() - w) // 2
        y = g.y() + (g.height() - h) // 2
        return QRect(x, y, w, h)

    def _clamp(self, r: QRect) -> QRect:
        """选区永远留在屏幕内，且不小于最小尺寸。"""
        g = self.screen_rect
        w = max(MIN_W, min(r.width(), g.width()))
        h = max(MIN_H, min(r.height(), g.height()))
        x = min(max(r.x(), g.x()), g.x() + g.width() - w)
        y = min(max(r.y(), g.y()), g.y() + g.height() - h)
        return QRect(x, y, w, h)

    def _hit(self, pos: QPoint) -> str:
        r = self.sel
        near_l = abs(pos.x() - r.left()) <= HIT
        near_r = abs(pos.x() - r.right()) <= HIT
        near_t = abs(pos.y() - r.top()) <= HIT
        near_b = abs(pos.y() - r.bottom()) <= HIT
        inside_x = r.left() - HIT <= pos.x() <= r.right() + HIT
        inside_y = r.top() - HIT <= pos.y() <= r.bottom() + HIT
        if near_t and near_l: return "nw"
        if near_t and near_r: return "ne"
        if near_b and near_l: return "sw"
        if near_b and near_r: return "se"
        if near_t and inside_x: return "n"
        if near_b and inside_x: return "s"
        if near_l and inside_y: return "w"
        if near_r and inside_y: return "e"
        if r.contains(pos): return "move"
        return ""

    # ---------------- 画
    def paintEvent(self, ev):                              # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        full = self.rect()
        r = self.sel
        mask = QColor(*MASK_RGBA)
        # 四块遮罩围出一个「透明洞」——选区内部能看见底下的画面
        p.fillRect(QRect(full.left(), full.top(), full.width(), r.top() - full.top()), mask)
        p.fillRect(QRect(full.left(), r.bottom() + 1, full.width(),
                         full.bottom() - r.bottom()), mask)
        p.fillRect(QRect(full.left(), r.top(), r.left() - full.left(), r.height()), mask)
        p.fillRect(QRect(r.right() + 1, r.top(),
                         full.right() - r.right(), r.height()), mask)

        # 边线：外发光 + 实线
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(34, 211, 238, 60), 7))
        p.drawRect(r.adjusted(-2, -2, 2, 2))
        p.setPen(QPen(QColor(ACCENT), 2))
        p.drawRect(r)

        # 8 个手柄
        hw, hh = HANDLE, HANDLE
        pts = [
            (r.left(), r.top()), (r.center().x(), r.top()), (r.right(), r.top()),
            (r.left(), r.center().y()), (r.right(), r.center().y()),
            (r.left(), r.bottom()), (r.center().x(), r.bottom()), (r.right(), r.bottom()),
        ]
        for cx, cy in pts:
            box = QRectF(cx - hw / 2.0, cy - hh / 2.0, hw, hh)
            p.setPen(QPen(HANDLE_EDGE, 1))
            p.setBrush(HANDLE_FILL)
            p.drawRoundedRect(box, 1.5, 1.5)

        # 左上：W × H
        size_txt = "%d × %d" % (r.width(), r.height())
        f = _font(15, True)
        p.setFont(f)
        fm = p.fontMetrics()
        tw, th = fm.horizontalAdvance(size_txt), fm.height()
        chip = QRect(r.left(), max(full.top() + 2, r.top() - th - 12),
                     tw + 18, th + 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(8, 13, 21, 225))
        p.drawRoundedRect(QRectF(chip), 5, 5)
        p.setPen(QColor(ACCENT))
        p.drawText(chip, Qt.AlignmentFlag.AlignCenter, size_txt)

        # 右下：操作提示
        hint = "拖边/角拉伸 · 拖动中间移动 · 回车确认 · Esc 取消"
        f2 = _font(13)
        p.setFont(f2)
        fm2 = p.fontMetrics()
        hw2, hh2 = fm2.horizontalAdvance(hint), fm2.height()
        hx = min(r.right() - hw2 - 14, full.width() - hw2 - 22)
        hy = r.bottom() + 10
        if hy + hh2 + 12 > full.bottom():
            hy = r.bottom() - hh2 - 12
        hrect = QRect(hx, hy, hw2 + 14, hh2 + 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(8, 13, 21, 210))
        p.drawRoundedRect(QRectF(hrect), 5, 5)
        p.setPen(QColor(214, 226, 240, 230))
        p.drawText(hrect, Qt.AlignmentFlag.AlignCenter, hint)

        # 屏幕中上：一行标题，避免用户不知道这是什么
        title = "框住 VRChat 聊天框 —— 回车确认 / Esc 取消 / Tab 复位"
        p.setFont(_font(14, True))
        fm3 = p.fontMetrics()
        tw3 = fm3.horizontalAdvance(title)
        tRect = QRect((full.width() - tw3) // 2 - 14, max(full.top() + 26, 26),
                      tw3 + 28, fm3.height() + 10)
        if tRect.bottom() > r.top() - 8:
            tRect.moveTop(max(full.top() + 6, r.top() - fm3.height() - 22))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(8, 13, 21, 200))
        p.drawRoundedRect(QRectF(tRect), 6, 6)
        p.setPen(QColor(ACCENT))
        p.drawText(tRect, Qt.AlignmentFlag.AlignCenter, title)
        p.end()

    # ---------------- 鼠标
    def mousePressEvent(self, ev):                          # noqa: N802
        if ev.button() == Qt.MouseButton.RightButton:
            self._cancel()
            return
        kind = self._hit(ev.position().toPoint())
        if not kind:
            return
        self._drag = (kind, ev.globalPosition().toPoint(), QRect(self.sel))

    def mouseMoveEvent(self, ev):                           # noqa: N802
        pos = ev.position().toPoint()
        if self._drag is None:
            kind = self._hit(pos)
            want = CURSORS.get(kind, Qt.CursorShape.ArrowCursor)
            if want != self._cursor:
                self._cursor = want
                self.setCursor(QCursor(want))
            return

        kind, start_g, start_sel = self._drag
        d = ev.globalPosition().toPoint() - start_g
        r = QRect(start_sel)
        if kind == "move":
            r.moveTo(start_sel.topLeft() + d)
            self.sel = self._clamp(r)
            return

        left, top, right, bottom = (start_sel.left(), start_sel.top(),
                                    start_sel.right(), start_sel.bottom())
        if "w" in kind:
            left += d.x()
        if "e" in kind:
            right += d.x()
        if "n" in kind:
            top += d.y()
        if "s" in kind:
            bottom += d.y()
        g = self.screen_rect
        # 交换时保持合法（拖过头也不会翻面），再夹进屏幕
        if right - left + 1 < MIN_W:
            if "w" in kind:
                left = right - MIN_W + 1
            else:
                right = left + MIN_W - 1
        if bottom - top + 1 < MIN_H:
            if "n" in kind:
                top = bottom - MIN_H + 1
            else:
                bottom = top + MIN_H - 1
        left = max(g.left(), min(left, g.right()))
        right = min(g.right(), max(right, g.left()))
        top = max(g.top(), min(top, g.bottom()))
        bottom = min(g.bottom(), max(bottom, g.top()))
        self.sel = QRect(QPoint(left, top), QPoint(right, bottom))
        self.update()

    def mouseReleaseEvent(self, ev):                        # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, ev):                    # noqa: N802
        if self.sel.contains(ev.position().toPoint()):
            self._confirm()

    # ---------------- 键盘
    def keyPressEvent(self, ev):                            # noqa: N802
        k = ev.key()
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()
            return
        if k == Qt.Key.Key_Escape:
            self._cancel()
            return
        if k == Qt.Key.Key_Tab:
            self.sel = QRect(self.initial)
            self.update()
            return
        step = 10 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        r = QRect(self.sel)
        if k == Qt.Key.Key_Left:
            r.moveLeft(r.left() - step)
        elif k == Qt.Key.Key_Right:
            r.moveLeft(r.left() + step)
        elif k == Qt.Key.Key_Up:
            r.moveTop(r.top() - step)
        elif k == Qt.Key.Key_Down:
            r.moveTop(r.top() + step)
        else:
            super().keyPressEvent(ev)
            return
        self.sel = self._clamp(r)
        self.update()

    # ---------------- 结果
    def _confirm(self):
        self.picked = QRect(self.sel)
        if self.args.shot:
            QTimer.singleShot(0, lambda: self._save_crop_then_quit(self.args.shot))
        else:
            self._emit_and_quit()

    def _cancel(self):
        self.cancelled = True
        print("[picker] 取消（Esc / 右键）", file=sys.stderr)
        self.close()
        QApplication.exit(0)

    def _emit_and_quit(self):
        r = self.picked or self.sel
        line = json.dumps({"region": [int(r.x()), int(r.y()),
                                      int(r.width()), int(r.height())]},
                          ensure_ascii=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        print("[picker] 确认:", line, file=sys.stderr)
        QApplication.exit(0)

    def _save_crop_then_quit(self, path: str):
        """--shot：把选区那一块真实屏幕（遮罩已藏起来）存成 PNG。"""
        self.hide()
        QApplication.processEvents()
        ok = False
        try:
            crop = self._desktop_pixmap(self.sel)
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            ok = crop.save(path, "PNG")
        except Exception as e:                              # noqa: BLE001
            print("[picker] 选区截图失败:", e, file=sys.stderr)
        print("[picker] --shot %s -> %s" % (path, "OK" if ok else "FAIL"), file=sys.stderr)
        self._emit_and_quit()

    # ---------------- GDI 抓屏（Qt 的 grabWindow 抓不到分层窗口，这里直接 BitBlt）
    def _desktop_pixmap(self, rect: QRect):
        from PySide6.QtGui import QImage, QPixmap

        scr = QGuiApplication.primaryScreen()
        dpr = float(scr.devicePixelRatio()) if scr else 1.0
        x, y = int(rect.x() * dpr), int(rect.y() * dpr)
        w, h = int(rect.width() * dpr), int(rect.height() * dpr)
        w, h = max(1, w), max(1, h)
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdesk = user32.GetDC(0)
        memdc = gdi32.CreateCompatibleDC(hdesk)
        hbmp = gdi32.CreateCompatibleBitmap(hdesk, w, h)
        gdi32.SelectObject(memdc, hbmp)
        SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
        gdi32.BitBlt(memdc, 0, 0, w, h, hdesk, x, y, SRCCOPY | CAPTUREBLT)

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]

        bi = BMIH()
        bi.biSize = ctypes.sizeof(BMIH)
        bi.biWidth, bi.biHeight = w, -h
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, hbmp, 0, h, buf, ctypes.byref(bi), 0)
        img = QImage(buf, w, h, QImage.Format.Format_RGB32).copy()
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdesk)
        pix = QPixmap.fromImage(img)
        pix.setDevicePixelRatio(1.0)
        return pix

    def _self_shot_then_quit(self, path: str):
        """--self-shot：抓一张「带遮罩的整屏」，用来自测这个框选器长什么样。"""
        try:
            pix = self._desktop_pixmap(self.screen_rect)
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            print("[picker] self-shot %s -> %s" % (path, "OK" if pix.save(path, "PNG") else "FAIL"),
                  file=sys.stderr)
        except Exception as e:                              # noqa: BLE001
            print("[picker] self-shot 失败:", e, file=sys.stderr)
        self.cancelled = True
        QApplication.exit(0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--shot", default="", help="自动确认并把选区截图存到指定路径")
    ap.add_argument("--self-shot", default="", help="自测：抓整屏（含遮罩）后退出")
    ap.add_argument("--region", default="", help="预设选区 x,y,w,h（逻辑像素）")
    ap.add_argument("--demo", action="store_true", help="只显示，不自动退出")
    args = ap.parse_args(argv)

    # Qt6 的 High-DPI 缩放默认就开（老 flag 已废弃，别再加，会往 stderr 喷警告）
    app = QApplication(sys.argv[:1])
    w = Picker(args)
    w.show()
    w.raise_()
    w.activateWindow()
    w.setFocus()
    if args.self_shot:
        QTimer.singleShot(700, lambda: w._self_shot_then_quit(args.self_shot))
    elif args.shot:
        # --shot 是验收通道：不用人按回车，600 ms 后自动确认（先让窗口画出来）
        QTimer.singleShot(600, w._confirm)
    code = app.exec()
    return int(code or 0)


if __name__ == "__main__":
    sys.exit(main())
