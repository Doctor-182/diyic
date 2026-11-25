# -*- coding: utf-8 -*-
"""后端总装：把 cfg / net / asr / mt / tts / audio / chains / ocr / server / hud / hotkey
接成一个进程。启动它，网页就活了。

    python -m vrdub.app            正常启动（自动开浏览器）
    python -m vrdub.app --no-hud   不开浮窗
    python -m vrdub.app --no-web   不开浏览器
    python -m vrdub.app --selftest 只做本地自检（模型/设备/端点），不起链路
"""
from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import traceback
import sys
import threading
import time
import webbrowser

from . import audio, bus as busmod, chains, metrics
from . import cfg as cfgmod
from . import mt as mtmod
from . import net as netmod
from . import osc as oscmod
from . import procloop as procloopmod
from . import server as srvmod
from . import tts as ttsmod
from . import util
from . import version
from . import paths as pathmod
from . import child as childmod
from . import asr as asr_mod
from .asr import Transcriber
from .vad import Segmenter

ROOT = cfgmod.ROOT

# ---- 子进程不能闪黑框 -------------------------------------------------------
# hud/danmaku/ocr_overlay 都是 Qt 程序，用 python.exe 起会在屏幕上闪控制台窗口。
# 起步方式统一问 child 模块：源码是 pythonw.exe + 脚本，装成 exe 之后是
# VRDub.exe --child NAME（同一个 exe 自己再进来一次，跑那个子程序就退出）。
CREATE_NO_WINDOW = childmod.CREATE_NO_WINDOW

# 兼容老调用点（tools/dev 里的探针可能还引用它）
_child_py = childmod.python


class App:
    def __init__(self, use_hud=True, use_web=True):
        self.t0 = time.time()
        self.cfg = cfgmod.Cfg()
        self.bus = busmod.Bus()
        self.use_hud = use_hud
        self.use_web = use_web
        self.stopping = False
        self.net = netmod.Net("ali", self.cfg.host, self.cfg.key, slots=10)
        self.synth = ttsmod.Synth(self.cfg, self.net)
        self.asr = Transcriber(self.cfg, self.net)
        self.mt = mtmod.Translator(self.cfg, self.net)
        self.player = None
        self.through = None
        self.mic_cap = None
        self.loop_cap = None
        self.proc_loop = None        # B 链输入：VRChat 进程回环（只出这个进程树的声音）
        self._proc_q = queue.Queue(maxsize=64)
        # 麦克风与 B 链那一路的电平包络（20ms 一格，各留 12 秒）：
        # 用来判断「麦克风听到的其实是耳机漏出来的那一路」——同一个声音两路一起响，
        # 电平曲线会高度相关。这比猜语言可靠（日语被中文 ASR 听成乱码时语言判据全失效）。
        self._env_mic = collections.deque(maxlen=600)
        self._env_loop = collections.deque(maxlen=600)
        self._proc_th = None
        self._proc_next = 0.0        # 下次尝试连 VRChat 的时间
        self.seg_a = None
        self.seg_b = None
        self.chain_a = chains.StreamChain("A", True, self.cfg, self.bus, self.asr, self.mt,
                                         self.synth, player=None, src_key="mic")
        self.chain_b = chains.StreamChain("B", False, self.cfg, self.bus, self.asr, self.mt,
                                         self.synth, player=None, src_key="loop")
        # VRChat chatbox：只给 A 链用（B 链译文绝不外发）
        self.osc = oscmod.OscChatbox(self.cfg, util.log)
        self.chain_a.osc = self.osc
        # A 链说话前问一句：麦克风听到的是不是耳机漏出来的那一路？
        self.chain_a.leak_check = self._a_leak
        self.chain_a.out_check = self._out_echo
        self.chain_a.echo_match = self._echo_match
        self.ocr_bridge = chains.OcrBridge(self.cfg, self.bus)
        self.ocr_bridge.raw_hook = self._ocr_raw      # 原文先浮
        self.ocr_bridge.show_hook = self._ocr_show    # 译文原地替换
        self.ocr = None
        self.overlay = None
        self.overlay_log = None
        self._ocr_ids = {}          # 屏幕位置 → 稳定 id（同一处不叠气泡）
        self.server = None
        self.hud = None
        self.hud_log = None
        self.danmaku = None
        self.danmaku_log = None
        self._io_lk = threading.Lock()
        self.last_crop = None
        self._dev_cache = (0.0, None)
        self._calib = None
        self._calib_b = None
        self._stop = threading.Event()
        util.add_sink(lambda rec: self.bus.emit("log", {"lv": rec["lv"], "msg": rec["msg"],
                                                        "tag": rec.get("tag", "")}))

    # ================================================================ 启动
    def start(self):
        rot = util.rotate_logs()          # 先转日志，别让上一轮的 8 MB 又长上去
        util.log(f"{version.full()} · pid={os.getpid()} · 冻结={version.frozen()} · 根目录 {ROOT}",
                 "info", "boot")
        util.log(f"用户目录 {pathmod.HOME}（配置 {pathmod.CONFIG_DIR} · 日志 {pathmod.LOG_DIR} · "
                 f"输出 {pathmod.OUT}）", "info", "boot")
        moved = list(getattr(self.cfg, "migrated", []) or [])
        if moved:
            util.log("已从程序目录搬到用户目录：" + "、".join(moved), "info", "boot")
        if rot:
            util.log("日志轮转：" + "、".join(rot), "info", "boot")
        self._check()
        if not self.cfg.key:
            util.log("没有 DASHSCOPE_TOKENPLAN_KEY（.env.local），云端功能会全挂", "err", "boot")
        threading.Thread(target=self.asr.warm, name="warm", daemon=True).start()
        self.chain_a.start()
        self.chain_b.start()
        if self.cfg.get("chatbox_on", True):
            self.osc.start()
            util.log(f"chatbox 输出开：OSC {self.cfg.get('osc_host')}:{self.cfg.get('osc_port')}"
                     f"（只发 A 链日文译文）", "info", "osc")
        self.start_player()
        self.start_mic()
        self.start_loop()
        self.server = srvmod.Server(self)
        port = self.server.start()
        if self.use_hud and self.cfg.get("hud_on", True):
            self.hud_start()
        if self.use_hud and self.cfg.get("danmaku", True):
            self.danmaku_start()
        if self.use_hud and self.cfg.get("ocr_overlay", True):
            self.overlay_start()
        threading.Thread(target=self._fanout, name="fanout", daemon=True).start()
        threading.Thread(target=self._tick, name="tick", daemon=True).start()
        threading.Thread(target=self._mem_watch, name="mem", daemon=True).start()
        self.bus.set_status("run", "运行中")
        self.bus.set_mic(on=bool(self.cfg.get("mic_on", True)))
        self.apply_ocr()
        self.hotkeys = self._hotkeys()
        if self.use_web:
            threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
        try:
            d = self.devices().get("out") or []
            nm = next((x.get("name", "") for x in d
                       if int(x.get("index", -1)) == int(self.cfg.get("out_dev", 19))), "")
            if "CABLE" in nm.upper():
                util.log("VRChat 里「麦克风」必须选 CABLE Output (VB-Audio Virtual Cable)，"
                         "否则对方听不到日语合成音", "info", "boot")
        except Exception:
            pass
        util.log(f"就绪 · http://127.0.0.1:{port}/ · A链→{self.cfg.get('dst_lang')} "
                 f"· 音色 {str(self.cfg.get('voice'))[-8:]}", "info", "boot")

    def _check(self):
        try:
            self.asr.local.ready()
            util.log(f"本地识别：{self.asr.local.note}", "info" if self.asr.local.ready() else "warn", "asr")
        except Exception as e:                          # noqa: BLE001
            util.log(f"本地识别不可用：{e!r}", "warn", "asr")

    def _hotkeys(self):
        from .hotkey import Hotkeys
        hk = Hotkeys(on_mic=lambda on: self.set_mic(on),
                     on_b=lambda: self.api_chains({"in_on": not self.cfg.get("in_on", True)}),
                     on_clear=lambda flag=False: (self.bus.clear() if flag else self.bus.clear()),
                     on_ocr=lambda: self.api_chains({"ocr_on": not self.cfg.get("ocr_on", False)}),
                     on_shot=lambda: threading.Thread(target=self._shot_bg,
                                                      name="ocr-shot", daemon=True).start(),
                     on_pick=lambda: threading.Thread(target=self._pick_bg,
                                                      name="ocr-pick", daemon=True).start())
        hk.start()
        return hk

    # ================================================================ 音频
    # ---------------------------------------------------------------- 设备绑定
    def _dev(self, kind, key, nk=None):
        """按「名字优先」解析设备索引。

        Windows 每次进程启动的 WASAPI 枚举顺序不固定（实测重启后 #19
        从 CABLE Input 变成扬声器 Rapoo），所以号码只能兜底，名字才是真身份。
        """
        try:
            idx = int(self.cfg.get(key, 0) or 0)
        except Exception:                               # noqa: BLE001
            idx = 0
        names = self.cfg.get("dev_names") or {}
        nm = names.get(nk or kind) if isinstance(names, dict) else None
        try:
            return audio.resolve_named(kind, idx, nm)
        except Exception as e:                          # noqa: BLE001
            util.log(f"设备解析失败 {key}={idx}（{e!r}），沿用号码", "warn", "audio")
            return idx

    def _bind(self, kind, key, nk=None):
        """解析 + 回写索引与名字（下次启动就能按名字找回来）。"""
        i = self._dev(kind, key, nk)
        if i != self.cfg.get(key):
            self.cfg.update({key: i})
        try:
            nm = audio.name_of(kind, i)
        except Exception:                               # noqa: BLE001
            nm = ""
        if nm:
            raw = self.cfg.get("dev_names")
            names = dict(raw) if isinstance(raw, dict) else {}
            if names.get(nk or kind) != nm:
                names[nk or kind] = nm
                self.cfg.update({"dev_names": names})
        return i

    def start_player(self):
        self.stop_player()
        try:
            out_i = self._bind("out", "out_dev", "out")
            mon_i = self._bind("out", "mon_dev", "mon") if self.cfg.get("mon_on", True) else 0
            self.player = audio.Player(out_i, mon_idx=mon_i, log=util.log)
            self.player.duck = float(self.cfg.get("duck", 0.0))
            self.player.gain_tts = float(self.cfg.get("gain", 1.0))
            try:
                self.player.mon_gain = float(self.cfg.get("mon_gain", 1.0))
            except Exception:
                pass
            self.player.start()
            self.chain_a.player = self.player
            self.start_through()
        except Exception as e:                          # noqa: BLE001
            self.player = None
            util.log(f"播放设备打不开：{e!r}", "err", "audio")
            util.dbg(traceback.format_exc())

    def start_through(self):
        """只把「VRChat 那条线」放回耳机。

        为什么：B 链如果直接听扬声器回环，听到的是耳机里的一切（对方 + 自己的 TTS
        监听 + 系统提示音），自己的日语会被当成对方说话。让 VRChat 的输出走一根独立
        虚拟线（CABLE In 16ch），B 链只读那条线，我们自己再把那条线直通回耳机——
        用户照旧听得见对方，B 链身上只有对方。
        """
        self.stop_through()
        if not bool(self.cfg.get("pass_on", True)):
            return
        # 进程回环模式下 VRChat 的声音本来就进耳机，再直通一遍就是双份（自己听自己加倍）
        if str(self.cfg.get("loop_src", "process") or "process").lower() != "device":
            util.log("B 链走 VRChat 进程回环：声音本来就到你耳机里 → 不开直通", "info", "audio")
            return
        try:
            lo = self._dev("loop", "loop_dev", "loop")
            mo = self._dev("out", "mon_dev", "mon")
        except Exception:                               # noqa: BLE001
            return
        if not lo or not mo or lo == mo:
            return
        if not audio.is_virtual(lo):
            util.log("回环是耳机自己的 Loopback：VRChat 的声音本来就在耳机里 → 不开直通", "info", "audio")
            return
        # 同一个虚拟线的进出：直通会把「我们自己放进线的 TTS」再放回耳机（实测双份）
        try:
            src = audio.name_of("out", self._dev("out", "out_dev", "out"))
            dst = audio.name_of("loop", lo)
            if src and audio.cable_key(src) == audio.cable_key(dst):
                util.log(f"直通线和 TTS 输出是同一根虚拟线（{src}）→ 不开直通，否则自己听两遍", "info", "audio")
                return
        except Exception:                               # noqa: BLE001
            pass
        try:
            th = audio.Through(lo, mo, gain=float(self.cfg.get("pass_gain", 1.0)), log=util.log)
            th.duck_fn = (lambda: (float(self.cfg.get("pass_duck", 0.35))
                                   if (self.player is not None and self.player.playing) else 1.0))
            th.start()
            self.through = th
        except Exception as e:                          # noqa: BLE001
            self.through = None
            util.log(f"直通开不了：{e!r}", "warn", "audio")

    def stop_through(self):
        th = getattr(self, "through", None)
        if th is not None:
            try:
                th.stop()
            except Exception:                           # noqa: BLE001
                pass
            self.through = None

    def stop_player(self):
        self.stop_through()
        if self.player is not None:
            try:
                self.player.stop()
            except Exception:
                pass
            self.player = None

    def start_mic(self):
        self.stop_mic()
        try:
            idx = self._bind("in", "mic_dev", "in")
            self.seg_a = self.seg_a or Segmenter(self.cfg, self.chain_a.on_seg, name="A")
            self.seg_a.thr = float(self.cfg.get("mic_db", -32))
            self.seg_a.on_level = lambda db: self.bus.level("A", db)
            self._calib = {"kind": "mic", "t0": time.time(), "dbs": []}
            self.mic_cap = audio.Capture(idx, self._mic_block, log=util.log, kind="mic")
            self.mic_cap.start()
            self.bus.set_mic(on=bool(self.cfg.get("mic_on", True)), dev=idx, reason="")
        except Exception as e:                          # noqa: BLE001
            util.log(f"麦克风打不开：{e!r}", "err", "audio")
            util.dbg(traceback.format_exc())
            self.bus.set_mic(on=False, dev=None, reason=f"麦克风打不开 {e!r}"[:60])

    def stop_mic(self):
        if self.mic_cap is not None:
            try:
                self.mic_cap.stop()
            except Exception:
                pass
            self.mic_cap = None

    def start_loop(self):
        """B 链输入。默认走进程回环：只采 VRChat 这个进程树的声音。

        为什么不用扬声器回环：那会把用户自己的麦克风、自己的 TTS 监听、系统提示音
        一起喂给 B 链（用户投诉：「你把我的麦克风也监听进去了」），B 链就会把自己
        说的话再翻一遍。VB-CABLE 也没法拆成两根线（实测 19/20/24/26/27 全串通）。
        进程回环是 Windows 10 2004+ 的 AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK，
        精确到进程树，不需要额外驱动。
        """
        self.stop_loop()
        if not self.cfg.get("in_on", True):
            return
        self.seg_b = self.seg_b or Segmenter(self.cfg, self.chain_b.on_seg, name="B")
        self.seg_b.thr = float(self.cfg.get("loop_db", -24))
        self.seg_b.on_level = lambda db: self.bus.level("B", db)
        self._calib_b = {"kind": "loop", "t0": time.time(), "dbs": []}
        src = str(self.cfg.get("loop_src", "process") or "process").lower()
        if src != "device":
            self._start_proc_loop()
            return
        try:
            idx = self._bind("loop", "loop_dev", "loop")
            self.loop_cap = audio.Capture(idx, self._loop_block, log=util.log, kind="loop")
            self.loop_cap.start()
            if audio.is_virtual(idx):
                util.log(f"B 链只听 #{idx}（虚拟线回环）：VRChat 的「输出设备」要选 "
                         f"CABLE In 16ch，否则听不到对方；选好后这路声音由直通放回耳机",
                         "info", "audio")
        except Exception as e:                          # noqa: BLE001
            util.log(f"回环采集打不开（B 链停摆）：{e!r}", "err", "audio")
            util.dbg(traceback.format_exc())

    def _start_proc_loop(self) -> bool:
        """连 VRChat 进程回环。没连上不算错：VRChat 没开的时候本来就该安静。"""
        name = str(self.cfg.get("loop_proc", "VRChat.exe") or "VRChat.exe")
        try:
            pl = procloopmod.ProcLoop(name=name, on_pcm=self._proc_block, log=util.log, mono=True)
            pl.start()
        except Exception as e:                          # noqa: BLE001
            self.proc_loop = None
            self._proc_next = time.time() + 10.0
            util.log(f"VRChat 进程回环没连上：{e!r}（每 10 s 重试，VRChat 起来会自动接上）",
                     "warn", "audio")
            return False
        self.proc_loop = pl
        if self._proc_th is None or not self._proc_th.is_alive():
            self._proc_th = threading.Thread(target=self._proc_writer, name="procloop-feed",
                                             daemon=True)
            self._proc_th.start()
        util.log(f"B 链只听 VRChat 进程 pid={pl.pid}（{pl.init_format}）："
                 f"麦克风、系统音、自己的 TTS 都不会进 B 链", "info", "audio")
        return True

    def _proc_block(self, pcm, rate):
        """进程回环的事件线程（20 ms 一块、50 次/秒）里只丢队列，绝不在这里干活。"""
        try:
            self._proc_q.put_nowait((pcm, int(rate)))
        except queue.Full:
            try:
                self._proc_q.get_nowait()
                self._proc_q.put_nowait((pcm, int(rate)))
            except Exception:                           # noqa: BLE001
                pass

    def _proc_writer(self):
        while not self._stop.is_set():
            try:
                item = self._proc_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            pcm, rate = item
            try:
                self._loop_block(pcm, rate)
            except Exception as e:                      # noqa: BLE001
                util.log(f"B 链喂块出错：{e!r}", "warn", "audio")

    def stop_loop(self):
        if self.proc_loop is not None:
            try:
                self.proc_loop.stop()
            except Exception:                           # noqa: BLE001
                pass
            self.proc_loop = None
        if self.loop_cap is not None:
            try:
                self.loop_cap.stop()
            except Exception:
                pass
            self.loop_cap = None

    def _noise_tick(self, which, db):
        """开麦/开回环的头 0.9 s 量本底噪声，据此定门限。

        真机踩过：默认 -32 dB 的门限挡不住房间底噪，云端 ASR 会把空调声「听成」
        「时常使用短句。」「识别识别。」这种幻觉，还带着 7–10 s 的耗时把链路堵住。
        """
        c = self._calib if which == "mic" else self._calib_b
        if not c or not bool(self.cfg.get("auto_noise", True)):
            return
        c["dbs"].append(db)
        if time.time() - c["t0"] < 0.9:
            return
        if which == "mic":
            self._calib = None
        else:
            self._calib_b = None
        dbs = sorted(d for d in c["dbs"] if d > -95.0)
        if len(dbs) < 20:
            return
        floor = dbs[len(dbs) // 2]
        margin = float(self.cfg.get("noise_margin", 10.0))
        key = "mic_db" if which == "mic" else "loop_db"
        old = float(self.cfg.get(key, -32.0))
        # 麦克风门限不低于 -46：头戴麦说话峰值远高于它，再往下压只会收进环境噪声
        lo = -46.0 if which == "mic" else -48.0
        # 上界只到 -30：原来夹在 -18，放歌/连续人声时本底 -21 会把门限顶到 -18，
        # 结果所有正常音量的句子都被当「太安静」丢掉（B 链整段没字幕）。
        hi = -30.0
        # 音源本身就很吵（VRChat 在放歌）时，门限只能贴着本底走，不能高过它
        if floor > hi - 6.0:
            thr = floor + 4.0
        else:
            thr = floor + (margin if which == "mic" else min(margin, 8.0))
        thr = round(max(lo, min(hi, thr)), 1)
        seg = self.seg_a if which == "mic" else self.seg_b
        if abs(thr - old) < 1.0:
            util.log(f"{'麦克风' if which == 'mic' else '回环'}本底 {floor:.1f} dBFS，门限 {old:.1f} 不变",
                     "info", "vad")
            return
        self.cfg.update({key: thr})
        if seg is not None:
            seg.thr = thr
        util.log(f"{'麦克风' if which == 'mic' else '回环'}本底 {floor:.1f} dBFS → 门限 {thr:.1f}"
                 f"（原 {old:.1f}）", "info", "vad")

    def _env_push(self, kind, db):
        (self._env_mic if kind == "mic" else self._env_loop).append((time.time(), float(db)))

    def _out_echo(self, t0, t1):
        """麦克风 vs 我们自己刚放出去的音频：返回 (最大相关, 麦克风均值dB, 输出均值dB)。

        真实回声（耳机漏音、外放）在物理上就是那段合成音，包络随它一起起伏，
        两路 dB 曲线做时移相关（±0.6 s，覆盖声学延迟和缓冲抖动）会非常高；
        而用户把同一句话说第二遍时，包络是另一场「演奏」，和合成音几乎不相关。
        这是唯一能把「我自己在说话」和「我的声音漏回来」分开的判据。
        """
        try:
            import numpy as np
            pl = getattr(self, "player", None)
            if pl is None:
                return 0.0, -99.0, -99.0
            with getattr(pl, "env_lock", threading.Lock()):
                env = list(getattr(pl, "env", ()) or ())
            a = [(t, d) for t, d in list(self._env_mic) if t0 - 0.15 <= t <= t1 + 0.15]
            b = [(t, d) for t, d in env if t0 - 1.5 <= t <= t1 + 1.2]
            if len(a) < 8 or len(b) < 12:
                return 0.0, -99.0, -99.0
            ta = np.array([x[0] for x in a], dtype=np.float64)
            va = np.clip(np.array([x[1] for x in a], dtype=np.float64), -70.0, 0.0)
            tb = np.array([x[0] for x in b], dtype=np.float64)
            vb = np.clip(np.array([x[1] for x in b], dtype=np.float64), -70.0, 0.0)
            if va.std() < 0.5:
                return 0.0, float(va.mean()), -99.0
            best, bmean = 0.0, -99.0
            for lag in np.arange(-0.60, 0.61, 0.02):
                v2 = np.interp(ta + lag, tb, vb)
                if v2.std() < 1.5:
                    continue
                c = float(np.corrcoef(va, v2)[0, 1])
                if c == c and c > best:
                    best, bmean = c, float(v2.mean())
            return best, float(va.mean()), bmean
        except Exception:                       # noqa: BLE001
            return 0.0, -99.0, -99.0

    def _echo_match(self, audio, t0, t1):
        """麦克风那一段 vs 我们刚放出去的波形：归一化互相关（匹配滤波）。

        返回 (峰值相关, 相对偏移秒, 能量差 dB)。
        · 真实漏音 = 同一段音频 → 峰值 0.3~0.9（实测）；
        · 用户把同一句话说第二遍 = 完全不同的波形 → 峰值 <0.05。
        这是回声检测的主判据；电平包络 / 文本相似度都只是辅助。
        """
        try:
            import numpy as np
            pl = getattr(self, "player", None)
            if pl is None or audio is None:
                return 0.0, 0.0, 0.0
            y = np.asarray(audio)
            if y.dtype == np.int16:
                y = y.astype(np.float32) / 32768.0
            else:
                y = y.astype(np.float32)
            if y.ndim > 1:
                y = y.mean(axis=1)
            y8 = util.resample(y, 16000, 8000)
            if len(y8) < 1600:                       # 少于 0.2 s 不做，样本太少没意义
                return 0.0, 0.0, 0.0
            pt0, x = pl.hist_window(float(t0) - 1.2, float(t1) + 0.2)
            if len(x) < len(y8):
                return 0.0, 0.0, 0.0
            k0 = int(round((float(t0) - pt0) * 8000.0))
            span = int(1.2 * 8000)
            c, lag, gap = util.xcorr_norm(x, y8, k0 - span, k0 + span)
            return float(c), (lag - k0) / 8000.0, float(gap)
        except Exception:                            # noqa: BLE001
            return 0.0, 0.0, 0.0

    def _a_leak(self, t0, t1):
        """麦克风是不是在听同一路声音？返回 (相关系数, 麦克风均值dB, 线里均值dB)。"""
        try:
            import numpy as np
            a = [(t, d) for t, d in list(self._env_mic) if t0 - 0.15 <= t <= t1 + 0.15]
            b = [(t, d) for t, d in list(self._env_loop) if t0 - 0.15 <= t <= t1 + 0.15]
            if len(a) < 8 or len(b) < 8:
                return 0.0, -99.0, -99.0
            ta = np.array([x[0] for x in a])
            va = np.array([x[1] for x in a])
            va = np.clip(va, -70.0, 0.0)
            tb = np.array([x[0] for x in b])
            vb = np.clip(np.array([x[1] for x in b]), -70.0, 0.0)
            vb = np.interp(ta, tb, vb)
            if va.std() < 0.5 or vb.std() < 0.5:
                return 0.0, float(va.mean()), float(vb.mean())
            c = float(np.corrcoef(va, vb)[0, 1])
            return (0.0 if c != c else c), float(va.mean()), float(vb.mean())
        except Exception:                       # noqa: BLE001
            return 0.0, -99.0, -99.0

    def _mic_block(self, block, rate):
        try:
            mono = block.mean(axis=1) if getattr(block, "ndim", 1) == 2 else block
            _db = util.db_of(mono)
            self._env_push("mic", _db)
            self._noise_tick("mic", _db)
            on = bool(self.cfg.get("mic_on", True))
            if on and (bool(self.cfg.get("passthru")) or self.cfg.get("mode") == "pass"):
                if self.player is not None:
                    self.player.push_mic(mono)
            if on and self.seg_a is not None:
                self.seg_a.feed(mono, rate)
        except Exception as e:                          # noqa: BLE001
            util.log(f"麦克风回调：{e!r}", "warn", "audio")

    def _loop_block(self, block, rate):
        try:
            if not self.cfg.get("in_on", True):
                return
            mono = block.mean(axis=1) if getattr(block, "ndim", 1) == 2 else block
            _db = util.db_of(mono)
            self._env_push("loop", _db)
            self._noise_tick("loop", _db)
            if self.seg_b is not None:
                self.seg_b.feed(mono, rate)
        except Exception as e:                          # noqa: BLE001
            util.log(f"回环回调：{e!r}", "warn", "audio")

    # ================================================================ 周期
    def _proc_watch(self):
        """VRChat 重启 / 换音频设备都会让进程回环失效（running 变 False），要重建。"""
        if str(self.cfg.get("loop_src", "process") or "process").lower() == "device":
            return
        if not self.cfg.get("in_on", True):
            return
        if time.time() < float(self._proc_next or 0.0):
            return
        pl = self.proc_loop
        if pl is not None and pl.running:
            return
        if pl is not None:
            util.log("VRChat 进程回环断了（VRChat 退出或换了音频设备）→ 重建", "warn", "audio")
            try:
                pl.stop()
            except Exception:                           # noqa: BLE001
                pass
            self.proc_loop = None
        self._start_proc_loop()

    # ---------------------------------------------------------------- 内存看门狗
    # 这台机器只有 16 GB，VRChat + 本地 ASR + OCR + 两个 Qt 子进程一起跑很容易撞顶，
    # 一旦真的分配不出来就是 MemoryError 整个进程消失（网页直接打不开）。
    # 所以主动盯着自己：软线先释放能释放的，硬线自己退出让 run.bat 的循环把它拉起来。
    def _rss_mb(self):
        try:
            import ctypes
            import ctypes.wintypes as wt

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            h = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return c.WorkingSetSize / 1048576.0
        except Exception:                       # noqa: BLE001
            pass
        return 0.0

    def _mem_watch(self):
        soft = float(self.cfg.get("mem_soft_mb", 900.0))
        hard = float(self.cfg.get("mem_hard_mb", 1500.0))
        peak = 0.0
        while not self._stop.is_set():
            self._stop.wait(15.0)
            if self._stop.is_set():
                return
            mb = self._rss_mb()
            peak = max(peak, mb)
            if mb <= 0:
                continue
            if mb >= soft:
                freed = []
                try:
                    if self.asr.ja is not None and self.asr.ja.unload():   # 精修模型 约 1.1GB
                        freed.append("日语精修")
                except Exception:                   # noqa: BLE001
                    pass
                try:
                    if self.asr.local.unload():     # 本地识别模型 228MB
                        freed.append("本地识别")
                except Exception:                   # noqa: BLE001
                    pass
                try:
                    import gc
                    gc.collect()
                except Exception:                   # noqa: BLE001
                    pass
                util.log(f"内存 {mb:.0f}MB 偏高（峰值 {peak:.0f}MB）→ 已释放{'/'.join(freed) or '缓存'}",
                         "warn", "mem")
            if mb >= hard:
                util.log(f"内存 {mb:.0f}MB 超过硬线 {hard:.0f}MB → 主动重启后端（日志不会被清，"
                         f"run.bat 会在 2 秒内拉起）", "err", "mem")
                try:
                    self.quit()
                except Exception:                   # noqa: BLE001
                    pass
                os._exit(9)
                return
            if self.cfg.get("dbg_timing", False):
                util.log(f"内存 {mb:.0f}MB", "info", "mem")

    def _tick(self):
        last_ping = 0.0
        last_proc = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_proc > 2.0:
                last_proc = now
                try:
                    self._proc_watch()
                except Exception as e:                  # noqa: BLE001
                    util.log(f"进程回环守护出错：{e!r}", "warn", "audio")
            if now - last_ping > 5.0:
                last_ping = now
                self.hud_send({"op": "ping"})
                try:                                        # 精修模型空闲就还显存给 VRChat
                    if self.asr.idle():
                        util.log("日语精修模型空闲，已释放显存", "info", "asr.whisper")
                except Exception:                           # noqa: BLE001
                    pass
                self.danmaku_send({"op": "ping"})
                self.overlay_send({"op": "ping"})
                if self.cfg.get("hud_on", True) and self.use_hud:
                    if self.hud is None or self.hud.poll() is not None:
                        self.hud_start()
                elif self.hud is not None:
                    self.hud_stop()
                if self.cfg.get("danmaku", True) and self.use_hud:
                    if self.danmaku is None or self.danmaku.poll() is not None:
                        self.danmaku_start()
                elif self.danmaku is not None:
                    self.danmaku_stop()
                if self.cfg.get("ocr_overlay", True) and self.use_hud:
                    if self.overlay is None or self.overlay.poll() is not None:
                        self.overlay_start()
                    else:
                        self.overlay_send(self._overlay_cfg())
                elif self.overlay is not None:
                    self.overlay_stop()
            time.sleep(0.25)

    # ================================================================ 浮窗
    def hud_start(self):
        self.hud_stop()
        if not self.use_hud:
            return
        if not childmod.exists("hud"):
            util.log("hud 子程序不在，浮窗跳过", "warn", "hud")
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
        self.hud_log = open(os.path.join(cfgmod.OUT, "hud.log"), "ab")
        try:
            self.hud = subprocess.Popen(childmod.argv("hud"),
                                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                    stderr=self.hud_log, cwd=ROOT, env=env,
                                                    creationflags=CREATE_NO_WINDOW)
            self.hud_send({"op": "cfg", "alpha": int(self.cfg.get("hud_alpha", 92)),
                           "scale": int(self.cfg.get("hud_scale", 100)),
                           "rows": int(self.cfg.get("hud_rows", 8)),
                           "click": bool(self.cfg.get("hud_click", False)),
                           "x": int(self.cfg.get("hud_x", -1)),
                           "y": int(self.cfg.get("hud_y", -1))})
            self.hud_send({"op": "mic", "on": bool(self.cfg.get("mic_on", True))})
            util.log(f"浮窗起 pid={self.hud.pid}", "info", "hud")
        except Exception as e:                          # noqa: BLE001
            self.hud = None
            util.log(f"浮窗起不来：{e!r}", "warn", "hud")

    def hud_stop(self):
        if self.hud is not None:
            try:
                self.hud_send({"op": "quit"})
                self.hud.terminate()
            except Exception:
                pass
            self.hud = None

    def hud_send(self, obj):
        if self.hud is None or self.hud.stdin is None:
            return
        try:
            with self._io_lk:
                self.hud.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.hud.stdin.flush()
        except Exception:
            self.hud = None

    # ---------------------------------------------------------------- C 链原位浮现
    @staticmethod
    def _dpr():
        """系统 DPI 缩放（150% → 1.5）。截图是物理像素，Qt 窗口是逻辑像素。"""
        try:
            import ctypes
            d = ctypes.windll.user32.GetDpiForSystem()
            if d:
                return float(d) / 96.0
        except Exception:                           # noqa: BLE001
            pass
        return 1.0

    def _ocr_box_screen(self, box):
        """region 相对（物理像素）→ 屏幕绝对（逻辑像素）。"""
        r = self.cfg.get("ocr_region") or [0, 0, 0, 0]
        d = self._dpr()
        b = list(box or [0, 0, 0, 0]) + [0, 0, 0, 0]
        return ((float(r[0]) + float(b[0])) / d, (float(r[1]) + float(b[1])) / d,
                float(b[2]) / d, float(b[3]) / d)

    def _overlay_cfg(self):
        return {"op": "cfg",
                "hold": float(self.cfg.get("ocr_hold", 3.5)),
                "fade": float(self.cfg.get("ocr_fade", 0.6)),
                "show_src": bool(self.cfg.get("ocr_overlay_src", True)),
                "font": int(self.cfg.get("ocr_font", 17)),
                "max_live": int(self.cfg.get("ocr_max_live", 12))}

    def overlay_start(self):
        self.overlay_stop()
        if not self.use_hud or not bool(self.cfg.get("ocr_overlay", True)):
            return
        if not childmod.exists("ocr_overlay"):
            util.log("ocr_overlay 子程序不在，原位浮现跳过", "warn", "overlay")
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.overlay_log = open(os.path.join(cfgmod.OUT, "overlay.log"), "ab")
        try:
            self.overlay = subprocess.Popen(childmod.argv("ocr_overlay"),
                                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                    stderr=self.overlay_log, cwd=ROOT, env=env,
                                                    creationflags=CREATE_NO_WINDOW)
            self.overlay_send(self._overlay_cfg())
            util.log(f"C 链原位浮现起 pid={self.overlay.pid}", "info", "overlay")
        except Exception as e:                      # noqa: BLE001
            self.overlay = None
            util.log(f"原位浮现起不来：{e!r}", "warn", "overlay")

    def overlay_stop(self):
        if self.overlay is not None:
            try:
                self.overlay_send({"op": "quit"})
                self.overlay.terminate()
            except Exception:
                pass
            self.overlay = None

    def overlay_send(self, obj):
        if self.overlay is None or self.overlay.stdin is None:
            return
        try:
            with self._io_lk:
                self.overlay.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.overlay.stdin.flush()
        except Exception:
            self.overlay = None

    def _ocr_stable_id(self, key, box):
        """同一个屏幕位置始终用同一个 id。

        实测：同一行字被 det 切成两块、跨帧重识别一次，key 就变了 → 原位浮现会在同一处
        叠三四个气泡。这里按「框重叠 >= 50% 就算同一处」把新 key 影射回旧 id。
        """
        now = time.time()
        x, y, w, h = self._ocr_box_screen(box)
        best, best_ov = None, 0.0
        for k, (bx, by, bw, bh, ts) in list(self._ocr_ids.items()):
            if now - ts > 8.0:
                self._ocr_ids.pop(k, None)
                continue
            ix = max(0.0, min(x + w, bx + bw) - max(x, bx))
            iy = max(0.0, min(y + h, by + bh) - max(y, by))
            inter = ix * iy
            small = max(1.0, min(w * h, bw * bh))
            ov = inter / small
            if ov > best_ov:
                best, best_ov = k, ov
        if best is not None and best_ov >= 0.5:
            self._ocr_ids[best] = (x, y, w, h, now)
            return best
        self._ocr_ids[key] = (x, y, w, h, now)
        return key

    def _ocr_raw(self, items):
        """OCR 一出来就先把原文浮在原处（大约比译文早 0.2-0.4 s）。"""
        if self.overlay is None:
            return
        out = []
        for it in items or []:
            x, y, w, h = self._ocr_box_screen(it.get("box"))
            out.append({"id": self._ocr_stable_id(it.get("key"), it.get("box")),
                        "x": round(x, 1), "y": round(y, 1),
                        "w": round(w, 1), "h": round(h, 1), "src": it.get("src", "")})
        if out:
            self.overlay_send({"op": "show", "items": out})

    def _ocr_show(self, key, src, dst, box, mt_ms=None):
        """译文到位：同一个 id 就地替换成中文（气泡重新计时）。"""
        if self.overlay is None or not dst:
            return
        x, y, w, h = self._ocr_box_screen(box)
        key = self._ocr_stable_id(key, box)
        self.overlay_send({"op": "show", "items": [
            {"id": key, "x": round(x, 1), "y": round(y, 1), "w": round(w, 1),
             "h": round(h, 1), "src": src, "dst": dst}]})
        if self.cfg.get("dbg_timing"):
            cap = 0.0
            try:
                cap = float(self.ocr.stats().get("cap_ts") or 0.0)
            except Exception:                       # noqa: BLE001
                cap = 0.0
            ms = (time.time() - cap) * 1000.0 if cap else 0.0
            ocr_ms = 0.0
            try:
                s2 = self.ocr.stats()
                ocr_ms = float(s2.get("last_ms") or 0.0) + float(s2.get("last_cap_ms") or 0.0)
            except Exception:                   # noqa: BLE001
                ocr_ms = 0.0
            util.log(f"C 链抓屏→浮现 {ms:.0f}ms（识别 {ocr_ms:.0f} + 翻译 {float(mt_ms or 0):.0f}）"
                     f"框({x:.0f},{y:.0f} {w:.0f}x{h:.0f}) '{dst[:16]}'", "info", "overlay")

    # ---------------------------------------------------------------- 弹幕
    def danmaku_start(self):
        self.danmaku_stop()
        if not self.use_hud:
            return
        if not childmod.exists("danmaku"):
            util.log("danmaku 子程序不在，弹幕跳过", "warn", "danmaku")
            return
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
        self.danmaku_log = open(os.path.join(cfgmod.OUT, "danmaku.log"), "ab")
        try:
            self.danmaku = subprocess.Popen(childmod.argv("danmaku"),
                                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                    stderr=self.danmaku_log, cwd=ROOT, env=env,
                                                    creationflags=CREATE_NO_WINDOW)
            self.danmaku_send(self._danmaku_cfg())
            util.log(f"弹幕浮窗起 pid={self.danmaku.pid}", "info", "danmaku")
        except Exception as e:                          # noqa: BLE001
            self.danmaku = None
            util.log(f"弹幕浮窗起不来：{e!r}", "warn", "danmaku")

    def _danmaku_cfg(self):
        return {"op": "cfg",
                "lanes": int(self.cfg.get("danmaku_lanes", 4)),
                "dur_s": float(self.cfg.get("danmaku_speed", 7.0)),
                "top_ratio": float(self.cfg.get("danmaku_top", 0.12)),
                "max_live": int(self.cfg.get("danmaku_max", 6)),
                # 大小与样式：弹幕自己的旋钮，不跟字幕浮窗共用
                "scale": int(self.cfg.get("danmaku_scale", 100)),
                "alpha": int(self.cfg.get("danmaku_alpha", 100)),
                "style": str(self.cfg.get("danmaku_style", "card") or "card")}

    def danmaku_stop(self):
        if self.danmaku is not None:
            try:
                self.danmaku_send({"op": "quit"})
                self.danmaku.terminate()
            except Exception:
                pass
            self.danmaku = None

    def danmaku_send(self, obj):
        if self.danmaku is None or self.danmaku.stdin is None:
            return
        try:
            with self._io_lk:
                self.danmaku.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.danmaku.stdin.flush()
        except Exception:
            self.danmaku = None

    def _fanout(self):
        """总线上所有的段/麦克风/清屏事件 → 浮窗与弹幕（同一套 stdin 协议）。"""
        sid, q = self.bus.sub()
        try:
            while not self._stop.is_set():
                try:
                    ev = q.get(timeout=0.5)
                except Exception:
                    continue
                k = ev.get("k")
                if k == "seg":
                    seg = ev.get("seg") or {}
                    self.hud_send({"op": "seg", "seg": seg})
                    if not self.cfg.get("danmaku_src", True) and seg.get("src"):
                        seg = dict(seg)
                        seg["src"] = ""          # 只要译文：副行原文关掉
                    self.danmaku_send({"op": "seg", "seg": seg})
                elif k == "mic":
                    self.hud_send({"op": "mic", "on": bool(ev.get("on", True))})
                elif k == "clear":
                    self.hud_send({"op": "clear"})
                    self.danmaku_send({"op": "clear"})
        finally:
            self.bus.unsub(sid)

    # ================================================================ 状态
    def devices(self):
        t, d = self._dev_cache
        if d is not None and time.time() - t < 4.0:
            return d
        d = audio.list_devices()
        self._dev_cache = (time.time(), d)
        return d

    def host_cond(self):
        """机器条件（缓存 60 s）：控制台要能看到「这台机器能跑什么、选了什么」。"""
        g = getattr(self, "_host_c", None)
        if g and (time.time() - g[0]) < 60.0:
            return g[1]
        try:
            from . import asr_engines
            v = asr_engines.probe()
            v = {"cores": v.get("cores"), "ram_gb": round((v.get("ram_total_mb") or 0) / 1024.0, 1),
                 "ram_free_mb": v.get("ram_free_mb"), "vram_free_mb": v.get("vram_free_mb"),
                 "gpu": v.get("gpu")}
        except Exception:                          # noqa: BLE001
            v = {}
        self._host_c = (time.time(), v)
        return v

    def state(self):
        c = cfgmod.clamp_cfg(self.cfg.all())
        segs = self.bus.segs
        return {
            "ok": True, "ver": version.short(), "ver_full": version.full(),
            "frozen": version.frozen(), "pid": os.getpid(),
            "uptime_s": round(time.time() - self.t0, 1),
            "cfg": c, "devices": self.devices(), "voices": cfgmod.VOICES,
            "metrics": {k: metrics.summarize(segs, k) for k in ("A", "B", "C")},
            "hud": {"on": bool(c.get("hud_on")), "pid": getattr(self.hud, "pid", 0) or 0,
                    "alive": bool(self.hud is not None and self.hud.poll() is None)},
            "danmaku": {"on": bool(c.get("danmaku")),
                        "pid": getattr(self.danmaku, "pid", 0) or 0,
                        "alive": bool(self.danmaku is not None and self.danmaku.poll() is None)},
            "overlay": {"on": bool(c.get("ocr_overlay")),
                        "pid": getattr(self.overlay, "pid", 0) or 0,
                        "alive": bool(self.overlay is not None and self.overlay.poll() is None),
                        "hold": float(c.get("ocr_hold", 3.5)),
                        "fade": float(c.get("ocr_fade", 0.6))},
            "ocr": {"mode": str(c.get("ocr_mode", "hotkey")),
                    "src": str(c.get("ocr_src", "auto")),
                    "on": bool(c.get("ocr_on")),
                    "region": list(c.get("ocr_region") or []),
                    "ready": bool(self.ocr is not None),
                    "vl": int((self.ocr.stats() or {}).get("vl", 0)) if self.ocr is not None else 0,
                    "vl_ms": float((self.ocr.stats() or {}).get("last_vl_ms", 0.0)) if self.ocr is not None else 0.0,
                    "strip": bool(c.get("ocr_label_strip", False))},
            "key": dict(getattr(self.hotkeys, "last", {}) or {}),
            "osc": {"on": bool(c.get("chatbox_on")), "host": c.get("osc_host"),
                    "port": c.get("osc_port"), "stat": dict(getattr(self.osc, "stat", {}) or {})},
            "last": self.bus.recent(50),
            "mic": dict(self.bus.mic), "status": dict(self.bus.status),
            "asr": {"engine": self.asr.engine(), "local": self.asr.local.note,
                    "a": self.chain_a.engine(), "b": self.chain_b.engine(),
                    # 引擎选档（注册表按这台机器的条件算出来的，换机器就换答案）
                    "pick": {lg: (self.asr.pick_local(lg)[0] or "-") for lg in ("zh", "ja")},
                    "host": self.host_cond(),
                    # 注意：这里绝不能调 refiner()——它会把精修模型load进来，
                    # 光是打开控制台就吃掉 1.1 GB 显存。只报「已经载着的」状态。
                    "refine": str(c.get("asr_refine", "auto")),
                    "ja": (self.asr.ja.stat() if self.asr.ja is not None
                           else {"device": "-", "note": "未加载"})},
            "loop": {"src": str(c.get("loop_src", "process")),
                     "proc": str(c.get("loop_proc", "VRChat.exe")),
                     "pid": int(getattr(self.proc_loop, "pid", 0) or 0),
                     "alive": bool(self.proc_loop is not None and self.proc_loop.running),
                     "stat": dict(getattr(self.proc_loop, "stats", {}) or {})},
            "logs": util.recent_logs(30),
        }

    def _st_event(self):
        c = cfgmod.clamp_cfg(self.cfg.all())
        segs = self.bus.segs
        self.bus.emit("st", {"cfg": c, "metrics": {k: metrics.summarize(segs, k)
                                                   for k in ("A", "B", "C")}})

    def api_metrics(self, send):
        segs = self.bus.segs
        send({"ok": True, "A": metrics.summarize(segs, "A"),
              "B": metrics.summarize(segs, "B"), "C": metrics.summarize(segs, "C"),
              "rows": self.bus.recent(50)})

    # ================================================================ 密钥
    def api_env(self, body):
        r"""看/写密钥。这是安装版里唯一必须手填的东西。

        - GET（空 body）：返回状态，密钥只给后 6 位。
        - POST {"key": "sk-...", "host": "..."}：写 %LOCALAPPDATA%\VRDub\config\.env.local
          并热生效，不用重启；exe 里永不内置任何 key。
        """
        body = body if isinstance(body, dict) else {}
        key = str(body.get("key") or "").strip()
        host = str(body.get("host") or "").strip()
        if key or host:
            cur = {}
            try:
                cur = cfgmod._load_env(pathmod.ENV_FILE) or {}
            except Exception:                       # noqa: BLE001
                cur = {}
            if key:
                cur["DASHSCOPE_TOKENPLAN_KEY"] = key
            if host:
                cur["DASHSCOPE_TOKENPLAN_BASE"] = host
            os.makedirs(pathmod.CONFIG_DIR, exist_ok=True)
            tmp = pathmod.ENV_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("# VRDub 本地密钥（只在这台机器上，不要提交到仓库）\n")
                for k, v in cur.items():
                    fh.write(f"{k}={v}\n")
            os.replace(tmp, pathmod.ENV_FILE)
            if key:
                self.cfg.key = key
            if host:
                self.cfg.host = cfgmod._host_of(host)
            try:
                self.net.reset(host=self.cfg.host, key=self.cfg.key)
            except Exception:                       # noqa: BLE001
                pass
            util.log(f"密钥已保存到 {pathmod.ENV_FILE}（后 6 位 …{self.cfg.key[-6:]}，"
                     f"端点 {self.cfg.host}）", "info", "key")
            # 换完 key 立刻验一次，别让用户猜为什么还是没声音
            try:
                st, _ = self.net.post("/compatible-mode/v1/chat/completions",
                                      {"model": self.cfg.get("mt_model") or "qwen3.6-flash",
                                       "messages": [{"role": "user", "content": "hi"}],
                                       "max_tokens": 1, "stream": False},
                                      timeout=12.0, retries=0)
                self._key_ok = (st == 200)
                util.log(f"密钥连通性自检：HTTP {st}", "info" if st == 200 else "err", "key")
            except Exception as e:                  # noqa: BLE001
                self._key_ok = False
                util.log(f"密钥连通性自检失败：{e!r}", "err", "key")
        k = self.cfg.key or ""
        probe = getattr(self, "_key_ok", None)
        return {"ok": True, "has_key": bool(k), "tail": k[-6:] if k else "",
                "host": self.cfg.host, "path": pathmod.ENV_FILE,
                "probe": "ok" if probe is True else ("fail" if probe is False else "未测")}

    # ================================================================ 改配置
    def api_cfg(self, patch):
        patch = {k: v for k, v in (patch or {}).items() if k in cfgmod.DEFAULTS}
        # 控制台那张「识别方式（默认）」是两条链一起改的快捷方式
        if "asr_engine" in patch:
            for k in ("asr_engine_a", "asr_engine_b"):
                patch.setdefault(k, patch["asr_engine"])
        changed = self.cfg.update(patch)
        if changed:
            cl = cfgmod.clamp_cfg(self.cfg.all())
            self.cfg.update({k: cl[k] for k in cl})
            # 用户手选设备：把名字一起记住（Windows 下次启动号码会变）
            for key, kind, nk in (("mic_dev", "in", "in"), ("out_dev", "out", "out"),
                                  ("mon_dev", "out", "mon"), ("loop_dev", "loop", "loop")):
                if key not in changed:
                    continue
                try:
                    nm = audio.name_of(kind, int(self.cfg.get(key)))
                except Exception:                       # noqa: BLE001
                    nm = ""
                if nm:
                    raw = self.cfg.get("dev_names")
                    names = dict(raw) if isinstance(raw, dict) else {}
                    names[nk] = nm
                    self.cfg.update({"dev_names": names})
            self.apply(changed)
            self._st_event()
            util.log("配置更新：" + ", ".join(f"{k}={v}" for k, v in changed.items()), "info", "cfg")
        return {"ok": True, "cfg": cfgmod.clamp_cfg(self.cfg.all())}

    def apply(self, changed):
        keys = set(changed)
        if keys & {"out_dev", "mic_dev", "loop_dev", "in_on", "mon_dev", "mon_on",
                    "pass_on", "pass_gain", "loop_src", "loop_proc"}:
            self.reopen_audio()
        if "mic_on" in keys:
            self.bus.set_mic(on=bool(self.cfg.get("mic_on", True)),
                             dev=self.cfg.get("mic_dev"), reason="")
        if self.player is not None:
            self.player.duck = float(self.cfg.get("duck", 0.0))
            self.player.gain_tts = float(self.cfg.get("gain", 1.0))
            try:
                self.player.mon_gain = float(self.cfg.get("mon_gain", 1.0))
            except Exception:
                pass
        if self.seg_a is not None and "mic_db" in keys:
            self.seg_a.thr = float(self.cfg.get("mic_db", -32))
        if self.seg_b is not None and "loop_db" in keys:
            self.seg_b.thr = float(self.cfg.get("loop_db", -24))
        if keys & {"hud_alpha", "hud_scale", "hud_rows", "hud_click", "hud_x", "hud_y", "hud_on"}:
            if self.cfg.get("hud_on", True):
                if self.hud is None:
                    self.hud_start()
                else:
                    self.hud_send({"op": "cfg", "alpha": int(self.cfg.get("hud_alpha", 92)),
                                   "scale": int(self.cfg.get("hud_scale", 100)),
                                   "rows": int(self.cfg.get("hud_rows", 8)),
                                   "click": bool(self.cfg.get("hud_click", False)),
                                   "x": int(self.cfg.get("hud_x", -1)),
                                   "y": int(self.cfg.get("hud_y", -1))})
            else:
                self.hud_stop()
        if keys & {"ocr_overlay", "ocr_overlay_src", "ocr_hold", "ocr_fade", "ocr_font",
                    "ocr_max_live", "ocr_region"}:
            if self.cfg.get("ocr_overlay", True) and self.use_hud:
                if self.overlay is None or self.overlay.poll() is not None:
                    self.overlay_start()
                else:
                    self.overlay_send(self._overlay_cfg())
            elif self.overlay is not None:
                self.overlay_stop()
            util.log("原位浮现：" + ("开" if (self.cfg.get("ocr_overlay", True) and self.overlay)
                                    else "关"), "info", "overlay")
        if keys & {"danmaku", "danmaku_speed", "danmaku_lanes", "danmaku_top",
                   "danmaku_max", "danmaku_scale", "danmaku_alpha", "danmaku_style"}:
            if self.cfg.get("danmaku", True) and self.use_hud:
                if self.danmaku is None or self.danmaku.poll() is not None:
                    self.danmaku_start()
                else:
                    self.danmaku_send(self._danmaku_cfg())
            elif self.danmaku is not None:
                self.danmaku_stop()
            util.log("弹幕浮窗：" + ("开" if self.cfg.get("danmaku", True) else "关"),
                     "info", "danmaku")
        if keys & {"chatbox_on", "chatbox_notify", "osc_host", "osc_port"}:
            self.osc.host = str(self.cfg.get("osc_host", "127.0.0.1"))
            self.osc.port = int(self.cfg.get("osc_port", 9000))
            self.osc.refresh()
            if self.osc.on:
                self.osc.start()
            util.log("chatbox 输出：" + ("开" if self.osc.on else "关"), "info", "osc")
        if "ocr_on" in keys or "ocr_region" in keys or "ocr_mode" in keys or "ocr_src" in keys:
            # ocr_mode 从 hotkey 切到 poll 必须重建：轮询线程只在 start() 里起，
            # 复用旧实例会「配置改了但一帧都不抓」（实测 10 s 零输出）。
            self.apply_ocr(force=True)

    def reopen_audio(self):
        """换设备：先把三路流全关掉、重建 PortAudio 实例，再按新设备开。

        WASAPI 在「还有别的流开着」时换设备会偶发 OSError -9999（实测），
        所以不能一路一路地 close/open，必须整体重来。
        """
        self.stop_player()
        self.stop_mic()
        self.stop_loop()
        time.sleep(0.08)
        try:
            audio.reset_pa()
        except Exception as e:                          # noqa: BLE001
            util.log(f"音频实例重建失败：{e!r}", "warn", "audio")
        self.start_player()
        self.start_mic()
        self.start_loop()

    # ================================================================ 麦克风 / 链
    def set_mic(self, on):
        self.cfg.update({"mic_on": bool(on)})
        self.bus.set_mic(on=bool(on))
        self.hud_send({"op": "mic", "on": bool(on)})
        util.log("麦克风 " + ("开" if on else "闭"), "info", "mic")
        self._st_event()
        return {"ok": True, "mic_on": bool(on)}

    def api_mic(self, body):
        return self.set_mic(bool((body or {}).get("on", not self.cfg.get("mic_on", True))))

    def api_chains(self, body):
        patch = {k: bool(v) for k, v in (body or {}).items()
                 if k in ("out_on", "in_on", "ocr_on")}
        self.api_cfg(patch)
        return {"ok": True, **{k: bool(self.cfg.get(k)) for k in ("out_on", "in_on", "ocr_on")}}

    def api_dk_preview(self, body):
        """弹幕试放：只往总线上丢一条假段，不合成、不出声、不花钱。
        改样式/大小/透明度时用它对着屏幕调，比等自己说一句话快得多。"""
        txt = str(body.get("dst") or "").strip() or "テスト、この字幕は見えますか？"
        src = str(body.get("src") or "").strip() or "测试，这个字幕看得见吗？"
        ch = str(body.get("chain") or "A").upper()[:1]
        if ch not in ("A", "B", "C"):
            ch = "A"
        seg = self.bus.new_seg(ch, src_lang="zh", dst_lang="ja", engine="preview")
        self.bus.upd(seg, src=src, dst=txt, state="done")
        return {"ok": True, "id": seg.get("id")}

    def api_inject(self, body):
        """闭环自测：合成一句（默认中文）直接从 out_dev 播出去，让对面那条线真的收到声音。

        用途：不接真人也能验「A 链 = 麦 → 识别 → 翻译 → 日语出声」整条链路。
        """
        body = body or {}
        text = (body.get("text") or "这是回环自测，一二三四五。").strip()
        lang = body.get("lang") or "zh"
        voice = body.get("voice") or self.cfg.get("voice")
        t0 = time.time()
        # 走真链路（除 ASR 外）：翻译 → 上字幕/弹幕 → 合成 → 播进 out_dev → 发 chatbox
        dst_lang = str(self.cfg.get("dst_lang", "ja")) if lang == "zh" else "zh"
        seg = self.bus.new_seg("A", src_lang=lang, dst_lang=dst_lang, engine="inject")
        self.bus.upd(seg, src=text, state="mt")
        dst = text
        mt_ms = 0.0
        try:
            tr = self.mt.translate(text, src=lang, dst=dst_lang, card=self.cfg.get("card"))
            dst = tr.get("text") or text
            mt_ms = float(tr.get("ms") or 0.0)
        except Exception as e:                          # noqa: BLE001
            util.log(f"注入翻译失败：{e!r}", "warn", "test")
        self.bus.upd(seg, dst=dst, mt_ms=round(mt_ms, 1), state="speak")
        pcm, first, total = self.synth.synth(dst, voice, dst_lang,
                                             rate=(float(self.cfg.get("speed", 1.0)) or None))
        audio_s = len(pcm) / 2.0 / 24000.0
        dev = int(body.get("dev") or self.cfg.get("out_dev", 19))
        if self.player is not None and not body.get("dev"):
            # 走播放环：和 A 链真出声同一条路（CABLE Input + 监听耳机都拿到）
            self.player.push_tts_pcm(pcm, 24000)
        else:
            threading.Thread(target=lambda: audio.play_pcm(dev, pcm, 24000,
                                                           float(self.cfg.get("gain", 1.0)),
                                                           log=util.log),
                             name="inject", daemon=True).start()
        if self.osc is not None and dst:
            # 只有「我说的话」（中文→日语）才进 chatbox；
            # 注入日语是模拟对方说话，那是 B 链的活儿，绝不能回灌聊天框。
            if lang == "zh" and str(dst_lang) != "zh":
                try:
                    self.osc.send(dst)
                except Exception:
                    pass
        e2e = (time.time() - t0) * 1000.0
        self.bus.upd(seg, state="done", tts_ms=round(first, 1), audio_ms=round(audio_s * 1000.0),
                     e2e_ms=round(e2e, 1), note="注入测试")
        res = {"ok": True, "text": text, "dst": dst, "lang": lang, "audio_s": round(audio_s, 2),
               "dev": dev, "first_pkt_ms": round(first, 1), "mt_ms": round(mt_ms, 1),
               "chatbox": bool(self.osc is not None and getattr(self.osc, "on", False)),
               "ms": round((time.time() - t0) * 1000, 1)}
        util.log(f"回环注入：{text[:20]} → {dst[:20]} → #{dev} {audio_s:.1f}s", "info", "test")
        return res

    # ================================================================ 试听
    def api_tts_test(self, body):
        body = body or {}
        text = (body.get("text") or "こんにちは、聞こえてますか。こちらはテストです。").strip()
        lang = body.get("lang") or self.cfg.get("dst_lang", "ja")
        voice = body.get("voice") or self.cfg.get("voice")
        play = bool(body.get("play", True))
        try:
            spd = float(body.get("speed") or self.cfg.get("speed", 1.0))
        except Exception:
            spd = 1.0
        rate = None if abs(spd - 1.0) < 0.02 else max(0.6, min(1.6, spd))
        t0 = time.time()
        pcm, first, total = self.synth.synth(text, voice, lang, rate=rate)
        ms = (time.time() - t0) * 1000.0
        audio_s = len(pcm) / 2.0 / 24000.0
        res = {"ok": True, "ms": round(ms, 1), "first_pkt_ms": round(first, 1),
               "audio_s": round(audio_s, 2), "text": text, "lang": lang,
               "speed": round(spd, 2), "rate": rate}
        self.bus.emit("tts_test", res)
        util.log(f"试听：首包 {first:.0f}ms / 共 {ms:.0f}ms / {audio_s:.1f}s 音频", "info", "tts")
        if play and pcm:
            dev = int(self.cfg.get("mon_dev", 18))
            threading.Thread(target=lambda: audio.play_pcm(dev, pcm, 24000,
                                                           float(self.cfg.get("gain", 1.0)),
                                                           log=util.log),
                             name="tts-test", daemon=True).start()
        return res

    # ================================================================ OCR
    def apply_ocr(self, force: bool = False):
        on = bool(self.cfg.get("ocr_on")) and bool(self.cfg.get("in_on", True))
        if force and self.ocr is not None:
            try:
                self.ocr.stop()
            except Exception:                           # noqa: BLE001
                pass
            self.ocr = None
        if on and self.ocr is None:
            try:
                from .ocr import OcrChain
                self.ocr = OcrChain(self.cfg, on_text=self.ocr_bridge.on_text,
                                    on_clear=self.ocr_bridge.on_clear, log=util.log,
                                    translator=self.mt,
                                    on_raw=self.ocr_bridge.on_raw,   # 原文先浮，不等翻译
                                    net=self.net)
                self.ocr.start()
                util.log("屏幕翻译（C 链）开", "info", "ocr")
            except Exception as e:                      # noqa: BLE001
                self.ocr = None
                util.log(f"C 链起不来：{e!r}", "err", "ocr")
        elif not on and self.ocr is not None:
            try:
                self.ocr.stop()
            except Exception:
                pass
            self.ocr = None
            util.log("屏幕翻译（C 链）停", "info", "ocr")

    def _ensure_ocr(self):
        """单帧识别 / 截图用：C 链总开关关着也要能建一个「只干这一下」的链。

        以前这里直接调 apply_ocr()，而 apply_ocr 的开链条件是
        ocr_on and in_on——C 链关着的时候 F6 只会回 ocr_not_ready，
        用户看到的就是「按了没反应」。单帧本来就该随手可用。
        """
        if self.ocr is not None:
            return self.ocr
        try:
            from .ocr import OcrChain
            self.ocr = OcrChain(self.cfg, on_text=self.ocr_bridge.on_text,
                                on_clear=self.ocr_bridge.on_clear, log=util.log,
                                translator=self.mt,
                                on_raw=self.ocr_bridge.on_raw,
                                net=self.net)
            self.ocr.start(snap_only=True)
            util.log("屏幕识别（C 链）就绪（单帧模式，不轮询）", "info", "ocr")
        except Exception as e:                      # noqa: BLE001
            self.ocr = None
            util.log(f"C 链起不来：{e!r}", "err", "ocr")
        return self.ocr

    def api_ocr_region(self, body):
        body = body or {}
        if "region" in body:
            r = [int(x) for x in (body.get("region") or [0, 0, 0, 0])][:4]
            while len(r) < 4:
                r.append(0)
            self.api_cfg({"ocr_region": r})
        self._ensure_ocr()
        crop = ""
        if self.ocr is not None:
            try:
                crop = self.ocr.capture() or ""
                self.last_crop = crop or None
            except Exception as e:                      # noqa: BLE001
                util.log(f"截图失败：{e!r}", "warn", "ocr")
        return {"ok": True, "region": self.cfg.get("ocr_region"), "crop": crop}

    def _shot_bg(self):
        try:
            r = self.api_ocr_once({})
            util.log("单帧识别：%s" % (("识别到 %d 行" % int(r.get("lines") or 0))
                                       if r.get("ok") else ("失败 %s" % r.get("error"))),
                     "info", "ocr")
        except Exception as e:                      # noqa: BLE001
            util.log(f"单帧识别异常：{e!r}", "warn", "ocr")

    def _pick_bg(self):
        try:
            r = self.api_region_pick({})
            if r.get("ok"):
                util.log("框选完成：%s" % (r.get("region"),), "info", "ocr")
            else:
                util.log("框选取消", "info", "ocr")
        except Exception as e:                      # noqa: BLE001
            util.log(f"框选异常：{e!r}", "warn", "ocr")

    def api_ocr_once(self, body):
        """只识别这一帧（快捷键 F6 / 控制台按钮）。"""
        body = body or {}
        if self._ensure_ocr() is None:
            return {"ok": False, "error": "ocr_not_ready"}
        region = body.get("region")
        if not region:
            region = self.cfg.get("ocr_region") or None
        if not region or len([v for v in region if int(v or 0) > 0]) < 4:
            return {"ok": False, "error": "no_region"}
        t0 = time.perf_counter()
        try:
            lines = self.ocr.once(region=region) or []
        except Exception as e:                      # noqa: BLE001
            util.log(f"单帧识别失败：{e!r}", "warn", "ocr")
            return {"ok": False, "error": repr(e)}
        ms = (time.perf_counter() - t0) * 1000.0
        items = [{"src": l.get("text", ""), "dst": l.get("zh", ""),
                  "box": list(l.get("box") or [0, 0, 0, 0]), "lang": l.get("lang", "")}
                 for l in lines]
        return {"ok": True, "ms": round(ms, 1), "lines": len(items), "items": items}

    def api_region_pick(self, body):
        """起选区器（vrdub/region_picker.py），用户拖好按回车后写回 cfg.ocr_region。

        选区器给的是**逻辑像素**（Qt 坐标系），cfg 里存的是**物理像素**（抓屏坐标系），
        这里按 DPI 缩放换算，否则 150% 缩放下选区会偏到右下角。
        """
        if not childmod.exists("region_picker"):
            return {"ok": False, "error": "picker_missing"}
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        out_fp = os.path.join(cfgmod.OUT, "region_pick.json")
        try:
            if os.path.exists(out_fp):
                os.remove(out_fp)
        except Exception:                       # noqa: BLE001
            pass
        try:
            pr = subprocess.run(childmod.argv("region_picker", ["--out", out_fp]),
                                capture_output=True, timeout=180,
                                cwd=ROOT, env=env)
        except Exception as e:                      # noqa: BLE001
            util.log(f"选区器起不来：{e!r}", "warn", "ocr")
            return {"ok": False, "error": repr(e)}
        out = (pr.stdout or b"").decode("utf-8", "replace").strip()
        try:
            if os.path.exists(out_fp):
                with open(out_fp, "r", encoding="utf-8") as fh:
                    out = fh.read().strip() or out
        except Exception:                       # noqa: BLE001
            pass
        line = ""
        for ln in out.splitlines():
            if ln.strip().startswith("{"):
                line = ln.strip()
        if not line:
            return {"ok": False, "cancel": True}
        try:
            region_logical = [int(round(float(v))) for v in json.loads(line).get("region", [])][:4]
        except Exception as e:                      # noqa: BLE001
            return {"ok": False, "error": f"bad_json {e!r}"}
        if len(region_logical) < 4 or region_logical[2] < 8 or region_logical[3] < 8:
            return {"ok": False, "cancel": True}
        dpr = self._dpr()
        phys = [int(round(region_logical[0] * dpr)), int(round(region_logical[1] * dpr)),
                int(round(region_logical[2] * dpr)), int(round(region_logical[3] * dpr))]
        self.api_cfg({"ocr_region": phys})
        try:
            if self.ocr is not None:
                self.ocr.set_region(*phys)
        except Exception:                           # noqa: BLE001
            pass
        util.log("C 链识别范围 → 物理 %s（逻辑 %s，dpr=%.2f）"
                 % (phys, region_logical, dpr), "info", "ocr")
        return {"ok": True, "region": phys, "logical": region_logical, "dpr": dpr}

    def api_ocr_test(self, body):
        if self.ocr is None:
            self.apply_ocr()
        if self.ocr is None:
            return {"ok": False, "err": "C 链没起来（看日志）"}
        try:
            r = self.ocr.test() or {}
        except Exception as e:                          # noqa: BLE001
            return {"ok": False, "err": repr(e)}
        r["ok"] = True
        return r

    def api_hud(self, body):
        body = body or {}
        patch = {k: body[k] for k in ("hud_on", "hud_alpha", "hud_scale", "hud_rows",
                                      "hud_click", "hud_x", "hud_y") if k in body}
        if patch:
            self.api_cfg(patch)
        if body.get("shot"):
            self.hud_send({"op": "shot", "path": os.path.join(cfgmod.OUT, "hud.png")})
            time.sleep(0.6)
            return {"ok": True, "shot": "/shot/hud.png"}
        return {"ok": True, "hud": True}

    # ================================================================ 退出
    def quit(self):
        util.log("退出中…", "info", "boot")
        self.stopping = True
        try:
            self.hud_send({"op": "quit"})
            self.danmaku_send({"op": "quit"})
            self.overlay_send({"op": "quit"})
        except Exception:
            pass
        self.hud_stop()
        self.danmaku_stop()
        self.overlay_stop()
        self.stop_mic()
        self.stop_loop()
        self.stop_player()
        if self.ocr is not None:
            try:
                self.ocr.stop()
            except Exception:
                pass
        if self.server is not None:
            self.server.stop()
        self._stop.set()
        time.sleep(0.2)
        os._exit(0)

    # ================================================================ 自检
    def run_selftest(self):
        """不起链路，把「能不能用」一次性验干净，报告写到 out/selftest.md。"""
        rows = []
        ok_all = True

        def add(name, ok, detail):
            nonlocal ok_all
            ok_all = ok_all and bool(ok)
            rows.append((name, "OK" if ok else "FAIL", detail))
            util.log(f"自检 {name}: {'OK' if ok else 'FAIL'} {detail}", "info" if ok else "err", "check")

        import platform
        import numpy as _np
        add("python", sys.version_info >= (3, 10), platform.python_version())
        add("numpy", True, _np.__version__)
        add("key", bool(self.cfg.key), ("…" + self.cfg.key[-6:]) if self.cfg.key else "缺 .env.local")
        try:
            self.asr.local.ready()
            add("local-asr", self.asr.local.ready(), self.asr.local.note)
        except Exception as e:                          # noqa: BLE001
            add("local-asr", False, repr(e))
        try:
            devs = self.devices()
            add("devices", bool(devs["input"] and devs["output"]),
                f"in {len(devs['input'])} / out {len(devs['output'])} / loop {len(devs['loop'])}")
            for k, key in (("mic", "mic_dev"), ("out", "out_dev"), ("loop", "loop_dev")):
                i = int(self.cfg.get(key))
                add("dev-" + k, audio.resolve("in" if k == "mic" else ("out" if k == "out" else "loop"), i) == i,
                    f"#{i}")
        except Exception as e:                          # noqa: BLE001
            add("devices", False, repr(e))
        # 合成 → 识别 回环（同一段话，验证两端都活着）
        try:
            pcm, first, total = self.synth.synth("今日は少し遅れちゃった、ごめんね。", self.cfg.get("voice"),
                                                 "ja")
            add("tts", len(pcm) > 4000, f"首包 {first:.0f}ms / 共 {total:.0f}ms / "
                                        f"{len(pcm)/48000:.2f}s 音频")
            p = os.path.join(cfgmod.OUT, "selftest_tts.wav")
            import wave
            with wave.open(p, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm)
            x = util.pcm16_to_float(pcm)
            x16 = asr_mod.as_i16(util.resample(x, 24000, 16000))
            t0 = time.time()
            r = self.asr.cloud.transcribe(x16)
            add("cloud-asr", bool(r["text"]), f"{r['ms']:.0f}ms · {r['text'][:40]}")
            if self.asr.local.ready():
                lt = self.asr.local.transcribe(x16, "ja")
                add("local-asr-run", bool(lt), f"{lt[:40]}")
        except Exception as e:                          # noqa: BLE001
            add("tts", False, repr(e))
        try:
            t0 = time.time()
            r = self.mt.translate("我今天可能要迟到十分钟，你们先开吧。", src="zh", dst="ja",
                                  card=self.cfg.get("card"))
            add("mt", bool(r["text"]), f"{r['ms']:.0f}ms · {r['text'][:44]}")
        except Exception as e:                          # noqa: BLE001
            add("mt", False, repr(e))
        # 报告
        lines = ["# 自检报告 " + time.strftime("%Y-%m-%d %H:%M:%S"), "",
                 "| 项 | 结果 | 细节 |", "|---|---|---|"]
        lines += [f"| {n} | {s} | {d} |" for n, s, d in rows]
        fp = os.path.join(cfgmod.OUT, "selftest.md")
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        util.log(("自检全部通过 " if ok_all else "自检有失败项 ") + fp, "info" if ok_all else "err", "check")
        print("\n".join(lines))
        return ok_all

    def run_forever(self):
        self.start()
        try:
            while True:
                line = ""
                try:
                    line = sys.stdin.readline()
                except Exception:
                    line = ""
                if not line:
                    time.sleep(0.5)
                    continue
                cmd = line.strip().lower()
                if cmd in ("q", "quit", "exit"):
                    break
                if cmd in ("m", "mic"):
                    self.set_mic(not self.cfg.get("mic_on", True))
                elif cmd in ("t", "test"):
                    self.api_tts_test({})
                elif cmd in ("c", "clear"):
                    self.bus.clear()
        except KeyboardInterrupt:
            pass
        self.quit()


_LOCK_FH = None


def _single_instance():
    """拿一把独占文件锁：拿不到 = 已经有一个 vrdub 在跑，本进程直接安静退出。

    用 HTTP 探活不够可靠——上一个进程刚被杀的瞬间端口还在 listen，
    新建的进程会误以为「已有实例」而直接退出（结果一个后端都没有）。
    """
    global _LOCK_FH
    try:
        import msvcrt
        fp = os.path.join(util.OUT, "vrdub.lock")
        fh = open(fp, "a+")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _LOCK_FH = fh
        return True
    except Exception:                       # noqa: BLE001
        return False


def main(argv):
    rc_child = childmod.run_if_child(argv)
    if rc_child is not None:
        return rc_child
    use_hud = "--no-hud" not in argv
    use_web = "--no-web" not in argv
    if "--force" not in argv and not _single_instance():
        url = "http://127.0.0.1:%d/" % srvmod.PORT
        print("已经有一个 vrdub 在跑了：%s （日志 %s）" % (url, util.OUT))
        try:
            webbrowser.open(url)
        except Exception:                   # noqa: BLE001
            pass
        return 0
    app = App(use_hud=use_hud, use_web=use_web)
    if "--selftest" in argv:
        app.run_selftest()
        return 0
    try:
        app.run_forever()
    except MemoryError:
        util.log("内存不够（MemoryError）→ 退出，交给 run.bat 的循环重启", "err", "mem")
        return 9
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))