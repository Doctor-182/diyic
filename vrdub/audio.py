# -*- coding: utf-8 -*-
"""WASAPI 采集 / 播放 / 回环，以及「TTS 出声时自动压直通麦」的混音器。

上一代踩过的三条硬规矩，这里直接按它们写死：
  1. PortAudio 全局只能有一个实例（多实例会把主线程卡死）→ pa_inst() 单例。
  2. WASAPI 只吃原生 48 kHz，传 16 k 直接 OSError -9997 → 一律 48 k 打开，
     需要 16 k 的地方在内存里降采样。
  3. 设备索引会变（换耳机就变）→ 上层每次重启按名字兜底（见 resolve()）。
"""
from __future__ import annotations

import collections
import queue
import threading
import time

import numpy as np

from . import util

_pa = None
_pa_lock = threading.Lock()
PA = None          # 模块对象，延迟 import（没装时后端还能起，只报一条日志）


def _mod():
    global PA
    if PA is None:
        import pyaudiowpatch as p
        PA = p
    return PA


def pa_inst():
    global _pa
    with _pa_lock:
        if _pa is None:
            _pa = _mod().PyAudio()
        return _pa


def _open(p, kw, tries=3):
    """WASAPI 换设备/抢占时偶发 OSError -9999，退避重试几次再认输。"""
    last = None
    for i in range(max(1, tries)):
        try:
            return p.open(**kw)
        except OSError as e:
            last = e
            time.sleep(0.12 * (i + 1))
        except Exception as e:                      # noqa: BLE001
            last = e
            break
    raise last


def reset_pa():
    """关掉并销毁 PyAudio 实例（换设备时 WASAPI 偶发 -9999，重建最干净）。"""
    global _pa
    with _pa_lock:
        if _pa is not None:
            try:
                _pa.terminate()
            except Exception:
                pass
        _pa = None
    return pa_inst()


def wasapi_index(p=None) -> int:
    p = p or pa_inst()
    return p.get_host_api_info_by_type(_mod().paWASAPI)["index"]


def list_devices() -> dict:
    """{'input':[...], 'output':[...], 'loop':[...]}，只列 WASAPI。"""
    try:
        p = pa_inst()
    except Exception as e:                        # noqa: BLE001
        util.log(f"音频设备枚举失败：{e!r}", "err", "audio")
        return {"input": [], "output": [], "loop": []}
    try:
        wi = wasapi_index(p)
    except Exception as e:                        # noqa: BLE001
        util.log(f"没有 WASAPI：{e!r}", "err", "audio")
        return {"input": [], "output": [], "loop": []}
    ins, outs, loops = [], [], []
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d.get("hostApi") != wi:
            continue
        item = {"i": i, "name": d["name"], "loop": bool(d.get("isLoopbackDevice", False)),
                "ch": int(max(d["maxInputChannels"], d["maxOutputChannels"])),
                "sr": int(d["defaultSampleRate"])}
        if d.get("isLoopbackDevice", False):
            loops.append(item)
        elif d["maxInputChannels"] > 0:
            ins.append(item)
        elif d["maxOutputChannels"] > 0:
            outs.append(item)
    return {"input": ins, "output": outs, "loop": loops}


def _bucket(kind: str) -> str:
    """kind(in/out/loop) → list_devices() 的桶名（input/output/loop）。

    踩过的坑：find_by_name 直接拿 kind='out' 去查 {'input','output','loop'}，
    永远查到空列表 → 名字解析恒失败 → 静默退回旧号码，表现成「按名绑定没生效」。
    """
    return {"in": "input", "out": "output", "loop": "loop"}.get(kind, kind)


def name_of(kind: str, idx: int) -> str:
    """按索引取设备名（存盘用）。取不到就返回空串。"""
    try:
        for d in list_devices()[_bucket(kind)]:
            if int(d["i"]) == int(idx):
                return str(d["name"])
    except Exception:                                 # noqa: BLE001
        pass
    try:
        return str(pa_inst().get_device_info_by_index(int(idx)).get("name") or "")
    except Exception:                                 # noqa: BLE001
        return ""


def find_by_name(kind: str, name):
    """按名字找设备：先全等，再「去括号前缀」包含匹配。

    为什么不能只存号码：Windows/PortAudio 每次进程启动枚举顺序都可能变，
    实测同一台机器重启后 #19 从「CABLE Input」变成「扬声器」——号码还活着、
    通道数也对，校验拦不住，于是日语被放给了自己听、对面一句没收到。
    """
    if not name:
        return None
    want = str(name).strip()
    if not want:
        return None
    allv = list_devices()
    devs = allv.get(_bucket(kind)) or []
    if kind == "loop":
        # 回环桶里只有 *[Loopback] 设备；但 B 链常常该听「虚拟线的录音端」
        # （CABLE Output），那在 input 桶里。两桶一起找，否则按名绑定永远落空。
        devs = devs + list(allv.get("input") or [])
    for d in devs:
        if str(d["name"]).strip() == want:
            return int(d["i"])
    key = want.split(" (")[0].strip().lower()
    if not key:
        return None
    hits = [d for d in devs if key in str(d["name"]).lower()]
    if len(hits) == 1:
        return int(hits[0]["i"])
    if hits:
        # 多个同名（比如 CABLE Input 与 CABLE In 16ch）：优先最短名，即最"原始"的那个
        hits.sort(key=lambda d: len(str(d["name"])))
        return int(hits[0]["i"])
    return None


def resolve_named(kind: str, idx: int, name=None):
    """名字优先、号码兜底：名字对上就用它对的那个索引。"""
    i = find_by_name(kind, name)
    if i is not None:
        return i
    return resolve(kind, idx)


def resolve(kind: str, idx: int):
    """设备索引失效时的兜底：index 还活着就用，否则按名字/默认找。"""
    try:
        p = pa_inst()
        d = p.get_device_info_by_index(int(idx))
        ok_in = d["maxInputChannels"] > 0
        ok_out = d["maxOutputChannels"] > 0
        # loop 也是采集设备（WASAPI loopback），必须当成输入来判 —— 漏了这一分支会导致
        # 「换了回环设备却永远只收 #25」，实测踩过。
        if (kind in ("in", "loop") and ok_in) or (kind == "out" and ok_out):
            return int(idx)
    except Exception:
        pass
    devs = list_devices()
    want = devs[_bucket(kind)]
    if kind == "out":
        for d in want:
            if "CABLE Input" in d["name"] and "16ch" not in d["name"]:
                return d["i"]
    if kind == "loop":
        for d in want:
            if "Loopback" in d["name"]:
                return d["i"]
    try:
        p = pa_inst()
        dm = p.get_default_wasapi_device(
            in_out=("in" if kind == "in" else "out")) if hasattr(p, "get_default_wasapi_device") else None
        if dm:
            return int(dm["index"])
    except Exception:
        pass
    if want:
        return want[0]["i"]
    raise RuntimeError(f"找不到任何 {kind} 设备")


class Ring:
    """定长单声道 float32 环形缓冲。满则丢最旧（延迟不会无限涨）。"""

    def __init__(self, n: int):
        self.buf = np.zeros(int(n), np.float32)
        self.n = int(n)
        self.w = 0
        self.r = 0
        self.avail = 0
        self._lk = threading.Lock()
        self.over = 0

    def write(self, x: np.ndarray):
        x = np.asarray(x, np.float32)
        m = x.size
        if m == 0:
            return
        with self._lk:
            if m > self.n:
                x = x[-self.n:]
                m = x.size
            over = self.avail + m - self.n
            if over > 0:
                self.r = (self.r + over) % self.n
                self.avail -= over
                self.over += over
            end = self.w + m
            if end <= self.n:
                self.buf[self.w:end] = x
            else:
                k = self.n - self.w
                self.buf[self.w:] = x[:k]
                self.buf[:end - self.n] = x[k:]
            self.w = end % self.n
            self.avail += m

    def read(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        with self._lk:
            m = min(n, self.avail)
            if m <= 0:
                return out
            end = self.r + m
            if end <= self.n:
                out[:m] = self.buf[self.r:end]
            else:
                k = self.n - self.r
                out[:k] = self.buf[self.r:]
                out[k:m] = self.buf[:end - self.n]
            self.r = end % self.n
            self.avail -= m
        return out

    def clear(self):
        with self._lk:
            self.r = self.w = self.avail = 0

    def level(self) -> int:
        return self.avail


class Capture:
    """48 kHz 采一路（麦克风或 Loopback），把 float32 (n, ch) 交给回调。"""

    def __init__(self, idx: int, on_block, blocksize: int = 480, log=util.log, kind="in"):
        self.idx = idx
        self.on_block = on_block
        self.blocksize = blocksize
        self.log = log
        self.kind = kind
        self.rate = 48000
        self.ch = 1
        self._st = None
        self._err = None

    def start(self):
        m = _mod()
        p = pa_inst()
        d = p.get_device_info_by_index(self.idx)
        self.rate = int(d["defaultSampleRate"]) or 48000
        ch = int(d["maxInputChannels"]) or 2
        self.ch = max(1, min(2, ch))
        kw = dict(format=m.paFloat32, channels=self.ch, rate=self.rate,
                  input=True, input_device_index=self.idx,
                  frames_per_buffer=self.blocksize, stream_callback=self._cb)
        self._st = _open(p, kw)
        self._st.start_stream()
        self.log(f"采集开 {self.kind} #{self.idx} {d['name'][:32]} {self.rate}Hz x{self.ch}",
                 "info", "audio")

    def _cb(self, in_data, frame_count, time_info, status):
        if in_data is None:
            return (None, _mod().paContinue)
        try:
            a = np.frombuffer(in_data, dtype=np.float32)
            if self.ch > 1:
                a = a.reshape(-1, self.ch)
            self.on_block(a, self.rate)
        except Exception as e:                    # noqa: BLE001
            self._err = e
        return (None, _mod().paContinue)

    def stop(self):
        try:
            if self._st is not None:
                self._st.stop_stream()
                self._st.close()
        except Exception:
            pass
        self._st = None


class Player:
    """一路 48 kHz 输出：直通麦 × duck + TTS 混音。

    为什么要 duck：我在说中文、TTS 在说日语时，VRChat 麦克风里必须只有日语。
    直通麦一直开着（不说话时对方能听见环境声），TTS 一出声就把直通压到 cfg.duck。
    """

    def __init__(self, idx: int, mon_idx: int = 0, log=util.log, rate=48000, blocksize=480):
        self.idx = idx
        self.mon_idx = int(mon_idx or 0)   # 监听耳机：只放 TTS，不放直通麦（否则自己听自己）
        self.mon = Ring(rate * 8)
        self._mon_st = None
        self._mon_ch = 2
        self._mon_q = queue.Queue(maxsize=24)   # 监听用「阻塞写」而不是回调：WASAPI 回调路实测掉音 27 dB
        self._mon_th = None
        self._mon_rate = rate
        self._mon_run = False
        self.mon_gain = 1.0
        self._mon_null = 0
        self.log = log
        self.rate = rate
        self.blocksize = blocksize
        self.mic = Ring(rate * 2)
        self.tts = Ring(rate * 8)
        self.gain_tts = 1.0
        self.gain_mic = 1.0
        self.duck = 0.0
        self._g = 1.0          # 平滑后的直通增益
        self._st = None
        self.playing = False
        self.on_idle = None
        self._played = 0
        self._lk = threading.Lock()
        self.underrun = 0
        self.ch = 2
        self._null = 0
        # 播放侧电平包络：每帧（10 ms）记一次「真正送进声卡的 TTS 音量 dB」，
        # 用来和麦克风那一路做相关 —— 真实回声在物理上就是这段音频本身，
        # 而人自己重说一遍同一句话时，包络和上一次合成音毫无关系。
        self.env = collections.deque(maxlen=2000)
        self.env_lock = threading.Lock()
        # 播放过的那段波形本身（8 kHz 单声道，15 s 环形滚动）：回声检测拿它当参考信号，
        # 用归一化互相关认「这段麦克风信号是不是我们放出去的」。人自己重说一遍时
        # 波形完全不同（相关 <0.05），所以这条路不会误杀复述。
        #
        # 为什么是环形缓冲而不是「每块带时间戳」：回调时间抖动几毫秒，按时间戳逐块摆放
        # 会在拼缝处产生小缺口/重叠，包络看着一模一样、波形相关却只有 0.09（实测）。
        # 这里改成按采样顺序连续累加，时间轴由一条平滑过的锚点 _hist_t0 给出。
        self.hist_rate = 8000
        self._hist_cap = self.hist_rate * 15
        self._hist_buf = np.zeros(self._hist_cap, np.float32)
        self._hist_n = 0              # 累计写入的采样数（单调递增）
        self._hist_t0 = None          # 全局采样号 0 对应的墙钟时间（缓慢跟随设备时钟）

    # ---------------------------------------------------------------- 生命周期
    def start(self):
        m = _mod()
        p = pa_inst()
        d = p.get_device_info_by_index(self.idx)
        self.rate = int(d["defaultSampleRate"]) or self.rate
        ch = max(1, min(2, int(d["maxOutputChannels"]) or 2))
        self.ch = ch
        self._st = _open(p, dict(format=m.paFloat32, channels=ch, rate=self.rate, output=True,
                                 output_device_index=self.idx,
                                 frames_per_buffer=self.blocksize, stream_callback=self._cb))
        self._st.start_stream()
        self._hist_reset()
        self.log(f"播放开 #{self.idx} {d['name'][:32]} {self.rate}Hz x{ch}", "info", "audio")
        self._open_mon()

    def _open_mon(self):
        """耳机监听：独立一路只喂 TTS。听不见自己的合成音=没法调音色和语速。

        走「常开流 + 阻塞写」而不是回调：回调路在 WASAPI 共享模式下实测只剩 -31 dB
        （10 ms 缓冲反复饿死），阻塞写这条路（和 play_pcm/试听同一条）是 -3 dB，能听见。
        """
        if not self.mon_idx or self.mon_idx == self.idx:
            return
        m = _mod()
        p = pa_inst()
        try:
            d = p.get_device_info_by_index(self.mon_idx)
            ch = max(1, min(2, int(d["maxOutputChannels"]) or 2))
            rate = int(d["defaultSampleRate"]) or self.rate
            self._mon_ch = ch
            self._mon_rate = rate
            self._mon_st = _open(p, dict(format=m.paFloat32, channels=ch, rate=rate, output=True,
                                         output_device_index=self.mon_idx))
            self._mon_st.start_stream()
            self._mon_run = True
            self._mon_th = threading.Thread(target=self._mon_writer, name="mon-out", daemon=True)
            self._mon_th.start()
            self.log(f"监听开 #{self.mon_idx} {d['name'][:32]} {rate}Hz x{ch}", "info", "audio")
        except Exception as e:                        # noqa: BLE001
            self._mon_st = None
            self._mon_run = False
            self.log(f"监听开不了 #{self.mon_idx}：{e!r}（合成音只在虚拟线上）", "warn", "audio")

    def _mon_writer(self):
        ch = max(1, int(self._mon_ch))
        st = self._mon_st
        while self._mon_run and st is not None:
            try:
                item = self._mon_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                x = np.clip(np.asarray(item, np.float32) * self.mon_gain, -1.0, 1.0)
                st.write((x.reshape(-1, 1) * np.ones((1, ch), np.float32)).astype(np.float32).tobytes())
            except Exception as e:                     # noqa: BLE001
                self._mon_null += 1
                if self._mon_null <= 3:
                    self.log(f"监听写入失败：{e!r}", "warn", "audio")
                break

    def stop(self):
        self._mon_run = False
        try:
            self._mon_q.put_nowait(None)
        except Exception:                              # noqa: BLE001
            pass
        th = getattr(self, "_mon_th", None)
        if th is not None:
            th.join(timeout=1.0)
            self._mon_th = None
        for attr in ("_st", "_mon_st"):
            st = getattr(self, attr, None)
            try:
                if st is not None:
                    st.stop_stream()
                    st.close()
            except Exception:
                pass
            setattr(self, attr, None)

    def reopen(self, idx: int, mon_idx=None):
        self.stop()
        self.idx = idx
        if mon_idx is not None:
            self.mon_idx = int(mon_idx or 0)
        self.mic.clear()
        self.tts.clear()
        self.mon.clear()
        self.start()

    # ---------------------------------------------------------------- 写入
    def push_mic(self, mono48k: np.ndarray):
        self.mic.write(mono48k)

    def backlog(self) -> float:
        """TTS 播放环里还压着多少秒音频（同传追上嘴的判据）。"""
        return self.tts.level() / float(self.rate or 48000)

    def wait_drain(self, timeout: float = 10.0):
        t0 = time.time()
        while self.tts.level() > self.rate * 0.02 and time.time() - t0 < timeout:
            time.sleep(0.02)

    def _hist_reset(self):
        """播放流重开时把参考波形清空（锚点也要重来，否则时间轴整体错位）。"""
        self._hist_buf[:] = 0.0
        self._hist_n = 0
        self._hist_t0 = None

    def _hist_push(self, low, now: float):
        """把一个 8 kHz 块按采样顺序接在环形缓冲尾部（调用方持 env_lock）。

        时间轴不靠每块的 time.time()，而是靠 _hist_t0 + 全局采样号/8k：
        每块只用 now 校正锚点，误差按 0.05 指数平滑吃掉，于是波形连续、无拼缝。
        """
        low = np.asarray(low, np.float32).reshape(-1)
        n_new = int(low.size)
        if n_new <= 0:
            return
        rate = float(self.hist_rate)
        cap = self._hist_cap
        if self._hist_t0 is None:
            self._hist_t0 = now - n_new / rate
            self._hist_n = 0
        else:
            want = self._hist_t0 + (self._hist_n + n_new) / rate
            err = now - want
            if abs(err) > 1.0:                        # 设备重开/系统卡顿：直接重锚
                self._hist_t0 = now - (self._hist_n + n_new) / rate
            else:
                self._hist_t0 += err * 0.05
        pos, g = 0, self._hist_n
        while pos < n_new:
            i = g % cap
            take = min(n_new - pos, cap - i)
            self._hist_buf[i:i + take] = low[pos:pos + take]
            pos += take
            g += take
        self._hist_n = g

    def hist_window(self, t0: float, t1: float):
        """把「我们放出去的声音」按真实时钟取一段 8 kHz 波形，返回 (t_start, arr)。

        中间没有播放的区间是 0（环形缓冲里本来就是 0）；时间轴和麦克风那一路同一个钟。
        """
        rate = float(self.hist_rate)
        cap = self._hist_cap
        with self.env_lock:
            anchor = self._hist_t0
            n_end = self._hist_n
            if anchor is None or n_end <= 0:
                return float(t0), np.zeros(0, np.float32)
            n_start = max(0, n_end - cap)
            g0 = int(round((t0 - anchor) * rate))
            g1 = int(round((t1 - anchor) * rate))
            g0 = max(g0, n_start)
            g1 = min(g1, n_end)
            if g1 <= g0:
                return float(t0), np.zeros(0, np.float32)
            out = np.empty(g1 - g0, np.float32)
            pos, g = 0, g0
            while pos < out.size:
                i = g % cap
                take = min(out.size - pos, cap - i)
                out[pos:pos + take] = self._hist_buf[i:i + take]
                pos += take
                g += take
            return anchor + g0 / rate, out

    def push_tts_pcm(self, pcm: bytes, sr=24000):
        """TTS 原始 PCM（任意采样率）→ 48 k 单声道 → 播放环。"""
        x = util.pcm16_to_float(pcm) if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm, np.float32)
        if sr != self.rate:
            x = util.resample(x, sr, self.rate)
        with self._lk:
            self.playing = True
        y = x * self.gain_tts
        self.tts.write(y)
        if self._mon_st is not None:
            # 同一份 TTS 进监听耳机（按监听设备自己的采样率再转一次）
            z = y if abs(self._mon_rate - self.rate) < 1 else util.resample(y, self.rate, self._mon_rate)
            try:
                if self._mon_q.full():                 # 落后太多就丢最旧，宁可跳一小段也不越拖越远
                    self._mon_q.get_nowait()
                self._mon_q.put_nowait(z)
            except Exception:                          # noqa: BLE001
                pass

    def flush(self):
        self.tts.clear()
        self.mon.clear()
        with self._lk:
            self.playing = False

    def busy(self) -> bool:
        return self.tts.level() > self.rate * 0.02

    # ---------------------------------------------------------------- 回调
    def _cb(self, out_data, frame_count, time_info, status):
        n = frame_count
        micv = self.mic.read(n)
        ttsv = self.tts.read(n)
        # 记录这一帧「我们放出去的 TTS」的电平（不加直通麦、不加增益）：
        # 回声检测拿它当参考信号，见 app._out_echo / chains._is_echo
        try:
            _rms = float(np.sqrt(np.mean(np.square(ttsv, dtype=np.float32)) + 1e-12))
            _db = 20.0 * np.log10(_rms + 1e-6)
            _now = time.time()
            _mono = ttsv if getattr(ttsv, "ndim", 1) == 1 else ttsv.reshape(-1)
            _low = util.resample(np.asarray(_mono, np.float32), self.rate, self.hist_rate)
            with self.env_lock:
                self.env.append((_now, float(_db)))
                self._hist_push(_low, _now)
        except Exception:
            pass
        target = self.duck if self.tts.level() > 0 or ttsv.any() else 1.0
        # 一阶平滑，避免爆音
        a = 0.35
        self._g += (target - self._g) * a
        mix = micv * self._g + ttsv
        if not ttsv.any() and self.mic.level() == 0:
            self.underrun += 1
        np.clip(mix, -1.0, 1.0, out=mix)
        if out_data is None:
            # 关键：pyaudiowpatch 的 WASAPI 输出流**每次**回调都给 out_data=None（实测 212/212），
            # 这时必须「返回」这一帧的音频。早期版本这里 return 了一坨全零，
            # 于是主路（#19 CABLE Input）从头到尾只有静音——对方当然听不到合成音；
            # 耳机监听走的是另一条阻塞写流，所以「自己能听到、VRChat 听不到」。
            self._null += 1
            ch = max(1, int(self.ch))
            buf = (mix.reshape(-1, 1) * np.ones((1, ch), np.float32)).astype(np.float32).tobytes()
            return (buf, _mod().paContinue)
        out = np.frombuffer(out_data, dtype=np.float32)
        ch = out.size // max(1, n)
        for c in range(ch):
            out[c::ch] = mix
        if self.playing and self.tts.level() == 0:
            with self._lk:
                self.playing = False
            if self.on_idle:
                try:
                    self.on_idle()
                except Exception:
                    pass
        return (None, _mod().paContinue)

def play_pcm(idx: int, pcm, sr: int = 24000, gain: float = 1.0, log=util.log):
    """同步播一段 PCM（测听 / 自检用）：24k PCM → 设备原生率，写完即关。"""
    m = _mod()
    p = pa_inst()
    d = p.get_device_info_by_index(int(idx))
    rate = int(d["defaultSampleRate"]) or 48000
    ch = max(1, min(2, int(d["maxOutputChannels"]) or 2))
    x = util.pcm16_to_float(pcm) if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm, np.float32)
    if sr != rate:
        x = util.resample(x, sr, rate)
    x = np.clip(x * gain, -1, 1)
    st = p.open(format=m.paFloat32, channels=ch, rate=rate, output=True,
                output_device_index=int(idx))
    try:
        frame = (x.reshape(-1, 1) * np.ones((1, ch), np.float32)).astype(np.float32).tobytes()
        st.write(frame)
    finally:
        try:
            st.stop_stream()
            st.close()
        except Exception:
            pass


class Through:
    """VRChat 专线直通：把「VRChat 输出线」读出来原样放回耳机。

    为什么要这么绕：B 链要听的是「只有对方」的那条线。直接听扬声器回环
    （扬声器 [Loopback]）听到的是耳机里的一切——对方 + 我们自己 TTS 的监听 +
    系统提示音，于是自己的日语会被当成对方说话（回环里全是回声）。
    做法：让 VRChat 的输出走一根独立虚拟线（CABLE In 16ch），这条线本来就没人听，
    我们把它读出来、原样放回耳机——用户照旧听见对方，B 链身上只有对方。
    TTS 出声时自动把对方压低（duck），否则自己的日语和对方的声音搅在一起听不清。
    """

    def __init__(self, in_idx: int, out_idx: int, gain: float = 1.0, log=util.log,
                 blocksize: int = 480, duck_fn=None):
        self.in_idx = int(in_idx)
        self.out_idx = int(out_idx)
        self.gain = float(gain)
        self.duck_fn = duck_fn
        self.log = log
        self.blocksize = blocksize
        self.run = False
        self._ins = None
        self._outs = None
        self._th = None
        self._in_rate = 48000
        self._out_rate = 48000
        self._errs = 0
        self.peak = 0.0

    def start(self):
        m = _mod()
        p = pa_inst()
        di = p.get_device_info_by_index(self.in_idx)
        do = p.get_device_info_by_index(self.out_idx)
        self._in_rate = int(di["defaultSampleRate"]) or 48000
        self._out_rate = int(do["defaultSampleRate"]) or 48000
        self._ins = _open(p, dict(format=m.paFloat32, channels=2, rate=self._in_rate,
                                  input=True, input_device_index=self.in_idx,
                                  frames_per_buffer=self.blocksize))
        ch = max(1, min(2, int(do["maxOutputChannels"]) or 2))
        self._outs = _open(p, dict(format=m.paFloat32, channels=ch, rate=self._out_rate,
                                   output=True, output_device_index=self.out_idx))
        self._out_ch = ch
        self._ins.start_stream()
        self._outs.start_stream()
        self.run = True
        self._th = threading.Thread(target=self._loop, name="through", daemon=True)
        self._th.start()
        self.log(f"直通开 #{self.in_idx} {str(di['name'])[:28]} → #{self.out_idx} "
                 f"{str(do['name'])[:24]}（VRChat 的声音原样回耳机）", "info", "audio")

    def _loop(self):
        n = self.blocksize
        ch_out = max(1, int(getattr(self, "_out_ch", 2)))
        while self.run:
            try:
                raw = self._ins.read(n, exception_on_overflow=False)
            except Exception as e:                     # noqa: BLE001
                self._errs += 1
                if self._errs <= 3:
                    self.log(f"直通读失败：{e!r}", "warn", "audio")
                time.sleep(0.05)
                continue
            try:
                x = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).mean(axis=1)
                self.peak = float(np.max(np.abs(x))) if x.size else 0.0
                g = self.gain
                if self.duck_fn is not None:
                    try:
                        g *= float(self.duck_fn())
                    except Exception:                  # noqa: BLE001
                        pass
                x = x * g
                if self._out_rate != self._in_rate:
                    x = util.resample(x, self._in_rate, self._out_rate)
                np.clip(x, -1.0, 1.0, out=x)
                self._outs.write((x.reshape(-1, 1) * np.ones((1, ch_out), np.float32))
                                 .astype(np.float32).tobytes())
            except Exception as e:                     # noqa: BLE001
                self._errs += 1
                if self._errs <= 3:
                    self.log(f"直通写失败：{e!r}", "warn", "audio")

    def stop(self):
        self.run = False
        th = self._th
        if th is not None:
            th.join(timeout=1.0)
            self._th = None
        for s in (self._ins, self._outs):
            try:
                if s is not None:
                    s.stop_stream()
                    s.close()
            except Exception:                          # noqa: BLE001
                pass
        self._ins = None
        self._outs = None


def cable_key(name: str) -> str:
    """把设备名归一成「同一根线」的键：CABLE Input / CABLE Output / CABLE In 16ch 算一根。

    为什么要这个：VB-Cable 的放音端叫 CABLE Input、录音端叫 CABLE Output，
    同一根线两个名字。判断「直通线和 TTS 输出是不是同一根线」时按全等比较会漏，
    结果把我们自己的 TTS 再放回耳机（自己听两遍）。
    """
    s = str(name or "").split(" [")[0].strip().lower()
    for w in (" in 16ch", " 16ch", " input", " output"):
        s = s.replace(w, "")
    return s


def is_virtual(idx: int) -> bool:
    """这根线是不是虚拟声卡（CABLE / Voicemeeter）——只有虚拟线才需要直通。"""
    try:
        d = pa_inst().get_device_info_by_index(int(idx))
        name = str(d.get("name") or "")
    except Exception:                                  # noqa: BLE001
        return False
    return any(k in name for k in ("CABLE", "Virtual Cable", "Voicemeeter"))


def record(dev_in: int, seconds: float, path: str, log=util.log) -> float:
    """录一段 WASAPI 输入（虚拟线/Loopback）到 wav，返回实际秒数。闭环测试用。"""
    import wave
    m = _mod()
    p = pa_inst()
    d = p.get_device_info_by_index(int(dev_in))
    rate = int(d["defaultSampleRate"]) or 48000
    ch = 2
    st = p.open(format=m.paFloat32, channels=ch, rate=rate, input=True,
                input_device_index=int(dev_in), frames_per_buffer=1024)
    chunks = []
    n = int(rate * seconds)
    got = 0
    st.start_stream()
    try:
        while got < n:
            b = st.read(1024, exception_on_overflow=False)
            chunks.append(b)
            got += 1024
    finally:
        try:
            st.stop_stream()
            st.close()
        except Exception:
            pass
    raw = b"".join(chunks)
    a = np.frombuffer(raw, dtype=np.float32).reshape(-1, ch)
    i16 = (np.clip(a, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(i16.tobytes())
    return got / rate