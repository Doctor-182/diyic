# -*- coding: utf-8 -*-
"""vrdub C 链截图：纯 ctypes GDI BitBlt（DPI aware）。

为什么不用 PIL.ImageGrab / mss：
  * ImageGrab 永远先抓全屏再裁，框多小都要 ~50ms（上一代实测）；
  * 本模块 BitBlt 直接只拷 ROI：540x420 约 1~4ms，2560x1600 全屏 ~50-70ms。
  * 而且必须先 SetProcessDpiAwarenessContext(-4)，否则 150% 缩放下拿到的是 1707x1067
    的逻辑分辨率，ROI 坐标会和真实像素错位。

进程级 DPI awareness 只能设一次；Qt / 别的库若先设了，这里会返回已设的状态而不是报错。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _bind():
    if user32 is None:
        raise OSError("不是 Windows，GDI 截图不可用")
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL


_bind()

_DPI_STATE = None


def ensure_dpi_aware() -> str:
    """进程级 DPI 感知。返回 'pmv2' / 'per-monitor' / 'system' / 'already' / 'unknown'。"""
    global _DPI_STATE
    if _DPI_STATE:
        return _DPI_STATE
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_STATE = "pmv2"
            return _DPI_STATE
    except AttributeError:
        pass
    except OSError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(2) == 0:
            _DPI_STATE = "per-monitor"
            return _DPI_STATE
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            _DPI_STATE = "system"
            return _DPI_STATE
    except Exception:
        pass
    # 已经被别人（Qt/manifest）设过：照旧用物理像素
    _DPI_STATE = "already"
    return _DPI_STATE


def screen_size() -> tuple[int, int]:
    """主屏物理像素（DPI aware 之后才是真分辨率）。"""
    ensure_dpi_aware()
    return int(user32.GetSystemMetrics(SM_CXSCREEN)), int(user32.GetSystemMetrics(SM_CYSCREEN))


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """全桌面（多屏）原点与尺寸，屏幕原点可能是负数。"""
    ensure_dpi_aware()
    x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    w = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)) or screen_size()[0]
    h = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)) or screen_size()[1]
    return x, y, w, h


def clamp_region(x, y, w, h) -> tuple[int, int, int, int]:
    """把 ROI 夹到主屏内，保证 w,h >= 8。"""
    sw, sh = screen_size()
    x, y, w, h = int(round(x)), int(round(y)), int(round(w)), int(round(h))
    w = max(8, min(w, sw))
    h = max(8, min(h, sh))
    x = max(0, min(x, sw - w))
    y = max(0, min(y, sh - h))
    return x, y, w, h


PIL_FALLBACKS = 0


class ScreenGrabber:
    """一个可复用的 BitBlt 取帧器：换 ROI 只重建 DIB 缓冲。"""

    def __init__(self):
        self.ok = False
        self.error = ""
        self.region = (0, 0, 0, 0)
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbmp = None
        self._pbits = None
        self._paddr = 0
        self._shape = None
        self._old_bmp = None
        try:
            self._hdc_screen = user32.GetDC(None)
            if not self._hdc_screen:
                raise OSError("GetDC(0) 失败，err=%d" % ctypes.get_last_error())
            self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)
            if not self._hdc_mem:
                raise OSError("CreateCompatibleDC 失败，err=%d" % ctypes.get_last_error())
            self.ok = True
        except Exception as exc:  # pragma: no cover - 只在无桌面会话时
            self.error = str(exc)
            self.close()

    # --- 生命周期 ---------------------------------------------------
    def close(self):
        if self._hbmp:
            try:
                if self._hdc_mem and self._old_bmp:
                    gdi32.SelectObject(self._hdc_mem, self._old_bmp)
                gdi32.DeleteObject(self._hbmp)
            except Exception:
                pass
        if self._hdc_mem:
            try:
                gdi32.DeleteDC(self._hdc_mem)
            except Exception:
                pass
        if self._hdc_screen:
            try:
                user32.ReleaseDC(None, self._hdc_screen)
            except Exception:
                pass
        self._hbmp = None
        self._hdc_mem = None
        self._hdc_screen = None
        self._pbits = None
        self._paddr = 0
        self._shape = None
        self.ok = False

    # --- ROI --------------------------------------------------------
    def set_region(self, x, y, w, h):
        self.region = clamp_region(x, y, w, h)
        if self._shape != (self.region[2], self.region[3]):
            self._rebuild()
        return self.region

    def _rebuild(self):
        w, h = self.region[2], self.region[3]
        if self._hbmp:
            try:
                gdi32.SelectObject(self._hdc_mem, self._old_bmp)
                gdi32.DeleteObject(self._hbmp)
            except Exception:
                pass
            self._hbmp = None
            self._pbits = None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # 负数 = top-down，行 0 就是最上面
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        pbits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            self._hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(pbits), None, 0
        )
        if not hbmp:
            raise OSError("CreateDIBSection 失败，err=%d" % ctypes.get_last_error())
        self._hbmp = hbmp
        self._pbits = pbits
        self._paddr = int(pbits.value or 0)
        self._old_bmp = gdi32.SelectObject(self._hdc_mem, hbmp)
        self._shape = (w, h)

    # --- 抓帧 -------------------------------------------------------
    def grab(self):
        """返回 BGR uint8 (h,w,3) 的 numpy 拷贝；失败返回 None。"""
        if not self.ok:
            return None
        x, y, w, h = self.region
        if w <= 0 or h <= 0:
            return None
        if self._shape != (w, h):
            self._rebuild()
        ok = gdi32.BitBlt(self._hdc_mem, 0, 0, w, h, self._hdc_screen, x, y, SRCCOPY | CAPTUREBLT)
        if not ok:
            ok = gdi32.BitBlt(self._hdc_mem, 0, 0, w, h, self._hdc_screen, x, y, SRCCOPY)
        if not ok:
            err = ctypes.windll.kernel32.GetLastError()
            self.error = ("BitBlt 失败 err=%d region=%s hdc_screen=%r hdc_mem=%r shape=%s"
                          % (err, self.region, self._hdc_screen, self._hdc_mem, self._shape))
            # 实测这台机上 BitBlt 会「忽然」开始失败（err=0，PIL 同一个 ROI 却能抓）：
            # 屏幕 DC 被别的东西锁住/显示模式变了都会这样。PIL 走另一条路，
            # 抓全屏再裁（850x230 ROI 约 20-40ms），够 F6 单帧和 0.35s 轮询用。
            frame = self._grab_pil(x, y, w, h)
            if frame is not None:
                return frame
            return None
        self.error = ""
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(self._paddr)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return np.ascontiguousarray(arr[:, :, :3])

    def _grab_pil(self, x, y, w, h):
        """BitBlt 失败时的兜底：PIL ImageGrab（另一条 GDI 路径，实测常常还能抓）。"""
        global PIL_FALLBACKS
        try:
            from PIL import ImageGrab
        except Exception as exc:            # noqa: BLE001
            self.error += " / 没有 PIL：%r" % (exc,)
            return None
        try:
            im = ImageGrab.grab(bbox=(int(x), int(y), int(x + w), int(y + h)))
            arr = np.asarray(im.convert("RGB"))
            if arr.ndim != 3 or arr.shape[0] != h or arr.shape[1] != w:
                self.error += " / PIL 兜底尺寸不对 %s" % (arr.shape,)
                return None
            PIL_FALLBACKS += 1
            return np.ascontiguousarray(arr[:, :, ::-1])          # RGB -> BGR
        except Exception as exc:            # noqa: BLE001
            self.error += " / PIL 兜底也失败 %r" % (exc,)
            return None

    def grab_padded(self, pad: int = 0):
        """抓 ROI 并向外扩 pad 像素（扩的部分会被夹在屏幕内），返回 (frame, (x,y))。"""
        x, y, w, h = self.region
        if pad:
            x, y, w, h = clamp_region(x - pad, y - pad, w + 2 * pad, h + 2 * pad)
        old = self.region
        try:
            self.region = (x, y, w, h)
            if self._shape != (w, h):
                self._rebuild()
            return self.grab(), (x, y)
        finally:
            self.region = old
            if self._shape != (old[2], old[3]):
                self._rebuild()
