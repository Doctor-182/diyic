# -*- coding: utf-8 -*-
"""版本与身份：一处定义，页面、接口、日志、打包脚本、安装器都从这里取。

为什么单独一个文件：以前版本号是 "1.0" 硬写在 state() 里（app.py），
打包脚本和安装器各自又写死一遍日期——用户报 bug 时没法对齐"他装的是哪一版"。
改这里就够了，别的地方一律 import。
"""
from __future__ import annotations

import os
import sys

APP_ID = "VRDub"
APP_NAME = "VRDub 无言势"
CODENAME = "无言势"
VERSION = "0.9.0"          # 语义化：主.次.补丁；发版前手动抬
CHANNEL = "dev"            # dev | beta | stable
BUILD = "20261125"         # 打包脚本会用构建时间覆盖

def build_time() -> str:
    """打包后 exe 的 mtime 当构建时间；源码跑就用 version.py 自己的 mtime。"""
    p = getattr(sys, "frozen", None) and sys.executable or __file__
    try:
        import time
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    except Exception:                       # noqa: BLE001
        return "-"

def short() -> str:
    """页面/托盘提示用：0.9.0-dev。"""
    return f"{VERSION}-{CHANNEL}" if CHANNEL != "stable" else VERSION

def full() -> str:
    """关于页/日志头用。"""
    return f"{APP_NAME} {VERSION} ({CHANNEL}) build {BUILD} · {build_time()}"

def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
