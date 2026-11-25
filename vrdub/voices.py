# -*- coding: utf-8 -*-
r"""音色克隆：10–20 秒干净人声 → 可用的 voice_id。

官方口径（阿里云百炼「音色复刻」HTTP）：

    POST /api/v1/services/audio/tts/customization
    {"model": "voice-enrollment",
     "input": {"action": "create_voice", "target_model": "<TTS 模型>",
               "prefix": "<小写字母数字>", "audio": "<公网 URL 或 data: URI>"}}
    → output.voice_id

    POST 同端点 {"model": "voice-enrollment",
                 "input": {"action": "query_voice", "voice_id": "..."}}
    → output.status：DEPLOYING 继续等，OK 可用，其它算失败

创建出来的音色**绑死在 target_model 上**（跨模型用不了），所以合成时必须用同一个
模型名，这也是我们把模型名写进配置而不是散落在代码里的原因。

音频硬要求：WAV(16bit)/MP3/M4A、10–20 s 最稳（上限 60 s）、<10 MB、≥24 kHz、
单声道、全程无背景音。我们录进来的是 48 kHz 双声道，所以先 to_clone_wav() 转一道：
下混单声道 + 线性重采样到 24 kHz + 过一遍峰值归一，别指望云端替我们收拾。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import wave

import numpy as np

from . import util

PATH = "/api/v1/services/audio/tts/customization"
MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}
SR_MIN = 24000
SEC_MIN = 3.0
SEC_MAX = 60.0
SEC_BEST = (10.0, 20.0)
BYTES_MAX = 10 * 1024 * 1024
PREFIX_RE = re.compile(r"^[a-z0-9]{1,16}$")


class VoiceError(RuntimeError):
    """克隆失败（网络、参数、云端状态都归到这里，前端只显示 message）。"""


# --------------------------------------------------------------------- 音频处理
def _read_wav(path):
    with wave.open(path, "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise VoiceError("样本必须是 16bit PCM wav（当前 %d 字节/样本）" % sw)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch)
    return a, ch, sr


def _resample(a, sr_in, sr_out):
    """线性重采样。够用：人声复刻只吃频谱包络，48k→24k 就是抗混叠降采样。"""
    if sr_in == sr_out:
        return a
    n_out = int(round(len(a) * sr_out / float(sr_in)))
    if n_out <= 0:
        return a
    x = np.linspace(0.0, len(a) - 1.0, n_out, dtype=np.float64)
    xp = np.arange(len(a), dtype=np.float64)
    return np.interp(x, xp, a.astype(np.float64)).astype(np.float32)


def to_clone_wav(src, dst, sr=SR_MIN, target_peak=0.89):
    """把任意 wav 转成克隆要求的形状：单声道 / 24 kHz / 16bit，并做峰值归一。

    返回 {dst, secs, sr, peak, rms_db, changed}，用来自检「录得太小声」「太长」。
    """
    a, ch, sr_in = _read_wav(src)
    if ch > 1:
        a = a.mean(axis=1)                      # 下混；双声道相位相反时也不会归零太多
    a = _resample(a, sr_in, sr)
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
    gain = 1.0
    if peak > 1e-6:
        # 最多提 30 dB：真人对着麦说话峰值常在 0.03–0.2，8 倍根本不够（实测只到 0.4）；
        # 30 dB 上限同时挡住"整段几乎静音"的坏样本被硬拉成噪声墙。
        gain = min(target_peak / peak, 31.6)
        a = np.clip(a * gain, -1.0, 1.0)
    i16 = (a * 32767.0).astype("<i2")
    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(i16.tobytes())
    secs = len(a) / float(sr)
    return {"dst": dst, "secs": round(secs, 2), "sr": sr, "ch": 1,
            "peak": round(peak, 4), "gain": round(gain, 2),
            "rms_db": round(20.0 * np.log10(rms + 1e-12), 1),
            "changed": bool(ch > 1 or sr_in != sr or abs(gain - 1.0) > 0.02)}


def check_sample(path, secs=None, size=None):
    """上传前把明显不合格的样本挡住，别等云端报错（它的报错信息很难读）。"""
    secs = float(secs if secs is not None else 0.0)
    size = int(size if size is not None else (os.path.getsize(path) if os.path.exists(path) else 0))
    if not os.path.exists(path):
        raise VoiceError("样本文件不在：" + str(path))
    if size > BYTES_MAX:
        raise VoiceError("样本 %.1f MB 超过 10 MB 上限（缩短一点）" % (size / 1048576.0))
    if secs and secs < SEC_MIN:
        raise VoiceError("样本只有 %.1f s，至少要 %.0f s 连续说话" % (secs, SEC_MIN))
    if secs and secs > SEC_MAX:
        raise VoiceError("样本 %.1f s 超过 %.0f s 上限（截前 20 s 就够）" % (secs, SEC_MAX))
    return True


def data_uri(path):
    ext = os.path.splitext(path)[1].lower()
    mime = MIME.get(ext)
    if not mime:
        raise VoiceError("只支持 wav / mp3 / m4a（给了 %s）" % (ext or "无扩展名"))
    with open(path, "rb") as fh:
        raw = fh.read()
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii")), mime, len(raw)


def clean_prefix(name, fallback="vdub"):
    """prefix 只收小写字母数字、≤16 位；中文名直接退化成 fallback。"""
    s = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not s:
        s = re.sub(r"[^a-z0-9]", "", fallback) or "vdub"
    return s[:16]


# --------------------------------------------------------------------- 请求体
def payload_create(target_model, prefix, audio_uri):
    return {"model": "voice-enrollment",
            "input": {"action": "create_voice", "target_model": str(target_model),
                      "prefix": clean_prefix(prefix), "audio": audio_uri}}


def payload_query(voice_id):
    return {"model": "voice-enrollment",
            "input": {"action": "query_voice", "voice_id": str(voice_id)}}


def payload_list(prefix=None, page=0, size=50):
    inp = {"action": "list_voice", "page_index": int(page), "page_size": int(size)}
    if prefix:
        inp["prefix"] = clean_prefix(prefix)
    return {"model": "voice-enrollment", "input": inp}


def payload_delete(voice_id):
    return {"model": "voice-enrollment",
            "input": {"action": "delete_voice", "voice_id": str(voice_id)}}


# --------------------------------------------------------------------- 客户端
class Cloner:
    """把「发请求 → 等部署 → 记进配置」串起来。net 可注入，离线用假对象就能测。"""

    def __init__(self, cfg, net, net_payg=None, log=util.log):
        self.cfg = cfg
        self.net = net
        self.net_payg = net_payg
        self.log = log
        # 某个端点明确说"没有这个模型"之后就别再试它：套餐端点实测返回
        # 404 {"code":"InvalidParameter","message":"Model not exist"}，
        # 每次都先撞一遍等于凭空多等 4 秒，还会在界面上留一条吓人的红字。
        self._bad = {}

    # ----- 端点选择：套餐端点先试，不行退按量付费端点
    def nets(self):
        """返回 [(名字, net)]，按尝试顺序。

        「音色复刻」不在所有套餐里，所以默认 auto = 先套餐、失败再按量；
        显式选 plan / payg 就只用那一个，方便排查是谁的锅。
        """
        want = str(self.cfg.get("voice_clone_endpoint", "auto") or "auto").lower()
        plan = [("plan", self.net)] if self.net is not None else []
        payg = [("payg", self.net_payg)] if self.net_payg is not None else []
        if want == "plan":
            return plan
        if want == "payg":
            return payg
        both = plan + payg
        good = [(n, x) for n, x in both if not self._bad.get(n)]
        return good or both          # 全被标过也还是试一遍，好把错误报全

    def _call(self, payload, timeout=60.0):
        """按 nets() 顺序试；全失败时把每个端点的错误一起报出来。"""
        nets = self.nets()
        if not nets:
            raise VoiceError("没有可用端点（先在「密钥」里填 key）")
        errs = []
        for name, net in nets:
            try:
                return name, net.post_json(PATH, payload, timeout=timeout)
            except Exception as exc:                     # noqa: BLE001
                msg = str(exc)
                errs.append("%s: %s" % (name, msg[:120]))
                if "404" in msg or "Model not exist" in msg:
                    self._bad[name] = msg[:80]
                    self.log("%s 端点没有音色复刻模型，本次进程内不再尝试" % name,
                             "warn", "voice")
                self.log("音色克隆走 %s 失败：%s" % (name, msg[:160]), "warn", "voice")
        raise VoiceError("；".join(errs) or "全部端点失败")

    # ----- 主流程
    def create(self, path, name="", target_model=None, secs_hint=None):
        """上传样本创建音色，返回 {voice_id, endpoint, target_model, meta}（不含轮询）。"""
        target = str(target_model or self.cfg.get("voice_clone_model",
                                                  "qwen-audio-3.0-tts-plus"))
        check_sample(path, secs=secs_hint)
        uri, mime, size = data_uri(path)
        prefix = clean_prefix(name or "vdub")
        body = payload_create(target, prefix, uri)
        name_, j = self._call(body, timeout=90.0)
        out = (j or {}).get("output") or {}
        vid = out.get("voice_id") or out.get("voice") or ""
        if not vid:
            raise VoiceError("云端没给 voice_id：%s" % json.dumps(j, ensure_ascii=False)[:200])
        self.log("音色已提交：%s（%s · %s · %.1f MB）" % (vid, name_, target, size / 1048576.0),
                 "info", "voice")
        return {"voice_id": vid, "endpoint": name_, "target_model": target,
                "mime": mime, "bytes": size, "prefix": prefix,
                "status": str(out.get("status") or "")}

    def wait(self, voice_id, tries=40, gap=1.0, progress=None):
        """轮询到 OK。DEPLOYING 继续等，其它状态立刻抛（别让用户干等 40 秒）。"""
        last = ""
        for i in range(max(1, int(tries))):
            _, j = self._call(payload_query(voice_id), timeout=30.0)
            out = (j or {}).get("output") or {}
            st = str(out.get("status") or "").upper()
            last = st or last
            if progress:
                try:
                    progress(i + 1, tries, st)
                except Exception:                        # noqa: BLE001
                    pass
            if st == "OK":
                return {"status": "OK", "tries": i + 1}
            if st in ("", "DEPLOYING", "PENDING", "RUNNING"):
                time.sleep(gap)
                continue
            raise VoiceError("云端状态 %s（不是 DEPLOYING/OK，停）" % (st or "空"))
        raise VoiceError("等了 %d 秒还是 %s，稍后到控制台再点一次「查询」" % (int(tries * gap), last or "DEPLOYING"))

    def list(self):
        """列已创建的音色。这个接口各家实现不完全一样，失败就当没有——不影响主流程。"""
        try:
            _, j = self._call(payload_list(), timeout=30.0)
            out = (j or {}).get("output") or {}
            items = out.get("voices") or out.get("voice_list") or []
            return [{"id": (it.get("voice_id") or it.get("id") or ""),
                     "name": it.get("prefix") or it.get("name") or "",
                     "status": it.get("status") or "",
                     "model": it.get("target_model") or ""}
                    for it in items if isinstance(it, dict)]
        except Exception as exc:                         # noqa: BLE001
            self.log("列音色失败：%r" % (exc,), "warn", "voice")
            return []

    def remove(self, voice_id):
        _, j = self._call(payload_delete(voice_id), timeout=30.0)
        return j
