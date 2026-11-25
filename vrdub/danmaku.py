#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VRDUB 弹幕浮窗（danmaku overlay）。

全屏透明、无边框、置顶、鼠标穿透、不进任务栏、不抢焦点。
每条字幕 = 一条从右向左匀速飘过的弹幕：主行译文（亮、链色），副行原文（暗、小一号）。

数据口和 hud.py 完全一致：stdin 每行一个 JSON（协议 §4），stdout 一个字节都不输出。
后端启动方式（app.py 里照这个起）：

    python -m vrdub.danmaku

子进程活着的判据和 hud.py 一样：后端每 5 s 写一行 {"op":"ping"}，20 s 收不到就自杀；
stdin 一断（后端死了）立刻退出，退出码 0，不留鬼影窗口。

单独调试（不需要后端）：

    python -m vrdub.danmaku --demo --size 1600x1000 --shot-b64

    python tools/danmaku_feed.py --demo --count 6 --hold 9 | python -m vrdub.danmaku
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time

from PySide6.QtCore import (QEasingCurve, QPointF, QRect, QRectF, QSize, Qt,
                            QTimer, QPropertyAnimation)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication,
                           QPainter, QPainterPath, QPen, QPixmap, QStaticText)
from PySide6.QtWidgets import QApplication, QWidget

PING_TIMEOUT_S = 20.0          # 后端 20 s 不 ping 就自杀（协议 §4）
FPS = 60                       # 60 fps 定时器（x 由时间算，掉帧也只是跳过采样，不会抖）
MIN_ON_SCREEN_S = 4.5          # 任何一条弹幕至少要在屏上活 4.5 s，短句也别飞过去
FADE_IN_S = 0.20               # 入场淡入
FADE_OUT_S = 0.30              # 出场淡出（按剩余距离折算）
CHAIN_RGB = {
    "A": (0x22, 0xD3, 0xEE),   # 青（和控制台一致）
    "B": (0xA7, 0x8B, 0xFA),   # 紫
    "C": (0xFB, 0x92, 0x3C),   # 橙
}
SUB_RGB = (0x9A, 0xA9, 0xBF)   # 原文：暗灰蓝
WHITE_RGB = (0xF2, 0xF6, 0xFC)  # 无底板样式的主行文字色（白字重描边，直播弹幕那种）
SUB_LIGHT_RGB = (0xD7, 0xDE, 0xE8)
STYLE_NAMES = ("card", "plain", "bar", "neon")


def dbg(*a):
    """诊断信息只走 stderr（stdout 必须保持 0 字节）。"""
    try:
        sys.stderr.write("[danmaku] " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


class Item:
    """一条弹幕：底板 + 左侧链色条 + 描边文字整条预渲染成一张 QPixmap。

    每帧只做一次 drawPixmap(浮点坐标)，文字不再逐帧重排/描边；
    横向位置由 x0 - speed * (now - born) 算出来，不累加 dx，掉帧也不会抖。
    """

    __slots__ = ("sid", "chain", "dst", "src", "lane", "x0", "w", "h", "speed",
                 "born", "seen", "pix", "y_top", "drawn_x")

    def __init__(self, sid: str, chain: str, lane: int, x0: float):
        self.sid = sid
        self.chain = chain if chain in CHAIN_RGB else "A"
        self.dst = ""
        self.src = ""
        self.lane = int(lane)
        self.x0 = float(x0)
        self.w = 0.0
        self.h = 0.0
        self.speed = 0.0
        self.born = time.monotonic()
        self.seen = self.born
        self.pix = None
        self.y_top = 0.0
        self.drawn_x = None          # 上一帧画到的 x（脏矩形要连旧位置一起擦）

    # ---- 位置
    def x_at(self, now: float) -> float:
        return self.x0 - self.speed * max(0.0, now - self.born)

    def fade(self, now: float, fade_out_px: float) -> float:
        age = now - self.born
        a = 1.0
        if FADE_IN_S > 0 and age < FADE_IN_S:
            a = max(0.0, age / FADE_IN_S)
        remain = self.x_at(now) + self.w
        if fade_out_px > 1.0 and remain < fade_out_px:
            a = min(a, max(0.0, remain / fade_out_px))
        return a

    # ---- 纹理
    def set_text(self, dst: str, src: str, style: dict, max_w: int) -> bool:
        """换字只重建纹理（同 id 不新开一条）；返回是否真的重画了。"""
        dst = (dst or "").strip()
        src = (src or "").strip()
        if not dst and not src:
            return False
        if dst == self.dst and src == self.src and self.pix is not None:
            return False
        self.dst, self.src = dst, src
        main_txt = dst or src
        sub_txt = src if (src and src != dst) else ""
        f_main = style["font_main"]
        f_sub = style["font_sub"]
        fm_main = style["fm_main"]
        fm_sub = style["fm_sub"]
        main_s = fm_main.elidedText(main_txt, Qt.TextElideMode.ElideRight, max_w)
        sub_s = fm_sub.elidedText(sub_txt, Qt.TextElideMode.ElideRight, max_w) if sub_txt else ""
        w_main = float(fm_main.horizontalAdvance(main_s))
        w_sub = float(fm_sub.horizontalAdvance(sub_s)) if sub_s else 0.0
        pad_x = style["pad_x"]
        pad_y = style["pad_y"]
        bar = style["bar"]
        bar_gap = style["bar_gap"]
        text_w = max(w_main, w_sub)
        _mode = str(style.get("mode", "card") or "card")
        _bar_w = (bar + bar_gap) if _mode in ("card", "bar", "neon") else 0
        w = float(pad_x * 2 + _bar_w + text_w)
        h = float(pad_y * 2 + fm_main.height() + ((fm_sub.height() if sub_s else 0) + 4 if sub_s else 0))
        dpr = style["dpr"]
        mode = style.get("mode", "card")
        has_card = (mode == "card")
        has_bar = mode in ("card", "bar", "neon")
        pix = QPixmap(max(1, int(round(w * dpr))), max(1, int(round(h * dpr))))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        r = float(style["radius"])
        rgb = CHAIN_RGB.get(self.chain, CHAIN_RGB["A"])
        # 底板只有 card 画：plain/bar/neon 都是「裸字浮在画面上」，不吃掉背后的画面
        if has_card:
            shadow = QPainterPath()
            shadow.addRoundedRect(QRectF(1.5, 3.0, w - 3.0, h - 3.5), r, r)
            p.fillPath(shadow, QColor(0, 0, 0, 78))
            body = QPainterPath()
            body.addRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 3.0), r, r)
            p.fillPath(body, QColor(7, 11, 19, 152))
            p.setPen(QPen(QColor(255, 255, 255, 30), 1.0))
            p.drawPath(body)
        p.setPen(Qt.PenStyle.NoPen)
        # 左侧链色条（plain 不要，纯白描边）
        tx = float(pad_x) * 0.9
        if has_bar:
            bar_x = float(pad_x) * 0.58
            barp = QPainterPath()
            barp.addRoundedRect(QRectF(bar_x, pad_y * 0.65, float(bar),
                                       max(6.0, h - pad_y * 1.3 - 3.0)), bar / 2.0, bar / 2.0)
            p.fillPath(barp, QColor(rgb[0], rgb[1], rgb[2], 248))
            tx = bar_x + bar + bar_gap
        # 主行：无底板时描边加粗 35%，压在任何亮画面（VRChat 白地板/天空）上都分离
        base_main = float(pad_y + fm_main.ascent())
        path = QPainterPath()
        path.addText(QPointF(tx, base_main), f_main, main_s)
        ow = float(style["outline"]) * (1.0 if has_card else 1.35)
        if mode == "neon":
            # 发光：两层同色半透明圆角描边垫在字下面，再压深色描边和实心字
            for mult, alpha in ((4.8, 46), (2.3, 82)):
                p.setPen(QPen(QColor(rgb[0], rgb[1], rgb[2], alpha), ow * mult,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                              Qt.PenJoinStyle.RoundJoin))
                p.setBrush(QColor(rgb[0], rgb[1], rgb[2], alpha))
                p.drawPath(path)
        ink = QColor(3, 6, 12, 240 if has_card else 255)
        p.setPen(QPen(ink, ow, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(ink)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        main_rgb = rgb if mode in ("card", "neon") else WHITE_RGB
        p.setBrush(QColor(main_rgb[0], main_rgb[1], main_rgb[2], 255))
        p.drawPath(path)
        # 副行：原文不抢译文；无底板时用亮灰 + 粗描边代替色块
        if sub_s:
            base_sub = float(pad_y + fm_main.height() + 4 + fm_sub.ascent())
            if has_card:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 122))
                p.drawRoundedRect(QRectF(tx - 3.0, base_sub - fm_sub.ascent() - 1.5,
                                         w_sub + 6.0, fm_sub.height() + 3.0), 3.5, 3.5)
            spath = QPainterPath()
            spath.addText(QPointF(tx, base_sub), f_sub, sub_s)
            sink = QColor(2, 4, 8, 210 if has_card else 245)
            p.setPen(QPen(sink, float(style["outline_sub"]) * (1.0 if has_card else 1.3),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(sink)
            p.drawPath(spath)
            p.setPen(Qt.PenStyle.NoPen)
            sc = SUB_RGB if mode in ("card", "neon") else SUB_LIGHT_RGB
            p.setBrush(QColor(sc[0], sc[1], sc[2], 242))
            p.drawPath(spath)
        p.end()
        self.pix = pix
        self.w, self.h = w, h
        self.seen = time.monotonic()
        return True


class DanmakuWindow(QWidget):
    def __init__(self, args):
        super().__init__(None)
        self.args = args
        self.cfg = {
            "alpha": 100,        # 文字不透明度 0–100
            "scale": 100,        # 整体缩放
            "style": "card",     # card 卡片 / plain 纯描边 / bar 只留色条 / neon 发光
            "top_ratio": 0.12,   # 弹幕带起点 = 屏幕高 * top_ratio（默认离顶部 12%，不压 UI）
            "lanes": 4,          # 泳道数 2–6
            "max_live": 4,       # 同屏最多几条（4 条刚好读得过来）
            "dur_s": 7.0,        # 一条弹幕横穿整屏的时间（秒，下限 4.5 s 由 MIN_ON_SCREEN_S 兜底）
        }
        self.items: list[Item] = []
        self.by_id: dict[str, Item] = {}
        self.waiting: list[dict] = []          # 泳道全忙时排队
        self._dirty_extra = QRect()            # 被撤掉的那条留下的残影区域
        self.last_ping = time.monotonic()
        self.last_tick = time.monotonic()
        self.quit_on_eof = not args.demo

        self.setWindowTitle("VRDUB 弹幕")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._apply_flags()

        self.scr = QGuiApplication.primaryScreen()
        g = self.scr.geometry() if self.scr else None
        w = int(args.size_w or (g.width() if g else 1600))
        h = int(args.size_h or (g.height() if g else 1000))
        self.win_w, self.win_h = w, h
        if g and not args.size_w:
            self.setGeometry(g)
        else:
            self.setGeometry(int(args.pos_x), int(args.pos_y), w, h)
        self._apply_metrics()
        dbg("度量:", self.metrics_desc())

        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._stdin_reader, args=(self.q,), daemon=True).start()

        self.t_drain = QTimer(self)
        self.t_drain.timeout.connect(self._drain)
        self.t_drain.start(25)

        self.t_tick = QTimer(self)
        self.t_tick.timeout.connect(self._tick)
        self.t_tick.start(max(16, int(1000 / FPS)))

        self.t_watch = QTimer(self)
        self.t_watch.timeout.connect(self._watchdog)
        self.t_watch.start(1000)

        if args.demo:
            QTimer.singleShot(120, self._demo)
        if args.probe_hit:
            QTimer.singleShot(int(max(0.5, args.probe_after) * 1000), self._probe_hit)
        if args.shot or args.shot_b64:
            QTimer.singleShot(int(max(0.2, args.shot_after) * 1000), self._take_shot)

    # ---------------- 窗口标志：鼠标穿透 + 不抢焦点 + 不进任务栏
    def _apply_flags(self):
        flags = (Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowStaysOnTopHint |
                 Qt.WindowType.Tool |                     # Tool：不进任务栏、不抢焦点
                 Qt.WindowType.WindowDoesNotAcceptFocus |
                 Qt.WindowType.WindowTransparentForInput)
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    # ---------------- 尺寸 / 字体（改 cfg 就重算）
    def _s(self) -> float:
        return max(0.5, float(self.cfg["scale"]) / 100.0)

    def _apply_metrics(self):
        s = self._s()
        # 主行 27px（缩放 22–34）：1080p/1440p 屏上隔一两米也能读
        self.px_main = max(18, int(round(27 * s)))
        self.px_sub = max(13, int(round(21 * s)))
        fams = ["Microsoft YaHei UI", "Segoe UI", "Noto Sans CJK SC", "sans-serif"]
        self.font_main = QFont()
        self.font_main.setFamilies(fams)
        self.font_main.setPixelSize(self.px_main)
        self.font_main.setWeight(QFont.Weight.DemiBold)
        self.font_sub = QFont()
        self.font_sub.setFamilies(fams)
        self.font_sub.setPixelSize(self.px_sub)
        self.font_sub.setWeight(QFont.Weight.Normal)
        fm_main = QFontMetrics(self.font_main)
        fm_sub = QFontMetrics(self.font_sub)
        pad_y = max(5, int(round(7 * s)))
        self.style = {
            "font_main": self.font_main, "font_sub": self.font_sub,
            "fm_main": fm_main, "fm_sub": fm_sub,
            "pad_x": max(9, int(round(14 * s))),
            "pad_y": pad_y,
            "bar": max(3, int(round(4 * s))),
            "bar_gap": max(5, int(round(8 * s))),
            "radius": max(6.0, 6.0 * s),
            "outline": max(2.6, 3.0 * s),       # 主行描边宽度（px）
            "outline_sub": max(2.0, 2.4 * s),
            "dpr": float(self.devicePixelRatioF() or 1.0),
            "mode": str(self.cfg.get("style", "card") or "card"),
        }
        # 一条纹理的实际高度（两行 + 内边距），泳道高 = 它 + 缝
        self.lane_gap = max(10, int(round(14 * s)))
        tex_h = pad_y * 2 + fm_main.height() + fm_sub.height() + 4 + 3
        self.lane_h = float(tex_h + self.lane_gap)
        self.max_text_w = max(240, int(self.win_w * 0.60))
        self.gap_px = max(60, int(round(110 * s)))
        self.band_top = int(round(self.win_h * max(0.0, min(0.6, float(self.cfg["top_ratio"])))))
        self.lanes = max(2, min(6, int(self.cfg["lanes"])))
        if getattr(self, "style", None) and self.items:
            now = time.monotonic()
            for it in list(self.items):
                if it.set_text(it.dst, it.src, self.style, self.max_text_w):
                    self._retime(it, now, entry=False)

    def metrics_desc(self) -> str:
        return ("px_main=%d px_sub=%d lane_h=%.0f lanes=%d band_top=%d win=%dx%d "
                "dpr=%.2f max_text_w=%d" % (
                    self.px_main, self.px_sub, self.lane_h, self.lanes, self.band_top,
                    self.win_w, self.win_h, self.style["dpr"], self.max_text_w))

    def lane_top(self, lane: int) -> float:
        return float(self.band_top + lane * self.lane_h)

    # ---------------- stdin：os.read 直读 fd 0（不碰 BufferedReader 锁，同 hud.py）
    def _stdin_reader(self, q: queue.Queue):
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
        except Exception as e:
            dbg("stdin 读取异常:", e)
        finally:
            q.put(None)

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
                continue
            try:
                obj = json.loads(item.decode("utf-8", "replace"))
            except Exception as e:
                dbg("JSON 解析失败:", e, item[:100])
                continue
            try:
                self.dispatch(obj)
            except Exception as e:
                dbg("处理失败:", e, item[:100])

    def dispatch(self, ev: dict):
        op = ev.get("op")
        if op == "seg":
            seg = ev.get("seg") or {}
            if isinstance(seg, dict):
                self.on_seg(seg)
        elif op == "cfg":
            self.on_cfg(ev)
        elif op == "ping":
            self.last_ping = time.monotonic()
        elif op == "clear":
            self.clear_all()
        elif op == "mic":
            pass                        # 弹幕不显示闭麦徽标（hud.py 负责）
        elif op == "status":
            pass
        elif op == "quit":
            self.quit_now("收到 quit")
        else:
            dbg("未知 op:", op)

    def on_cfg(self, ev: dict):
        changed = False
        for k in ("alpha", "lanes", "scale", "top_ratio", "dur_s", "max_live"):
            if k in ev and ev[k] is not None:
                v = num(ev[k], self.cfg[k])
                if k in ("alpha", "lanes", "max_live", "scale"):
                    v = int(v)
                if float(v) != float(self.cfg[k]):
                    self.cfg[k] = v
                    changed = True
        if ev.get("style") is not None:
            s = str(ev["style"]).strip().lower()
            if s in STYLE_NAMES and s != self.cfg["style"]:
                self.cfg["style"] = s
                changed = True
        self.cfg["alpha"] = max(10, min(100, int(self.cfg["alpha"])))
        self.cfg["lanes"] = max(2, min(6, int(self.cfg["lanes"])))
        self.cfg["max_live"] = max(1, min(12, int(self.cfg["max_live"])))
        self.cfg["scale"] = max(50, min(240, int(self.cfg["scale"])))
        self.cfg["dur_s"] = max(2.5, min(20.0, float(self.cfg["dur_s"])))
        self.cfg["top_ratio"] = max(0.0, min(0.6, float(self.cfg["top_ratio"])))
        if changed:
            self._apply_metrics()
            now = time.monotonic()
            for it in list(self.items):
                self._retime(it, now, entry=False)
            self.update()

    # ---------------- 弹幕逻辑
    def on_seg(self, seg: dict):
        sid = seg.get("id")
        if not sid:
            return
        sid = str(sid)
        now = time.monotonic()
        chain = str(seg.get("chain") or "A").upper()[:1] or "A"
        state = str(seg.get("state") or "")
        dst = (seg.get("dst") or "").strip()
        src = (seg.get("src") or "").strip()

        if state == "drop":                     # 被弃段：已经在飘的那条直接撤掉
            it = self.by_id.get(sid)
            if it is not None:
                self._remove(it)
                self.update()
            return
        if not dst and not src:
            return

        it = self.by_id.get(sid)
        if it is not None:                      # 同 id 原地换字，不新开一条（保住 x/泳道）
            if chain in CHAIN_RGB:
                it.chain = chain
            if it.set_text(dst, src, self.style, self.max_text_w):
                self._retime(it, now, entry=False)
            self.update()
            return

        job = {"sid": sid, "chain": chain, "dst": dst, "src": src}
        lane = self._pick_lane(now)
        if len(self.items) >= int(self.cfg["max_live"]):
            # 满了：先丢最旧的一条（同屏最多 max_live，保证不刷屏）
            if self.items:
                self._remove(self.items[0])
            lane = self._pick_lane(now)
        if lane is None:
            if len(self.waiting) >= 12:         # 排队上限，别无限积压
                self.waiting.pop(0)
            self.waiting.append(job)
            return
        self._spawn(job, lane, now)
        self.update()

    def _pick_lane(self, now: float = None):
        """挑一条尾部已经空出 gap 的泳道，保证弹幕不重叠。"""
        if now is None:
            now = time.monotonic()
        busy = {}
        for it in self.items:
            busy[it.lane] = max(busy.get(it.lane, -1e9), it.x_at(now) + it.w)
        best, best_tail = None, None
        for lane in range(self.lanes):
            tail = busy.get(lane, -1e9)
            if tail <= self.win_w - self.gap_px:
                if best_tail is None or tail < best_tail:
                    best, best_tail = lane, tail
        return best

    def _retime(self, it: Item, now: float, entry: bool):
        """速度按「整屏 + 纹理宽 / 时长」算；时长有下限，短句也不会一闪而过。"""
        dur = max(MIN_ON_SCREEN_S, float(self.cfg["dur_s"]))
        if entry:
            it.x0 = float(self.win_w) + 16.0
        else:
            it.x0 = it.x_at(now)                # 换字/改缩放：当前位置不动
        it.born = now
        it.speed = (self.win_w + max(it.w, 120.0)) / dur
        it.y_top = self.lane_top(it.lane)

    def _spawn(self, job: dict, lane: int, now: float = None):
        if now is None:
            now = time.monotonic()
        it = Item(job["sid"], job["chain"], lane, float(self.win_w) + 16.0)
        it.set_text(job["dst"], job["src"], self.style, self.max_text_w)
        self._retime(it, now, entry=True)
        self.items.append(it)
        self.by_id[it.sid] = it

    def _remove(self, it: Item):
        self.by_id.pop(it.sid, None)
        if it in self.items:
            self.items.remove(it)
        # 半路被撤（max_live 挤掉 / drop）：记住它的矩形，下一帧擦掉残影
        if it.drawn_x is not None and it.drawn_x + it.w > -4.0:
            self._dirty_extra = self._dirty_extra.united(QRect(
                int(it.drawn_x) - 6, int(it.y_top) - 8,
                int(it.w) + 16, int(it.h) + 18))

    def clear_all(self):
        self.items = []
        self.by_id = {}
        self.waiting = []
        self.update()

    def _tick(self):
        now = time.monotonic()
        if not self.items and not self.waiting:
            return
        dirty = QRect()
        dead = []
        for it in self.items:
            x = it.x_at(now)
            if x + it.w < -4.0:
                dead.append(it)
                continue
            xs = [x] if it.drawn_x is None else [x, it.drawn_x]
            x0 = int(min(xs)) - 6
            x1 = int(max(xs) + it.w) + 10
            dirty = dirty.united(QRect(x0, int(it.y_top) - 8,
                                       x1 - x0, int(it.h) + 18))
        for it in dead:
            self._remove(it)   # 已经在屏外，不需要重画
        # 排队里的，等泳道空出来再发
        while self.waiting and len(self.items) < int(self.cfg["max_live"]):
            lane = self._pick_lane(now)
            if lane is None:
                break
            self._spawn(self.waiting.pop(0), lane, now)
        if not self._dirty_extra.isNull():
            dirty = dirty.united(self._dirty_extra)
            self._dirty_extra = QRect()
        if not dirty.isNull():
            self.update(dirty)
        elif dead:
            self.update()

    # ---------------- 绘制：整条纹理一次贴上去（文字不再逐帧重排）
    def paintEvent(self, _ev):
        if not self.items:
            return
        now = time.monotonic()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        ga = max(0.0, min(1.0, float(self.cfg["alpha"]) / 100.0))
        for it in list(self.items):
            if it.pix is None:
                continue
            a = it.fade(now, it.speed * FADE_OUT_S)
            if a <= 0.02:
                continue
            x = round(it.x_at(now))
            p.setOpacity(a * ga)
            p.drawPixmap(QPointF(x, round(it.y_top)), it.pix)
            it.drawn_x = x
        p.end()

    # ---------------- watchdog / 退出
    def _watchdog(self):
        if time.monotonic() - self.last_ping > PING_TIMEOUT_S:
            self.quit_now("%.0f s 未收到 ping" % PING_TIMEOUT_S)

    def quit_now(self, why: str):
        dbg("退出:", why)
        for t in ("t_drain", "t_tick", "t_watch"):
            try:
                getattr(self, t).stop()
            except Exception:
                pass
        QApplication.quit()

    # ---------------- 自测：内置假数据（6 条，A/B/C 轮转，长度接近真字幕）
    DEMO = [
        ("A", "收到，我马上过去，你们先别开怪。", "わかった、すぐ行くから先に殴らないで。"),
        ("B", "这个副本我们上周就打过了，机制我熟。", "このダンジョン先週クリアしたよ、ギミックは覚えてる。"),
        ("C", "别走传送门，先在集合点等我三十秒。", "ポータルはやめて、集合場所で三十秒待ってて。"),
        ("A", "我的网络刚才卡了一下，可能会掉线。", "ネットがちょっと重くて、落ちるかもしれない。"),
        ("B", "今天的队伍配置挺稳的，治疗跟得上。", "今日の編成は安定してるね、ヒールも間に合ってる。"),
        ("C", "第二阶段注意脚下，红圈要提前散开。", "第二フェーズは足元注意、赤い輪は早めに散開して。"),
    ]

    def _demo(self):
        self.last_ping = time.monotonic()

        def feed(i=0):
            if i >= len(self.DEMO):
                return
            chain, dst, src = self.DEMO[i]
            self.on_seg({"id": "demo-%d" % i, "chain": chain, "state": "done",
                         "dst": dst, "src": src})
            self.last_ping = time.monotonic()
            QTimer.singleShot(1300, lambda: feed(i + 1))

        feed(0)

    # ---------------- 自测：鼠标穿透 / 不抢焦点 实证（Win32）
    def _probe_hit(self):
        """证明 WindowTransparentForInput 真生效：窗口正中取一点，
        WindowFromPoint 若返回的不是本窗口 → 点下去的是下层窗口（=穿透）。"""
        if sys.platform != "win32":
            dbg("probe: 非 Windows，跳过")
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
            bits = {"WS_EX_TRANSPARENT": 0x20, "WS_EX_NOACTIVATE": 0x08000000,
                    "WS_EX_TOOLWINDOW": 0x80, "WS_EX_LAYERED": 0x80000,
                    "WS_EX_TOPMOST": 0x8}
            flags = [k for k, b in bits.items() if ex & b]
            cx = int(self.x() + self.width() / 2)
            cy = int(self.y() + max(30, self.band_top + 20))
            hit = u.WindowFromPoint(wintypes.POINT(cx, cy))
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hit, cls, 256)
            fg = u.GetForegroundWindow()
            dbg("probe hwnd=0x%X exstyle=%s" % (hwnd, "|".join(flags) or "none"))
            dbg("probe point=(%d,%d) WindowFromPoint=0x%X class=%s passthrough=%s"
                % (cx, cy, hit, cls.value, hit != hwnd))
            dbg("probe foreground=0x%X steals_focus=%s" % (fg, fg == hwnd))
        except Exception as e:
            dbg("probe 失败:", e)

    # ---------------- 自测截图（子进程常常写不了盘，PNG 走 stderr base64）
    def _grab_physical(self):
        """抓「合成器真正输出的像素」（GDI BitBlt + CAPTUREBLT）。

        QWidget.grab() 走的是另一条渲染路径：DPR=1.5 时纹理会被按 1:1 贴进逻辑坐标系，
        截出来的字比屏幕上小一圈还被裁掉——那不是玩家看到的样子。屏幕像素才是。
        """
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            g = ctypes.windll.gdi32
            dpr = float(self.devicePixelRatioF() or 1.0)
            w = int(round(self.width() * dpr))
            h = int(round(self.height() * dpr))
            x = int(round(self.x() * dpr))
            y = int(round(self.y() * dpr))
            hdc = u.GetDC(0)
            mdc = g.CreateCompatibleDC(hdc)
            bmp = g.CreateCompatibleBitmap(hdc, w, h)
            g.SelectObject(mdc, bmp)
            g.BitBlt(mdc, 0, 0, w, h, hdc, x, y, 0x00CC0020 | 0x40000000)
            class BMIH(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]
            bi = BMIH()
            bi.biSize = ctypes.sizeof(BMIH)
            bi.biWidth = w
            bi.biHeight = -h
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            g.DeleteObject(bmp)
            g.DeleteDC(mdc)
            u.ReleaseDC(0, hdc)
            from PySide6.QtGui import QImage
            return QImage(bytes(buf), w, h, QImage.Format.Format_ARGB32).copy()
        except Exception as e:
            dbg("桌面抓图失败:", e)
            return None

    def _take_shot_b64(self):
        from PySide6.QtCore import QBuffer, QByteArray
        import base64
        try:
            img = None if self.args.shot_window else self._grab_physical()
            if img is not None:
                # 屏幕上真实的一帧（含后面的游戏画面），物理像素 1:1
                pix = QPixmap.fromImage(img)
                dbg("抓屏: %dx%d (物理像素)" % (pix.width(), pix.height()))
            else:
                pix = self.grab()
            # 高 DPI 屏（DPR 1.5/2）下 grab 出来是设备像素，截图要按 --size 还原成逻辑尺寸
            if img is None and self.args.size_w and pix.width() != int(self.args.size_w):
                pix = pix.scaled(int(self.args.size_w), int(self.args.size_h),
                                 Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            if img is None and not self.args.shot_bg:
                # 透明背景直接看会像「白底黑字」，合成一层深色模拟游戏画面（只影响截图）
                from PySide6.QtGui import QImage, QLinearGradient, QBrush
                img = QImage(pix.size(), QImage.Format.Format_ARGB32_Premultiplied)
                gp = QPainter(img)
                grad = QLinearGradient(0, 0, 0, img.height())
                grad.setColorAt(0.0, QColor(18, 24, 34))
                grad.setColorAt(1.0, QColor(9, 12, 18))
                gp.fillRect(img.rect(), QBrush(grad))
                # 粗糙的「游戏地面」示意线，方便看出弹幕在上 1/4 区域
                gp.setPen(QColor(255, 255, 255, 12))
                for k in range(1, 6):
                    y = int(img.height() * (0.45 + 0.09 * k))
                    gp.drawLine(0, y, img.width(), y)
                gp.drawPixmap(0, 0, pix)
                gp.end()
                pix = QPixmap.fromImage(img)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QBuffer.OpenModeFlag.WriteOnly)
            pix.save(buf, "PNG")
            buf.close()
            raw = bytes(ba)
            sys.stderr.write("DANMAKU_SHOT_B64 %dx%d %d %s\n" % (
                pix.width(), pix.height(), len(raw),
                base64.b64encode(raw).decode("ascii")))
            sys.stderr.flush()
            dbg("b64 截图: %dx%d %d bytes items=%d" % (
                pix.width(), pix.height(), len(raw), len(self.items)))
            for it in self.items:
                pw = it.pix.width() / (it.pix.devicePixelRatio() or 1.0) if it.pix else 0
                dbg("  item %s lane=%d x=%.1f w=%.1f pixw=%.1f top=%.1f chain=%s" % (
                    it.sid, it.lane, it.x_at(time.monotonic()), it.w, pw, it.y_top, it.chain))
        except Exception as e:
            dbg("b64 截图失败:", e)
        if self.args.quit_after_shot:
            QTimer.singleShot(120, QApplication.quit)

    def _take_shot(self):
        if self.args.shot_b64 or not self.args.shot:
            self._take_shot_b64()
            return
        try:
            path = os.path.abspath(self.args.shot)
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            ok = self.grab().save(path, "PNG")
            dbg("截图:", path, "ok=" + str(ok), "items=%d" % len(self.items))
        except Exception as e:
            dbg("截图失败:", e)
        if self.args.quit_after_shot:
            QTimer.singleShot(120, QApplication.quit)


def _parse_size(txt):
    if not txt:
        return None, None
    t = str(txt).lower().replace("*", "x").split("x")
    if len(t) != 2:
        return None, None
    try:
        return int(t[0]), int(t[1])
    except ValueError:
        return None, None


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--demo", action="store_true", help="内置假数据自己飘（不读 stdin 也能看效果）")
    ap.add_argument("--size", default=None, help="强制窗口尺寸 WxH（截图用，平时铺满主屏）")
    ap.add_argument("--pos", default="0,0", help="--size 时的左上角坐标 x,y")
    ap.add_argument("--shot", default=None, help="自测：截图存到这个 png")
    ap.add_argument("--shot-after", type=float, default=4.0, help="启动多少秒后截图")
    ap.add_argument("--shot-b64", action="store_true", help="自测：PNG 以 base64 打到 stderr（DANMAKU_SHOT_B64 行）")
    ap.add_argument("--shot-bg", action="store_true", help="自测：截图不合成深色底（保留透明）")
    ap.add_argument("--shot-window", action="store_true",
                    help="自测：只抓本窗口（QWidget.grab，默认改为抓真实屏幕像素）")
    ap.add_argument("--quit-after-shot", action="store_true", help="截图后退出")
    ap.add_argument("--probe-hit", action="store_true", help="自测：Win32 实测鼠标穿透/不抢焦点，结果打 stderr")
    ap.add_argument("--probe-after", type=float, default=1.5, help="--probe-hit 在启动多少秒后测")
    ap.add_argument("--style", default=None, choices=list(STYLE_NAMES),
                    help="自测：卡片/纯描边/色条/发光")
    ap.add_argument("--scale", type=int, default=None, help="自测：整体大小 %")
    args = ap.parse_args()

    args.size_w, args.size_h = _parse_size(args.size)
    try:
        px, py = str(args.pos).split(",")
        args.pos_x, args.pos_y = int(px), int(py)
    except Exception:
        args.pos_x, args.pos_y = 0, 0

    # stdout 保证 0 字节
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

    app = QApplication(sys.argv[:1])
    app.setApplicationName("VRDUB Danmaku")
    app.setQuitOnLastWindowClosed(False)

    w = DanmakuWindow(args)
    if args.style or args.scale:            # 自测：命令行临时定样式，不动 settings.json
        w.cfg["style"] = args.style or w.cfg["style"]
        w.cfg["scale"] = int(args.scale or w.cfg["scale"])
        w._apply_metrics()
    w.show()
    try:                                # 弹幕同样从抓屏里排除（不然 C 链会去读自己的弹幕）
        from . import util as _util
        _util.hide_from_capture(w)
    except Exception:                   # noqa: BLE001
        pass
    dbg("启动 pid=%d size=%dx%d demo=%s hwnd=0x%X" % (os.getpid(), w.win_w, w.win_h,
                                                   args.demo, int(w.winId())))
    rc = app.exec()
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(int(rc or 0))


if __name__ == "__main__":
    sys.exit(main())
