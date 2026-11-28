# -*- coding: utf-8 -*-
r"""悬浮球 / 说话人窗口打后端用的极简客户端（只用标准库 + Qt 的异步壳）。

这两个窗口是后端的子进程，和后端在同一台机器上，所以直接 http.client 打
127.0.0.1:VRDUB_PORT（默认 8899）就够了——为一次 GET 引进 requests 不值得，
装包体积也不该为它涨 3 MB。

Caller 是给 Qt 用的异步版本：请求在后台线程发，结果用信号回到主线程
（Signal 从别的线程 emit 是线程安全的，Qt 会排队到主线程执行），界面不卡。
后端没起来时所有请求都返回 {"ok": False, "err": "..."}，调用方老老实实显示
「后端没起来」，绝不拿假数据糊弄。

    r = uiapi.get("/api/state")
    uiapi.post("/api/cfg", {"dst_lang": "ja"})
"""
from __future__ import annotations

import http.client
import json
import os
import threading

from PySide6.QtCore import QObject, Signal


def port():
    try:
        return int(os.environ.get("VRDUB_PORT") or 8899)
    except Exception:                                  # noqa: BLE001
        return 8899


def _req(method, path, body=None, timeout=4.0):
    c = None
    try:
        c = http.client.HTTPConnection("127.0.0.1", port(), timeout=timeout)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        if r.status >= 400:
            return {"ok": False, "err": "HTTP %d" % r.status}
        try:
            v = json.loads(raw.decode("utf-8"))
        except Exception:                              # noqa: BLE001
            return {"ok": False, "err": "响应不是 JSON"}
        return v if isinstance(v, dict) else {"ok": False, "err": "响应格式不对"}
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:                          # noqa: BLE001
                pass


def get(path, timeout=4.0):
    return _req("GET", path, None, timeout)


def post(path, body=None, timeout=20.0):
    return _req("POST", path, body if body is not None else {}, timeout)


def state(timeout=6.0):
    return get("/api/state", timeout)


class Caller(QObject):
    """后台线程发请求，结果回主线程。每个窗口一个实例即可。"""

    done = Signal(object)          # 结果 dict（一定带 "ok"；带 "_tag" 区分是谁的回包）

    def __init__(self, parent=None):
        super().__init__(parent)

    def get(self, path, tag="", timeout=4.0):
        self._spawn("GET", path, None, tag, timeout)

    def post(self, path, body=None, tag="", timeout=20.0):
        self._spawn("POST", path, body if body is not None else {}, tag, timeout)

    def _spawn(self, method, path, body, tag, timeout):
        def run():
            r = _req(method, path, body, timeout)
            if not isinstance(r, dict):
                r = {"ok": False, "err": "坏响应"}
            r = dict(r)
            r["_tag"] = tag
            try:
                self.done.emit(r)
            except RuntimeError:                       # 窗口已经销毁，信号没人接
                pass
        threading.Thread(target=run, name="uiapi", daemon=True).start()
