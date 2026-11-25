# -*- coding: utf-8 -*-
"""HTTP + SSE 服务（标准库 http.server，只绑 127.0.0.1:8899）。

每个连接一个线程；SSE 连接各自从 bus 取有界队列，慢连接丢旧帧不拖累别人。
网页是纯静态文件，没有构建步骤 —— 现场改完刷新就能看。
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import util
from . import version

# 端口默认 8899；VRDUB_PORT 可覆盖——冒烟测试/多开时用它避开正在跑的那一份。
PORT = int(os.environ.get("VRDUB_PORT") or 8899)
from . import paths as _paths

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
OUT = _paths.OUT
_CT = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
       "css": "text/css; charset=utf-8", "svg": "image/svg+xml",
       "png": "image/png", "json": "application/json; charset=utf-8"}


class H(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"
    server_version = "vrdub/1.0"

    # ---------------------------------------------------------------- 基础
    def log_message(self, fmt, *a):          # 静音：访问日志进控制台没意义
        pass

    def handle_one_request(self):
        """客户端断连（刷新网页、SSE 重连、pwsh 超时）不是错误，别往控制台喷 traceback。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            self.close_connection = True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, body: bytes, ctype="text/plain; charset=utf-8", code=200, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/state":
                return self._json(self.app.state())
            if path == "/api/devices":
                return self._json({"ok": True, "devices": self.app.devices()})
            if path == "/api/metrics":
                return self.app.api_metrics(self._json)
            if path == "/api/health":
                h = {"ok": True, "ts": time.time(),
                     "ver": version.short(), "ver_full": version.full(), "pid": os.getpid()}
                try:
                    j = self.app.asr.ja
                    h["asr"] = {"refine": str(self.app.cfg.get("asr_refine", "auto")),
                                "ja": (j.stat() if j is not None else {"device": "-"})}
                except Exception:                     # noqa: BLE001
                    pass
                return self._json(h)
            if path == "/api/env":
                return self._json(self.app.api_env({}))
            if path == "/api/voice_clone":
                return self._json(self.app.api_voice_job({}))
            if path == "/api/events":
                return self._sse()
            if path.startswith("/api/ocr/crop") and self.app.last_crop:
                fp = self.app.last_crop
                if os.path.exists(fp):
                    with open(fp, "rb") as fh:
                        return self._raw(fh.read(), "image/png")
            if path in ("/", "/index.html"):
                return self._file("index.html")
            if path.startswith("/static/"):
                return self._file(path[len("/static/"):])
            if path.startswith("/shot/"):                 # 浮窗自截图
                fp = os.path.join(OUT, path[len("/shot/"):])
                if os.path.exists(fp):
                    with open(fp, "rb") as fh:
                        return self._raw(fh.read(), "image/png")
            return self._json({"ok": False, "err": "not found"}, 404)
        except Exception as e:                        # noqa: BLE001
            util.log(f"GET {path} 出错：{e!r}", "err", "srv")
            try:
                return self._json({"ok": False, "err": repr(e)}, 500)
            except Exception:
                pass

    def _file(self, name):
        name = os.path.normpath(name).replace("\\", "/").lstrip("/")
        fp = os.path.join(WEB, name)
        if not os.path.abspath(fp).startswith(os.path.abspath(WEB)) or not os.path.exists(fp):
            return self._json({"ok": False, "err": "no file"}, 404)
        ext = fp.rsplit(".", 1)[-1].lower()
        with open(fp, "rb") as fh:
            data = fh.read()
        return self._raw(data, _CT.get(ext) or mimetypes.guess_type(fp)[0] or "text/plain")

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        try:
            if path == "/api/cfg":
                return self._json(self.app.api_cfg(body))
            if path == "/api/env":
                return self._json(self.app.api_env(body))
            if path == "/api/tts_test":
                return self._json(self.app.api_tts_test(body))
            if path == "/api/voice_sample":
                return self._json(self.app.api_voice_sample(body))
            if path == "/api/voice_clone":
                return self._json(self.app.api_voice_clone(body))
            if path == "/api/voice_use":
                return self._json(self.app.api_voice_use(body))
            if path == "/api/voice_del":
                return self._json(self.app.api_voice_del(body))
            if path == "/api/dk_preview":
                return self._json(self.app.api_dk_preview(body))
            if path == "/api/inject":
                return self._json(self.app.api_inject(body))
            if path == "/api/mic":
                return self._json(self.app.api_mic(body))
            if path == "/api/chains":
                return self._json(self.app.api_chains(body))
            if path == "/api/ocr/region":
                return self._json(self.app.api_ocr_region(body))
            if path == "/api/ocr/test":
                return self._json(self.app.api_ocr_test(body))
            if path == "/api/ocr/once":
                return self._json(self.app.api_ocr_once(body))
            if path == "/api/region/pick":
                return self._json(self.app.api_region_pick(body))
            if path == "/api/clear":
                self.app.bus.clear()
                return self._json({"ok": True})
            if path == "/api/hud":
                return self._json(self.app.api_hud(body))
            if path == "/api/quit":
                threading.Thread(target=self.app.quit, daemon=True).start()
                return self._json({"ok": True})
            return self._json({"ok": False, "err": "not found"}, 404)
        except Exception as e:                        # noqa: BLE001
            util.log(f"POST {path} 出错：{e!r}", "err", "srv")
            return self._json({"ok": False, "err": repr(e)}, 500)

    # ---------------------------------------------------------------- SSE
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sid, q = self.app.bus.sub()
        last_hb = 0.0
        last_lv = 0.0
        try:
            hello = {"k": "hello", "ts": time.time(), "pid": os.getpid()}
            self._ev(hello)
            while not self.app.stopping:
                try:
                    ev = q.get(timeout=0.1)
                except queue.Empty:
                    ev = None
                now = time.time()
                if ev is not None:
                    self._ev(ev)
                if now - last_lv >= 0.1:
                    last_lv = now
                    lv = self.app.bus.levels
                    self._ev({"k": "level", "mic": lv.get("A", -90.0), "loop": lv.get("B", -90.0),
                              "gate": float(self.app.cfg.get("mic_db", -32))})
                if now - last_hb >= 15.0:
                    last_hb = now
                    self._ev({"k": "hb", "ts": time.time()})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.bus.unsub(sid)
            try:
                self.close_connection = True
            except Exception:
                pass

    def _ev(self, ev):
        data = ("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys
        et = sys.exc_info()[0]
        if et and issubclass(et, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Server:
    def __init__(self, app, port=PORT, log=util.log):
        self.app = app
        self.log = log
        self.port = port
        self.httpd = None
        self.thr = None

    def start(self):
        H.app = self.app
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer.daemon_threads = True
        for port in range(self.port, self.port + 12):
            try:
                self.httpd = _Httpd(("127.0.0.1", port), H)
                self.port = port
                break
            except OSError as e:
                self.log(f"端口 {port} 占用（{e}），换下一个", "warn", "srv")
        if self.httpd is None:
            raise RuntimeError("8899 起不来：端口全被占")
        self.thr = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                    name="http", daemon=True)
        self.thr.start()
        self.log(f"控制台 http://127.0.0.1:{self.port}/", "info", "srv")
        return self.port

    def stop(self):
        try:
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception:
            pass