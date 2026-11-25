# -*- coding: utf-8 -*-
"""C 链「原处浮现」覆盖层：屏幕识别出来的句子，就在它原来的位置浮出一个中文气泡，
停留几秒后淡出。纯装饰层——不抢焦点、不挡鼠标、不进任何输入。

    python -m vrdub.ocr_overlay          # 由 app.py 拉起，一般不用手动跑
    python -m vrdub.ocr_overlay --feed   # 手动喂几行演示

stdin 协议（每行一个 JSON，UTF-8）：
    {"op":"cfg","hold":3.5,"fade":0.6,"show_src":true,"font":17,"scale":1.0}
    {"op":"show","items":[{"id":"k1","x":120,"y":400,"w":300,"h":26,"src":"こんにちは","dst":"你好"}]}
    {"op":"show","items":[{"id":"k1","src":"こんにちは"}]}      # 只更新 src（译文还没来）
    {"op":"clear"}
    {"op":"ping"}
    {"op":"quit"}

坐标一律是**逻辑像素**（Qt 的坐标系），物理像素→逻辑像素的换算在后端做。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time


def _round_rect(painter, rect, r):
    from PySide6.QtGui import QPainterPath
    p = QPainterPath()
    p.addRoundedRect(rect, r, r)
    return p


class Overlay(object):
    def __init__(self, cfg):
        self.cfg = dict(cfg or {})
        self.items = {}            # id -> dict
        self.order = []            # 由旧到新，超出 max_live 先淘汰最旧的
        self.q = queue.Queue(maxsize=512)
        self._last_ping = time.time()
        self._win = None
        self._app = None

    # ------------------------------------------------------------------ 协议
    def apply(self, obj):
        op = obj.get("op")
        if op == "cfg":
            for k in ("hold", "fade", "show_src", "font", "scale", "max_live", "accent"):
                if k in obj:
                    self.cfg[k] = obj[k]
            if self._win is not None:
                self._win.apply_cfg(self.cfg)
        elif op == "show":
            for it in obj.get("items") or []:
                self.put(it)
        elif op == "clear":
            self.items.clear()
            self.order = []
        elif op == "ping":
            self._last_ping = time.time()
        elif op == "quit":
            raise SystemExit(0)

    def put(self, it):
        iid = str(it.get("id") or "%f" % time.time())
        cur = self.items.get(iid)
        if cur is None:
            cur = {"id": iid, "x": 0, "y": 0, "w": 0, "h": 0, "src": "", "dst": "", "t": time.time()}
            self.items[iid] = cur
            self.order.append(iid)
            max_live = int(self.cfg.get("max_live", 12) or 12)
            while len(self.order) > max_live:
                old = self.order.pop(0)
                self.items.pop(old, None)
        for k in ("x", "y", "w", "h", "src", "dst"):
            if k in it and it[k] not in (None, ""):
                cur[k] = it[k]
        # 内容变了/新气泡 → 重新计时，让它再停一会儿
        cur["t"] = time.time()

    # ------------------------------------------------------------------ 窗口
    def run(self):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication

        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        app = QApplication(sys.argv[:1])
        app.setQuitOnLastWindowClosed(False)
        self._app = app
        w = OverlayWindow(self)
        self._win = w
        w.apply_cfg(self.cfg)
        w.show()
        # 读 stdin 的线程：只入队，真正的绘制在 GUI 线程里做
        threading.Thread(target=self._reader, name="ov-stdin", daemon=True).start()
        tick = QTimer()
        tick.setInterval(16)
        tick.timeout.connect(self._pump)
        tick.start()
        app.exec()

    def _pump(self):
        w = self._win
        if w is None:
            return
        got = False
        for _ in range(64):
            try:
                obj = self.q.get_nowait()
            except Exception:
                break
            got = True
            self.apply(obj)
        if got:
            w.sync(self.items, self.order)
        w.tick()
        # 20 秒收不到 ping 就自己退出（后端没了不该留着幽灵窗口）
        if time.time() - self._last_ping > 20.0:
            raise SystemExit(0)

    def _reader(self):
        stream = sys.stdin
        while True:
            line = stream.readline()
            if not line:
                time.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            try:
                self.q.put_nowait(obj)
            except Exception:
                try:
                    self.q.get_nowait()
                    self.q.put_nowait(obj)
                except Exception:
                    pass


class OverlayWindow(object):
    """全屏透明、点击穿透、不抢焦点的绘制面。"""

    def __init__(self, owner):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QFont, QGuiApplication
        from PySide6.QtWidgets import QWidget

        self.owner = owner
        self.cfg = dict(owner.cfg or {})
        self.items = {}
        self.order = []
        self._repaint = False

        class W(QWidget):
            pass

        win = W()
        win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                           Qt.Tool | Qt.WindowTransparentForInput |
                           Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        win.setAttribute(Qt.WA_TranslucentBackground, True)
        win.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        win.setAttribute(Qt.WA_ShowWithoutActivating, True)
        geo = QGuiApplication.primaryScreen().virtualGeometry()
        win.setGeometry(geo)
        win.paintEvent = lambda ev: self._paint(win)
        self.win = win
        self.geo = geo
        self._hide_from_capture()
        self.font_px = int(self.cfg.get("font", 17) or 17)
        self.f_dst = QFont("Microsoft YaHei UI")
        self.f_dst.setPixelSize(self.font_px)
        self.f_src = QFont("Yu Gothic UI")
        self.f_src.setPixelSize(max(11, int(self.font_px * 0.72)))
        self.accent = QColor(str(self.cfg.get("accent", "#fb923c")))

    def _hide_from_capture(self):
        """让本窗口对屏幕抓取不可见（Win10 2004+ 的 WDA_EXCLUDEFROMCAPTURE）。

        不开这个，OCR 会把自己画出来的中文字又识别一遍 → 再翻译 → 屏幕上越翻越乱。
        必须等窗口真正有 hwnd 之后才生效，所以 show() 之后还要再调一次。
        """
        try:
            import ctypes
            hwnd = int(self.win.winId())
            if not hwnd:
                return
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if not ok:
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x00000001)  # 退到 WDA_MONITOR
        except Exception:                           # noqa: BLE001
            pass

    def show(self):
        self.win.show()
        self._hide_from_capture()

    def update(self):
        self.win.update()

    def width(self):
        return self.win.width()

    def height(self):
        return self.win.height()

    # noinspection PyUnusedLocal
    def apply_cfg(self, cfg):
        self.cfg = dict(cfg or {})
        self.font_px = int(self.cfg.get("font", 17) or 17)
        self.f_dst.setPixelSize(self.font_px)
        self.f_src.setPixelSize(max(11, int(self.font_px * 0.72)))
        self._repaint = True

    def sync(self, items, order):
        self.items = items
        self.order = order
        self._repaint = True

    def tick(self):
        if self._has_live():
            self.win.update()
        elif self._repaint:
            self.win.update()

    def _has_live(self):
        if not self.items:
            return False
        hold = float(self.cfg.get("hold", 3.5) or 3.5)
        fade = float(self.cfg.get("fade", 0.6) or 0.6)
        now = time.time()
        for it in self.items.values():
            if now - float(it.get("t", 0)) < hold + fade:
                return True
        return False

    # ------------------------------------------------------------------ 绘制
    def _paint(self, win):
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QPen, QBrush, QFontMetrics, QPainter

        hold = float(self.cfg.get("hold", 3.5) or 3.5)
        fade = max(0.05, float(self.cfg.get("fade", 0.6) or 0.6))
        show_src = bool(self.cfg.get("show_src", True))
        scale = float(self.cfg.get("scale", 1.0) or 1.0)
        now = time.time()

        p = QPainter(win)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        dead = []
        for iid in list(self.order):
            it = self.items.get(iid)
            if not it:
                continue
            age = now - float(it.get("t", 0))
            if age >= hold + fade:
                dead.append(iid)
                continue
            k = 1.0 if age <= hold else max(0.0, 1.0 - (age - hold) / fade)
            dst = it.get("dst") or ""
            src = it.get("src") or ""
            if not dst and not (show_src and src):
                continue
            x = float(it.get("x", 0)) * 1.0
            y = float(it.get("y", 0)) * 1.0
            bw = float(it.get("w", 0)) * 1.0
            bh = float(it.get("h", 0)) * 1.0
            pad_x, pad_y = 8.0, 5.0
            maxw = max(120.0, min(win.width() - x - 16.0, 640.0))
            # 先量文本：气泡宽度跟着译文走，但不超过原框 + 240px
            fm_d = QFontMetrics(self.f_dst)
            fm_s = QFontMetrics(self.f_src)
            text_w = fm_d.horizontalAdvance(dst) if dst else 0
            if show_src and src:
                text_w = max(text_w, int(fm_s.horizontalAdvance(src) * 0.92))
            box_w = min(max(bw, 0.0) + 40.0, max(text_w + pad_x * 2, 120.0))
            box_w = min(box_w, maxw)
            lines_h = (fm_d.height() if dst else 0) + ((fm_s.height() if (show_src and src) else 0))
            box_h = max(bh, 0.0) * 0.0 + (lines_h + pad_y * 2)
            # 竖直居中压在原文那一行上（不然气泡会从行顶往下长，看起来「浮在下面」）
            rx = QRectF(x, y - (box_h - max(bh, 0.0)) * 0.5, box_w, box_h)
            # 底：深色半透明 + 品牌色细边
            bg = QColor(12, 14, 20, int(196 * k))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(bg))
            p.drawPath(_round_rect(p, rx, 7.0))
            pen = QPen(QColor(self.accent.red(), self.accent.green(), self.accent.blue(),
                              int(150 * k)), 1.2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(_round_rect(p, rx.adjusted(0.6, 0.6, -0.6, -0.6), 7.0))
            ty = y + pad_y
            if show_src and src:
                p.setFont(self.f_src)
                p.setPen(QPen(QColor(220, 226, 240, int(168 * k))))
                p.drawText(QRectF(x + pad_x, ty, box_w - pad_x * 2, fm_s.height()),
                           int(Qt.AlignLeft | Qt.AlignVCenter),
                           fm_s.elidedText(src, Qt.ElideRight, int(box_w - pad_x * 2)))
                ty += fm_s.height()
            if dst:
                p.setFont(self.f_dst)
                p.setPen(QPen(QColor(255, 255, 255, int(244 * k))))
                p.drawText(QRectF(x + pad_x, ty, box_w - pad_x * 2, fm_d.height() + 2),
                           int(Qt.AlignLeft | Qt.AlignVCenter),
                           fm_d.elidedText(dst, Qt.ElideRight, int(box_w - pad_x * 2)))
        p.end()
        for iid in dead:
            self.owner.items.pop(iid, None)
            try:
                self.owner.order.remove(iid)
            except ValueError:
                pass


def _demo():
    """手动演示：屏幕中部浮 3 条，译文 0.6 秒后才到（模拟真实节奏）。"""
    import subprocess
    from . import child as childmod
    p = subprocess.Popen(childmod.argv("ocr_overlay"),
                         stdin=subprocess.PIPE,
                         creationflags=childmod.CREATE_NO_WINDOW,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    def send(o):
        p.stdin.write((json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8"))
        p.stdin.flush()
    send({"op": "cfg", "hold": 3.0, "fade": 0.8, "show_src": True, "font": 18})
    items = [{"id": "d1", "x": 420, "y": 420, "w": 300, "h": 24, "src": "こんにちは、今日は一緒に遊べて嬉しいな"},
             {"id": "d2", "x": 420, "y": 480, "w": 260, "h": 24, "src": "ちょっと待ってて、今行くから"},
             {"id": "d3", "x": 420, "y": 540, "w": 220, "h": 24, "src": "このワールド重くない？"}]
    send({"op": "show", "items": items})
    time.sleep(0.6)
    send({"op": "show", "items": [{"id": "d1", "dst": "你好呀，今天能一起玩真开心"},
                                  {"id": "d2", "dst": "等一下，我马上过去"},
                                  {"id": "d3", "dst": "这个世界不卡吗？"}]})
    time.sleep(5.0)
    send({"op": "quit"})


if __name__ == "__main__":
    if "--feed" in sys.argv:
        _demo()
    else:
        Overlay({}).run()
