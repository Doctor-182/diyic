# -*- coding: utf-8 -*-
r"""进程回环自验：证明 ProcLoop 只采「目标进程」的声音，不串别人的。

做法：
  1) 起一个子进程（同一个 python），让它往 mono 设备（默认 #18）播 4 s 440 Hz；
  2) ProcLoop(pid=子进程) 采 4~5 s，打印 peak / rms / 时长，peak > 0.02 判「通」；
  3) 再起一个只 sleep 的子进程做对照，采它应该全零（peak ≈ 0）。

用法（项目根下）：
  python tools\procloop_check.py
  python tools\procloop_check.py --dev 18 --secs 5.5 --freq 440 --gain 0.25
退出码：0 通过；1 结论不对；2 采集器起不来（会打印 HRESULT 说明）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vrdub.procloop import ProcLoop        # noqa: E402

# 子进程脚本：先等一下（让父进程把采集跑起来），再播一段正弦
TONE = r"""
import sys, time
import numpy as np
dev, dur, freq, gain = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
sr = 24000
sys.stdout.reconfigure(encoding="utf-8")
time.sleep(0.9)
n = int(sr * dur)
t = np.arange(n, dtype=np.float64) / sr
pcm = (gain * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
from vrdub import audio
audio.play_pcm(dev, pcm, sr)
print("CHILD_TONE_DONE", flush=True)
"""

SLEEP = "import time; time.sleep(30)"


def _child_env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def spawn_tone(dev, dur, freq, gain):
    return subprocess.Popen([sys.executable, "-u", "-c", TONE, str(dev), str(dur),
                             str(freq), str(gain)],
                            cwd=ROOT, env=_child_env())


def spawn_sleep():
    return subprocess.Popen([sys.executable, "-u", "-c", SLEEP], cwd=ROOT, env=_child_env())


def capture(pid, secs, name, quiet=False):
    """采 secs 秒，返回 (pcm, stats)。"""
    chunks = []

    def on_pcm(pcm, rate):
        chunks.append(np.array(pcm, dtype=np.float32, copy=True))

    pl = ProcLoop(pid=pid, on_pcm=on_pcm)
    t0 = time.time()
    pl.start()
    try:
        time.sleep(secs)
    finally:
        pl.stop()
    wall = time.time() - t0
    x = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    st = pl.stats
    return x, {"peak": peak, "rms": rms, "frames": int(x.size), "dur": st["dur"],
               "wall": round(wall, 2), "blocks": st["blocks"], "drops": st["drops"],
               "silent": st["silent"], "fmt": pl.init_format}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", type=int, default=18, help="播声音的渲染设备（默认 18 = Rapoo 耳机）")
    ap.add_argument("--secs", type=float, default=5.5, help="采集时长（默认 5.5 s）")
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("--gain", type=float, default=0.25)
    ap.add_argument("--tone-secs", type=float, default=4.0)
    ap.add_argument("--skip-control", action="store_true")
    args = ap.parse_args()

    print("== procloop_check ==")
    print("python  :", sys.executable)
    print("device  : #%d（tone %.0f Hz, %.1f s, gain %.2f）"
          % (args.dev, args.freq, args.tone_secs, args.gain))

    ok = True
    tone = None
    try:
        tone = spawn_tone(args.dev, args.tone_secs, args.freq, args.gain)
        print("\n[1] 目标进程 pid=%d 采 %.2f s" % (tone.pid, args.secs))
        _x, s = capture(tone.pid, args.secs, "tone")
        print("    fmt      : %s" % s["fmt"])
        print("    frames   : %d（%.2f s / 墙钟 %.2f s）blocks=%d drops=%d silent=%d"
              % (s["frames"], s["dur"], s["wall"], s["blocks"], s["drops"], s["silent"]))
        print("    peak     : %.4f" % s["peak"])
        print("    rms      : %.4f" % s["rms"])
        passed = s["peak"] > 0.02
        print("    结论     : %s" % ("通：确实采到了这个进程的声音"
                                     if passed else "没声音：peak<=0.02，见 docs/PROCLOOP.md 排查表"))
        ok = ok and passed
    except RuntimeError as e:
        print("    起不来   : %s" % e)
        return 2
    finally:
        if tone is not None:
            try:
                tone.kill()
            except Exception:                      # noqa: BLE001
                pass

    if not args.skip_control:
        ctl = None
        try:
            ctl = spawn_sleep()
            time.sleep(0.4)
            print("\n[2] 对照进程 pid=%d（只 sleep，不出声）采 %.2f s" % (ctl.pid, min(2.5, args.secs)))
            _y, s2 = capture(ctl.pid, min(2.5, args.secs), "control")
            print("    frames   : %d blocks=%d silent=%d" % (s2["frames"], s2["blocks"], s2["silent"]))
            print("    peak     : %.6f   rms: %.6f" % (s2["peak"], s2["rms"]))
            quiet = s2["peak"] < 1e-3
            print("    结论     : %s" % ("静音：没串到别的进程的声音"
                                        if quiet else "有串音：非目标进程也采到声音，说明录制没隔离"))
            ok = ok and quiet
        except RuntimeError as e:
            print("    起不来   : %s" % e)
            ok = False
        finally:
            if ctl is not None:
                try:
                    ctl.kill()
                except Exception:                  # noqa: BLE001
                    pass

    print("\n%s" % ("全部通过" if ok else "有项目未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
