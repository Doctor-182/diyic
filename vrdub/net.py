# -*- coding: utf-8 -*-
"""HTTP 客户端：**每路请求一条独立连接**，绝不在锁里串行。

上一代把「整个请求-响应」放在一把锁里，池里只有一条 HTTPSConnection，
识别一慢就把同池的翻译堵死 —— 26 秒的来源。这里改成：
  - 一个 Net 持有 N 条空闲长连接（LIFO 复用，省掉 150-300ms 握手）
  - 借出即独占，用完归还；出错直接丢弃，不污染下一次
  - 空闲超过 IDLE_DROP 秒的连接在借出时换新（中间设备会静默掐死长连接）
  - 流式接口单独占一条连接，读到 EOF 才归还
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time

from . import util

IDLE_DROP = 30.0


def _parse_host(spec: str):
    """'host' / 'https://host:8443' / 'http://127.0.0.1:8787' -> (scheme, host, port)。

    裸域名一律按 https:443 处理（老行为）；自建子密钥网关可以填 http://ip:port，
    局域网内省掉证书这一层。
    """
    s = str(spec or "").strip()
    scheme = "https"
    low = s.lower()
    if low.startswith("http://"):
        scheme, s = "http", s[7:]
    elif low.startswith("https://"):
        scheme, s = "https", s[8:]
    host, _, port = s.split("/")[0].partition(":")
    host = host.strip()
    if port.isdigit():
        return scheme, host, int(port)
    return scheme, host, 443 if scheme == "https" else 80


class NetError(RuntimeError):
    pass


class _Conn:
    __slots__ = ("c", "last")

    def __init__(self, c):
        self.c = c
        self.last = time.time()


class Net:
    def __init__(self, name: str, host: str, key: str, slots: int = 6):
        self.name = name
        self.host = host
        self.key = key
        self.slots = slots
        self._idle: list[_Conn] = []
        self._lk = threading.Lock()
        self._ctx = ssl.create_default_context()
        self.stat = {"calls": 0, "errors": 0, "conn": 0}
        self._target = _parse_host(host)

    # ---------------------------------------------------------------- 连接
    def _new(self, timeout):
        scheme, host, port = self._target
        if scheme == "http":
            c = http.client.HTTPConnection(host, port, timeout=timeout)
        else:
            c = http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ctx)
        c.connect()
        with self._lk:
            self.stat["conn"] += 1
        return _Conn(c)

    def _take(self, timeout) -> _Conn:
        with self._lk:
            while self._idle:
                it = self._idle.pop()
                if time.time() - it.last < IDLE_DROP:
                    try:
                        if it.c.sock is None:
                            raise OSError("closed")
                        return it
                    except Exception:
                        try:
                            it.c.close()
                        except Exception:
                            pass
                        continue
                try:
                    it.c.close()
                except Exception:
                    pass
        return self._new(timeout)

    def _give(self, it: _Conn):
        it.last = time.time()
        with self._lk:
            if len(self._idle) < self.slots:
                self._idle.append(it)
                return
        try:
            it.c.close()
        except Exception:
            pass

    def _drop(self, it: _Conn):
        try:
            it.c.close()
        except Exception:
            pass

    def reset(self, host=None, key=None):
        """换端点/换密钥后把空闲连接全丢掉（旧连接还连着老 host，不能复用）。"""
        if host:
            self.host = host
            self._target = _parse_host(host)
        if key is not None:
            self.key = key
        with self._lk:
            idle, self._idle = self._idle, []
        for it in idle:
            self._drop(it)

    def _headers(self, extra=None):
        h = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        if extra:
            h.update(extra)
        return h

    # ---------------------------------------------------------------- 请求
    def post(self, path: str, body: dict | bytes, timeout: float = 15.0,
             extra=None, retries: int = 2):
        """同步 POST，返回 (status, bytes)。只有「连接层失败」才重试。"""
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body, ensure_ascii=True).encode()
        hdrs = self._headers(extra)
        err = None
        for attempt in range(retries + 1):
            it = None
            try:
                it = self._take(timeout)
                it.c.timeout = timeout
                try:
                    it.c.sock.settimeout(timeout)
                except Exception:
                    pass
                it.c.request("POST", path, body=data, headers=hdrs)
                r = it.c.getresponse()
                raw = r.read()
                self.stat["calls"] += 1
                if r.status >= 500:
                    self._drop(it)
                    if attempt < retries:
                        time.sleep(0.15 * (attempt + 1))
                        continue
                    return r.status, raw
                self._give(it)
                return r.status, raw
            except Exception as e:              # noqa: BLE001
                err = e
                if it is not None:
                    self._drop(it)
                self.stat["errors"] += 1
                if attempt < retries:
                    time.sleep(0.12 * (2 ** attempt))
                    continue
        raise NetError(f"{self.name} POST {path} 失败：{type(err).__name__}: {err}")

    def post_json(self, path, body, timeout=15.0, extra=None, retries: int = 2):
        st, raw = self.post(path, body, timeout, extra, retries)
        if st >= 400:
            raise NetError(f"{self.name} HTTP {st}: {raw[:400].decode('utf-8', 'replace')}")
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            raise NetError(f"{self.name} 返回不是 JSON：{raw[:200]!r} ({e})")

    def stream(self, path: str, body: dict | bytes, timeout: float = 20.0, extra=None,
               first_retries: int = 1):
        """流式 POST：逐块 yield bytes。首块之前失败可重试，之后抛错。"""
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body, ensure_ascii=True).encode()
        hdrs = self._headers(extra)
        err = None
        for attempt in range(first_retries + 1):
            it = None
            started = False
            try:
                it = self._take(timeout)
                it.c.timeout = timeout
                try:
                    it.c.sock.settimeout(timeout)
                except Exception:
                    pass
                it.c.request("POST", path, body=data, headers=hdrs)
                r = it.c.getresponse()
                if r.status >= 400:
                    raw = r.read()
                    self._drop(it)
                    raise NetError(f"{self.name} HTTP {r.status}: {raw[:300].decode('utf-8', 'replace')}")
                self.stat["calls"] += 1
                buf = b""
                while True:
                    try:
                        ch = r.read1(4096)
                    except AttributeError:
                        ch = r.read(1)
                    if not ch:
                        break
                    started = True
                    buf += ch
                    # 交出去的是原始字节；调用方按行切
                    yield buf
                    buf = b""
                self._give(it)
                return
            except GeneratorExit:
                if it is not None:
                    self._drop(it)
                raise
            except Exception as e:              # noqa: BLE001
                err = e
                if it is not None:
                    self._drop(it)
                self.stat["errors"] += 1
                if started or attempt >= first_retries:
                    raise
                time.sleep(0.12)
        raise NetError(f"{self.name} 流式失败：{err}")


def iter_sse(chunks, key_fields=("delta",)):
    """把字节流解析成 SSE 的 data 行 JSON。yield dict。"""
    buf = b""
    for ch in chunks:
        buf += ch
        while True:
            k = buf.find(b"\n")
            if k < 0:
                break
            line, buf = buf[:k], buf[k + 1:]
            s = line.decode("utf-8", "replace").strip()
            if not s or not s.startswith("data:"):
                continue
            s = s[5:].strip()
            if s == "[DONE]":
                return
            if not s:
                continue
            try:
                yield json.loads(s)
            except Exception:
                continue


def fetch_url(url: str, timeout=60.0) -> bytes:
    """下 OSS 链接（TTS 非流式兜底用）。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "vrdub/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()
