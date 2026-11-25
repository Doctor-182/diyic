# -*- coding: utf-8 -*-
r"""子密钥网关真值表：认证 / 白名单 / 配额 / 原样转发 / SSE 不被攒包 / 运维接口。

跑法（项目根）：
    $env:PYTHONPATH="D:\vrc无言势;D:\vrc无言势\.pylibs"; $env:PYTHONIOENCODING="utf-8"
    python -m tests.test_relay
"""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tools import relay as relaymod

UP = []          # 上游收到的请求，测试断言用


class UpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_GET(self):
        body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        UP.append({"path": self.path, "auth": self.headers.get("Authorization"),
                   "len": len(raw), "body": raw[:200]})
        if self.path.startswith("/api/v1/services/audio/tts"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                data = ("data: {\"i\": %d}\n\n" % i).encode()
                self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
                time.sleep(0.15)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        if self.path.startswith("/compatible-mode/v1/chat/completions"):
            out = {"ok": True, "path": self.path, "auth": self.headers.get("Authorization"),
                   "got": len(raw)}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"echo": true}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


def _req(port, method, path, body=None, key=None, timeout=10.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return r.status, raw, dict(r.getheaders())


def main():
    tmp = tempfile.mkdtemp(prefix="vrdub_relay_")
    up = _serve(UpHandler)
    up_host = "http://127.0.0.1:%d" % up.server_address[1]
    os.environ["TEST_MASTER"] = "MASTER-FAKE-0001"     # 假的：别用 sk- 形状，免得泄密扫描报警
    cfg = {
        "listen": "127.0.0.1:0",
        "upstream": up_host,
        "master_key_env": "TEST_MASTER",
        "admin_key": "adm-test",
        "keys": [{"name": "friend", "key": "sub-good", "daily": 2},
                 {"name": "bulk", "key": "sub-bulk", "daily": 0}],
        "allow": ["/api/", "/compatible-mode/"],
        "max_body_mb": 0.001,   # ~1 KB，专门用来试 413
        "log": os.path.join(tmp, "relay.log"),
        "stats": os.path.join(tmp, "stats.json"),
    }
    box = {}
    ready = threading.Event()

    def on_ready(host, port):
        box["port"] = port
        ready.set()

    threading.Thread(target=relaymod.serve, args=(cfg,), kwargs={"ready": on_ready, "log": lambda s: None},
                     daemon=True).start()
    ready.wait(5)
    port = box["port"]

    fails = []

    def chk(name, cond, info=""):
        print(("  OK  " if cond else "  FAIL") + "  " + name + (("  <- " + str(info)) if not cond else ""))
        if not cond:
            fails.append(name)

    print("[1] 认证")
    st, raw, _ = _req(port, "POST", "/compatible-mode/v1/chat/completions", {"m": 1})
    chk("无子密钥 401", st == 401, st)
    st, raw, _ = _req(port, "POST", "/compatible-mode/v1/chat/completions", {"m": 1}, key="sub-bad")
    chk("错子密钥 401", st == 401, st)
    st, raw, _ = _req(port, "POST", "/v1/other", {"m": 1}, key="sub-good")
    chk("白名单外 403", st == 403, st)

    print("[2] 原样转发（换主密钥 + 保留路径与体）")
    UP.clear()
    st, raw, hdr = _req(port, "POST", "/compatible-mode/v1/chat/completions?x=1", {"hello": "world"}, key="sub-good")
    got = json.loads(raw or b"{}")
    chk("200", st == 200, st)
    chk("路径与查询原样", got.get("path") == "/compatible-mode/v1/chat/completions?x=1", got)
    chk("上游收到主密钥", got.get("auth") == "Bearer MASTER-FAKE-0001", got.get("auth"))
    chk("上游收到同样的体大小", got.get("got") == len(json.dumps({"hello": "world"})), got)
    chk("响应头带 JSON", "application/json" in (hdr.get("Content-Type") or ""), hdr.get("Content-Type"))

    print("[3] SSE 流式不被攒包")
    t0 = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/v1/services/audio/tts/SpeechSynthesizer", body=b"{}",
                 headers={"Authorization": "Bearer sub-good", "Content-Type": "application/json"})
    r = conn.getresponse()
    first_at = None
    chunks = []
    while True:
        b = r.read1(4096)
        if not b:
            break
        if first_at is None:
            first_at = time.time() - t0
        chunks.append(b)
    total = time.time() - t0
    conn.close()
    text = b"".join(chunks).decode("utf-8", "replace")
    chk("三个事件都到齐", text.count("data:") == 3, text[:120])
    chk("首块早于上游完成（<0.3s）", first_at is not None and first_at < 0.3, first_at)
    chk("整体耗时覆盖 3x0.15s", total >= 0.35, total)

    print("[4] 请求体上限（换一把不限量的子密钥）")
    st, raw, _ = _req(port, "POST", "/compatible-mode/v1/chat/completions", b"x" * 4000, key="sub-bulk")
    chk("超限 413", st == 413, st)

    print("[5] 配额（friend daily=2，已用 2）")
    st, raw, hdr = _req(port, "POST", "/compatible-mode/v1/chat/completions", {"m": 1}, key="sub-good")
    chk("第 3 次 429", st == 429, st)
    chk("带 Retry-After", "Retry-After" in hdr, list(hdr)[:6])

    print("[6] 运维接口")
    st, raw, _ = _req(port, "GET", "/__relay/health")
    h = json.loads(raw or b"{}")
    chk("health 不需要密钥", st == 200 and h.get("ok") is True, (st, h))
    chk("health 显示主密钥就绪", h.get("master") is True, h)
    st, raw, _ = _req(port, "GET", "/__relay/stats", key="adm-test")
    s = json.loads(raw or b"{}")
    chk("admin 看到两把", st == 200 and len(s.get("keys") or []) == 2, s)
    st, raw, _ = _req(port, "GET", "/__relay/stats")
    chk("stats 无密钥 401", st == 401, st)
    st, raw, _ = _req(port, "GET", "/__relay/stats", key="sub-good")
    chk("子密钥只能看自己", st == 200 and len(json.loads(raw).get("keys") or []) == 1, raw[:120])

    print("[7] 记账落盘")
    relaymod.serve.last.book.save()
    used = json.load(open(cfg["stats"], encoding="utf-8"))["used"]
    chk("friend 记 2 次（429 不计数）", used.get("friend") == 2, used)
    chk("bulk 记 1 次（413 在读体时已占名额）", used.get("bulk") == 1, used)

    up.shutdown()
    print("\n" + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
