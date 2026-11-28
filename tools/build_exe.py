# -*- coding: utf-8 -*-
r"""把源码打成 Windows 程序目录（PyInstaller onedir），并补齐模型、做冒烟自检。

为什么是 onedir 而不是 onefile：
- onefile 每次启动要把 300 MB 解压到 %TEMP%，首启 5–10 s、磁盘翻倍、还容易被杀软误判；
- onedir 是「VRDub.exe + _internal\ + models\」并列，双击就走，冷启动 2 s 内。

产物结构（dist\exe\VRDub\）：
    VRDub.exe            主程序（窗口子系统，没有控制台黑框）
    _internal\           Python 运行时、8 个三方库、vrdub\web 静态页
    models\sensevoice\  本地识别模型（228 MB）
    models\ocr\         屏幕 OCR 模型（24 MB）

用法：
    py -3.12 tools\build_exe.py                # 完整构建 + 冒烟
    py -3.12 tools\build_exe.py --skip-build   # 只补模型/冒烟（改完打包脚本时省时间）
    py -3.12 tools\build_exe.py --no-smoke    # 只构建
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vrdub import version                      # noqa: E402

DIST = os.path.join(ROOT, "dist", "exe")
BUILD = os.path.join(ROOT, "build", "pyi")
NAME = "VRDub"
APP_DIR = os.path.join(DIST, NAME)
SMOKE_PORT = 8912          # 避开平时占着的 8899

# 模型清单：必需（装进安装包）；anime-whisper 732 MB 是可选备用，不进来
MODEL_FILES = [
    "models/sensevoice/model.int8.onnx",
    "models/sensevoice/tokens.txt",
    "models/ocr/japan_rec.onnx",
    "models/ocr/japan_dict.txt",
    "models/ocr/ch_PP-OCRv4_det_infer.onnx",
    "models/ocr/ch_PP-OCRv4_rec_infer.onnx",
    "models/ocr/config_ch.yaml",
    "models/ocr/config_ja.yaml",
    "models/spk/voxceleb.onnx",
]

# GGUF 档（tools/fetch_asr_models.py 下到 models/asr）——只在 --with-gguf 时进包
GGUF_FILES = [
    "Qwen3-ASR-1.7B-Q5_K_M.gguf",
    "Qwen3-ASR-0.6B-Q8_0.gguf",
    "Fun-ASR-Nano-2512-Q8_0.gguf",
]

HIDDEN = [
    # 四个 Qt 子程序是按名字（--child NAME）动态导入的，静态分析看不到，必须显式点名，
    # 否则装完之后 hud/danmaku/overlay 会 ModuleNotFoundError，界面上只看到"浮窗起不来"。
    "vrdub.hud", "vrdub.danmaku", "vrdub.ocr_overlay", "vrdub.region_picker",
    "vrdub.ball", "vrdub.spkui",
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "onnxruntime", "onnxruntime.capi._pybind_state",
    "rapidocr_onnxruntime", "cv2", "yaml", "PIL", "PIL.Image",
    "pyaudiowpatch", "numpy",
]

EXCLUDE = [
    "matplotlib", "pandas", "scipy", "tkinter", "PyQt5", "PyQt6", "PySide2",
    "IPython", "pytest", "notebook", "jupyter", "setuptools", "pip", "wheel",
    "sqlite3", "test", "unittest", "pydoc_data", "lib2to3",
]


def log(msg: str) -> None:
    print("[build] %s" % msg, flush=True)


def version_file() -> str:
    """生成 VSVersionInfo：资源管理器里能看到公司/版本/说明，安装器也读它。"""
    v = [int(x) for x in version.VERSION.split(".")] + [0, 0]
    v = v[:4]
    path = os.path.join(BUILD, "version_info.txt")
    os.makedirs(BUILD, exist_ok=True)
    body = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(v)s, prodvers=%(v)s, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
        StringStruct('CompanyName', %(company)r),
        StringStruct('FileDescription', %(desc)r),
        StringStruct('FileVersion', %(ver)r),
        StringStruct('InternalName', 'VRDub'),
        StringStruct('LegalCopyright', %(legal)r),
        StringStruct('OriginalFilename', 'VRDub.exe'),
        StringStruct('ProductName', %(name)r),
        StringStruct('ProductVersion', %(ver)r)])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % {
        "v": tuple(v),
        "company": "VRDub",
        "desc": version.APP_NAME + " · VRChat 实时语音同传",
        "ver": "%d.%d.%d.%d" % tuple(v),
        "legal": "个人项目 · 仅供朋友内测",
        "name": version.APP_NAME,
    }
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def run_pyinstaller() -> None:
    import PyInstaller.__main__ as pyi
    args = [
        "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", NAME,
        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", BUILD,
        "--paths", ROOT,
        "--paths", os.path.join(ROOT, ".pylibs"),
        "--add-data", os.path.join(ROOT, "vrdub", "web") + ";vrdub/web",
        "--collect-all", "rapidocr_onnxruntime",
        "--icon", os.path.join(ROOT, "packaging", "vrdub.ico"),
        "--version-file", version_file(),
        "--log-level", "WARN",
    ]
    for h in HIDDEN:
        args += ["--hidden-import", h]
    for e in EXCLUDE:
        args += ["--exclude-module", e]
    args.append(os.path.join(ROOT, "packaging", "entry.py"))
    t0 = time.time()
    log("PyInstaller 开始：%s %s" % (NAME, version.short()))
    pyi.run(args)
    log("PyInstaller 完成，用时 %.1f s" % (time.time() - t0))


def copy_models() -> None:
    if not os.path.isdir(APP_DIR):
        raise SystemExit("先构建：%s 不存在" % APP_DIR)
    total = 0
    for rel in MODEL_FILES:
        src = os.path.join(ROOT, rel.replace("/", os.sep))
        dst = os.path.join(APP_DIR, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            raise SystemExit("缺模型文件：%s（先跑 tools/fetch_models.py）" % rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        total += os.path.getsize(dst)
    log("模型就位 %d 个文件 / %.1f MB" % (len(MODEL_FILES), total / 1048576.0))
    # 许可证与说明随包走，装机后能找到
    for rel in ("LICENSE", "CHANGELOG.md"):
        src = os.path.join(ROOT, rel)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(APP_DIR, rel))


def copy_asr_runtime() -> None:
    r"""可选：把 GGUF 识别档（transcribe_cpp + 3 个 gguf 模型）也装进产物。

    默认不装 —— 加一起 4 GB，安装包会从 264 MB 涨到 1.5 GB 以上。
    需要「朋友那台机器不联网也能用 Qwen3-ASR」时才加 --with-gguf。

    运行库必须放进 _internal（冻结后它就是 sys.path 的一员，import 找得到）：
    transcribe_cpp 用 ctypes 加载 _native 里的 dll，且 ggml-cuda.dll 只认 PATH，
    所以 asr_gguf._env_setup() 会把该目录前置进 PATH。
    """
    src_pkg = os.path.join(ROOT, ".pylibs", "transcribe_cpp")
    src_nat = os.path.join(ROOT, ".pylibs", "transcribe_cpp_native_cu12")
    if not (os.path.isdir(src_pkg) and os.path.isdir(src_nat)):
        raise SystemExit("缺 transcribe_cpp 运行库（先跑 tools/fetch_asr_models.py --runtime）")
    internal = os.path.join(APP_DIR, "_internal")
    os.makedirs(internal, exist_ok=True)
    n = 0
    for src in (src_pkg, src_nat):
        dst = os.path.join(internal, os.path.basename(src))
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        n += dir_size(dst)
    log("GGUF 运行库就位 %.1f MB" % n)
    m = 0
    for fn in GGUF_FILES:
        src = os.path.join(ROOT, "models", "asr", fn)
        if not os.path.isfile(src):
            raise SystemExit("缺模型 %s（先跑 tools/fetch_asr_models.py --download）" % fn)
        dst = os.path.join(APP_DIR, "models", "asr", fn)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        m += os.path.getsize(dst)
    log("GGUF 模型就位 %d 个 / %.1f MB" % (len(GGUF_FILES), m / 1048576.0))


def dir_size(path: str) -> float:
    n = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return n / 1048576.0


def smoke() -> bool:
    """真空跑一次：独立用户目录 + 关浮窗，等 /api/health，再收尸。"""
    exe = os.path.join(APP_DIR, "VRDub.exe")
    if not os.path.isfile(exe):
        log("没有 %s" % exe)
        return False
    home = os.path.join(ROOT, "build", "smoke_home")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env["VRDUB_HOME"] = home
    env["VRDUB_PORT"] = str(SMOKE_PORT)
    log("冒烟：启动 %s（用户目录 %s）" % (exe, home))
    p = subprocess.Popen([exe, "--no-hud"], cwd=APP_DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < 90:
        if p.poll() is not None:
            log("进程提前退出，code=%s" % p.returncode)
            break
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % SMOKE_PORT, timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
            log("健康检查通过：%s（%.1f s）" % (data, time.time() - t0))
            ok = True
            break
        except Exception:                       # noqa: BLE001
            time.sleep(1.0)
    try:
        p.terminate()
        p.wait(timeout=10)
    except Exception:                           # noqa: BLE001
        try:
            p.kill()
        except Exception:                       # noqa: BLE001
            pass
    srv = os.path.join(home, "out", "srv.log")
    if os.path.isfile(srv):
        with open(srv, "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-1500:]
        log("srv.log 尾部：\n%s" % tail)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true", help="跳过 PyInstaller，只补模型+冒烟")
    ap.add_argument("--no-smoke", action="store_true")
    ap.add_argument("--with-gguf", action="store_true",
                    help="把 GGUF 识别档（运行库 1 GB + 模型 3 GB）也装进产物")
    a = ap.parse_args(argv)
    if not a.skip_build:
        run_pyinstaller()
    copy_models()
    if a.with_gguf:
        copy_asr_runtime()
    log("产物体积 %.1f MB（%s）" % (dir_size(APP_DIR), APP_DIR))
    if not a.no_smoke:
        if not smoke():
            log("冒烟失败")
            return 1
        log("冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
