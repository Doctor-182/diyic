# -*- coding: utf-8 -*-
"""模型资产：检查 / 下载（权重不入库，换机器、重装之后跑这个）。

    python tools/fetch_models.py --check      # 只看缺什么，不下载
    python tools/fetch_models.py              # 下载缺的（OCR 走 ModelScope）

需要的资产（相对程序目录）：
    models/sensevoice/model.int8.onnx   228 MB   本地识别（zh/ja 共用，int8）
    models/sensevoice/tokens.txt        0.3 MB   随仓库提供，缺了也能下载
    models/ocr/japan_rec.onnx           9.3 MB   日文识别（PP-OCRv4 mobile）
    models/ocr/japan_dict.txt            17 KB
    models/ocr/ch_PP-OCRv4_det_infer.onnx / ch_PP-OCRv4_rec_infer.onnx  4.7 + 10.4 MB
    models/ocr/config_ch.yaml / config_ja.yaml
    models/spk/voxceleb.onnx           80 MB   说话人分离（ECAPA，B 链字幕前标 1/2/3…）
备选（不装也能跑，装了可以用 anime-whisper 引擎对比）：
    models/anime-whisper-ct2-int8/model.bin   732 MB

下载源：ModelScope（GitHub raw / HuggingFace 在国内经常被重置）。
SenseVoice 的 int8 onnx 需要自己转：把 ModelScope 的 iic/SenseVoiceSmall 用
sherpa-onnx / onnxruntime 导出成 int8，或直接复制别人机器上的这 228 MB 文件。
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")

MSC_API = ("https://modelscope.cn/api/v1/models/RapidAI/RapidOCR/repo"
           "?Revision=master&FilePath=")
# (远端仓库内路径, 落地相对 models/ 的路径, 说明)
ASSETS = [
    ("onnx/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile.onnx", "ocr/japan_rec.onnx", "日文识别"),
    ("onnx/PP-OCRv4/rec/japan_dict.txt", "ocr/japan_dict.txt", "日文字典"),
]
# 必须存在但不由本脚本下载的（大文件/需要转换）
REQUIRED = [
    ("sensevoice/model.int8.onnx", "本地识别主干（228 MB，需自行导出或从发布包获取）"),
    ("sensevoice/tokens.txt", "SenseVoice 词表"),
    ("ocr/japan_rec.onnx", "日文识别"),
    ("ocr/japan_dict.txt", "日文字典"),
    ("ocr/ch_PP-OCRv4_det_infer.onnx", "文字检测"),
    ("ocr/ch_PP-OCRv4_rec_infer.onnx", "中文识别"),
    ("spk/voxceleb.onnx", "说话人分离（ECAPA 80 MB，缺了 B 链就不标 1/2/3）"),
]
OPTIONAL = [("anime-whisper-ct2-int8/model.bin", "备用识别引擎（732 MB，不装可用）")]


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def check(verbose: bool = True) -> int:
    miss = 0
    if verbose:
        print("模型目录：%s" % MODELS)
    for rel, desc in REQUIRED:
        p = os.path.join(MODELS, rel.replace("/", os.sep))
        n = _size(p)
        if n <= 0:
            miss += 1
            if verbose:
                print("  [缺] %-38s %s" % (rel, desc))
        elif verbose:
            print("  [有] %-38s %8.1f MB" % (rel, n / 1048576.0))
    for rel, desc in OPTIONAL:
        p = os.path.join(MODELS, rel.replace("/", os.sep))
        n = _size(p)
        if verbose:
            print("  [%s] %-38s %8.1f MB  %s" % ("有" if n else "无", rel, n / 1048576.0, desc))
    if verbose:
        print("缺 %d 项。" % miss if miss else "资产齐全。")
    return miss


def fetch(remote: str, local: str) -> bool:
    dst = os.path.join(MODELS, local.replace("/", os.sep))
    if _size(dst) > 1000:
        print("[skip] %s 已存在 %.1f MB" % (local, _size(dst) / 1048576.0))
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    url = MSC_API + urllib.request.quote(remote, safe="")
    print("[get ] %s" % remote)
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            data = r.read()
        with open(dst, "wb") as fh:
            fh.write(data)
        print("[ ok ] %s  %.1f MB" % (local, len(data) / 1048576.0))
        return True
    except Exception as e:                      # noqa: BLE001
        print("[fail] %s -> %r" % (remote, e))
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="模型资产检查/下载")
    ap.add_argument("--check", action="store_true", help="只检查，不下载")
    a = ap.parse_args(argv)
    miss = check()
    if a.check:
        return 0 if miss == 0 else 1
    for remote, local, desc in ASSETS:
        fetch(remote, local)
    miss = check()
    return 0 if miss == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
