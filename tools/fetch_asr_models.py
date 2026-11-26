# -*- coding: utf-8 -*-
r"""把 GGUF 识别档装到本机（换机器、或朋友那台机器第一次跑时用）。

为什么不用 pip：transcribe_cpp 的 Windows/CUDA 运行库只在 GitHub Release 上有，
PyPI 上没有 cublas 的 win 轮子；模型也不在 pip 里。所以这里手写一遍下载 + 解包。

用法（一律走项目根；Windows 上建议用 scripts\下载识别模型.bat）：

  python tools\fetch_asr_models.py --check        # 只检查，不下载（默认）
  python tools\fetch_asr_models.py --download     # 下三个 gguf → models\asr\
  python tools\fetch_asr_models.py --runtime      # 下 transcribe_cpp 两个轮子 → .pylibs\
  python tools\fetch_asr_models.py --all          # 两样都做

网络：国内直连 huggingface 会 reset，加 --proxy http://127.0.0.1:2080；
或 --mirror https://hf-mirror.com（镜像不需要代理，但本项目实测 2.2 MB/s < 代理 10 MB/s）。

体积账单：qwen17 1.41 GB + qwen06 0.79 GB + fun 0.83 GB = 3.03 GB；
运行库两个轮子 248.7 MB，解包后 .pylibs 里约 1.03 GB。
"""
import argparse
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models", "asr")
PYLIBS = os.path.join(ROOT, ".pylibs")

HF = "https://huggingface.co"
GH = "https://github.com/handy-computer/transcribe.cpp/releases/download/v0.5.0"

# (引擎 id, HF 仓库, 文件名, 大约 MB)  —— 与 vrdub/asr_gguf.py 的 ENGINES 一一对应
GGUF = [
    ("qwen17", "handy-computer/Qwen3-ASR-1.7B-gguf", "Qwen3-ASR-1.7B-Q5_K_M.gguf", 1447),
    ("qwen06", "handy-computer/Qwen3-ASR-0.6B-gguf", "Qwen3-ASR-0.6B-Q8_0.gguf", 811),
    ("fun", "handy-computer/Fun-ASR-Nano-2512-gguf", "Fun-ASR-Nano-2512-Q8_0.gguf", 850),
]

WHEELS = [
    "transcribe_cpp-0.5.0-py3-none-any.whl",
    "transcribe_cpp_native_cu12-0.5.0-py3-none-win_amd64.whl",
]


def mb(n):
    return "%.1f MB" % (n / (1024.0 * 1024.0))


def opener(proxy=""):
    if proxy:
        h = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(h)
    return urllib.request.build_opener()


def fetch(url, dst, proxy="", expect=0):
    r"""下载到 dst（先写 .part，完成再改名：断线不会留下半个文件当成品）。"""
    op = opener(proxy)
    req = urllib.request.Request(url, headers={"User-Agent": "VRDub-fetch/1.0"})
    t0 = time.time()
    got = 0
    tmp = dst + ".part"
    with op.open(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or expect or 0)
        with open(tmp, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if total:
                    pct = got * 100.0 / total
                    speed = got / max(0.5, time.time() - t0) / (1024.0 * 1024.0)
                    sys.stdout.write("\r    %5.1f%%  %s / %s  %.1f MB/s"
                                     % (pct, mb(got), mb(total), speed))
                    sys.stdout.flush()
    sys.stdout.write("\n")
    if total and got != total:
        os.remove(tmp)
        raise RuntimeError("大小不对（%d / %d），已删掉重试" % (got, total))
    os.replace(tmp, dst)
    return got


def check():
    print("模型目录：%s" % MODELS)
    print("运行库目录：%s" % PYLIBS)
    miss = []
    print("\n[GGUF 识别档]")
    for eid, repo, fn, approx in GGUF:
        p = os.path.join(MODELS, fn)
        if os.path.isfile(p):
            print("  ok   %-7s %-34s %s" % (eid, fn, mb(os.path.getsize(p))))
        else:
            print("  --   %-7s %-34s 缺（约 %d MB）" % (eid, fn, approx))
            miss.append(fn)
    print("\n[transcribe_cpp 运行库]")
    okpkg = os.path.isfile(os.path.join(PYLIBS, "transcribe_cpp", "__init__.py"))
    nat = os.path.join(PYLIBS, "transcribe_cpp_native_cu12", "_native")
    oknat = os.path.isdir(nat)
    cublas = os.path.isfile(os.path.join(nat, "cublas64_12.dll")) if oknat else False
    print("  %s   transcribe_cpp（Python 包装）" % ("ok  " if okpkg else " -- "))
    print("  %s   _native（CUDA/Vulkan 后端 DLL，%s）"
          % ("ok  " if oknat else " -- ", "含 cublas" if cublas else "缺 cublas"))
    if not (okpkg and oknat):
        miss.append("runtime")
    if miss:
        print("\n缺：%s" % "、".join(miss))
        print("装法：python tools\\fetch_asr_models.py --all --proxy http://127.0.0.1:2080")
    else:
        print("\n全部就绪。控制台里把「识别」下拉切到 Qwen3-1.7B / Qwen3-0.6B / Fun-ASR-Nano 即可。")
    return not miss


def download_gguf(proxy="", mirror=""):
    os.makedirs(MODELS, exist_ok=True)
    base = (mirror or HF).rstrip("/")
    for eid, repo, fn, approx in GGUF:
        dst = os.path.join(MODELS, fn)
        if os.path.isfile(dst) and os.path.getsize(dst) > approx * 0.9 * 1024 * 1024:
            print("[%s] 已有 %s，跳过" % (eid, fn))
            continue
        url = "%s/%s/resolve/main/%s" % (base, repo, fn)
        print("[%s] %s" % (eid, url))
        try:
            n = fetch(url, dst, proxy=proxy, expect=approx * 1024 * 1024)
            print("    完成 %s" % mb(n))
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            print("    失败：%r（换 --proxy 或 --mirror 再试）" % (e,))


def download_runtime(proxy=""):
    os.makedirs(PYLIBS, exist_ok=True)
    tmpdir = os.path.join(PYLIBS, "_wheels")
    os.makedirs(tmpdir, exist_ok=True)
    for whl in WHEELS:
        dst = os.path.join(tmpdir, whl)
        if not (os.path.isfile(dst) and os.path.getsize(dst) > 1024 * 1024):
            url = "%s/%s" % (GH, whl)
            print("[runtime] %s" % url)
            try:
                fetch(url, dst, proxy=proxy)
            except (urllib.error.URLError, RuntimeError, OSError) as e:
                print("    失败：%r" % (e,))
                continue
        print("  解包 %s" % whl)
        with zipfile.ZipFile(dst) as z:
            z.extractall(PYLIBS)          # 轮子就是 zip，直接摊进 .pylibs
    print("运行库解包完成：%s" % PYLIBS)


def main():
    ap = argparse.ArgumentParser(description="装 GGUF 识别档（qwen17 / qwen06 / fun）")
    ap.add_argument("--check", action="store_true", help="只检查（默认）")
    ap.add_argument("--download", action="store_true", help="下 gguf 模型")
    ap.add_argument("--runtime", action="store_true", help="下 transcribe_cpp 运行库")
    ap.add_argument("--all", action="store_true", help="模型 + 运行库")
    ap.add_argument("--proxy", default="", help="如 http://127.0.0.1:2080")
    ap.add_argument("--mirror", default="", help="如 https://hf-mirror.com")
    a = ap.parse_args()
    did = False
    if a.all or a.runtime:
        download_runtime(a.proxy)
        did = True
    if a.all or a.download:
        download_gguf(a.proxy, a.mirror)
        did = True
    if not did or a.check:
        sys.exit(0 if check() else 1)


if __name__ == "__main__":
    main()
