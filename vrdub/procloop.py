# -*- coding: utf-8 -*-
"""进程级 WASAPI 回环：只采「一个进程（树）」渲染出来的声音。

为什么要它：B 链原来采「扬声器 [Loopback]」，把用户自己的麦克风、TTS 监听、
系统提示音全收进去了；VB-CABLE 的几根线又实测互相串通（19↔27、20↔24），
没法用第二根线隔离。Windows 10 2004（build 19041）起有进程回环：
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK + 虚拟设备 "VAD\\Process_Loopback"，
走 ActivateAudioInterfaceAsync 拿 IAudioClient，就只要目标进程树的声音。

全部纯 ctypes + COM，不依赖 comtypes。vtable 索引按头文件真实顺序：

  IAudioClient（audioclient.h，继承 IUnknown）
    0 QueryInterface   1 AddRef   2 Release
    3 Initialize       4 GetBufferSize   5 GetStreamLatency   6 GetCurrentPadding
    7 IsFormatSupported 8 GetMixFormat   9 GetDevicePeriod
    10 Start           11 Stop     12 Reset     13 SetEventHandle     14 GetService
  IAudioCaptureClient（audioclient.h）
    0..2 IUnknown      3 GetBuffer   4 ReleaseBuffer   5 GetNextPacketSize
  IActivateAudioInterfaceAsyncOperation（mmdeviceapi.h）
    0..2 IUnknown      3 GetActivateResult
  IActivateAudioInterfaceCompletionHandler（mmdeviceapi.h）
    0..2 IUnknown      3 ActivateCompleted

用法：
    pl = ProcLoop(name="VRChat.exe", on_pcm=lambda pcm, rate: ...)
    pl.start()          # 失败直接 RuntimeError("进程回环开不了：0x… …")
    ...
    pl.stop()
"""
from __future__ import annotations

import ctypes
import struct
import sys
import threading
import time
from ctypes import wintypes

import numpy as np

from . import util

if sys.platform != "win32":                     # pragma: no cover
    raise ImportError("vrdub.procloop 只在 Windows 上可用")

__all__ = ["ProcLoop", "find_pid", "list_pids"]

# --------------------------------------------------------------------------- 常量
COINIT_MULTITHREADED = 0x0
S_OK = 0
S_FALSE = 1
E_NOINTERFACE = 0x80004002
E_POINTER = 0x80004003
E_FAIL = 0x80004005
E_INVALIDARG = 0x80070057
E_ACCESSDENIED = 0x80070005
RPC_E_CHANGED_MODE = 0x80010106

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_PROCESS_TREE = 0
VT_BLOB = 65
VAD_PROCESS_LOOPBACK = r"VAD\Process_Loopback"
AUDCLNT_SHAREMODE_SHARED = 0

AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_FLAGS_AUTO = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
               | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
_FLAGS_BASE = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK

AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = (1 << 64) - 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
INFINITE = 0xFFFFFFFF

# HRESULT 速查（报错信息里带中文含义，接线时一眼看出是权限、格式还是设备问题）
_HR = {
    0x80070005: "E_ACCESSDENIED 拒绝访问（目标进程权限更高 / 受保护进程）",
    0x80070057: "E_INVALIDARG 参数无效（格式或缓冲期不被进程回环接受，也可能系统太旧）",
    0x8007000E: "E_OUTOFMEMORY 内存不足",
    0x80004002: "E_NOINTERFACE 接口不支持（系统没有进程回环接口）",
    0x80004003: "E_POINTER 空指针（激活没返回接口）",
    0x80004005: "E_FAIL 一般失败（进程已退出 / 不在同一会话 / 没有音频会话）",
    0x8000000E: "E_ILLEGAL_METHOD_CALL 调用时机/线程不对（完成回调查 QI 时不认 IAgileObject 最常见）",
    0x80010106: "RPC_E_CHANGED_MODE COM 套间模式冲突（该线程已被初始化为 STA）",
    0x8001010E: "RPC_E_WRONG_THREAD 调错线程",
    0x88890001: "AUDCLNT_E_NOT_INITIALIZED 未初始化",
    0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED 已初始化过",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED 设备失效（换设备 / 目标进程退出）",
    0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT 格式不支持（换 PCM16 44100 兜底）",
    0x8889000A: "AUDCLNT_E_DEVICE_IN_USE 设备被独占",
    0x8889000E: "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED 不允许独占",
    0x8889000F: "AUDCLNT_E_ENDPOINT_CREATE_FAILED 端点创建失败",
    0x88890010: "AUDCLNT_E_SERVICE_NOT_RUNNING Windows 音频服务没跑",
    0x88890011: "AUDCLNT_E_EVENTHANDLE_NOT_EXPECTED 没开 EVENTCALLBACK 却 SetEventHandle",
    0x88890012: "AUDCLNT_E_EXCLUSIVE_MODE_ONLY 只能独占",
    0x88890013: "AUDCLNT_E_BUFDURATION_PERIOD_NOT_EQUAL 缓冲期与周期不匹配",
    0x88890014: "AUDCLNT_E_EVENTHANDLE_NOT_SET 没设事件句柄",
    0x88890015: "AUDCLNT_E_INCORRECT_BUFFER_SIZE 缓冲大小不对",
    0x88890019: "AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED 缓冲大小未对齐",
    0x88890020: "AUDCLNT_E_INVALID_DEVICE_PERIOD 设备周期无效",
    0x88890021: "AUDCLNT_E_INVALID_STREAM_FLAG 流标志无效（进程回环必须 LOOPBACK|EVENTCALLBACK）",
    0x88890026: "AUDCLNT_E_RESOURCES_INVALIDATED 资源失效（目标进程退出最常见）",
    0x88890027: "AUDCLNT_E_RAW_MODE_UNSUPPORTED 不支持 RAW",
}

_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_mmdev = ctypes.WinDLL("mmdevapi", use_last_error=True)


def _hr_text(hr: int) -> str:
    code = int(hr) & 0xFFFFFFFF
    return "0x%08X %s" % (code, _HR.get(code, "未知 HRESULT"))


def _fail(prefix: str, hr: int) -> RuntimeError:
    return RuntimeError("进程回环开不了：%s（%s）" % (prefix, _hr_text(hr)))


# --------------------------------------------------------------------------- GUID
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str = ""):
        super().__init__()
        if text:
            # 文本里的 Data1/2/3 就是字段值本身（大端书写），别再做一次字节序翻转：
            # 之前按 raw[0:4] 再 int.from_bytes(..., "little") 解析，
            # 结果 IID_IAudioClient 变成 4CADB91C-... 之类，QI 永远匹配不上。
            h = text.strip().strip("{}").replace("-", "")
            if len(h) != 32:
                raise ValueError("GUID 文本长度不对：%r" % (text,))
            self.Data1 = int(h[0:8], 16)
            self.Data2 = int(h[8:12], 16)
            self.Data3 = int(h[12:16], 16)
            for i in range(8):
                self.Data4[i] = int(h[16 + i * 2:18 + i * 2], 16)

    def __bytes__(self) -> bytes:
        """内存布局（16 字节），直接和 QI 传进来的 riid 内存比较。"""
        return ctypes.string_at(ctypes.byref(self), 16)

    def __repr__(self) -> str:
        return "GUID(%08X-%04X-%04X-%s)" % (
            self.Data1, self.Data2, self.Data3,
            "".join("%02X" % b for b in self.Data4))

    def __eq__(self, other) -> bool:
        try:
            return bytes(self) == bytes(other)
        except Exception:                                # noqa: BLE001
            return False

    __hash__ = None


IID_IUnknown = GUID("00000000-0000-0000-C000-000000000046")
IID_IAudioClient = GUID("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
IID_IActivateAudioInterfaceCompletionHandler = GUID("41D949AB-9862-444A-80F6-C261334DA5EB")
# Windows 强制要求完成回调是「agile 对象」，激活时会 QI 这个 IID；
# 不认就会直接拿到 E_ILLEGAL_METHOD_CALL(0x8000000E)，一步都走不下去。
IID_IAgileObject = GUID("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")

# --------------------------------------------------------------------------- 结构
class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int),
                ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.POINTER(ctypes.c_byte))]


class PROPVARIANT(ctypes.Structure):
    # x64: vt(2)+wReserved1..3(6)=8，union 从偏移 8 开始；BLOB 16 字节 → 共 24
    _fields_ = [("vt", wintypes.WORD), ("wReserved1", wintypes.WORD),
                ("wReserved2", wintypes.WORD), ("wReserved3", wintypes.WORD),
                ("blob", _BLOB)]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


# --------------------------------------------------------------------------- 函数签名
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k32.ProcessIdToSessionId.restype = wintypes.BOOL
_k32.GetCurrentProcessId.restype = wintypes.DWORD
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.SetEvent.argtypes = [wintypes.HANDLE]
_k32.SetEvent.restype = wintypes.BOOL
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.WaitForSingleObject.restype = wintypes.DWORD

_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.CoTaskMemFree.restype = None

_mmdev.ActivateAudioInterfaceAsync.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID),
                                               ctypes.POINTER(PROPVARIANT), ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
_mmdev.ActivateAudioInterfaceAsync.restype = ctypes.c_long

_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG


# --------------------------------------------------------------------------- COM 调用
def _as_ptr(ptr):
    """int / c_void_p 都收，统一成 c_void_p（ctypes.c_void_p(c_void_p) 会抛
    TypeError: cannot be converted to pointer，GetService 返回的就是 c_void_p）。"""
    if isinstance(ptr, ctypes.c_void_p):
        v = ptr.value
    else:
        v = int(ptr or 0)
    if not v:
        raise RuntimeError("进程回环开不了：接口指针为空（COM 对象已释放）")
    return ctypes.c_void_p(v)


def _vcall(ptr, index: int, restype, argtypes, *args):
    """按 vtable 索引调用：this 指针 + 参数，函数原型全用 WINFUNCTYPE 现场构造。"""
    p = _as_ptr(ptr)
    vtbl = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(p, *args)


class _IAudioClient:
    """IAudioClient vtable：3 Initialize / 4 GetBufferSize / 8 GetMixFormat /
    10 Start / 11 Stop / 13 SetEventHandle / 14 GetService（0..2 是 IUnknown）。"""

    def __init__(self, ptr):
        self.ptr = _as_ptr(ptr)

    def Initialize(self, share_mode, stream_flags, buf_dur, periodicity, fmt_ptr, session_guid=None):
        return _vcall(self.ptr, 3, ctypes.c_long,
                      [ctypes.c_int, ctypes.c_uint32, ctypes.c_int64, ctypes.c_int64,
                       ctypes.c_void_p, ctypes.c_void_p],
                      share_mode, stream_flags, buf_dur, periodicity, fmt_ptr, session_guid)

    def GetBufferSize(self):
        n = ctypes.c_uint32(0)
        hr = _vcall(self.ptr, 4, ctypes.c_long,
                    [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n))
        return hr, int(n.value)

    def GetMixFormat(self):
        p = ctypes.c_void_p()
        hr = _vcall(self.ptr, 8, ctypes.c_long,
                    [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(p))
        return hr, p

    def Start(self):
        return _vcall(self.ptr, 10, ctypes.c_long, [])

    def Stop(self):
        return _vcall(self.ptr, 11, ctypes.c_long, [])

    def SetEventHandle(self, handle):
        return _vcall(self.ptr, 13, ctypes.c_long, [ctypes.c_void_p], handle)

    def GetService(self, iid):
        p = ctypes.c_void_p()
        hr = _vcall(self.ptr, 14, ctypes.c_long,
                    [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(iid), ctypes.byref(p))
        return hr, p

    def Release(self):
        return _vcall(self.ptr, 2, ctypes.c_ulong, [])


class _IAudioCaptureClient:
    """IAudioCaptureClient vtable：3 GetBuffer / 4 ReleaseBuffer / 5 GetNextPacketSize。"""

    def __init__(self, ptr):
        self.ptr = _as_ptr(ptr)

    def GetNextPacketSize(self):
        n = ctypes.c_uint32(0)
        hr = _vcall(self.ptr, 5, ctypes.c_long,
                    [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n))
        return hr, int(n.value)

    def GetBuffer(self):
        data = ctypes.c_void_p()
        frames = ctypes.c_uint32(0)
        flags = ctypes.c_uint32(0)
        hr = _vcall(self.ptr, 3, ctypes.c_long,
                    [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_void_p],
                    ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
        return hr, data.value, int(frames.value), int(flags.value)

    def ReleaseBuffer(self, frames):
        return _vcall(self.ptr, 4, ctypes.c_long, [ctypes.c_uint32], int(frames))

    def Release(self):
        return _vcall(self.ptr, 2, ctypes.c_ulong, [])


_QI_T = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID),
                           ctypes.POINTER(ctypes.c_void_p))
_AR_T = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_DONE_T = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)


class _CompletionHandler:
    """自制 IActivateAudioInterfaceCompletionHandler（只有一个 4 槽 vtable）。

    关键（踩过的坑）：COM 对象是「两级间接」——
        this ──► [vtable 指针] ──► [QI, AddRef, Release, ActivateCompleted]
    所以传出去的 this 必须是「存放 vtable 指针那块内存」的地址，不是 vtable 自己的地址。
    一开始直接把 &vt[0] 当 this 传，Windows 会把 QI 的函数地址当 vtable 去解引用，
    拿机器码当函数指针跳（0x4825048B4865 这种地址），进程直接 fail-fast 0xC0000409。
    """

    def __init__(self):
        self.event = _k32.CreateEventW(None, True, False, None)   # 手动复位
        if not self.event:
            raise RuntimeError("进程回环开不了：CreateEventW 失败（err=%d）" % ctypes.get_last_error())
        self.hr = E_FAIL
        self.iface = None
        self.refs = 1
        self._vt = (ctypes.c_void_p * 4)()
        self._f_qi = _QI_T(self._qi)
        self._f_addref = _AR_T(self._addref)
        self._f_release = _AR_T(self._release)
        self._f_done = _DONE_T(self._activate_completed)
        self._vt[0] = ctypes.cast(self._f_qi, ctypes.c_void_p)
        self._vt[1] = ctypes.cast(self._f_addref, ctypes.c_void_p)
        self._vt[2] = ctypes.cast(self._f_release, ctypes.c_void_p)
        self._vt[3] = ctypes.cast(self._f_done, ctypes.c_void_p)
        # 第一级：对象本体只放一个指向 vtable 的指针（COM 的两级间接）
        self._obj = (ctypes.c_void_p * 1)(ctypes.cast(self._vt, ctypes.c_void_p))
        self.addr = ctypes.addressof(self._obj)
        self.ptr = ctypes.c_void_p(self.addr)

    # 0 QueryInterface / 1 AddRef / 2 Release / 3 ActivateCompleted
    def _qi(self, this, riid, ppv):
        try:
            want = bytes(riid.contents)
            if want in (bytes(IID_IUnknown),
                        bytes(IID_IActivateAudioInterfaceCompletionHandler),
                        bytes(IID_IAgileObject)):
                ppv[0] = ctypes.c_void_p(self.addr)
                self.refs += 1
                return S_OK
            if ppv:
                ppv[0] = ctypes.c_void_p(None)
            return E_NOINTERFACE
        except Exception:                                    # noqa: BLE001
            return E_FAIL

    def _addref(self, this):
        self.refs += 1
        return self.refs

    def _release(self, this):
        self.refs = max(0, self.refs - 1)
        return self.refs

    def _activate_completed(self, this, operation):
        try:
            hr = ctypes.c_long(E_FAIL)
            p = ctypes.c_void_p()
            rc = _vcall(operation, 3, ctypes.c_long,
                        [ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_void_p)],
                        ctypes.byref(hr), ctypes.byref(p))
            self.hr = int(rc) if int(rc) != 0 else int(hr.value)
            self.iface = p.value
        except Exception as e:                               # noqa: BLE001
            self.hr = E_FAIL
            self.iface = None
            self.err = e
        finally:
            try:
                _k32.SetEvent(self.event)
            except Exception:                                # noqa: BLE001
                pass
        return S_OK

    def close(self):
        if self.event:
            try:
                _k32.CloseHandle(self.event)
            except Exception:                                # noqa: BLE001
                pass
            self.event = None


# --------------------------------------------------------------------------- 格式工具
def _build_fmt(rate: int, ch: int, bits: int, enc: str):
    """造一块 WAVEFORMATEX（18 字节，cbSize=0），返回 (缓冲区, 描述)。

    坑：ctypes 里 WAVEFORMATEX 因为有 DWORD 成员会被对齐到 20 字节，
    from_buffer 要求缓冲区 ≥20，可 WAVEFORMATEX 真身只有 18 字节。
    所以这里用 struct.pack 自己拼 18 字节，绕开对齐。
    """
    tag = 3 if enc in ("f32", "f64") else 1
    block = max(1, ch * (bits // 8))
    raw = struct.pack("<HHIIHHH", tag, ch, rate, rate * block, block, bits, 0)
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    return buf, {"enc": enc, "ch": ch, "rate": rate, "bits": bits, "block": block,
                 "tag": tag}


def _parse_fmt(raw: bytes) -> dict:
    """解析 WAVEFORMATEX / WAVEFORMATEXTENSIBLE。"""
    tag, ch, rate, _avg, block, bits, _cb = struct.unpack("<HHIIHHH", raw[:18])
    tag, ch, rate, block, bits = int(tag), int(ch), int(rate), int(block), int(bits)
    if tag == 0xFFFE and len(raw) >= 40:                     # WAVE_FORMAT_EXTENSIBLE
        sub = raw[24:40]
        base = int.from_bytes(sub[0:4], "little")
        if base == 3:
            enc = "f32" if bits == 32 else "f64"
        elif base == 1:
            enc = {8: "u8", 16: "s16", 24: "s24", 32: "s32"}.get(bits, "")
        else:
            raise RuntimeError("进程回环开不了：不认识的子格式 GUID %s" % sub.hex())
    elif tag == 3:
        enc = "f32" if bits == 32 else "f64"
    elif tag == 1:
        enc = {8: "u8", 16: "s16", 24: "s24", 32: "s32"}.get(bits, "")
    else:
        enc = ""
    if not enc:
        raise RuntimeError("进程回环开不了：不支持的格式 tag=0x%04X bits=%d" % (tag, bits))
    if ch <= 0 or block <= 0:
        raise RuntimeError("进程回环开不了：格式字段异常 ch=%d block=%d" % (ch, block))
    return {"enc": enc, "ch": ch, "rate": rate, "bits": bits, "block": block, "tag": tag}


def _decode(raw: bytes, fmt: dict) -> np.ndarray:
    """原始字节 → float32 (frames, ch)。"""
    ch, block = int(fmt["ch"]), int(fmt["block"])
    n = (len(raw) // block) * ch
    if n <= 0:
        return np.zeros((0, ch), np.float32)
    enc = fmt["enc"]
    if enc == "f32":
        a = np.frombuffer(raw, "<f4", count=n)
    elif enc == "f64":
        a = np.frombuffer(raw, "<f8", count=n).astype(np.float32)
    elif enc == "s16":
        a = np.frombuffer(raw, "<i2", count=n).astype(np.float32) / 32768.0
    elif enc == "s32":
        a = (np.frombuffer(raw, "<i4", count=n).astype(np.float64) / 2147483648.0).astype(np.float32)
    elif enc == "u8":
        a = (np.frombuffer(raw, "u1", count=n).astype(np.float32) - 128.0) / 128.0
    elif enc == "s24":
        b = np.frombuffer(raw, np.uint8, count=n * 3).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 0x800000, v - 0x1000000, v)
        a = v.astype(np.float32) / 8388608.0
    else:
        raise RuntimeError("进程回环开不了：解码遇到未知编码 %r" % enc)
    return a.reshape(-1, ch)


# --------------------------------------------------------------------------- 进程查找
def _iter_processes():
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise RuntimeError("进程回环开不了：CreateToolhelp32Snapshot 失败（err=%d）"
                           % ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            yield int(entry.th32ProcessID), str(entry.szExeFile)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)


def _window_pids() -> set:
    """有「可见 + 有标题 + 非工具窗」顶层窗口的 PID 集合（用来优先挑真身）。"""
    found = set()

    def _cb(hwnd, lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            if _user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            if _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                found.add(int(pid.value))
        except Exception:                                    # noqa: BLE001
            pass
        return True

    proc = _EnumWindowsProc(_cb)
    _user32.EnumWindows(proc, 0)
    return found


def list_pids(name: str = "VRChat.exe") -> list:
    """所有同名进程 [(pid, exe)]，方便排查「开了两个」。"""
    want = str(name).strip().lower()
    me = int(_k32.GetCurrentProcessId())
    return [(pid, exe) for pid, exe in _iter_processes()
            if exe.strip().lower() == want and pid not in (0, 4, me)]


def find_pid(name: str = "VRChat.exe") -> int:
    """按进程名找 PID：优先有主窗口的（VRChat 会留后台启动器/子进程）。"""
    hits = list_pids(name)
    if not hits:
        total = sum(1 for _ in _iter_processes())
        raise RuntimeError("进程回环开不了：没找到 %s（枚举了 %d 个进程，都不叫这个名字）"
                           % (name, total))
    wins = _window_pids()
    with_win = [h for h in hits if h[0] in wins]
    pid, _exe = (with_win or hits)[0]
    return int(pid)


def _proc_name(pid: int):
    """PID → 可执行文件名（打不开时返回 None，不抛）。"""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return None
    finally:
        _k32.CloseHandle(h)


# --------------------------------------------------------------------------- 主体
class ProcLoop:
    """只采一个进程树的声音（Windows 进程回环）。

    pid 给了就用 pid；否则按 name 枚举进程（优先有可见主窗口的）。
    on_pcm(pcm: np.ndarray, rate: int)：float32 [-1,1]，默认单声道（mono=False 给 (n, ch)）。
    stats：{'frames','dur','drops','blocks','silent','errs','peak'}。
    """

    def __init__(self, pid=None, name="VRChat.exe", on_pcm=None, rate=48000, channels=2,
                 log=None, mono=True):
        self.pid_arg = int(pid) if pid else None
        self.name = str(name) if name else ""
        self.on_pcm = on_pcm
        self.rate = int(rate) if rate else 48000
        self.channels = max(1, int(channels or 2))
        self.mono = bool(mono)
        self.log = log if log is not None else util.log

        self._pid = None
        self._target = ""
        self._th = None
        self._lk = threading.RLock()
        self._ready = threading.Event()
        self._stop_evt = threading.Event()
        self._coinit = False
        self._err = None
        self.last_error = None

        self._client = None          # IAudioClient
        self._cap = None             # IAudioCaptureClient
        self._evt = None             # 采集事件
        self._wake = None            # 唤醒线程的事件（手动复位）
        self._handler = None
        self._fmt = None
        self._buf_frames = 0
        self.init_format = ""
        self._halted = False

        self._frames = 0
        self._drops = 0
        self._blocks = 0
        self._silent = 0
        self._cb_err = 0
        self._peak = 0.0

    # -------------------------------------------------------------- 只读属性
    @property
    def pid(self):
        return self._pid

    @property
    def running(self):
        th = self._th
        return bool(th is not None and th.is_alive() and not self._err)

    @property
    def stats(self):
        r = float(self.rate or 48000)
        return {"frames": int(self._frames),
                "dur": round(self._frames / r, 3) if r else 0.0,
                "drops": int(self._drops),
                "blocks": int(self._blocks),
                "silent": int(self._silent),
                "errs": int(self._cb_err),
                "peak": round(float(self._peak), 4)}

    # -------------------------------------------------------------- 生命周期
    def start(self):
        with self._lk:
            if self._th is not None and self._th.is_alive() and not self._err:
                return self
            build = int(getattr(sys.getwindowsversion(), "build", 0) or 0)
            if build and build < 19041:
                raise RuntimeError("进程回环开不了：需要 Windows 10 2004（build 19041）以上，"
                                   "本机 build %d" % build)
            self._pid = self._resolve_pid()
            self._err = None
            self.last_error = None
            self._halted = False
            self._ready.clear()
            self._stop_evt.clear()
            self._frames = self._drops = self._blocks = self._silent = self._cb_err = 0
            self._peak = 0.0
            self._th = threading.Thread(target=self._thread_main, name="procloop-pid%d" % self._pid,
                                        daemon=True)
            self._th.start()
        if not self._ready.wait(25.0):
            self.stop()
            raise RuntimeError("进程回环开不了：激活超时（25 s 内没有拿到 IAudioClient，"
                               "目标进程或音频服务无响应）")
        if self._err:
            err = self._err
            self.stop()
            raise RuntimeError(err)
        return self

    def stop(self):
        th = self._th
        self._stop_evt.set()
        w = self._wake
        if w:
            try:
                _k32.SetEvent(w)
            except Exception:                                # noqa: BLE001
                pass
        if th is not None and th is not threading.current_thread():
            th.join(15.0)
        with self._lk:
            self._th = None
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    # -------------------------------------------------------------- 内部
    def _log(self, msg, lv="info"):
        try:
            fn = self.log
            if fn is None:
                return
            try:
                fn("[procloop] " + msg, lv, "audio")
            except TypeError:
                fn("[procloop] " + msg)
        except Exception:                                    # noqa: BLE001
            pass

    def _resolve_pid(self) -> int:
        cd = 0
        if self.pid_arg:
            pid = int(self.pid_arg)
            h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE, False, pid)
            err = ctypes.get_last_error()
            if not h:
                if err == 87:                                # ERROR_INVALID_PARAMETER
                    raise RuntimeError("进程回环开不了：PID %d 不存在（OpenProcess 0x57）" % pid)
                self._log("PID %d 打不开（GetLastError=%d），仍尝试激活" % (pid, err), "warn")
            else:
                _k32.CloseHandle(h)
            self._target = _proc_name(pid) or ""
        else:
            pid = find_pid(self.name)
            self._target = _proc_name(pid) or self.name
        # 会话检查：进程回环只能采「同一会话」的进程
        ss = wintypes.DWORD(0)
        ns = wintypes.DWORD(0)
        if (_k32.ProcessIdToSessionId(pid, ctypes.byref(ss))
                and _k32.ProcessIdToSessionId(int(_k32.GetCurrentProcessId()), ctypes.byref(ns))
                and ss.value != ns.value):
            self._log("目标进程在会话 %d，本进程在会话 %d —— 跨会话采不到声音"
                      % (ss.value, ns.value), "warn")
        self._log("目标进程 PID %d %s" % (pid, self._target or self.name), "info")
        return int(pid)

    def _thread_main(self):
        try:
            hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
            if hr not in (S_OK, S_FALSE):
                raise _fail("CoInitializeEx(MTA) 失败", hr)
            self._coinit = True
            try:
                self._setup()
            except Exception as e:                           # noqa: BLE001
                self._err = str(e)
                self._ready.set()
                return
            self._ready.set()
            if not self._stop_evt.is_set():
                self._capture_loop()
        except Exception as e:                               # noqa: BLE001
            self._err = str(e) if str(e).startswith("进程回环开不了") else (
                "进程回环开不了：%s" % e)
            self._ready.set()
        finally:
            self._teardown()
            if self._coinit:
                try:
                    _ole32.CoUninitialize()
                except Exception:                            # noqa: BLE001
                    pass
                self._coinit = False

    def _setup(self):
        pid = int(self._pid)
        # 1) ActivateAudioInterfaceAsync(VAD\Process_Loopback, IID_IAudioClient, 进程参数)
        handler = _CompletionHandler()
        self._handler = handler
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_PROCESS_TREE
        pv = PROPVARIANT()
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.POINTER(ctypes.c_byte))
        op = ctypes.c_void_p()
        hr = _mmdev.ActivateAudioInterfaceAsync(VAD_PROCESS_LOOPBACK, ctypes.byref(IID_IAudioClient),
                                                ctypes.byref(pv), handler.ptr, ctypes.byref(op))
        if hr != S_OK:
            raise _fail("ActivateAudioInterfaceAsync 失败", hr)
        try:
            rc = _k32.WaitForSingleObject(handler.event, 10000)
            if rc == WAIT_FAILED:
                raise RuntimeError("进程回环开不了：等待激活回调失败（err=%d）"
                                   % ctypes.get_last_error())
            if rc == WAIT_TIMEOUT:
                raise RuntimeError("进程回环开不了：激活回调 10 s 没回来（目标进程无响应）")
        finally:
            if op:
                try:
                    _vcall(op, 2, ctypes.c_ulong, [])
                except Exception:                            # noqa: BLE001
                    pass
        if int(handler.hr) != S_OK:
            raise _fail("激活回调返回失败", int(handler.hr))
        if not handler.iface:
            raise _fail("激活成功但没拿到 IAudioClient", E_POINTER)
        client = _IAudioClient(handler.iface)
        self._client = client

        # 2) Initialize：进程回环要求 LOOPBACK|EVENTCALLBACK，缓冲期 ≥ 20 ms
        req_buf, req_fmt = _build_fmt(self.rate, self.channels, 32, "f32")
        alt_buf, alt_fmt = _build_fmt(44100, 2, 16, "s16")
        tries = [
            ("float32 %dHz/%dch + AUTOCONVERTPCM 20ms" % (self.rate, self.channels),
             req_buf, req_fmt, _FLAGS_AUTO, 200000),
            ("float32 %dHz/%dch 无 AUTOCONVERTPCM 20ms" % (self.rate, self.channels),
             req_buf, req_fmt, _FLAGS_BASE, 200000),
            ("float32 %dHz/%dch + AUTOCONVERTPCM 100ms" % (self.rate, self.channels),
             req_buf, req_fmt, _FLAGS_AUTO, 1000000),
            ("float32 %dHz/%dch 无 AUTOCONVERTPCM 100ms" % (self.rate, self.channels),
             req_buf, req_fmt, _FLAGS_BASE, 1000000),
            ("pcm16 44100Hz/2ch 20ms", alt_buf, alt_fmt, _FLAGS_BASE, 200000),
            ("pcm16 44100Hz/2ch 100ms", alt_buf, alt_fmt, _FLAGS_BASE, 1000000),
        ]
        chosen = None
        last = None
        for label, buf, fmt, flags, dur in tries:
            hr = client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, dur, 0,
                                   ctypes.addressof(buf), None)
            if hr == S_OK:
                chosen = (label, fmt)
                break
            last = (label, hr)
            self._log("Initialize 失败 %s（%s）" % (label, _hr_text(hr)), "warn")
            if hr == 0x88890019:                             # BUFFER_SIZE_NOT_ALIGNED
                _hr, frames = client.GetBufferSize()
                if frames > 0 and fmt["rate"]:
                    dur = int(1e7 * frames / fmt["rate"] + 0.5)
                    hr = client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, dur, 0,
                                           ctypes.addressof(buf), None)
                    if hr == S_OK:
                        chosen = (label + "（对齐 %d 帧）" % frames, fmt)
                        break
                    last = (label, hr)

        if chosen is None:
            # 3) 最后兜底：问虚拟设备要 mix format（AUTOCONVERTPCM 不生效时它最稳）
            hr, mix = client.GetMixFormat()
            if hr == S_OK and mix:
                try:
                    head = ctypes.string_at(mix, 18)          # 只读 18 字节，别按下标读结构体
                    cb = struct.unpack("<H", head[16:18])[0]
                    raw = ctypes.string_at(mix, 18 + int(cb))
                    fmt = _parse_fmt(raw)
                    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
                    for dur in (200000, 1000000):
                        hr2 = client.Initialize(AUDCLNT_SHAREMODE_SHARED, _FLAGS_BASE, dur, 0,
                                                ctypes.addressof(buf), None)
                        if hr2 == S_OK:
                            chosen = ("mix format %dHz/%dch/%s %dms"
                                      % (fmt["rate"], fmt["ch"], fmt["enc"], dur // 10000), fmt)
                            break
                        last = ("mix format", hr2)
                finally:
                    _ole32.CoTaskMemFree(mix)
            else:
                self._log("GetMixFormat 也失败（%s）" % _hr_text(hr), "warn")

        if chosen is None:
            label, lhr = last or ("Initialize", E_FAIL)
            raise _fail("Initialize 全部尝试失败，最后一次 %s" % label, lhr)
        self.init_format = chosen[0]
        self._fmt = chosen[1]

        # 4) 事件 + 采集接口 + 启动
        self._evt = _k32.CreateEventW(None, False, False, None)   # 自动复位
        if not self._evt:
            raise RuntimeError("进程回环开不了：CreateEventW 失败（err=%d）"
                               % ctypes.get_last_error())
        hr = client.SetEventHandle(self._evt)
        if hr != S_OK:
            raise _fail("SetEventHandle 失败", hr)
        hr, cap = client.GetService(IID_IAudioCaptureClient)
        if hr != S_OK or not cap:
            raise _fail("GetService(IAudioCaptureClient) 失败", hr)
        self._cap = _IAudioCaptureClient(cap)
        hr, frames = client.GetBufferSize()
        self._buf_frames = frames
        hr = client.Start()
        if hr != S_OK:
            raise _fail("IAudioClient::Start 失败", hr)
        self._wake = _k32.CreateEventW(None, True, False, None)   # 手动复位，stop 用它打断等待
        self._log("进程回环开：PID %d %s | %s | buffer=%d 帧 | %s"
                  % (self._pid, self._target or self.name, self.init_format,
                     self._buf_frames, "mono" if self.mono else "ch=%d" % self._fmt["ch"]), "info")

    def _capture_loop(self):
        while not self._stop_evt.is_set() and not self._halted:
            waiters = [self._evt]
            rc = _k32.WaitForSingleObject(self._evt, 20)
            if rc == WAIT_FAILED:
                self.last_error = "WaitForSingleObject 失败（err=%d）" % ctypes.get_last_error()
                self._log(self.last_error, "err")
                break
            if self._stop_evt.is_set():
                break
            try:
                self._pump()
            except Exception as e:                           # noqa: BLE001
                self._cb_err += 1
                self._log("采集循环异常：%r" % (e,), "err")
                self.last_error = repr(e)
                break
        # 收尾：把已经排队的包抽干（stop 后还有一点尾巴）
        try:
            self._pump()
        except Exception:                                    # noqa: BLE001
            pass

    def _pump(self):
        cap = self._cap
        if cap is None:
            return 0
        total = 0
        for _ in range(200):                                 # 单次最多抽 200 包，防死循环
            hr, nxt = cap.GetNextPacketSize()
            if hr != S_OK:
                self.last_error = "GetNextPacketSize 失败（%s）" % _hr_text(hr)
                self._log(self.last_error, "err")
                self._halted = True
                return total
            if nxt <= 0:
                return total
            hr, data, frames, flags = cap.GetBuffer()
            if hr != S_OK:
                self.last_error = "GetBuffer 失败（%s）" % _hr_text(hr)
                self._log(self.last_error, "err")
                self._halted = True
                return total
            try:
                if frames > 0:
                    self._deliver(data, frames, flags)
            except Exception as e:                           # noqa: BLE001
                self._cb_err += 1
                if self._cb_err <= 3:
                    self._log("处理音频包失败：%r" % (e,), "warn")
            finally:
                cap.ReleaseBuffer(frames)
            total += frames
        return total

    def _deliver(self, data_ptr, frames: int, flags: int):
        fmt = self._fmt
        if fmt is None:
            return
        ch = int(fmt["ch"])
        if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
            self._drops += 1
        if (flags & AUDCLNT_BUFFERFLAGS_SILENT) or not data_ptr:
            x = np.zeros((frames, ch), np.float32)
            self._silent += 1
        else:
            raw = ctypes.string_at(data_ptr, frames * int(fmt["block"]))
            x = _decode(raw, fmt)
        if x.shape[0] == 0:
            return
        if ch > 1:
            x = x.mean(axis=1) if self.mono else x
        else:
            x = x.reshape(-1)
        if int(fmt["rate"]) != self.rate and x.size:
            if x.ndim == 1:
                x = util.resample(x, int(fmt["rate"]), self.rate)
            else:
                x = np.stack([util.resample(x[:, c], int(fmt["rate"]), self.rate)
                              for c in range(x.shape[1])], axis=1)
        x = np.ascontiguousarray(x, dtype=np.float32)
        np.clip(x, -1.0, 1.0, out=x)
        n = int(x.shape[0])
        self._frames += n
        self._blocks += 1
        if x.size:
            p = float(np.max(np.abs(x)))
            if p > self._peak:
                self._peak = p
        if callable(self.on_pcm):
            try:
                self.on_pcm(x, self.rate)
            except Exception as e:                            # noqa: BLE001
                self._cb_err += 1
                if self._cb_err <= 3:
                    self._log("on_pcm 回调抛错：%r" % (e,), "warn")

    def _teardown(self):
        client = self._client
        cap = self._cap
        try:
            if client is not None:
                client.Stop()
        except Exception:                                    # noqa: BLE001
            pass
        for obj in (cap, client):
            if obj is not None:
                try:
                    obj.Release()
                except Exception:                            # noqa: BLE001
                    pass
        self._cap = None
        self._client = None
        for attr in ("_evt", "_wake"):
            h = getattr(self, attr, None)
            if h:
                try:
                    _k32.CloseHandle(h)
                except Exception:                            # noqa: BLE001
                    pass
                setattr(self, attr, None)
        handler = self._handler
        self._handler = None
        if handler is not None:
            handler.close()
