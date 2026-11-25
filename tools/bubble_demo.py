# -*- coding: utf-8 -*-
"""C 链闭环演示：在屏幕上画一个 VRChat 风格聊天气泡（日文），给 OCR 框选用。

用法：
    python tools/bubble_demo.py --x 260 --y 240 --w 620 --h 150 \
        --text "こんにちは！今日は一緒に遊べて嬉しいな、また明日も会おうね。" --hold 20

启动后第一行 stdout 打印 JSON 几何 {"x","y","w","h"}，之后安静等到 --hold 秒结束。
窗口：无边框、置顶、不抢焦点、鼠标穿透（跟真实聊天气泡一样浮在 VRChat 上面）。
"""
import json
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget


def arg(name, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


class Bubble(QWidget):
    def __init__(self, text, w, h):
        super().__init__()
        self.text = text
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.resize(w, h)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        r = self.rect().adjusted(2, 2, -3, -3)
        path = QPainterPath()
        path.addRoundedRect(r, 16, 16)
        p.fillPath(path, QColor(24, 28, 38, 238))
        p.setPen(QPen(QColor(90, 200, 220, 200), 2))
        p.drawPath(path)
        # 名字
        f = QFont("Microsoft YaHei", 10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(150, 220, 235))
        p.drawText(r.adjusted(16, 8, -16, 0), Qt.AlignLeft | Qt.AlignTop, "Aoi")
        # 正文
        f2 = QFont("Microsoft YaHei", 12)
        p.setFont(f2)
        p.setPen(QColor(240, 244, 250))
        p.drawText(r.adjusted(16, 34, -16, -12), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self.text)
        p.end()


def main():
    text = arg("--text", "こんにちは！今日は一緒に遊べて嬉しいな、また明日も会おうね。")
    x = int(arg("--x", 260)); y = int(arg("--y", 240))
    w = int(arg("--w", 620)); h = int(arg("--h", 150))
    hold = float(arg("--hold", 20))
    app = QApplication(sys.argv)
    b = Bubble(text, w, h)
    b.move(x, y)
    b.show()
    b.raise_()
    app.processEvents()
    time.sleep(0.35)
    app.processEvents()
    dpr = b.devicePixelRatioF() or 1.0
    g = b.frameGeometry()
    phy = {"x": int(g.x() * dpr), "y": int(g.y() * dpr),
           "w": int(g.width() * dpr), "h": int(g.height() * dpr)}
    print(json.dumps({"logical": {"x": x, "y": y, "w": w, "h": h}, "dpr": dpr,
                      "physical": phy, "text": text}, ensure_ascii=False), flush=True)
    QTimer.singleShot(int(hold * 1000), app.quit)
    app.exec()


if __name__ == "__main__":
    main()