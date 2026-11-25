# -*- coding: utf-8 -*-
"""把项目打成两个 zip：代码包（小，能推 GitHub）+ 模型包（238 MB，走网盘/Release）。

用法：
    python tools/pack.py                # 打到 dist/
    python tools/pack.py --no-models    # 只打代码包

代码包里**包含 .env.local**（作者同意随包分发；公开发布前记得去后台禁用 key）。
排除：out/（日志截图）、dist/、__pycache__、models/**/*.onnx、data/settings.json、
      _probe/、%SystemDrive%/、tools/_*.py 这类一次性脚本。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = dt.datetime.now().strftime("%Y%m%d")
NAME = "vrc-dub"

ROOT_FILES = [
    "README.md", "run.bat", "检查环境.bat", "评测.bat", "打包.bat",
    "requirements.txt", "LICENSE", "CONTRIBUTING.md", ".gitignore", ".env.local",
]
SRC_DIRS = ["vrdub", "docs", "tools", "models"]

# 模型权重：models/ 下除 README.md 之外的东西全进模型包（onnx + 词表/字典要配套）
MODEL_GLOBS = (".onnx",)
MODEL_DIR = "models"
SKIP_DIRS = {"__pycache__", "out", "dist", "_probe", ".git", "%SystemDrive%",
             ".idea", ".vscode", "node_modules"}
SKIP_FILES = {"settings.json", ".DS_Store", "Thumbs.db"}
SKIP_PREFIX = ("_",)          # tools/_patch_anno.py 之类一次性脚本


def parts_ok(rel: str) -> bool:
    """models/ 下的非 README 文件都算模型包（权重、字典、词表、config）。"""
    return rel.split("/")[0] == MODEL_DIR


def keep(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    base = parts[-1]
    if base in SKIP_FILES or base.endswith((".pyc", ".pyo")):
        return False
    if parts[0] == "tools" and base.startswith(SKIP_PREFIX):
        return False
    return True


def walk():
    """yield (相对路径, 绝对路径, 是否模型权重)"""
    for f in ROOT_FILES:
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            yield f.replace(os.sep, "/"), p, False
    for d in SRC_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                if not keep(rel):
                    continue
                yield rel, p, fn.lower().endswith(MODEL_GLOBS)


def sha256(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(tag: str, files, out_dir: str, manifest_name: str):
    os.makedirs(out_dir, exist_ok=True)
    total = 0
    lines = []
    for rel, p, _is_model in files:
        size = os.path.getsize(p)
        total += size
        lines.append("%10d  %s  %s" % (size, sha256(p)[:16], rel))
    lines.append("")
    lines.append("# %d 个文件，合计 %.1f MB，生成于 %s"
                 % (len(files), total / 1048576.0, dt.datetime.now().isoformat(" ", "seconds")))
    man = os.path.join(out_dir, tag + "-" + manifest_name)
    with open(man, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    zip_path = os.path.join(out_dir, tag + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, p, _ in files:
            z.write(p, arcname=tag + "/" + rel)
        z.write(man, arcname=tag + "/" + manifest_name)
    return zip_path, total, len(files)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-models", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args()

    every = list(walk())
    src = [(r, p, m) for r, p, m in every if not m]
    # 模型包要能直接解压覆盖到项目上：models/ 下除 README.md 全带上（含 config/字典/下载脚本）
    mdl = [(r, p, True) for r, p, m in every
           if parts_ok(r) and r.split("/")[-1] != "README.md"]

    dist = a.out
    tag_src = "%s-src-%s" % (NAME, STAMP)
    zp, size, n = build(tag_src, src, dist, "MANIFEST.txt")
    print("[代码包] %s\n          %d 个文件 · %.2f MB" % (zp, n, size / 1048576.0))
    if not a.no_models and mdl:
        tag_mdl = "%s-models-%s" % (NAME, STAMP)
        zp2, size2, n2 = build(tag_mdl, mdl, dist, "MANIFEST.txt")
        print("[模型包] %s\n          %d 个文件 · %.1f MB" % (zp2, n2, size2 / 1048576.0))
    print("\n完成。目录：%s" % dist)
    return 0


if __name__ == "__main__":
    sys.exit(main())
