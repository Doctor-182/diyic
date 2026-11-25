import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vrdub import audio
p = audio.pa_inst()
for i in range(p.get_device_count()):
    d = p.get_device_info_by_index(i)
    ino = int(d.get("maxInputChannels") or 0)
    outo = int(d.get("maxOutputChannels") or 0)
    if ino or outo:
        print("%2d | in%d out%d | %-46s | %.0fHz | %s" % (i, ino, outo, str(d.get("name"))[:46], d.get("defaultSampleRate"), ("WASAPI" if d.get("isLoopbackDevice") else "")))
