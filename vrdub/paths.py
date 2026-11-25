# -*- coding: utf-8 -*-
"""路径策略：程序目录只读，用户数据一律放到 %LOCALAPPDATA%\VRDub。

为什么要有这一层：
- 老版本把 settings.json / 日志 / 录到的语料全写在程序目录下的 data\ 和 out\。
  装到 C:\Program Files 之后那块是只读的，非管理员写不进去，程序会静默丢配置。
- 换台机器、换个用户名，程序目录也不该变；用户数据跟着 Windows 用户走才合理。

约定（可用环境变量覆盖，方便调试和多开）：
    VRDUB_HOME   默认 %LOCALAPPDATA%\VRDub        —— 配置/日志/缓存/录音的根
    VRDUB_ROOT   默认 程序所在目录（源码目录或安装目录）

目录结构：
    <home>\config\settings.json     界面里的所有开关（热改后立刻落盘）
    <home>\config\.env.local        密钥与端点
    <home>\logs\*.log               运行日志（8 MB × 1 份自动轮转）
    <home>\cache\                   模型/OCR 的中间产物
    <home>\out\                     截图、语料、bench 报告（调试用，可整目录删）
    <home>\recordings\              保留的现场录音
"""
from __future__ import annotations

import os
import shutil
import sys

APP_DIR_NAME = "VRDub"


def _exe_dir() -> str:
    """冻结成 exe 时是 exe 所在目录，源码运行时是本文件上两级（项目根）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _local_appdata() -> str:
    v = os.environ.get("LOCALAPPDATA")
    if v:
        return v
    return os.path.join(os.path.expanduser("~"), "AppData", "Local")


ROOT = os.path.abspath(os.environ.get("VRDUB_ROOT") or _exe_dir())
HOME = os.path.abspath(os.environ.get("VRDUB_HOME") or os.path.join(_local_appdata(), APP_DIR_NAME))

CONFIG_DIR = os.path.join(HOME, "config")
LOG_DIR = os.path.join(HOME, "logs")
CACHE_DIR = os.path.join(HOME, "cache")
OUT = os.path.join(HOME, "out")
REC_DIR = os.path.join(HOME, "recordings")

SETTINGS = os.path.join(CONFIG_DIR, "settings.json")
ENV_FILE = os.path.join(CONFIG_DIR, ".env.local")

# 源码目录里的老位置（只读兜底 + 首次启动的迁移来源）
LEGACY_SETTINGS = os.path.join(ROOT, "data", "settings.json")
LEGACY_ENV = os.path.join(ROOT, ".env.local")
LEGACY_OUT = os.path.join(ROOT, "out")


def ensure() -> None:
    """把用户目录建出来。写不进去就退回到程序目录（便携版/绿色版场景）。"""
    global HOME, CONFIG_DIR, LOG_DIR, CACHE_DIR, OUT, REC_DIR, SETTINGS, ENV_FILE
    for d in (CONFIG_DIR, LOG_DIR, CACHE_DIR, OUT, REC_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            _fallback_to_root()
            return


def _fallback_to_root() -> None:
    """%LOCALAPPDATA% 不可写（极少见：被策略锁掉）时，整体退回程序目录。"""
    global HOME, CONFIG_DIR, LOG_DIR, CACHE_DIR, OUT, REC_DIR, SETTINGS, ENV_FILE
    HOME = ROOT
    CONFIG_DIR = os.path.join(ROOT, "data")
    LOG_DIR = os.path.join(ROOT, "out")
    CACHE_DIR = os.path.join(ROOT, "cache")
    OUT = os.path.join(ROOT, "out")
    REC_DIR = os.path.join(ROOT, "recordings")
    SETTINGS = os.path.join(CONFIG_DIR, "settings.json")
    ENV_FILE = os.path.join(ROOT, ".env.local")
    for d in (CONFIG_DIR, LOG_DIR, CACHE_DIR, OUT, REC_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def _copy_if_absent(src: str, dst: str) -> bool:
    if not (src and os.path.exists(src)) or os.path.exists(dst):
        return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def migrate() -> list:
    """老版本（配置写在程序目录）→ 用户目录。返回做了哪些迁移，用于打日志。"""
    done = []
    if _copy_if_absent(LEGACY_SETTINGS, SETTINGS):
        done.append("settings.json")
    if _copy_if_absent(LEGACY_ENV, ENV_FILE):
        done.append(".env.local")
    return done


def describe() -> dict:
    return {"root": ROOT, "home": HOME, "config": CONFIG_DIR, "logs": LOG_DIR,
            "cache": CACHE_DIR, "out": OUT, "settings": SETTINGS, "env": ENV_FILE}


def has_settings() -> bool:
    return os.path.exists(SETTINGS)
