# -*- coding: utf-8 -*-
r"""生成程序图标（packaging\vrdub.ico）：深色圆角方块 + 青蓝渐变 + 白色「言」字。

为什么自己画：请美术不现实，随便找个图标又不像"软件"。程序化生成的好处是
颜色跟着品牌走、随时能改，且不需要外部素材就能进版本库（几 KB）。
用法：py -3.12 tools\make_icon.py
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "packaging", "vrdub.ico")

C1 = (0x2B, 0xE0, 0xD0)      # 青
C2 = (0x2F, 0x7B, 0xFF)      # 蓝
BG = (0x0B, 0x10, 0x18)      # 近黑
S = 1024                     # 先大图再缩，边缘才干净


def _font(size: int):
    for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:                   # noqa: BLE001
                continue
    return ImageFont.load_default()


def main() -> int:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角底
    pad, r = int(S * 0.04), int(S * 0.22)
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=r, fill=BG + (255,))

    # 渐变：逐行插值，只画在圆角遮罩里
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / float(S - 1)
        c = tuple(int(C1[i] + (C2[i] - C1[i]) * t) for i in range(3))
        gd.line([(0, y), (S, y)], fill=c + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=r, fill=255)
    img.paste(grad, (0, 0), mask)

    # 内层暗底：让字浮在深色上，缩小后仍能看清
    inset = int(S * 0.115)
    d.rounded_rectangle([inset, inset, S - inset, S - inset],
                        radius=int(r * 0.78), fill=(6, 10, 16, 235))

    # 「言」字居中
    f = _font(int(S * 0.52))
    text = "言"
    box = d.textbbox((0, 0), text, font=f)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((S - tw) / 2 - box[0], (S - th) / 2 - box[1]), text, font=f, fill=(0xF2, 0xF7, 0xFF, 255))

    # 底部两点：A/B 双链的暗示
    cy = int(S * 0.775)
    for i, (x, col) in enumerate(((0.40, C1), (0.60, C2))):
        cx = int(S * x)
        d.ellipse([cx - int(S * 0.022), cy - int(S * 0.022),
                   cx + int(S * 0.022), cy + int(S * 0.022)], fill=col + (255,))

    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(OUT, format="ICO", sizes=sizes)
    print("icon ->", OUT, os.path.getsize(OUT), "bytes")
    png = os.path.join(ROOT, "out", "icon.png")
    os.makedirs(os.path.dirname(png), exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(png)
    print("png  ->", png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
