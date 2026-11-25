# -*- coding: utf-8 -*-
import json, io
p = "data/settings.json"
d = json.load(io.open(p, encoding="utf-8"))
d["asr_engine_b"] = "hybrid"
io.open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=1))
print("asr_engine_b =", d["asr_engine_b"], "dst_lang =", d["dst_lang"])
