# -*- coding: utf-8 -*-
"""热键：小键盘 + 识别一帧，小键盘 - 框选范围；CapsLock 闭麦，F7 常驻，F8 B 链，F9 清屏。

两条路一起用，谁先看到谁算：
1) RegisterHotKey + 消息循环——操作系统直接把按键派给我们，轻点不会漏，
   也不怕轮询周期撞车；某一个键被别的软件占了就自动退到第 2 条。
2) GetAsyncKeyState 轮询（20 ms）——用低位粘滞位判断「自上次查询以来按下过」，
   哪怕只按 10 ms 也能看到；GetAsyncKeyState 在非活动桌面会全 0，
   所以它是兜底，不是主力。

CapsLock 不注册（要读锁定状态），仍旧用 GetKeyState 的 bit0 比对。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

from . import util

VK = {"caps": 0x14, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
      "f9": 0x78, "f10": 0x79,
      # 小键盘：加号=识别一帧，减号=框选范围（不和任何软件抢键）
      "add": 0x6B, "sub": 0x6D}
_LL = ctypes.windll.user32
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
WM_QUIT = 0x0012

_LABEL = {"add": "小键盘+", "sub": "小键盘-"}


def _down(vk: int) -> bool:
    return bool(_LL.GetAsyncKeyState(vk) & 0x8000)


def _hit(vk: int) -> bool:
    """「自上次调用以来按下过」的粘滞位（bit0），轮询兜底用。"""
    return bool(_LL.GetAsyncKeyState(vk) & 0x0001)


def _latch(vk: int) -> bool:
    return bool(_LL.GetKeyState(vk) & 1)


class Hotkeys:
    def __init__(self, on_mic=None, on_b=None, on_clear=None, on_ocr=None,
                 on_shot=None, on_pick=None, log=util.log):
        self.on_mic = on_mic
        self.on_b = on_b
        self.on_clear = on_clear
        self.on_ocr = on_ocr
        self.on_shot = on_shot          # 识别这一帧（小键盘 + / F6）
        self.on_pick = on_pick          # 框选范围（小键盘 - / F5）
        self.log = log
        self._run = False
        self._thr = None                # 轮询线程
        self._mthr = None               # 消息循环线程
        self._mtid = 0
        self._edge = {}
        self._caps = None
        self._registered = {}
        self._poll = set()
        # 诊断用：最后一次按下的键名与时间戳（日志 + /api/state 能看到「按了」）
        self.last = {"name": None, "ts": 0.0, "n": 0}

    # ---------------------------------------------------------------- 启动
    def start(self):
        self._run = True
        self._caps = _latch(VK["caps"])
        self._mthr = threading.Thread(target=self._msg_loop, name="hotkey-win",
                                      daemon=True)
        self._mthr.start()
        self._thr = threading.Thread(target=self._loop, name="hotkey", daemon=True)
        self._thr.start()
        time.sleep(0.12)                # 让注册结果先落地，日志才准确
        reg = ", ".join(_LABEL.get(k, k.upper()) for k in self._registered.values()
                        if not k.startswith(("add", "sub"))) or ""
        self.log("热键就绪：小键盘 + 识别一帧 / 小键盘 - 框选范围 / CapsLock 闭麦 / "
                 "F7 常驻识别 / F8 B链 / F9 清屏（F5/F6 同功能，备用）"
                 + ("；系统级注册：%d 个" % len(self._registered)),
                 "info", "key")

    def stop(self):
        self._run = False
        try:
            if self._mtid:
                _LL.PostThreadMessageW(self._mtid, WM_QUIT, 0, 0)
        except Exception:                       # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 触发
    def _fire(self, name: str):
        self.last = {"name": _LABEL.get(name, name.upper()),
                     "ts": time.time(), "n": int(self.last.get("n", 0)) + 1}
        self.log("按下 %s" % _LABEL.get(name, name.upper()), "info", "key")
        fn = {"add": self.on_shot, "sub": self.on_pick,
              "f5": self.on_pick, "f6": self.on_shot, "f7": self.on_ocr,
              "f8": self.on_b, "f9": self.on_clear, "f10": None}.get(name)
        try:
            if name == "f10":
                if self.on_clear:
                    self.on_clear(True)
            elif fn:
                fn()
        except Exception as e:                  # noqa: BLE001
            self.log(f"热键 {name} 处理出错：{e!r}", "warn", "key")

    # ---------------------------------------------------------------- 路径 1
    def _msg_loop(self):
        self._mtid = ctypes.windll.kernel32.GetCurrentThreadId()
        want = {"add": 1, "sub": 2, "f7": 3, "f8": 4, "f9": 5, "f6": 6, "f5": 7}
        for name, ident in want.items():
            try:
                ok = bool(_LL.RegisterHotKey(None, ident, MOD_NOREPEAT, VK[name]))
            except Exception:                   # noqa: BLE001
                ok = False
            if ok:
                self._registered[ident] = name
            else:
                self._poll.add(name)
        try:
            msg = wt.MSG()
            while self._run:
                r = _LL.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    name = self._registered.get(int(msg.wParam))
                    if name:
                        self._fire(name)
        except Exception as e:                  # noqa: BLE001
            self.log(f"热键消息循环退出：{e!r}", "warn", "key")
        finally:
            for ident in list(self._registered):
                try:
                    _LL.UnregisterHotKey(None, ident)
                except Exception:               # noqa: BLE001
                    pass
            self._registered.clear()

    # ---------------------------------------------------------------- 路径 2
    def _loop(self):
        while self._run:
            try:
                st = _latch(VK["caps"])
                if self._caps is not None and st != self._caps:
                    self._caps = st
                    if self.on_mic:
                        self.on_mic(not st)
                for name in list(self._poll):
                    vk = VK[name]
                    d = _down(vk)
                    if (d and not self._edge.get(name)) or _hit(vk):
                        self._edge[name] = True
                        self._fire(name)
                    elif not d:
                        self._edge[name] = False
            except Exception as e:                    # noqa: BLE001
                self.log(f"热键线程：{e!r}", "warn", "key")
            time.sleep(0.02)
