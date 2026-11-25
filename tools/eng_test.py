# -*- coding: utf-8 -*-
"""按链选引擎的端到端小测：只喂一段已经录好的 wav 进 StreamChain，看识别走本地还是云端。

python tools\\eng_test.py B cloud out/clips/ja1.wav
python tools\\eng_test.py A hybrid out/clips/zh1.wav
"""
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from vrdub import bus as busmod
from vrdub import chains
from vrdub import cfg as cfgmod
from vrdub import mt as mtmod
from vrdub import net as netmod
from vrdub import util
from vrdub.asr import Transcriber


def load16(path):
    with wave.open(path, "rb") as fh:
        n, ch, sr, wd = fh.getnframes(), fh.getnchannels(), fh.getframerate(), fh.getsampwidth()
        raw = fh.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        x = util.resample(x, sr, 16000)
    return (np.clip(x, -1, 1) * 32767.0).astype("<i2")


def main():
    chain_name = sys.argv[1] if len(sys.argv) > 1 else "B"
    eng = sys.argv[2] if len(sys.argv) > 2 else "cloud"
    path = sys.argv[3] if len(sys.argv) > 3 else "out/clips/ja1.wav"
    cfg = cfgmod.Cfg()
    key = "asr_engine_a" if chain_name == "A" else "asr_engine_b"
    cfg.update({key: eng, "live": True, "out_on": False, "in_on": True,
                "echo_guard": False, "refine": True})
    net = netmod.Net("ali", cfg.host, cfg.key, slots=6)
    asr = Transcriber(cfg, net, util.log)
    mt = mtmod.Translator(cfg, net, util.log)
    bus = busmod.Bus()
    src_key = "mic" if chain_name == "A" else "loop"
    ch = chains.StreamChain(chain_name, chain_name == "A", cfg, bus, asr, mt, None,
                            player=None, src_key=src_key, log=util.log)
    ch.start()
    print(f"[eng] chain={chain_name} engine={ch.engine()} clip={path} bytes={os.path.getsize(path)}")
    if eng != "cloud":
        asr.warm()          # 本地模型冷载 1.8 s，不计进稳态数字
    pcm = load16(path)
    dur = pcm.size / 16000.0
    times = int(os.environ.get("ENG_REPEAT", "2"))
    for i in range(times):
        now = time.time()
        ch.on_seg({"kind": "final", "audio": pcm, "dur": dur, "t0": now - dur,
                   "t1": now, "lvl": -22.0, "final": True})
        t_end = time.time() + float(os.environ.get("ENG_WAIT", "12"))
        last = None
        while time.time() < t_end:
            rows = bus.recent(5)
            if rows:
                cur = (rows[-1].get("src"), rows[-1].get("dst"), rows[-1].get("state"))
                if cur != last:
                    last = cur
            if rows and rows[-1].get("state") in ("done", "drop", "err"):
                break
            time.sleep(0.1)
        r = (bus.recent(5) or [{}])[-1]
        print("[eng] #%d state=%s engine=%s asr_ms=%s mt_ms=%s" % (
            i + 1, r.get("state"), r.get("engine"), r.get("asr_ms"), r.get("mt_ms")))
        print("[eng] #%d src=%r" % (i + 1, r.get("src")))
        print("[eng] #%d dst=%r" % (i + 1, r.get("dst")))
        time.sleep(0.4)
    ch.stop()


if __name__ == "__main__":
    main()
