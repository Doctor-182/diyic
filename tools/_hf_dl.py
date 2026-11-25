# -*- coding: utf-8 -*-
import os, sys
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:2080")
os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:2080")
from huggingface_hub import snapshot_download
p = snapshot_download(
    repo_id="quantumcookie/anime-whisper-ct2-int8",
    local_dir=r"D:\vrc无言势\models\anime-whisper-ct2-int8",
    allow_patterns=["*.json", "*.bin", "*.txt", "README.md"],
    max_workers=4,
)
print("OK", p)
for f in sorted(os.listdir(p)):
    print("  ", f, "%.1f MB" % (os.path.getsize(os.path.join(p, f)) / 1048576.0))
