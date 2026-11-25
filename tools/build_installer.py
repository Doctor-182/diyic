# -*- coding: utf-8 -*-
r"""打 Windows 安装包：先确保 dist\exe\VRDub 是最新的，再调 Inno Setup 出 setup.exe。

Inno Setup 位置：D:\tools\InnoSetup\ISCC.exe（或环境变量 VRDUB_ISCC / 默认安装路径）。
用法：
    py -3.12 tools\build_installer.py             # 用现有产物打安装包
    py -3.12 tools\build_installer.py --rebuild   # 先重新跑 build_exe.py
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vrdub import version                      # noqa: E402

CANDIDATES = [
    os.environ.get("VRDUB_ISCC", ""),
    r"D:\tools\InnoSetup\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]
ISS = os.path.join(ROOT, "packaging", "vrdub.iss")
OUTDIR = os.path.join(ROOT, "dist", "installer")


def find_iscc() -> str:
    for c in CANDIDATES:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("找不到 ISCC.exe（Inno Setup 6）。设 VRDUB_ISCC 或装到默认路径。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="先重新构建 onedir 产物")
    a = ap.parse_args(argv)
    if a.rebuild:
        rc = subprocess.call([sys.executable, os.path.join(HERE, "build_exe.py")], cwd=ROOT)
        if rc != 0:
            return rc
    app_dir = os.path.join(ROOT, "dist", "exe", version and "VRDub")
    if not os.path.isfile(os.path.join(app_dir, "VRDub.exe")):
        raise SystemExit("产物不存在：%s（先跑 tools\build_exe.py）" % app_dir)
    os.makedirs(OUTDIR, exist_ok=True)
    for old in glob.glob(os.path.join(OUTDIR, "VRDub-*-setup.exe")):
        try:
            os.remove(old)
        except OSError:
            pass
    iscc = find_iscc()
    args = [
        iscc,
        "/DAppVer=" + ".".join(version.VERSION.split(".")[:3]),
        "/DAppVerFull=" + version.short(),
        "/DProjRoot=" + ROOT,
        "/Qp",
        ISS,
    ]
    t0 = time.time()
    print("[installer] %s" % " ".join(args), flush=True)
    rc = subprocess.call(args, cwd=ROOT)
    print("[installer] ISCC 退出码 %s，用时 %.1f s" % (rc, time.time() - t0), flush=True)
    if rc != 0:
        return rc
    made = sorted(glob.glob(os.path.join(OUTDIR, "VRDub-*-setup.exe")))
    for f in made:
        print("[installer] %s  %.1f MB" % (f, os.path.getsize(f) / 1048576.0), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
