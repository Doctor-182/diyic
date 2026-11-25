# -*- coding: utf-8 -*-
import json, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8899/api/metrics", timeout=5).read())
rows = d.get("rows") or []
for r in rows[-12:]:
    print(f'{r.get("chain")} {r.get("id")} state={r.get("state")} lvl={r.get("lvl")} dur={r.get("dur")} '
          f'asr={r.get("asr_ms")} mt={r.get("mt_ms")} e2e={r.get("e2e_ms")} src={str(r.get("src"))[:26]!r}')
