# -*- coding: utf-8 -*-
"""vrdub C 链：对方在 VRChat 里打字（屏幕气泡）→ 我屏幕上看到中文。

对外只有 OcrChain 一个类（Lead 的 app.py 直接 import 它）：

    chain = OcrChain(cfg, on_text, on_clear, log)
    chain.start()      # 起后台轮询线程；ORT 会话在后台线程里懒加载，不卡启动
    chain.capture()    # 抓一帧存 out/ocr_shot.png，返回路径（给 /api/ocr/region 预览）
    chain.test()       # 同步跑一次，返回 {ok, ms, src, dst, boxes}
    chain.stop()
    chain.close()

回调契约（严格）：
    on_text(key: str, src: str, dst: str, box: tuple, ms: float)
        key  —— 屏幕位置的稳定 id，同一个气泡位置多帧同 key（chunks 变化时原地更新）
        src  —— 屏幕原文（多为日语）
        dst  —— 中文译文（MT 失败时回落为原文，绝不空）
        box  —— (x, y, w, h) 相对 region 左上角
        ms   —— 这一行的端到端耗时（抓帧 + OCR + MT）
    on_clear()
        气泡区域从「有字」变成「连续两帧没字」时调用一次（= 屏幕清了）。

设计要点（历史与实测见 _ocr_engine.py 顶部注释）：
  * 截图 ctypes GDI BitBlt，只拷 ROI（540x420 约 2~4ms）；PIL.ImageGrab 固定 50ms，不用。
  * 双模型路由：det 一次 → 主模型 rec 全量 → 低分/空串的行换另一个模型重跑取高分。
    假名只有 ja 读得出（ch 对「ちょっと」给 0.51 且假名全丢），汉字只有 ch 读得准
    （ja 把「不安与孤独」读成「不安与珂独」）。主模型按上一帧的主导语言自适应。
  * 帧差 hash（64x16 灰度，平均差 <2% 直接跳过）：静止画面 0ms，CPU 不烧。
  * 去重：同一指纹 60s 内不重复上报；离开画面再回来算新的。
  * 大区域分块：region 长边 >1100（未标定时默认「全屏下半部分」就有 2560x800）时切成
    ≤900 的块分别识别再合并，否则 det 会把 2560 宽压到 736，13px 的字直接消失。
  * 失败只 log，绝不抛到调用方（掉帧不能影响音频链）。
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import paths as _paths

import numpy as np

from ._ocr_capture import (
    ScreenGrabber,
    clamp_region,
    ensure_dpi_aware,
    screen_size,
    virtual_screen_rect,
)
from . import _ocr_engine as engmod
from ._ocr_engine import DualOcrEngine, fingerprint, norm_text

__all__ = ["OcrChain", "suggest_region", "DEFAULT_REGION_SIZE"]

# VRChat chatbox 默认在右上角：距右边缘 20px、距顶 120px
DEFAULT_REGION_SIZE = (540, 420)
DEFAULT_MARGIN = (20, 120)
DEFAULT_INTERVAL = 0.6  # cfg.ocr_interval 秒

_HASH_W, _HASH_H = 64, 16   # 帧差指纹尺寸（灰度）
_TILE = 900                 # 大区域分块边长
_TILE_OVERLAP = 24          # 块间重叠，避免把一行字切断
_TILE_TRIGGER = 1100        # 长边超过它就分块

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = _paths.OUT


def suggest_region(screen_w: Optional[int] = None, screen_h: Optional[int] = None) -> List[int]:
    """VRChat chatbox 区域建议值（屏幕物理像素）：右上角 [屏宽-560, 120, 540, 420]。"""
    if screen_w is None or screen_h is None:
        sw, sh = screen_size()
        screen_w = screen_w or sw
        screen_h = screen_h or sh
    w, h = DEFAULT_REGION_SIZE
    mx, my = DEFAULT_MARGIN
    w = int(min(w, max(160, screen_w - 2 * mx)))
    h = int(min(h, max(120, screen_h - my - 20)))
    return list(clamp_region(screen_w - w - mx, my, w, h))


def _imwrite(path: str, img: np.ndarray) -> bool:
    """cv2.imwrite 在含非 ASCII 的路径（D:\\vrc无言势）会静默失败 → 用 imencode + tofile。"""
    try:
        import cv2

        ok, buf = cv2.imencode(os.path.splitext(path)[1] or ".png", img)
        if not ok:
            return False
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        buf.tofile(path)
        return True
    except Exception:
        return False


class OcrChain:
    """屏幕文字 OCR + 翻译链。所有公开方法都不抛异常，内部一把 RLock。"""

    def __init__(self, cfg: Any = None, on_text: Optional[Callable] = None,
                 on_clear: Optional[Callable] = None, log: Optional[Callable] = None,
                 translator: Any = None, on_raw: Optional[Callable] = None,
                 net: Any = None):
        # 必须在任何 Qt/HWND 之前设 DPI 感知，否则 150% 缩放下 ROI 坐标错位
        self.dpi_mode = ensure_dpi_aware()
        self.cfg = cfg if cfg is not None else {}
        self.on_text = on_text
        self.on_clear = on_clear
        # 原文先浮出来的回调（OCR 一出来就调，翻译还没跑）：屏幕上先看到日文，
        # 大约 200-400ms 后中文替换掉它——观感比等译文才出现好很多。
        self.on_raw = on_raw
        self._log_fn = log
        self._lock = threading.RLock()
        self._engine: Optional[DualOcrEngine] = None
        self._engine_err = ""
        self._grabber = ScreenGrabber()
        self._mt = translator            # 注入的 MT（app 里那条，与 A/B 链共用连接池）
        self._mt_err = ""
        self._net = net                  # 多语种视觉抄字用同一条连接池
        self._vl = None

        self._region: Optional[Tuple[int, int, int, int]] = None
        self._region_key = ""            # 区域 id（进 on_text 的 key 前缀）
        self._prev_sig: Optional[np.ndarray] = None
        self._last_lines: List[dict] = []
        self._visible: Set[str] = set()
        self._seen_ts: Dict[str, float] = {}
        self._seen_order: List[str] = []
        self._empty_frames = 0
        self._had_lines = False
        self._zh_cache: Dict[str, str] = {}

        # cfg 里同时存在 ocr_fps / ocr_poll（Lead 的前端字段）→ ocr_interval 优先，
        # 其次是 ocr_poll（秒），都没有再用 0.6
        self.interval = self._cfg_float(("ocr_interval", "ocr_poll"), DEFAULT_INTERVAL)
        if self.interval <= 0:
            self.interval = DEFAULT_INTERVAL
        # 未标定区域（全屏下半部分）单帧实测 5.3s，照 0.6s 跑会把线程钉死 → 自动降频
        self.slow_interval = self._cfg_float(("ocr_slow_interval",), 2.0) or 2.0
        self._region_inferred = False
        self._slow_logged = False
        self._warned_region = False
        self.diff_thresh = self._cfg_float(("ocr_diff_thresh",), 0.02)
        self.cell_thresh = self._cfg_float(("ocr_cell_thresh",), 0.05)
        self.dedup_ttl = self._cfg_float(("ocr_dedup_ttl",), 60.0)
        self.crop_scale = self._cfg_float(("ocr_crop_scale",), 3.0)

        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._stats: Dict[str, Any] = {
            "frames": 0, "ocr_frames": 0, "skipped": 0, "errors": 0,
            "emitted": 0, "sum_ms": 0.0, "max_ms": 0.0, "last_ms": 0.0,
            "last_cap_ms": 0.0, "last_det_ms": 0.0, "last_rec_ms": 0.0,
            "last_ja_ms": 0.0, "last_boxes": 0, "mt_err": 0,
            "vl": 0, "last_vl_ms": 0.0, "pil": 0,
        }

    # ------------------------------------------------------------ 基础
    def _cfg_float(self, keys: Sequence[str], dflt: float) -> float:
        for k in keys:
            try:
                v = self.cfg.get(k) if hasattr(self.cfg, "get") else None
            except Exception:
                v = None
            if v is None or v == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return dflt

    def _log(self, msg: str):
        fn = self._log_fn
        if fn is None:
            return
        try:
            fn(msg)
        except Exception:
            pass

    # ------------------------------------------------------------ 区域
    def _resolve_region(self) -> Tuple[int, int, int, int]:
        try:
            raw = self.cfg.get("ocr_region") if hasattr(self.cfg, "get") else None
        except Exception:
            raw = None
        vals: List[int] = []
        try:
            for v in list(raw or [])[:4]:
                vals.append(int(float(v)))
        except (TypeError, ValueError):
            vals = []
        if len(vals) == 4 and vals[2] > 0 and vals[3] > 0:
            x, y, w, h = clamp_region(*vals)
            self._region_key = "r%d.%d.%d.%d" % (x, y, w, h)
            self._region_inferred = False
            return (x, y, w, h)
        # 未标定 → 全屏下半部分（Lead 口径）；也是唯一「永远有东西可抓」的兜底
        self._region_inferred = True
        sw, sh = screen_size()
        x, y, w, h = clamp_region(0, int(sh * 0.5), int(sw), int(sh - int(sh * 0.5)))
        self._region_key = "full%d.%d" % (w, h)
        self._log("[ocr] 未标定区域 → 全屏下半部分 %s（建议改成 chatbox：%s）"
                  % ([x, y, w, h], suggest_region(sw, sh)))
        return (x, y, w, h)

    def reset_dedupe(self):
        """清空「已播报」状态：重新标定区域 / 换场景后想让同一句话再出来时用。"""
        with self._lock:
            self._visible.clear()
            self._seen_ts.clear()
            self._seen_order.clear()
            self._last_lines = []
            self._prev_sig = None
            self._empty_frames = 0
            self._had_lines = False

    def set_region(self, x, y, w, h) -> List[int]:
        """换 ROI（屏幕物理像素）。区域变了就清帧差缓存，下一帧一定重跑。"""
        with self._lock:
            self._region = tuple(int(v) for v in clamp_region(x, y, w, h))
            self._region_key = "r%d.%d.%d.%d" % self._region
            self._region_inferred = False
            self._warned_region = False
            try:
                self._grabber.set_region(*self._region)
            except Exception as exc:
                self._bump_err()
                self._log("[ocr] set_region 失败 %s: %s" % (type(exc).__name__, exc))
            self._prev_sig = None
            self._last_lines = []
            self._visible = set()
            self._had_lines = False
            self._empty_frames = 0
            return list(self._region)

    @property
    def region(self) -> List[int]:
        with self._lock:
            self._ensure_region()
            return list(self._region or ())

    def suggest_region(self, screen_w: Optional[int] = None, screen_h: Optional[int] = None) -> List[int]:
        return suggest_region(screen_w, screen_h)

    def _ensure_region(self) -> Tuple[int, int, int, int]:
        if self._region is None:
            self._region = self._resolve_region()
            try:
                self._grabber.set_region(*self._region)
            except Exception as exc:
                self._bump_err()
                self._log("[ocr] 初始区域设置失败 %s: %s" % (type(exc).__name__, exc))
        return self._region

    def _bump_err(self, n: int = 1):
        self._stats["errors"] += n

    # ------------------------------------------------------------ 抓帧
    def _grab(self) -> Optional[np.ndarray]:
        """BGR (h,w,3) uint8 帧；失败 None（只 log 一次）。"""
        with self._lock:
            self._ensure_region()
            if self._region and tuple(self._grabber.region or ()) != tuple(self._region):
                try:
                    self._grabber.set_region(*self._region)
                except Exception:
                    pass
        try:
            frame = self._grabber.grab()
        except Exception as exc:
            self._bump_err()
            if self._stats["errors"] <= 3:
                self._log("[ocr] capture 异常 %s: %s" % (type(exc).__name__, exc))
            return None
        if frame is None:
            self._bump_err()
            err = getattr(self._grabber, "error", "") or ""
            if self._stats["errors"] <= 3:
                self._log("[ocr] capture 返回 None（无桌面会话 / ROI 越界）：ok=%s region=%s %s"
                          % (getattr(self._grabber, "ok", "?"),
                             getattr(self._grabber, "region", "?"), err))
            # DC 一旦失效就永远失效（换显示模式/驱动重置/DWM 重启之后常见）：
            # 这里直接换一个新的取帧器，并当场重试一帧，不用等下一次 F6、更不用重启后端。
            if "BitBlt" in err or not getattr(self._grabber, "ok", False):
                self._rebuild_grabber()
                try:
                    retry = self._grabber.grab()
                except Exception:                       # noqa: BLE001
                    retry = None
                if retry is not None:
                    self._log("[ocr] 取帧器重建后恢复（%dx%d）"
                              % (retry.shape[1], retry.shape[0]))
                    return retry
        return frame

    def _rebuild_grabber(self):
        """抓屏器自愈：关掉旧的、按当前 ROI 新建一个。"""
        old = self._grabber
        try:
            old.close()
        except Exception:                           # noqa: BLE001
            pass
        try:
            gb = ScreenGrabber()
            if self._region:
                gb.set_region(*self._region)
            self._grabber = gb
            self._log("[ocr] 取帧器已重建（%s）" % ("" if gb.ok else (gb.error or "仍不可用"),))
        except Exception as exc:                    # noqa: BLE001
            self._log("[ocr] 取帧器重建失败 %s: %s" % (type(exc).__name__, exc))

    def capture(self) -> str:
        """抓一帧存 out/ocr_shot.png，返回绝对路径；失败返回 ""。"""
        frame = self._grab()
        if frame is None:
            return ""
        path = os.path.join(_OUT, "ocr_shot.png")
        return path if _imwrite(path, frame) else ""

    # ------------------------------------------------------------ 模型
    def _ensure_engine(self) -> bool:
        """懒加载 + warmup（首次 ~1.2s）。失败只 log 一次，返回 False。"""
        if self._engine is not None and self._engine.loaded:
            return True
        with self._lock:
            if self._engine is not None and self._engine.loaded:
                return True
            if self._engine is None:
                self._engine = DualOcrEngine(self.cfg, log=self._log)
            try:
                ok = self._engine.load()
            except Exception as exc:
                self._engine_err = "%s: %s" % (type(exc).__name__, exc)
                self._log("[ocr] 模型加载异常 %s" % self._engine_err)
                return False
            if ok:
                self._engine_err = ""
                self._log("[ocr] 就绪 det=%dms warmup=%dms dpi=%s region=%s"
                          % (int(self._engine.load_ms), int(max(0.0, self._engine.warmup_ms)),
                             self.dpi_mode, list(self._region or ())))
            else:
                self._engine_err = str(getattr(self._engine, "load_error", "") or "load failed")
                self._log("[ocr] 模型不可用：%s" % self._engine_err)
            return ok

    @property
    def ready(self) -> bool:
        return bool(self._engine is not None and self._engine.loaded)

    # ------------------------------------------------------------ 帧差
    def _signature(self, frame: np.ndarray) -> np.ndarray:
        import cv2

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (_HASH_W, _HASH_H), interpolation=cv2.INTER_AREA).astype(np.float32)

    def _changed(self, frame: np.ndarray) -> bool:
        """与上一次真正跑过 OCR 的帧比，判「值不值得跑 OCR」。

        只比**全区域平均差**是不够的：1050x450 里换掉一行 13px 文字，平均差只有
        0.2%~0.5%，过不了 2% 的阈值 → 气泡改了字却完全不识别（实测踩过）。
        所以加第二条判据：64x16 的**单格最大差**。一格约 16x28 像素，一行文字改动
        会让 2~9 个格子的均值明显变化（实测单格差 ~0.2~0.4），而整体亮度/远处轻微
        抖动只会拉低平均差、抬不高单格差。任一条超阈值就重跑。
        """
        sig = self._signature(frame)
        prev = self._prev_sig
        self._prev_sig = sig
        if prev is None or prev.shape != sig.shape:
            return True
        delta = np.abs(sig - prev)
        mean_d = float(delta.mean()) / 255.0
        cell_d = float(delta.max()) / 255.0
        self._stats["last_diff"] = round(mean_d, 5)
        self._stats["last_cell"] = round(cell_d, 5)
        return mean_d >= self.diff_thresh or cell_d >= self.cell_thresh

    # ------------------------------------------------------------ 识别
    def _read_frame(self, frame: np.ndarray) -> List[dict]:
        """一帧 → 行列表（坐标相对 region）。本地识别 + 多语种视觉兜底。"""
        lines = self._read_local(frame)
        try:
            lines = self._maybe_vl(frame, lines)
        except Exception as exc:                    # noqa: BLE001
            self._bump_err()
            self._log("[ocr] VL 兜底异常 %s: %s" % (type(exc).__name__, exc))
        return lines

    # 本地 rec 只有中文+日文两套词表；这些字符集一个都不在里面
    _VL_ONLY_SRC = ("ko", "ru", "th", "ar", "hi", "el", "he", "other")
    _SRC_HINT = {"ko": "韩语", "ru": "俄语", "th": "泰语", "ar": "阿拉伯语",
                 "hi": "印地语", "el": "希腊语", "he": "希伯来语"}

    def _vl_ocr(self):
        if getattr(self, "_vl", None) is None and getattr(self, "_net", None) is not None:
            try:
                from . import vlocr as vlmod

                self._vl = vlmod.VlOcr(self.cfg, self._net, self._log)
            except Exception as exc:                # noqa: BLE001
                self._log("[ocr] VL 初始化失败 %s: %s" % (type(exc).__name__, exc))
                self._vl = False
        return self._vl or None

    def _vl_boxes(self, frame: np.ndarray) -> List[List[int]]:
        """det 一次拿框（DB 检测与语种无关，韩文俄文一样能框住）。"""
        try:
            got = self._engine.detect(frame) or []
        except Exception:                           # noqa: BLE001
            return []
        out = []
        for item in got:
            rect = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else item
            try:
                out.append([int(v) for v in list(rect)[:4]])
            except Exception:                       # noqa: BLE001
                continue
        out.sort(key=lambda b: (b[1], b[0]))
        return out

    @staticmethod
    def _looks_garbled(lines: List[dict]) -> bool:
        """本地识别结果「稀得不正常」= 词表里没有这个字符集，应该换视觉抄字。

        判据：字符数 / 预期字符数（按框宽 ÷ 0.8 倍字高估）< 0.35。
        实测同一张韩语气泡：本地给 'Aoi.'（4 字 / 预期 40+ → 0.1），
        正常的 13px 日文行给 0.7-1.1，中文行 0.8-1.3。
        """
        for ln in lines:
            box = ln.get("box") or [0, 0, 0, 0]
            w = float(box[2] or 0)
            h = max(6.0, float(box[3] or 0))
            text = ln.get("text") or ""
            if not text or w < 40:
                continue
            exp = max(1.0, w / (h * 0.8))
            if len(text) / exp < 0.35:
                return True
        return False

    @staticmethod
    def _coverage_gap(lines: List[dict], boxes: List[List[int]]) -> bool:
        """det 框住了宽正文，识别结果里却没有对应的一行 → 本地没认出来。"""
        for bx in boxes or []:
            if bx[2] < 100:
                continue
            cx = bx[0] + bx[2] / 2.0
            cy = bx[1] + bx[3] / 2.0
            hit = False
            for ln in lines:
                b = ln.get("box") or [0, 0, 0, 0]
                if b[0] - 8 <= cx <= b[0] + b[2] + 8 and b[1] - 8 <= cy <= b[1] + b[3] + 8:
                    hit = True
                    break
            if not hit:
                return True
        return False

    def _maybe_vl(self, frame: np.ndarray, lines: List[dict]) -> List[dict]:
        """本地认不出来 / 语种不在词表里 → 让多模态模型抄一遍（约 0.6-1.0 s）。"""
        vl = self._vl_ocr()
        if vl is None or not vl.available():
            return lines
        src = str(self.cfg.get("ocr_src", "auto") or "auto").lower()
        fallback = bool(self.cfg.get("ocr_vl_fallback", True))
        want = (src in self._VL_ONLY_SRC) or (not lines and fallback)
        boxes: List[List[int]] = []
        if want or (fallback and lines and (self._looks_garbled(lines) or True)):
            boxes = self._vl_boxes(frame)
        if not want and fallback and lines:
            # 两道「本地白念了」的判据：
            # ① 文字稀得不正常（换了字符集，词表里没有 → 只吐两三个字母）
            # ② det 框住了宽正文，但 rec 一个字都没给（韩文/泰文这种情况最常见）
            if self._looks_garbled(lines) or self._coverage_gap(lines, boxes):
                want = True
        if not want:
            return lines
        expect = max(len(boxes), len(lines))
        t0 = time.perf_counter()
        got = vl.read(frame, expect=expect, hint=self._SRC_HINT.get(src, ""))
        vl_ms = (time.perf_counter() - t0) * 1000.0
        texts = [t for t in (got.get("lines") or []) if t]
        if not texts:
            if got.get("err"):
                self._log("[ocr] VL 没给出文字（%s）" % (got.get("err"),))
            return lines
        out: List[dict] = []
        if lines and len(texts) == len(lines):
            for ln, txt in zip(sorted(lines, key=lambda l: (l["box"][1], l["box"][0])), texts):
                item = dict(ln)
                item["text"] = engmod.norm_text(txt)
                item["rec"] = "vl"
                item["lang"] = engmod.detect_lang(item["text"])
                item["score"] = max(float(ln.get("score") or 0.0), 0.9)
                out.append(item)
        elif boxes and len(texts) == len(boxes):
            for bx, txt in zip(boxes, texts):
                t = engmod.norm_text(txt)
                out.append({"text": t, "box": bx, "score": 0.9,
                            "lang": engmod.detect_lang(t), "rec": "vl"})
        else:
            if lines:
                bx = list(lines[0]["box"])
            elif boxes:
                x0 = min(b[0] for b in boxes)
                y0 = min(b[1] for b in boxes)
                x1 = max(b[0] + b[2] for b in boxes)
                y1 = max(b[1] + b[3] for b in boxes)
                bx = [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]
            else:
                hh, ww = frame.shape[:2]
                bx = [0, 0, ww, hh]
            t = engmod.norm_text(" ".join(texts))
            out.append({"text": t, "box": bx, "score": 0.9,
                        "lang": engmod.detect_lang(t), "rec": "vl"})
        if self.cfg.get("ocr_label_strip", False):
            out = self._strip_label_line(out)
        with self._lock:
            self._stats["vl"] = int(self._stats.get("vl", 0)) + 1
            self._stats["last_vl_ms"] = round(vl_ms, 2)
        self._log("[ocr] 视觉抄字 %d 行 · %.0fms · %s"
                  % (len(out), vl_ms, (out and out[0]["text"][:16]) or ""))
        return out

    @staticmethod
    def _strip_label_line(lines: List[dict]) -> List[dict]:
        """剔掉气泡左上角的「说话人名字」行（默认关闭，cfg.ocr_label_strip）。

        只做一件事：第一行短、窄、没有句读 → 当成名字丢掉。
        其余一律原样保留——屏幕上认到几个字就翻几个字，绝不猜哪行是噪声。
        宁可多一行，也不许吞掉用户真正想读的聊天内容。
        """
        if len(lines) < 2:
            return lines
        rows = sorted(lines, key=lambda l: (l["box"][1], l["box"][0]))
        txt = (rows[0].get("text") or "").strip()
        bw = float(rows[0]["box"][2] or 0)
        if (txt and len(txt) <= 24
                and not txt.endswith(("。", "！", "？", "!", "?", "…", "、", "，", ","))
                and bw <= 0.45 * float(rows[1]["box"][2] or 1)):
            return rows[1:]
        return rows

    def _read_local(self, frame: np.ndarray) -> List[dict]:
        """本地 ONNX 识别（det + rec）。大区域自动分块。"""
        if not self._ensure_engine():
            return []
        h, w = frame.shape[:2]
        if max(h, w) <= _TILE_TRIGGER:
            res = self._engine.run(frame, merge=True)
            self._note_engine(res)
            lines = self._filter_noise(list(res.get("lines") or []))
            return engmod.merge_same_row(lines)

        lines: List[dict] = []
        step = _TILE - _TILE_OVERLAP
        t_det = t_rec = t_ja = 0.0
        nbox = 0
        for ty in range(0, max(1, h), step):
            for tx in range(0, max(1, w), step):
                tile = frame[ty:ty + _TILE, tx:tx + _TILE]
                if tile.shape[0] < 24 or tile.shape[1] < 40:
                    continue
                try:
                    res = self._engine.run(tile, merge=True)
                except Exception as exc:
                    self._bump_err()
                    self._log("[ocr] 分块识别异常 %s: %s" % (type(exc).__name__, exc))
                    continue
                t_det += float(res.get("det_ms", 0.0))
                t_rec += float(res.get("rec_ms", 0.0))
                t_ja += float(res.get("ja_ms", 0.0))
                nbox += int(res.get("n_boxes", 0))
                for ln in res.get("lines") or []:
                    b = ln.get("box") or [0, 0, 0, 0]
                    ln = dict(ln)
                    ln["box"] = [int(b[0]) + tx, int(b[1]) + ty, int(b[2]), int(b[3])]
                    lines.append(ln)
        self._stats["last_det_ms"] = round(t_det, 2)
        self._stats["last_rec_ms"] = round(t_rec, 2)
        self._stats["last_ja_ms"] = round(t_ja, 2)
        self._stats["last_boxes"] = nbox
        self._stats["primary"] = getattr(self._engine, "last_primary", "")
        return engmod.merge_same_row(self._filter_noise(self._dedupe_tiles(lines)))

    @staticmethod
    def _filter_noise(lines: List[dict]) -> List[dict]:
        """丢掉 1~2 个字符且低分的行。

        实测（2560x1600 桌面当 region 时）：会捡到「で」0.651、「の允」0.505、「B」0.726
        这类图标/单字碎片 —— 气泡里没有这种东西。长句不受影响。
        """
        out: List[dict] = []
        for ln in lines:
            txt = (ln.get("text") or "").strip()
            score = float(ln.get("score") or 0.0)
            solid = sum(1 for ch in txt if ch.strip() and ch not in "。、，,．.！!？?…・「」『』()（）[]【】")
            if solid <= 2 and score < 0.75:
                continue
            out.append(ln)
        return out

    def _note_engine(self, res: dict):
        self._stats["last_det_ms"] = round(float(res.get("det_ms", 0.0)), 2)
        self._stats["last_rec_ms"] = round(float(res.get("rec_ms", 0.0)), 2)
        self._stats["last_ja_ms"] = round(float(res.get("ja_ms", 0.0)), 2)
        self._stats["last_boxes"] = int(res.get("n_boxes", 0))
        self._stats["primary"] = getattr(self._engine, "last_primary", "")

    @staticmethod
    def _dedupe_tiles(lines: List[dict]) -> List[dict]:
        """块间重叠区会读到同一行两次 → 同指纹且中心距离近的只留分高的。"""
        out: List[dict] = []
        for ln in sorted(lines, key=lambda l: -float(l.get("score", 0.0))):
            fp = fingerprint(ln.get("text", ""))
            b = ln.get("box") or [0, 0, 0, 0]
            cx, cy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
            dup = False
            for kept in out:
                if fingerprint(kept.get("text", "")) != fp:
                    continue
                kb = kept.get("box") or [0, 0, 0, 0]
                if abs(kb[0] + kb[2] / 2.0 - cx) <= 40 and abs(kb[1] + kb[3] / 2.0 - cy) <= 24:
                    dup = True
                    break
            if not dup:
                out.append(ln)
        return out

    # ------------------------------------------------------------ 翻译
    def _translator(self):
        """惰性建 MT（复用 vrdub.mt，不自己发 HTTP）。失败只 log 一次。"""
        if self._mt is not None or self._mt_err:
            return self._mt
        with self._lock:
            if self._mt is not None or self._mt_err:
                return self._mt
            try:
                from . import mt as mtmod
                from . import net as netmod

                g = self.cfg.get if hasattr(self.cfg, "get") else (lambda k, d=None: d)
                host = g("host", "") or getattr(self.cfg, "host", "") or ""
                key = g("key", "") or getattr(self.cfg, "key", "") or ""
                if not host or not key:
                    self._mt_err = "no host/key"
                    self._log("[ocr] 无 MT 配置（host/key 空）→ 只显示原文")
                    return None
                net = netmod.Net("ali", host, key, slots=2)
                self._mt = mtmod.Translator(self.cfg, net, self._log)
            except Exception as exc:
                self._mt_err = "%s: %s" % (type(exc).__name__, exc)
                self._log("[ocr] MT 不可用：%s" % self._mt_err)
                return None
        return self._mt

    def _translate_many(self, texts) -> dict:
        """一屏多行并行翻（串行会让最后一行晚 1 s 以上，视觉上像漏翻）。"""
        uniq = []
        for t in texts or []:
            t = (t or "").strip()
            if t and t not in uniq:
                uniq.append(t)
        if not uniq:
            return {}
        if len(uniq) == 1:
            return {uniq[0]: self._translate(uniq[0])}
        try:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(4, len(uniq)),
                                    thread_name_prefix="ocr-mt") as ex:
                out = list(ex.map(self._translate, uniq))
            return dict(zip(uniq, out))
        except Exception as exc:                # noqa: BLE001
            self._log("[ocr] 并行翻译失败，退回串行 %s: %s" % (type(exc).__name__, exc))
            return {t: self._translate(t) for t in uniq}

    def _translate(self, text: str) -> str:
        """日/中 → 中文。失败回落原文（字幕宁可给日语也不空）。"""
        text = (text or "").strip()
        if not text:
            return ""
        # 纯拉丁的名字/ID 不翻（chatbox 里 "Aoi" 翻成「葵」「青」纯属噪声）
        if re.fullmatch(r"[A-Za-z0-9_.\- ]{1,24}", text):
            return text
        hit = self._zh_cache.get(text)
        if hit is not None:
            return hit
        mt = self._translator()
        if mt is None:
            return text
        src = "auto"
        try:                                    # 多语种：cfg.ocr_src=auto 时让 MT 自己判断源语言
            _s = str(self.cfg.get("ocr_src", "auto") or "auto") if hasattr(self.cfg, "get") else "auto"
            if _s and _s != "auto":
                src = _s
        except Exception:                       # noqa: BLE001
            src = "auto"
        try:
            r = mt.translate(text, src=src, dst="zh")
            zh = norm_text((r or {}).get("text", "")) if isinstance(r, dict) else norm_text(str(r or ""))
            if not zh:
                raise ValueError("empty mt result")
        except Exception as exc:
            self._stats["mt_err"] += 1
            if self._stats["mt_err"] <= 2:
                self._log("[ocr] MT 失败（回落原文）%s: %s" % (type(exc).__name__, exc))
            return text
        if len(self._zh_cache) > 512:
            self._zh_cache.clear()
        self._zh_cache[text] = zh
        return zh

    # ------------------------------------------------------------ 对外读
    def read(self, frame: Optional[np.ndarray] = None, force: bool = False) -> List[dict]:
        """整帧识别，返回 [{text, score, box, lang, zh}]。frame=None 时自己抓屏。"""
        if self._closed:
            return []
        cap_ms = 0.0
        if frame is None:
            t0 = time.perf_counter()
            frame = self._grab()
            cap_ms = (time.perf_counter() - t0) * 1000.0
        if frame is None:
            return []
        with self._lock:
            self._stats["frames"] += 1
            self._stats["last_cap_ms"] = round(cap_ms, 2)
            if not force and not self._changed(frame):
                self._stats["skipped"] += 1
                return [dict(l) for l in self._last_lines]
            t0 = time.perf_counter()
            lines = self._read_frame(frame)
            ms = (time.perf_counter() - t0) * 1000.0 + cap_ms
            self._stats["ocr_frames"] += 1
            self._stats["last_ms"] = round(ms, 2)
            self._stats["sum_ms"] += ms
            self._stats["max_ms"] = max(self._stats["max_ms"], ms)
            for ln in lines:
                ln["zh"] = self._translate(ln.get("text", ""))
            self._last_lines = lines
            return [dict(l) for l in lines]

    def once(self, region: Optional[Sequence[int]] = None,
             translate: bool = True) -> List[dict]:
        """只识别这一帧（快捷键 F6 / 控制台「识别一帧」）。

        与常驻轮询的区别：不比较帧差、不去重、不看「区域未标定」，抓到什么就报什么，
        并立刻走 on_raw/on_text 回调 → 原位浮现 + 段逻辑，跟轮询路径完全一致。
        """
        if self._closed:
            return []
        if region:
            try:
                self.set_region(*[int(v) for v in list(region)[:4]])
            except Exception as exc:            # noqa: BLE001
                self._log("[ocr] 单帧识别换区域失败 %s: %s" % (type(exc).__name__, exc))
        if not self._ensure_engine():
            return []
        t_cap = time.time()
        cap0 = time.perf_counter()
        frame = self._grab()
        cap_ms = (time.perf_counter() - cap0) * 1000.0
        if frame is None:
            self._log("[ocr] 单帧识别：抓屏失败（区域越界 / 没有桌面会话）")
            return []
        with self._lock:
            self._stats["frames"] += 1
            self._stats["cap_ts"] = t_cap
            self._stats["last_cap_ms"] = round(cap_ms, 2)
            t0 = time.perf_counter()
            lines = self._read_frame(frame)
            ms = (time.perf_counter() - t0) * 1000.0 + cap_ms
            self._stats["ocr_frames"] += 1
            self._stats["last_ms"] = round(ms, 2)
            self._stats["sum_ms"] += ms
            self._stats["max_ms"] = max(self._stats["max_ms"], ms)
            self._last_lines = lines
        if translate:
            mp = self._translate_many([ln.get("text", "") for ln in lines if not ln.get("zh")])
            for ln in lines:
                if not ln.get("zh"):
                    ln["zh"] = mp.get((ln.get("text") or "").strip(), "")
        self._raw_emit(lines)
        self._emit(lines, ms)
        self._log("[ocr] 单帧识别 %d 行 · %.0fms · 范围 %s"
                  % (len(lines), ms, list(self._region or ())))
        return [dict(l) for l in lines]

    def poll(self, translate: bool = True) -> List[dict]:
        """增量：只回这一帧新出现的行（内部去重/可见集合）。"""
        if self._closed:
            return []
        frame = self._grab()
        if frame is None:
            return []
        with self._lock:
            if not self._changed(frame):
                self._stats["skipped"] += 1
                return []
        lines = self.read(frame, force=True)
        return self._take_new(lines)

    def _take_new(self, lines: List[dict]) -> List[dict]:
        """从一帧结果里挑出「还没播报过」的行（不进抓屏、不跑 OCR）。

        _tick 用它：_tick 已经自己抓过帧、跑过 _changed()，如果再调 poll() 会
        重新抓一帧、重新比帧差 → 永远判定「没变」→ 永远报 0 行（实测踩过）。
        """
        with self._lock:
            now = time.time()
            visible = {fingerprint(l["text"]) for l in lines if l.get("text")}
            new: List[dict] = []
            for ln in lines:
                fp = fingerprint(ln.get("text", ""))
                if not fp or fp in self._visible:
                    continue
                ts = self._seen_ts.get(fp)
                if ts is not None and (now - ts) < self.dedup_ttl:
                    continue
                self._seen_ts[fp] = now
                self._seen_order.append(fp)
                if len(self._seen_order) > 4096:
                    for d in self._seen_order[:2048]:
                        self._seen_ts.pop(d, None)
                    self._seen_order = self._seen_order[2048:]
                new.append(dict(ln))
            self._visible = visible
            return new

    # ------------------------------------------------------------ 同步自检
    def test(self, region: Optional[Sequence[int]] = None, shot: Optional[str] = None) -> Dict[str, Any]:
        """同步跑一次：抓帧 → OCR → MT。返回 {ok, ms, src, dst, boxes}（永不抛）。"""
        t_all = time.perf_counter()
        out: Dict[str, Any] = {"ok": False, "ms": 0.0, "src": "", "dst": "", "boxes": 0,
                               "lines": [], "cap_ms": 0.0, "region": [], "err": ""}
        try:
            if region and len(region) == 4:
                self.set_region(*region)
            out["region"] = self.region
            t0 = time.perf_counter()
            frame = self._grab()
            out["cap_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
            if frame is None:
                out["err"] = "capture failed: %s" % (getattr(self._grabber, "error", "") or "?")
                out["ms"] = round((time.perf_counter() - t_all) * 1000.0, 2)
                return out
            if shot:
                _imwrite(shot, frame)
            if not self._ensure_engine():
                out["err"] = "engine: %s" % (self._engine_err or "load failed")
                out["ms"] = round((time.perf_counter() - t_all) * 1000.0, 2)
                return out
            t1 = time.perf_counter()
            lines = self._read_frame(frame)
            ocr_ms = (time.perf_counter() - t1) * 1000.0
            zh = [self._translate(l.get("text", "")) for l in lines]
            out.update(
                ok=True,
                ms=round((time.perf_counter() - t_all) * 1000.0, 2),
                ocr_ms=round(ocr_ms, 2),
                src="\n".join(l.get("text", "") for l in lines),
                dst="\n".join(zh),
                boxes=len(lines),
                lines=[{"text": l.get("text", ""), "score": round(float(l.get("score", 0.0)), 3),
                        "lang": l.get("lang", ""), "box": list(l.get("box", [])), "zh": z}
                       for l, z in zip(lines, zh)],
                det_ms=self._stats.get("last_det_ms", 0.0),
                rec_ms=self._stats.get("last_rec_ms", 0.0),
                ja_ms=self._stats.get("last_ja_ms", 0.0),
                primary=self._stats.get("primary", ""),
            )
        except Exception as exc:
            out["err"] = "%s: %s" % (type(exc).__name__, exc)
            out["ms"] = round((time.perf_counter() - t_all) * 1000.0, 2)
            self._log("[ocr] test 异常 %s" % out["err"])
        return out

    # ------------------------------------------------------------ 后台线程
    def _emit(self, lines: List[dict], ms: float):
        cb = self.on_text
        if cb is None:
            return
        need = []
        for ln in lines:
            t = (ln.get("text") or "").strip()
            b = ln.get("box") or [0, 0, 0, 0]
            if t and not ln.get("zh") and not self._already_zh(t) and self._worth(t, b):
                need.append(t)
        batch = {}
        batch_ms = 0.0
        if need:
            t_b = time.perf_counter()
            batch = self._translate_many(need)
            batch_ms = (time.perf_counter() - t_b) * 1000.0
        for ln in lines:
            text = ln.get("text", "")
            if not text:
                continue
            b = ln.get("box") or [0, 0, 0, 0]
            if not self._worth(text, b):
                continue
            key = "%s:%d:%d" % (self._region_key, int(b[0] + b[2] / 2) // 8, int(b[1] + b[3] / 2) // 8)
            zh = ln.get("zh") or ""
            mt_ms = 0.0
            if not zh and self._already_zh(text):
                zh = text                      # 屏幕上本来就是中文（或纯数字/符号）→ 不用翻
            if not zh and batch.get(text.strip()):
                zh = batch[text.strip()]        # 这一批一起翻的，耗时算在整批上
                mt_ms = batch_ms
                ln["zh"] = zh
            if not zh:
                t_mt = time.perf_counter()
                zh = self._translate(text)      # _tick 路径没经过 read()，这里补翻译
                mt_ms = (time.perf_counter() - t_mt) * 1000.0
                ln["zh"] = zh
            try:
                self._call_cb(cb, key, text, zh, tuple(int(v) for v in b), float(ms), mt_ms)
                self._stats["emitted"] += 1
            except Exception as exc:
                self._bump_err()
                self._log("[ocr] on_text 回调异常 %s: %s" % (type(exc).__name__, exc))

    @staticmethod
    def _worth(text: str, box=None) -> bool:
        """这行值不值得浮出来。

        两道筛子（都来自实测噪声）：
        1) 只数「字」（汉字/假名/字母），数字不算 —— 挡掉「:55:33」「11:13」这类时间戳；
        2) 没有汉字也没有假名的拉丁碎片（「Ind」「th」「d」这种窗口边缘的 OCR 碎屑）
           要求至少 3 个字母且框宽 >= 40px，否则丢掉。
        """
        n = sum(1 for ch in text if ch.isalpha())
        if n < 2:
            return False
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
        if cjk == 0:
            w = float((box or [0, 0, 0, 0])[2] or 0.0)
            if n < 3 or w < 40.0:
                return False
        return True

    @staticmethod
    def _already_zh(text: str) -> bool:
        """已经是中文（或压根没有日语假名）就不用再过一遍翻译。

        实测踩到的噪声：把面板上的「:55:33」翻成「五十五分三十三秒」、「图片H」翻成「图片H」，
        纯属浪费一次 MT 还把屏幕糊满。判据：没有假名 + 有汉字 = 视为中文。
        """
        try:
            from . import util
            if util.has_kana(text):
                return False
            cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
            return cjk >= 1
        except Exception:                       # noqa: BLE001
            return False

    def _tick(self) -> int:
        """一轮轮询。返回上报行数。"""
        # 没框选区域就别抓了：全屏兜底会把桌面上的字全当成聊天气泡（实测 55 条噪声段）
        if self._region_inferred:
            if not self._warned_region:
                self._warned_region = True
                self._log("[ocr] C 链还没框选区域 → 暂停抓屏；控制台里点「框选聊天框」框住那一块再开")
            return 0
        frame = self._grab()
        if frame is None:
            return 0
        with self._lock:
            self._stats["frames"] += 1
            self._stats["cap_ts"] = time.time()     # 抓屏时刻（给「抓屏→浮现」计时用）
            if not self._changed(frame):
                self._stats["skipped"] += 1
                return 0
        t0 = time.perf_counter()
        lines = self._read_frame(frame)
        ocr_ms = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self._stats["ocr_frames"] += 1
            self._stats["last_ms"] = round(ocr_ms, 2)
            self._stats["sum_ms"] += ocr_ms
            self._stats["max_ms"] = max(self._stats["max_ms"], ocr_ms)
            self._last_lines = lines
            if not lines:
                self._empty_frames += 1
                if self._had_lines and self._empty_frames >= 2:
                    self._had_lines = False
                    self._visible = set()
                    cb = self.on_clear
                    if cb is not None:
                        try:
                            cb()
                        except Exception as exc:
                            self._bump_err()
                            self._log("[ocr] on_clear 回调异常 %s: %s" % (type(exc).__name__, exc))
                return 0
            self._empty_frames = 0
            self._had_lines = True

        new = self._take_new(lines)
        if not new:
            return 0
        self._raw_emit(new)                 # 原文先浮（不等翻译）
        ms = ocr_ms + float(self._stats.get("last_cap_ms", 0.0) or 0.0)
        self._emit(new, ms)
        return len(new)

    def _raw_emit(self, lines: List[dict]):
        cb = self.on_raw
        if cb is None:
            return
        items = []
        for ln in lines:
            text = ln.get("text", "")
            if not text:
                continue
            b = ln.get("box") or [0, 0, 0, 0]
            key = "%s:%d:%d" % (self._region_key,
                                int(b[0] + b[2] / 2) // 8, int(b[1] + b[3] / 2) // 8)
            items.append({"key": key, "src": text, "box": [int(v) for v in b]})
        if not items:
            return
        try:
            cb(items)
        except Exception as exc:                    # noqa: BLE001
            self._log("[ocr] on_raw 回调异常 %s: %s" % (type(exc).__name__, exc))

    # 让 _emit 能带回翻译耗时（不改变老回调签名）
    def _call_cb(self, cb, key, text, zh, box, ms, mt_ms):
        try:
            cb(key, text, zh, box, ms, mt_ms)
        except TypeError:
            cb(key, text, zh, box, ms)

    def start(self, snap_only: bool = False) -> bool:
        """起后台轮询线程（ocr_mode=hotkey 时只准备好区域，不轮询）。

        snap_only=True 时无论 ocr_mode 是什么都不起轮询线程——「F6 只识别这一帧」
        和「截图看一眼」走的就是这条路：C 链总开关关着也能用。
        """
        if self._closed:
            return False
        mode = "hotkey"
        try:
            mode = str(self.cfg.get("ocr_mode", "hotkey") or "hotkey").lower()
        except Exception:                       # noqa: BLE001
            mode = "hotkey"
        if snap_only:
            mode = "hotkey"
        with self._lock:
            if mode == "hotkey":
                self._ensure_region()
                self._log("[ocr] C 链「按一下识别一帧」模式：F6 识别当前范围 / F5 改范围"
                          "（不再常驻轮询，不占 CPU 也不会误读桌面）")
                return True
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop_evt.clear()
            self._ensure_region()
            th = threading.Thread(target=self._loop, name="vrdub-ocr", daemon=True)
            self._thread = th
            th.start()
        return True

    def _loop(self):
        err_logged = False
        self._log("[ocr] 轮询启动 interval=%.0fms region=%s"
                  % (self._interval_eff() * 1000.0, list(self._region or ())))
        while not self._stop_evt.is_set() and not self._closed:
            t0 = time.perf_counter()
            try:
                self._tick()
                if err_logged:
                    self._log("[ocr] 轮询恢复")
                    err_logged = False
            except Exception as exc:  # 掉帧/权限/COM 抖动一律吞掉，不能影响音频链
                self._bump_err()
                if not err_logged:
                    self._log("[ocr] 轮询异常（已忽略）%s: %s" % (type(exc).__name__, exc))
                    err_logged = True
            wait = self._interval_eff() - (time.perf_counter() - t0)
            if wait > 0:
                self._stop_evt.wait(min(wait, 1.0))

    def _interval_eff(self) -> float:
        """未标定区域时自动降频（全屏下半部分单帧 ~5s，按 0.6s 跑等于满载）。"""
        if not self._region_inferred:
            return self.interval
        if not self._slow_logged:
            self._slow_logged = True
            self._log("[ocr] 区域未标定，轮询降到 %.1fs/次（标定 chatbox 后回到 %.2fs/次）"
                      % (self.slow_interval, self.interval))
        return max(self.interval, self.slow_interval)

    def stop(self):
        self._stop_evt.set()
        th = self._thread
        if th is not None and th.is_alive():
            try:
                th.join(timeout=2.0)
            except Exception:
                pass
        self._thread = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop()
        try:
            self._grabber.close()
        except Exception:
            pass
        self._engine = None
        self._mt = None

    # ------------------------------------------------------------ 诊断
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            st = dict(self._stats)
            st["frames"] = st.get("frames", 0)
            n = max(1, int(st.get("ocr_frames", 0)))
            st["avg_ms"] = round(float(st.get("sum_ms", 0.0)) / n, 2)
            for k in ("sum_ms", "max_ms", "last_ms", "last_cap_ms", "last_det_ms",
                      "last_rec_ms", "last_ja_ms"):
                st[k] = round(float(st.get(k, 0.0) or 0.0), 2)
            st["region"] = list(self._region or ())
            st["region_key"] = self._region_key
            st["dpi"] = self.dpi_mode
            st["screen"] = list(screen_size())
            st["interval"] = self.interval
            st["n_lines"] = len(self._last_lines)
            st["running"] = bool(self._thread is not None and self._thread.is_alive())
            st["engine"] = None
            if self._engine is not None:
                st["engine"] = {
                    "loaded": self._engine.loaded,
                    "error": self._engine.load_error,
                    "load_ms": round(self._engine.load_ms, 1),
                    "warmup_ms": round(self._engine.warmup_ms, 1),
                    "primary": getattr(self._engine, "last_primary", ""),
                    "crop_scale": self._engine.crop_scale,
                }
            elif self._engine_err:
                st["engine"] = {"loaded": False, "error": self._engine_err}
            st["mt"] = {"ready": self._mt is not None, "error": self._mt_err}
            return st


# ---------------------------------------------------------------- 自检入口
def main(argv=None):  # pragma: no cover
    import argparse
    import json

    ap = argparse.ArgumentParser(description="C 链自检：抓一次屏幕 → OCR → 翻译")
    ap.add_argument("--test", action="store_true", help="同步跑一次并打印结果")
    ap.add_argument("--region", nargs=4, type=int, default=None, help="x y w h（物理像素）")
    ap.add_argument("--shot", default="", help="另存一张 PNG 到这个路径")
    ap.add_argument("--interval", type=float, default=0.0, help="覆盖 cfg.ocr_interval")
    ap.add_argument("--watch", type=float, default=0.0, help="后台轮询 N 秒，逐行打印 on_text")
    ap.add_argument("--suggest", action="store_true", help="只打印建议的 chatbox 区域")
    args = ap.parse_args(argv)

    if args.suggest:
        print(json.dumps({"suggest": suggest_region(), "screen": list(screen_size()),
                          "virtual": list(virtual_screen_rect())}, ensure_ascii=False))
        return 0

    try:
        from .cfg import Cfg

        cfg = Cfg()
    except Exception:
        cfg = {}
    if args.region:
        try:
            cfg.update({"ocr_region": [int(v) for v in args.region]})
        except Exception:
            pass
    if args.interval > 0:
        try:
            cfg.update({"ocr_interval": float(args.interval)})
        except Exception:
            pass

    events: List[Tuple] = []
    chain = OcrChain(cfg, on_text=lambda *a: events.append(a),
                     on_clear=lambda: events.append(("clear",)), log=print)
    if args.region:
        chain.set_region(*args.region)

    r = chain.test(shot=args.shot or "")
    print("--- test ok=%s ms=%.1f (cap %.1f / det %.1f / rec %.1f / ja %.1f) boxes=%d primary=%s"
          % (r.get("ok"), float(r.get("ms", 0.0)), float(r.get("cap_ms", 0.0)),
             float(r.get("det_ms", 0.0) or 0.0), float(r.get("rec_ms", 0.0) or 0.0),
             float(r.get("ja_ms", 0.0) or 0.0), int(r.get("boxes", 0)), r.get("primary", "")))
    print("region=%s" % (r.get("region"),))
    if not r.get("ok"):
        print("err=%s" % r.get("err"))
    for ln in r.get("lines") or []:
        print("  [%s] %.3f %s" % (ln.get("lang", ""), float(ln.get("score", 0.0)), ln.get("text", "")))
        print("      -> %s" % ln.get("zh", ""))
    print(json.dumps({k: v for k, v in chain.stats().items() if k not in ("engine",)},
                     ensure_ascii=False))

    if args.watch > 0:
        chain.start()
        t_end = time.time() + args.watch
        while time.time() < t_end:
            time.sleep(0.2)
        chain.stop()
        print("--- 轮询 %ss：on_text %d 次" % (args.watch, sum(1 for e in events if len(e) == 5)))
        for e in events[:40]:
            if len(e) == 5:
                print("  key=%s %.0fms %s -> %s" % (e[0], e[4], e[1], e[2]))
            else:
                print("  on_clear()")
    chain.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())