# -*- coding: utf-8 -*-
"""OCR 模型一键下载（models/ocr/*.onnx 被 .gitignore 排除，换机/重装后跑这个）

用法：
    python models/ocr/fetch_models.py

下的是 PP-OCRv4 日文识别模型（9.7MB）+ 字典。检测模型与中文识别模型
随 rapidocr_onnxruntime 包自带，不需要单独下。

ModelScope 优先（GitHub raw / HuggingFace 在这台机上会被重置）。
"""
import os, sys, urllib.request, json

HERE = os.path.dirname(os.path.abspath(__file__))
API = ("https://modelscope.cn/api/v1/models/RapidAI/RapidOCR/repo/files"
       "?Revision=master&Recursive=True")
FILES = [
    ("onnx/PP-OCRv4/rec/japan_PP-OCRv4_rec_mobile.onnx", "japan_rec.onnx"),
    ("onnx/PP-OCRv4/rec/japan_dict.txt", "japan_dict.txt"),
]

def fetch(remote, local):
    dst = os.path.join(HERE, local)
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        print("[skip] %s 已存在 %.1f MB" % (local, os.path.getsize(dst) / 1048576.0))
        return
    url = ("https://modelscope.cn/api/v1/models/RapidAI/RapidOCR/repo"
           "?Revision=master&FilePath=" + urllib.request.quote(remote, safe=""))
    print("[get ] %s" % remote)
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    with open(dst, "wb") as f:
        f.write(data)
    print("[ ok ] %s  %.1f MB" % (local, len(data) / 1048576.0))

def main():
    print("目标目录：%s" % HERE)
    for remote, local in FILES:
        try:
            fetch(remote, local)
        except Exception as e:
            print("[fail] %s -> %r" % (remote, e))
            return 1
    print("完成。config_ja.yaml 里的路径若指向别处，改一下 model_path / rec_keys_path。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
