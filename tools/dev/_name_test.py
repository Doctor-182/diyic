# -*- coding: utf-8 -*-
"""验证 find_by_name 能不能按名字找到设备。"""
import sys
sys.path.insert(0, r'D:\vrc无言势')
from vrdub import audio

D = audio.list_devices()
for kind in ('input', 'output', 'loop'):
    print('==', kind, len(D[kind]))
    for d in D[kind]:
        print('   ', d['i'], repr(d['name']), d['ch'])
print('---- settings 里存的 ----')
import json
cfg = json.load(open(r'D:\vrc无言势\data\settings.json', encoding='utf-8'))
print(json.dumps(cfg.get('dev_names'), ensure_ascii=False))
print('---- lookup ----')
for nk, kind in (('in', 'in'), ('out', 'out'), ('mon', 'out'), ('loop', 'loop')):
    nm = (cfg.get('dev_names') or {}).get(nk)
    print(nk, repr(nm), '->', audio.find_by_name(kind, nm))
