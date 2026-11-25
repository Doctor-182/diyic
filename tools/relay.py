# -*- coding: utf-8 -*-
r"""子密钥网关（relay）：把套餐主密钥留在自己这边，只把「子密钥」发给别人。

为什么需要它
------------
百炼的 API Key 是「账号 + 地域」维度的一把凭证（官方文档：最多 50 把，可归属 RAM
用户、可设 IP/模型白名单）。如果套餐档位只允许一把 key，又要给别人用，就不能把主
密钥直接交出去 —— 那等于把账号余额和全部模型一起交出去。

这个网关的做法是：别人的程序（VRDub 或任何 OpenAI 兼容客户端）连你，用你发的子密
钥认证；网关在校验通过后，把你机器上的主密钥填进请求头，转发给真正的百炼端点，再
把响应（含 SSE 流）原样吐回去。子密钥可以单独吊销、限每日请求数，主密钥永不出本机。

用法
----
    python tools\relay.py --init                # 生成配置模板（含一把随机子密钥）
    python tools\relay.py                       # 按默认配置起网关
    python tools\relay.py --config X.json       # 指定配置
    python tools\relay.py --print-keys          # 打印配置里的子密钥（只在本地终端显示）

对方那边怎么填
--------------
    DASHSCOPE_TOKENPLAN_BASE=http://<你的地址>:8787
    DASHSCOPE_TOKENPLAN_KEY=<子密钥>

VRDub 控制台「密钥」卡里把「端点」填 http://<你的地址>:8787、「密钥」填子密钥即可，
保存时会自动探活（一次 max_tokens=1 的 chat 请求）。

安全边界（务必读）
------------------
* 默认只绑 127.0.0.1。要给外网用：配置里把 listen 改成 "0.0.0.0:8787"，并且**必须**
  在前面放 HTTPS（Caddy / nginx / frp+tls），否则子密钥和语音内容在公网上是明文。
* 网关不做 TLS，也不做用户体系；它只做「子密钥 -> 主密钥」的替换 + 配额 + 记账。
* 配置文件里有子密钥、环境里有主密钥，两者都别提交到 git（.gitignore 已排除用户目录）。
"""
from __future__ import annotations

import argparse
import atexit
import hmac
import http.client
import json
import os
import secrets
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:                      # 有项目就能找到用户目录；单独拷走也能跑
    from vrdub import paths as _paths
    _CONFIG_DIR = _paths.CONFIG_DIR
    _ENV_FILE = _paths.ENV_FILE
except Exception:         # noqa: BLE001
    _CONFIG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "VRDub", "config")
    _ENV_FILE = os.path.join(_CONFIG_DIR, ".env.local")

DEFAULT_CFG = os.path.join(_CONFIG_DIR, "relay.json")
DEFAULT_STATS = os.path.join(_CONFIG_DIR, "relay_stats.json")
HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}
DEFAULT_UPSTREAM = "https://token-plan.cn-beijing.maas.aliyuncs.com"


# ----------------------------------------------------------------- 小工具
def _read_env_file(path):
    env = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _split_host(spec):
    s = str(spec or "").strip()
    scheme = "https"
    if s.lower().startswith("http://"):
        scheme, s = "http", s[7:]
    elif s.lower().startswith("https://"):
        scheme, s = "https", s[8:]
    host, _, port = s.split("/")[0].partition(":")
    if port.isdigit():
        return scheme, host, int(port)
    return scheme, host, 443 if scheme == "https" else 80


def _norm_prefixes(raw):
    out = []
    for p in raw or ["/"]:
        p = str(p or "/").strip()
        if not p:
            continue
        out.append(p if p.startswith("/") else "/" + p)
    return out or ["/"]


def _template():
    sub = "sub-" + secrets.token_urlsafe(18)
    return {
        "listen": "127.0.0.1:8787",
        "upstream": DEFAULT_UPSTREAM,
        "master_key_env": "DASHSCOPE_TOKENPLAN_KEY",
        "admin_key": "adm-" + secrets.token_urlsafe(18),
        "keys": [
            {
                "name": "friend",
                "key": sub,
                "daily": 2000,
                "note": "给朋友的子密钥；daily 是每天最多转发多少请求，用完当天 429",
            }
        ],
        "allow": ["/api/", "/compatible-mode/"],
        "max_body_mb": 32,
        "timeout": 180.0,
        "log": os.path.join(_CONFIG_DIR, "relay.log"),
    }


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:      # noqa: BLE001
        return default


# ----------------------------------------------------------------- 记忆体
class Book:
    """每日请求数记账 + 落盘（2 秒节流，退出时补一次）。"""

    def __init__(self, path):
        self.path = path
        self.lk = threading.Lock()
        self.day = time.strftime("%Y-%m-%d")
        self.used = {}
        disk = _load_json(path, {})
        if isinstance(disk, dict) and disk.get("day") == self.day:
            used = disk.get("used")
            if isinstance(used, dict):
                self.used = {str(k): int(v) for k, v in used.items() if str(v).isdigit()}
        self._dirty = False
        self._stop = False
        atexit.register(self.save)
        threading.Thread(target=self._loop, name="relay-book", daemon=True).start()

    def _roll(self):
        day = time.strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.used, self._dirty = day, {}, True

    def take(self, name, limit=0):
        """占一个名额。返回 (ok, used, limit)。limit<=0 表示不限。"""
        with self.lk:
            self._roll()
            used = int(self.used.get(name, 0))
            if limit > 0 and used >= limit:
                return False, used, limit
            self.used[name] = used + 1
            self._dirty = True
            return True, used + 1, limit

    def peek(self, name):
        with self.lk:
            self._roll()
            return int(self.used.get(name, 0))

    def save(self):
        with self.lk:
            if not self._dirty:
                return
            data = {"day": self.day, "used": dict(self.used)}
            self._dirty = False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _loop(self):
        while not self._stop:
            time.sleep(2.0)
            self.save()


class Gateway:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.listen = str(cfg.get("listen") or "127.0.0.1:8787")
        self.up_spec = str(cfg.get("upstream") or DEFAULT_UPSTREAM)
        self.up = _split_host(self.up_spec)
        self.ctx = ssl.create_default_context()
        env_name = str(cfg.get("master_key_env") or "DASHSCOPE_TOKENPLAN_KEY")
        env = _read_env_file(_ENV_FILE)
        self.master = (os.environ.get(env_name) or env.get(env_name) or "").strip()
        self.admin = str(cfg.get("admin_key") or "").strip()
        self.keys = {}
        for it in cfg.get("keys") or []:
            if not isinstance(it, dict):
                continue
            k = str(it.get("key") or "").strip()
            if k:
                self.keys[k] = {"name": str(it.get("name") or "key"), "daily": int(it.get("daily") or 0)}
        self.allow = _norm_prefixes(cfg.get("allow"))
        self.max_body = int(float(cfg.get("max_body_mb") or 32) * 1024 * 1024)
        self.timeout = float(cfg.get("timeout") or 180.0)
        log_path = str(cfg.get("log") or "")
        self._log_fh = None
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                self._log_fh = open(log_path, "a", encoding="utf-8", buffering=1)
            except OSError:
                self._log_fh = None
        self.book = Book(str(cfg.get("stats") or DEFAULT_STATS))
        self.stat = {"req": 0, "deny": 0, "err": 0}

    # -------------------------------------------------------------- 记账
    def write(self, line):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"{stamp} {line}"
        try:
            self.log(text)
        except Exception:  # noqa: BLE001
            pass
        fh = self._log_fh
        if fh:
            try:
                fh.write(text + "\n")
            except Exception:  # noqa: BLE001
                pass

    def allowed(self, path):
        p = path or "/"
        return any(p.startswith(pre) for pre in self.allow)

    def find(self, token):
        if not token:
            return None
        for k, meta in self.keys.items():
            if hmac.compare_digest(k, token):
                return meta
        return None

    # -------------------------------------------------------------- 转发
    def forward(self, method, path, headers, body, timeout=None):
        scheme, host, port = self.up
        to = float(timeout or self.timeout)
        if scheme == "http":
            conn = http.client.HTTPConnection(host, port, timeout=to)
        else:
            conn = http.client.HTTPSConnection(host, port, timeout=to, context=self.ctx)
        hdrs = {}
        for k, v in (headers or {}).items():
            lk = k.lower()
            if lk in HOP or lk in ("authorization", "x-api-key", "host"):
                continue
            hdrs[k] = v
        hdrs["Authorization"] = "Bearer " + self.master
        hdrs["Host"] = host if port in (80, 443) else "%s:%d" % (host, port)
        conn.connect()
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body) if body else 0))
        conn.endheaders()
        if body:
            conn.send(body)
        return conn, conn.getresponse()


# ----------------------------------------------------------------- 服务
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vrdub-relay/1.0"
    gateway: Gateway = None       # 由 serve() 注入

    # ---- 基础
    def log_message(self, fmt, *args):   # 默认 stderr 太吵，走自己的日志
        return

    def _token(self):
        raw = self.headers.get("Authorization") or ""
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def _send_json(self, code, obj, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def _body(self):
        gw = self.gateway
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            total = 0
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                try:
                    n = int(line.split(b";")[0], 16)
                except ValueError:
                    raise ValueError("chunked 长度解析失败")
                if n == 0:
                    while True:
                        tail = self.rfile.readline(65536)
                        if tail in (b"\r\n", b"\n", b""):
                            break
                    break
                total += n
                if total > gw.max_body:
                    raise MemoryError("请求体超过 max_body_mb")
                chunks.append(self.rfile.read(n))
                self.rfile.read(2)
            return b"".join(chunks)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > gw.max_body:
            raise MemoryError("请求体超过 max_body_mb")
        return self.rfile.read(n) if n > 0 else b""

    # ---- 入口
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self, method):
        gw = self.gateway
        t0 = time.time()
        url = urlsplit(self.path)
        path = url.path or "/"
        gw.stat["req"] += 1
        # ---- 自己的接口
        if path.startswith("/__relay/"):
            return self._admin(method, path)
        # ---- 认证
        meta = gw.find(self._token())
        if not meta:
            gw.stat["deny"] += 1
            gw.write(f"deny 401 {method} {path[:80]}（没有子密钥或不对）")
            return self._send_json(401, {"error": {"code": "Unauthorized",
                                                   "message": "需要子密钥：Authorization: Bearer <子密钥>"}})
        if not gw.master:
            gw.stat["err"] += 1
            return self._send_json(500, {"error": {"code": "NoMasterKey",
                                                  "message": "网关这边没有主密钥，检查环境变量或 .env.local"}})
        if not gw.allowed(path):
            gw.stat["deny"] += 1
            gw.write(f"deny 403 {method} {path[:80]}（allow 白名单外）")
            return self._send_json(403, {"error": {"code": "PathNotAllowed",
                                                  "message": f"这个路径不在白名单里：{path}"}})
        # ---- 配额
        ok, used, limit = gw.book.take(meta["name"], meta["daily"])
        if not ok:
            gw.stat["deny"] += 1
            gw.write(f"deny 429 {method} {path[:80]} {meta['name']} {used}/{limit}")
            return self._send_json(429, {"error": {"code": "QuotaExceeded",
                                                  "message": f"{meta['name']} 今天已用满 {limit} 次"}},
                                   {"Retry-After": "1800"})
        # ---- 读体 + 转发
        try:
            body = self._body()
        except MemoryError:
            return self._send_json(413, {"error": {"code": "BodyTooLarge",
                                                  "message": f"请求体超过 {gw.max_body // (1024*1024)} MB"}})
        except ValueError as e:
            return self._send_json(400, {"error": {"code": "BadRequest", "message": str(e)}})
        try:
            conn, resp = gw.forward(method, self.path, self.headers, body)
        except Exception as e:      # noqa: BLE001
            gw.stat["err"] += 1
            gw.write(f"err 502 {method} {path[:80]} {type(e).__name__}: {e}")
            return self._send_json(502, {"error": {"code": "UpstreamError",
                                                  "message": f"{type(e).__name__}: {e}"}})
        self._relay_response(conn, resp, method, path, meta["name"], t0)

    # ---- 把上游响应原样吐回（含 SSE）
    def _relay_response(self, conn, resp, method, path, name, t0):
        gw = self.gateway
        try:
            self.send_response(resp.status)
            out = []
            for k, v in resp.getheaders():
                if k.lower() in HOP:
                    continue
                out.append((k, v))
            for k, v in out:
                self.send_header(k, v)
            length = resp.getheader("Content-Length")
            if length and length.isdigit():
                self.send_header("Content-Length", length)
                self.end_headers()
                sent = 0
                while True:
                    b = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not b:
                        break
                    self.wfile.write(b)
                    sent += len(b)
                    try:
                        self.wfile.flush()
                    except OSError:
                        break
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                sent = 0
                while True:
                    b = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not b:
                        break
                    self.wfile.write(b"%X\r\n" % len(b) + b + b"\r\n")
                    sent += len(b)
                    try:
                        self.wfile.flush()
                    except OSError:
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
            gw.write(f"ok {resp.status} {method} {path[:80]} {name} {sent}B {(time.time()-t0)*1000:.0f}ms")
        except Exception as e:      # noqa: BLE001
            gw.stat["err"] += 1
            gw.write(f"err stream {method} {path[:80]} {name} {type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except Exception:       # noqa: BLE001
                pass

    # ---- 运维接口
    def _admin(self, method, path):
        gw = self.gateway
        if path == "/__relay/health":
            return self._send_json(200, {"ok": True, "ver": self.server_version,
                                         "day": gw.book.day, "keys": len(gw.keys),
                                         "master": bool(gw.master), "upstream": gw.up_spec})
        token = self._token()
        is_admin = bool(gw.admin) and hmac.compare_digest(token, gw.admin)
        meta = gw.find(token)
        if not is_admin and not meta:
            gw.stat["deny"] += 1
            return self._send_json(401, {"error": {"code": "Unauthorized",
                                                   "message": "要用 admin_key 或子密钥"}})
        rows = []
        for k, m in gw.keys.items():
            if not is_admin and m["name"] != meta["name"]:
                continue
            rows.append({"name": m["name"], "daily": m["daily"], "used": gw.book.peek(m["name"]),
                         "key_tail": k[-4:]})
        return self._send_json(200, {"ok": True, "day": gw.book.day, "upstream": gw.up_spec,
                                     "stat": dict(gw.stat), "keys": rows})


def serve(cfg, ready=None, log=print):
    """起网关。ready(host, port) 在监听成功后回调（测试用）。"""
    gw = Gateway(cfg, log=log)
    host, _, port = gw.listen.partition(":")
    Handler.gateway = gw
    httpd = ThreadingHTTPServer((host.strip() or "127.0.0.1", int(port or 8787)), Handler)
    httpd.daemon_threads = True
    serve.last = gw          # 测试钩子：跑完手动 book.save() / 读计数
    if ready:
        ready(httpd.server_address[0], httpd.server_address[1])
    gw.write(f"网关已起 {gw.listen} -> {gw.up_spec}（子密钥 {len(gw.keys)} 把，主密钥{'已就绪' if gw.master else '缺失'}）")
    if not gw.master:
        gw.write("警告：没有主密钥，所有转发都会 500。请在环境变量或 .env.local 里配 " +
                 str(cfg.get("master_key_env") or "DASHSCOPE_TOKENPLAN_KEY"))
    try:
        httpd.serve_forever(poll_interval=0.3)
    finally:
        httpd.server_close()
        gw.book.save()
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="VRDub 子密钥网关")
    ap.add_argument("--config", default=DEFAULT_CFG)
    ap.add_argument("--init", action="store_true", help="写一份配置模板（不覆盖已有文件）")
    ap.add_argument("--print-keys", action="store_true", help="打印配置里的子密钥")
    a = ap.parse_args(argv)

    if a.init:
        if os.path.exists(a.config):
            print("配置已存在，没动它：" + a.config)
        else:
            os.makedirs(os.path.dirname(a.config), exist_ok=True)
            with open(a.config, "w", encoding="utf-8") as fh:
                json.dump(_template(), fh, ensure_ascii=False, indent=2)
            print("已生成：" + a.config)
        cfg = _load_json(a.config, {}) or {}
        for it in cfg.get("keys") or []:
            print(f"  子密钥 {it.get('name')}: {it.get('key')}")
        print(f"  admin : {cfg.get('admin_key')}")
        print("把它交给对方时只给子密钥；主密钥留在 .env.local 里。")
        return 0

    cfg = _load_json(a.config, None)
    if cfg is None:
        print("没有配置文件：" + a.config + "（先跑 --init）")
        return 2
    if a.print_keys:
        for it in cfg.get("keys") or []:
            print(f"{it.get('name')}\t{it.get('key')}\tdaily={it.get('daily')}")
        return 0
    try:
        serve(cfg)
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
