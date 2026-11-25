# -*- coding: utf-8 -*-
"""多语种 OCR 兜底：用多模态模型（qwen3.6-flash）直接抄图里的字。

为什么需要它：本地识别只有两套 rec 权重 —— 中文（ch_PP-OCRv4，拉丁字母也能认）
和日文（japan_rec，假名+汉字）。韩文/俄文/泰文/阿拉伯文这些字符集压根不在词表里，
本地 rec 只会吐乱码。而 det（DB 文本检测）跟语种无关，框永远是对的 —— 所以：

    det 还是本地（~25-40 ms），只把「认字」这一步换成 VL（一次 chat 请求）。

同一句 20 词的英文行，本地 rec 会给 "Hey, sorry I'm late -- the metro…"（破折号变 --），
VL 给的是 "Hey, sorry I'm late — the metro…"；气泡里的说话人名字也能分开成行。
所以 VL 既是兜底，也是「精确模式」。
"""
from __future__ import annotations

import base64
import time

from . import util

PATH = "/compatible-mode/v1/chat/completions"

_SYS = (
    "你是 OCR 引擎。把图片里所有可见文字按从上到下、从左到右逐行抄下来。"
    "不要翻译，不要解释，不要总结，不要补全，不要加任何前后缀，不要输出 Markdown 代码块。"
    "文字是什么语言就照原样抄，标点、大小写、空格都保持原样。"
    "如果图里没有文字，只输出 <empty>。"
)


def _png_b64(img) -> str:
    """numpy BGR / PNG bytes / 文件路径 → data URL。"""
    if isinstance(img, (bytes, bytearray)):
        raw = bytes(img)
    elif isinstance(img, str):
        with open(img, "rb") as fh:
            raw = fh.read()
    else:
        import cv2

        ok, buf = cv2.imencode(".png", img)
        if not ok:
            raise ValueError("PNG 编码失败")
        raw = buf.tobytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def parse_lines(content: str):
    """把模型回的内容拆成行：去代码块、去空行、去 <empty>、去项目符号。"""
    s = (content or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    out = []
    for ln in s.splitlines():
        t = ln.strip()
        for bullet in ("- ", "* ", "• ", "· "):
            if t.startswith(bullet):
                t = t[len(bullet):].strip()
        if not t or t == "<empty>" or t.lower() == "<empty>":
            continue
        out.append(t)
    return out


class VlOcr:
    """一次 chat 请求抄一屏文字。线程安全（net 自己管连接池）。"""

    def __init__(self, cfg, net, log=util.log):
        self.cfg = cfg
        self.net = net
        self.log = log
        self._errs = 0

    def available(self) -> bool:
        if self.net is None:
            return False
        if not bool(self.cfg.get("ocr_vl", True)):
            return False
        return bool(getattr(self.cfg, "key", "") or self.cfg.get("key"))

    def _model(self) -> str:
        return str(self.cfg.get("ocr_vl_model") or self.cfg.get("mt_model") or "qwen3.6-flash")

    def read(self, img, expect: int = 0, hint: str = "", timeout: float = 12.0) -> dict:
        """返回 {"lines": [str], "ms": float, "err": str, "model": str}。绝不抛异常。"""
        t0 = time.time()
        if not self.available():
            return {"lines": [], "ms": 0.0, "err": "vl_off", "model": ""}
        try:
            url = _png_b64(img)
        except Exception as exc:                       # noqa: BLE001
            return {"lines": [], "ms": 0.0, "err": "encode: %r" % (exc,), "model": ""}
        ask = "逐行抄写图片里的文字。"
        if expect > 1:
            ask += "图里大约有 %d 行文字，请输出 %d 行以内、每行一条。" % (expect, expect)
        if hint:
            ask += "语言可能是%s。" % (hint,)
        body = {
            "model": self._model(),
            "messages": [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": ask},
                ]},
            ],
            "enable_thinking": False,
            "temperature": 0.0,
        }
        try:
            j = self.net.post_json(PATH, body, timeout=timeout)
        except Exception as exc:                       # noqa: BLE001
            self._errs += 1
            if self._errs <= 3:
                self.log("[ocr] VL 抄字失败（第 %d 次）：%r" % (self._errs, exc), "warn", "ocr")
            return {"lines": [], "ms": (time.time() - t0) * 1000.0,
                    "err": repr(exc), "model": self._model()}
        content = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        lines = parse_lines(content)
        ms = (time.time() - t0) * 1000.0
        return {"lines": lines, "ms": ms, "err": "", "model": self._model(),
                "usage": j.get("usage") or {}}
