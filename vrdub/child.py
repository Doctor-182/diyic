# -*- coding: utf-8 -*-
"""子进程调度：源码运行时是「python + 脚本」，冻结成 exe 之后是「VRDub.exe --child NAME」。

为什么要有这一层：
- hud / danmaku / ocr_overlay / region_picker 原先都是 `python.exe vrdub/xxx.py` 起的；
- PyInstaller 打包后 sys.executable 变成 VRDub.exe，再拿脚本路径去启动，等于让 exe 去
  "执行某个 .py"，必然起不来（不报错、不闪窗，静默失败）；
- 所以统一走一个入口：入口里先看 --child NAME，命中就直接跑那个子程序，否则才走主程序。
  这样源码一套、安装版一套，行为一致。

对外：
    exists(name) -> bool         子程序在不在（源码看文件，冻结看白名单）
    argv(name, extra) -> list    启动参数，可直接丢给 subprocess.Popen
    run_if_child(argv) -> int|None   非 None 表示本进程被子程序接管，调用方立刻返回该退出码
"""
from __future__ import annotations

import importlib
import os
import sys

from . import paths, version

CREATE_NO_WINDOW = 0x08000000

# 名字 -> 模块名（导入后调 _RUNNERS 里对应入口）
CHILDREN = {
    "hud": "vrdub.hud",
    "danmaku": "vrdub.danmaku",
    "ocr_overlay": "vrdub.ocr_overlay",
    "region_picker": "vrdub.region_picker",
}

_RUNNERS = {
    "hud": lambda mod, rest: mod.main(),
    "danmaku": lambda mod, rest: mod.main(),
    "ocr_overlay": lambda mod, rest: mod.Overlay({}).run(),
    "region_picker": lambda mod, rest: mod.main(rest),
}


def script_path(name: str) -> str:
    return os.path.join(paths.ROOT, "vrdub", name + ".py")


def python() -> str:
    """源码模式优先 pythonw.exe——Qt 子进程不该占控制台、更不该闪黑框。"""
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return exe


def exists(name: str) -> bool:
    if name not in CHILDREN:
        return False
    if version.frozen():
        return True
    return os.path.isfile(script_path(name))


def argv(name: str, extra=None) -> list:
    if name not in CHILDREN:
        raise KeyError(name)
    extra = [str(x) for x in (extra or [])]
    if version.frozen():
        return [sys.executable, "--child", name] + extra
    return [python(), script_path(name)] + extra


def _ensure_streams():
    """windowed 打包后 sys.stdout/stderr 是 None，print 直接 AttributeError，兜成日志文件。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        try:
            os.makedirs(paths.LOG_DIR, exist_ok=True)
            fh = open(os.path.join(paths.LOG_DIR, "child-%s.log" % name),
                      "a", encoding="utf-8", buffering=1)
        except Exception:                       # noqa: BLE001
            fh = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, fh)


def run_if_child(argv) -> "int | None":
    if "--child" not in list(argv):
        return None
    argv = list(argv)
    i = argv.index("--child")
    name = argv[i + 1] if len(argv) > i + 1 else ""
    rest = argv[i + 2:]
    _ensure_streams()
    # 子程序自己的 argparse 会去读 sys.argv（比如 danmaku 的 --style/--scale），
    # 不把 --child NAME 摘掉就会报 "unrecognized arguments: --child danmaku"。
    sys.argv = [sys.argv[0]] + rest
    if name not in CHILDREN:
        print("未知子程序：%r，可选 %s" % (name, sorted(CHILDREN)))
        return 2
    mod = importlib.import_module(CHILDREN[name])
    try:
        rc = _RUNNERS[name](mod, rest)
    except SystemExit as e:                     # 子程序自己 sys.exit()
        rc = e.code or 0
    return int(rc or 0)
