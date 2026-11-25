# -*- coding: utf-8 -*-
"""尾片（final tail）出声验证：speak_q_max=0 把所有碎片背压跳过，只剩收尾整句尾片。
若 audio_ms > 0 即证明「_final_tail 不再被 _is_echo 误杀」。
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vrdub.util as util
import vrdub.cfg as cfgmod
import vrdub.bench as B


def main():
    c = cfgmod.Cfg()
    c.update({"out_on": True, "in_on": True, "speak_q_max": 0, "dbg_timing": True})
    rig = B.Rig(c, util.log)
    try:
        clips = B.ensure_clips(rig.synth, c, util.log)
        zh = [(t, p) for lang, t, p in clips if lang == "zh"]
        text, path = zh[0]
        print("== 背压全跳过，只留收尾尾片 ==  " + text, flush=True)
        r = B.lat_once(rig, "A", text, path, c, util.log, wait=30.0)
        print("TAIL_RESULT " + repr(r), flush=True)
        c.update({"speak_q_max": 1})
        print("== 正常背压（对照） ==", flush=True)
        r2 = B.lat_once(rig, "A", zh[1][0], zh[1][1], c, util.log, wait=30.0)
        print("NORMAL_RESULT " + repr(r2), flush=True)
    finally:
        rig.stop()


if __name__ == "__main__":
    main()
