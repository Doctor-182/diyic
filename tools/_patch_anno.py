# -*- coding: utf-8 -*-
import io, os, sys
P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vrdub', 'util.py')
s = io.open(P, encoding='utf-8').read()
i = s.index('def strip_anno(s: str) -> str:')
j = s.index('def clean_translation(')
new = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_new_anno.txt'), encoding='utf-8').read()
s = s[:i] + new + s[j:]
io.open(P, 'w', encoding='utf-8', newline='\n').write(s)
print('patched', len(s))