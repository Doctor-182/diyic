# -*- coding: utf-8 -*-
"""基础件：日志、重采样、电平、文本工具。无第三方依赖（只用 numpy）。"""
from __future__ import annotations

import math
import os
import re
import sys
import threading
import time
import unicodedata
from collections import deque

import numpy as np

# 日志、截图、报告都写到用户目录（%LOCALAPPDATA%\VRDub\out），程序目录不再被写。
from . import paths as _paths

_paths.ensure()
OUT = _paths.OUT
try:
    os.makedirs(OUT, exist_ok=True)
except Exception:                       # noqa: BLE001
    pass

# ---------------------------------------------------------------- 日志
_LOCK = threading.Lock()
_RING: deque = deque(maxlen=400)
_SINKS: list = []          # fn(rec: dict)
_T0 = time.time()


def add_sink(fn):
    """注册日志落点（SSE / 文件）。fn 必须很快返回，别在里面做 IO。"""
    with _LOCK:
        _SINKS.append(fn)


def dbg(text: str):
    """异常堆栈落到 out/err.log（诊断用，不进日志环、不污染界面）。"""
    try:
        with open(os.path.join(OUT, "err.log"), "a", encoding="utf-8") as fh:
            fh.write(f"\n[{time.strftime('%H:%M:%S')}] {text}")
    except Exception:
        pass


_LOG_FH = None
_LOG_PATH = ""

# 日志轮转：一个文件最多 8 MB，留一份 .1 备份。以前是 "ab" 无限追加，
# out\ 长到 362 MB / 5929 个文件，用户想发日志给我都没法发。
LOG_MAX_MB = 8.0
LOG_FILES = ("err.log", "hud.log", "overlay.log", "danmaku.log",
             "tray.log", "backend.log", "backend.err.log", "keepalive.log")


def rotate_logs(limit_mb: float = None):
    """启动时跑一次：超限的日志改名成 xxx.log.1（旧的 .1 直接删）。"""
    lim = (LOG_MAX_MB if limit_mb is None else float(limit_mb)) * 1024 * 1024
    done = []
    for name in LOG_FILES:
        p = os.path.join(OUT, name)
        try:
            if not os.path.exists(p) or os.path.getsize(p) <= lim:
                continue
            old = p + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.replace(p, old)
            done.append(name)
        except Exception:                   # noqa: BLE001
            pass
    return done


def hide_from_capture(widget) -> bool:
    """让这个窗口对屏幕抓取不可见（Win10 2004+ 的 WDA_EXCLUDEFROMCAPTURE）。

    为什么必须有：C 链（OCR 原处浮现）和弹幕/浮窗都是置顶绘制层，抓屏会把它们自己也抓进去，
    于是「屏幕上的中文 → 再 OCR → 再翻译 → 再画」形成自反馈，屏幕上越翻越乱。
    加了这条之后，人眼看得见、抓屏看不见。
    """
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        u = ctypes.windll.user32
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        if u.SetWindowDisplayAffinity(hwnd, 0x00000011):     # WDA_EXCLUDEFROMCAPTURE
            return True
        return bool(u.SetWindowDisplayAffinity(hwnd, 0x00000001))   # 退 WDA_MONITOR
    except Exception:                                       # noqa: BLE001
        return False


def log(msg, lv: str = "info", tag: str = ""):
    rec = {"t": time.time(), "lv": lv, "tag": tag, "msg": str(msg)}
    with _LOCK:
        _RING.append(rec)
        sinks = list(_SINKS)
    ts = time.strftime("%H:%M:%S", time.localtime(rec["t"]))
    head = f"{ts}.{int((rec['t'] % 1) * 1000):03d}"
    line = f"{head} [{lv[:4]:4s}] {('[' + tag + '] ') if tag else ''}{msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    # 同时落盘：run.bat 起的进程控制台是隐藏/最小化的，只看 stdout 等于没有日志。
    # 单文件 + 超过 5MB 换名，避免长跑把盘写满。
    try:
        global _LOG_FH, _LOG_PATH
        if _LOG_FH is None:
            _LOG_PATH = os.path.join(OUT, "srv.log")
            _LOG_FH = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
        if _LOG_FH.tell() > 5 * 1024 * 1024:
            try:
                _LOG_FH.close()
            except Exception:                   # noqa: BLE001
                pass
            try:
                os.replace(_LOG_PATH, _LOG_PATH + ".1")
            except Exception:                   # noqa: BLE001
                pass
            _LOG_FH = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
        _LOG_FH.write(line + "\n")
    except Exception:                           # noqa: BLE001
        pass
    for fn in sinks:
        try:
            fn(rec)
        except Exception:
            pass


def recent_logs(n: int = 120):
    with _LOCK:
        return list(_RING)[-n:]


def uptime() -> float:
    return time.time() - _T0


# ---------------------------------------------------------------- 音频
def to_mono(x: np.ndarray) -> np.ndarray:
    """任意声道 → 单声道 float32。"""
    a = np.asarray(x, dtype=np.float32)
    if a.ndim == 2:
        a = a.mean(axis=1)
    return a


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """线性插值重采样（整倍数走便宜路径）。

    48k↔24k↔16k 的整倍数用得最多；48k→16k 先做 3 点均值（粗低通，压掉 8k 以上
    再抽取，避免混叠），24k→48k 直接线性插值（高频略有损失，对 TTS 播放无感）。
    """
    if sr_in == sr_out:
        return np.asarray(x, dtype=np.float32)
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return x
    if sr_in % sr_out == 0:                       # 降采样
        f = sr_in // sr_out
        if x.size >= f:
            n = x.size // f
            y = x[: n * f].reshape(n, f).mean(axis=1)
            return y.astype(np.float32)
        return x
    if sr_out % sr_in == 0:                       # 升采样
        f = sr_out // sr_in
        n = x.size * f
        idx = np.arange(n, dtype=np.float32) / f
        return np.interp(idx, np.arange(x.size, dtype=np.float32), x).astype(np.float32)
    n = max(1, int(round(x.size * sr_out / sr_in)))
    idx = np.linspace(0.0, x.size - 1.0, n, dtype=np.float32)
    return np.interp(idx, np.arange(x.size, dtype=np.float32), x).astype(np.float32)


def db_of(x: np.ndarray) -> float:
    """RMS → dBFS。静音给 -100。"""
    a = np.asarray(x, dtype=np.float32)
    if a.size == 0:
        return -100.0
    r = float(np.sqrt(np.mean(a * a)))
    return 20.0 * math.log10(r + 1e-9)


def pcm16_to_float(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(x: np.ndarray) -> bytes:
    return np.clip(np.asarray(x, np.float32) * 32768.0, -32768, 32767).astype("<i2").tobytes()


# ---------------------------------------------------------------- 文本
_PUNCT = "，。！？、；：,.!?;:…—～~「」『』()（）[]【】\"'“”‘’ "


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF        # 假名
        or 0x3400 <= o <= 0x9FFF     # 汉字
        or 0xF900 <= o <= 0xFAFF
        or 0xFF66 <= o <= 0xFF9F     # 半角片假名
    )


_ARROW = ("→", "->", "⇒", "=>", "＝>")
# 模型爱加的标注/情绪标签：整段括号里是短标注就删掉，这些绝不能被念出来
_ANNO_PAIRS = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"), ("〔", "〕"),
               ("〈", "〉"), ("《", "》"), ("<", ">"), ("｛", "｝"), ("{", "}"))
_ANNO_WORDS = {
    "happy", "sad", "angry", "laugh", "laughs", "laughing", "sigh", "sighs", "smile",
    "smiling", "cry", "crying", "whisper", "shout", "excited", "nervous", "calm",
    "end", "start", "begin", "stop", "pause", "emotion", "tone", "voice", "lang",
    "ja", "jp", "zh", "en", "chinese", "japanese", "english", "translation",
    "开心", "高兴", "难过", "生气", "愤怒", "笑", "大笑", "哭", "叹气", "小声",
    "大喊", "激动", "紧张", "平静", "温柔", "疑惑", "惊讶", "停顿", "语气", "情绪",
    "开始", "结束", "注", "旁白", "括号", "音效", "翻译",
}


def _is_anno(inner: str) -> bool:
    v = (inner or "").strip()
    if not v or len(v) > 12:
        return False
    low = v.lower().strip(" :：,，.。!！?？")
    if low in _ANNO_WORDS:
        return True
    if any(c in "，。！？、；,.!?;" for c in v):
        return False
    if any(c.isdigit() for c in v):
        return False
    latin = any("a" <= c.lower() <= "z" for c in v)
    kana = any(0x3040 <= ord(c) <= 0x30FF for c in v)
    if latin or kana:
        return True
    # 纯汉字/数字以外的短标注（微笑 / 小声 / 叹气）也当标注处理：括号里放一句话才是正文
    return len(v) <= 6


def strip_anno(s: str) -> str:
    """去掉标注/情绪标签：模型偶尔吐 (happy)…(end)、[笑]、<emotion>、*叹气* 这类东西，
    TTS 会当成正文念出来（实测念成 happy / end），字幕里也很难看。"""
    t = (s or "")
    if not t:
        return t
    pairs = {op: cl for op, cl in _ANNO_PAIRS if op != cl}
    out = []
    i, n = 0, len(t)
    while i < n:
        ch = t[i]
        cl = pairs.get(ch)
        if cl:
            j = t.find(cl, i + 1)
            if j > i and _is_anno(t[i + 1:j]):
                i = j + 1
                continue
        out.append(ch)
        i += 1
    t = "".join(out)
    # 星号/井号/波浪包起来的短标注：*叹气* / #旁白#
    for mark in ("*", "#", "~"):
        if t.count(mark) < 2:
            continue
        parts = t.split(mark)
        keep = [parts[0]]
        for k in range(1, len(parts), 2):
            inner = parts[k].strip()
            if _is_anno(inner):
                keep.append("")
            else:
                keep.append(parts[k])
            if k + 1 < len(parts):
                keep.append(parts[k + 1])
        t = "".join(keep)
    # 开头的纯标签词（happy：你好 / end。好的）
    t = t.strip()
    for _ in range(3):
        m = re.match(r"^([A-Za-z\u4e00-\u9fff]{2,12})\s*[：:]\s*", t)
        if m and m.group(1).lower() in _ANNO_WORDS:
            t = t[m.end():].strip()
            continue
        break
    # 结尾孤立标签词（…好的。end / …だよ end）
    for _ in range(3):
        m = re.search(r"\s([A-Za-z]{2,12})\s*[。.！!？?]?$", t)
        if m and m.group(1).lower() in _ANNO_WORDS:
            t = t[:m.start()].rstrip(" 。．.!！?？,，、")
            continue
        break
    t = re.sub(r"([。！？!?])\1+", r"\1", t)
    return t.strip(" \t\u3000")

_LABELS = ("译文：", "译文:", "翻译：", "翻译:", "日文：", "日文:", "中文：", "中文:",
           "訳：", "訳:", "翻訳：", "翻訳:", "Translation:", "Output:")


def clean_translation(s: str) -> str:
    """模型偶尔把「原文→译文」或「译文：xxx」整块吐出来，进 TTS / chatbox 前切干净。"""
    s = (s or "").strip()
    if not s:
        return s
    # 取箭头之后的部分（模型自问自答最常见的形态）
    cut = -1
    for a in _ARROW:
        k = s.rfind(a)
        if k > cut:
            cut = k + len(a)
    if cut > 0:
        tail = s[cut:].strip()
        if len(tail) >= 2:
            s = tail
    # 「译文：」这类标签只在开头出现时去掉
    for lb in _LABELS:
        if s.startswith(lb):
            s = s[len(lb):].strip()
            break
    # 全角/半角引号整体包裹（模型爱加）
    if len(s) >= 4 and s[0] in "「『" and s[-1] in "」』":
        s = s[1:-1].strip()
    # 标注/情绪标签（(happy)…(end)、[笑]、*叹气*）：绝不能让 TTS 念出来
    s = strip_anno(s)
    return clean_text(s)


def clean_text(s: str) -> str:
    """去掉模型爱塞的空白与零宽字符，中日文之间不留空格（TTS 会读成停顿）。"""
    s = (s or "").strip()
    if not s:
        return s
    s = s.replace("\u200b", "").replace("\ufeff", "").replace("\u3000", " ")
    out = []
    for ch in s:
        if ch in " \t" and out and _is_cjk(out[-1]):
            continue
        out.append(ch)
    s = "".join(out).strip()
    # 中文字之间残留的空格
    res = []
    for ch in s:
        if ch == " " and res and _is_cjk(res[-1]):
            continue
        res.append(ch)
    return "".join(res).strip()


def bare(s: str) -> str:
    """只留内容字符，用于相似度/回声判定。"""
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(ch for ch in s if ch not in _PUNCT and not ch.isspace()).lower()


def similarity(a: str, b: str) -> float:
    """0-1，字符级相似度（difflib，够用且纯 Python）。"""
    import difflib
    a, b = bare(a), bare(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


_KANA = set("ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをんゔ"
            "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴ")


def kana_ratio(s: str) -> float:
    """假名占内容字符的比例（只算实字，不算标点空白）。"""
    t = bare(s)
    if not t:
        return 0.0
    k = sum(1 for ch in t if 0x3040 <= ord(ch) <= 0x30FF or 0x31F0 <= ord(ch) <= 0x31FF)
    return k / float(len(t))


def _ratios(s: str):
    t = bare(s)
    if not t:
        return 0.0, 0.0, 0.0
    kana = latin = cjk = 0
    for ch in t:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
            kana += 1
            cjk += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            cjk += 1
        elif "a" <= ch <= "z":
            latin += 1
    n = float(len(t))
    return kana / n, cjk / n, latin / n


def lang_ok(s: str, dst: str) -> bool:
    """译文真的是目标语言吗？

    真机踩过：模型偶尔把中文原样吐回来、或者干脆翻成英文，
    结果中文串进日语、英文串进中文字幕，还会被 TTS 念出来。
    """
    t = bare(s)
    if len(t) < 4:
        return True                     # 太短（ok / 嗯）不判，避免误杀
    kana, cjk, latin = _ratios(s)
    if dst == "ja":
        return kana >= 0.15
    if dst == "zh":
        # 中文里夹英文词很常见（「进行一些ERP」「这个PC配置」），
        # 拉丁比例放到 0.55，仍能挡住整句英文（latin≈0.9、cjk=0）。
        return cjk >= 0.45 and kana <= 0.10 and latin <= 0.55
    if dst == "en":
        return latin >= 0.45
    return True


def hangul_ratio(s: str) -> float:
    """韩文谚文占内容字符的比例（日语链里出现韩文＝模型串台）。"""
    t = bare(s)
    if not t:
        return 0.0
    k = sum(1 for ch in t if 0xAC00 <= ord(ch) <= 0xD7A3 or 0x1100 <= ord(ch) <= 0x11FF)
    return k / float(len(t))


def lang_why(s: str, want: str) -> str:
    """语种对不对；不对的时候给一句人话原因（对的返回空串）。"""
    if lang_ok(s, want):
        return ""
    kana, cjk, latin = _ratios(s)
    bits = ["假名 %.0f%%" % (kana * 100.0), "汉字 %.0f%%" % (cjk * 100.0),
            "拉丁 %.0f%%" % (latin * 100.0)]
    hg = hangul_ratio(s)
    if hg > 0.05:
        bits.append("韩文 %.0f%%" % (hg * 100.0))
    return "，".join(bits)


def has_kana(s: str) -> bool:
    return any(ch in _KANA for ch in s or "")


def char_count(s: str) -> int:
    return len(bare(s))


def split_sentences(text: str):
    """把一段文本按句末标点切开（保留标点）。"""
    text = (text or "").strip()
    if not text:
        return []
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in "。！？!?…":
            if cur.strip():
                out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_ms(v) -> str:
    return "-" if v is None else f"{int(v)}ms"

def xcorr_norm(x, y, kmin=None, kmax=None):
    """归一化互相关（匹配滤波）：在长信号 x 上滑动短信号 y，允许负的时移。

    返回 (峰值相关, 峰值位置 k, 能量差 dB)。k 的含义：x[k + i] ≈ gain * y[i]，
    能量差 = 10*log10(E_y / E_x(k))，负值表示麦克风那一路更小（回声的常态）。

    用途：判断「麦克风里的这段声音是不是我们刚放出去的那段音频」。
    真实漏音是同一段波形，相关能到 0.3~0.99；人自己重说一遍同一句话时波形完全不同，
    相关 < 0.05（实测 0.00–0.02）—— 这是唯一不会误杀复述的判据。

    注意：kmin 允许为负（x 的起点可能晚于 y）。
    """
    import numpy as np
    x = np.asarray(x, np.float32).ravel()
    y = np.asarray(y, np.float32).ravel()
    L = int(len(y))
    if L < 400 or len(x) < L:
        return 0.0, 0, 0.0
    span = len(x) - L
    kmin = -span if kmin is None else max(int(kmin), -span)
    kmax = span if kmax is None else min(int(kmax), span)
    if kmax < kmin:
        return 0.0, 0, 0.0
    pad_l = max(0, -kmin)
    pad_r = max(0, kmax)
    xp = np.concatenate([np.zeros(pad_l, np.float32), x, np.zeros(pad_r, np.float32)])
    N = 1
    while N < len(xp) + L:
        N <<= 1
    C = np.fft.irfft(np.fft.rfft(xp, N) * np.conj(np.fft.rfft(y, N)), N)
    xd = xp.astype(np.float64)
    e = np.concatenate(([0.0], np.cumsum(xd * xd)))
    idx = np.arange(kmin + pad_l, kmax + pad_l + 1)
    ex = np.maximum(e[idx + L] - e[idx], 1e-12)
    ey = float(np.maximum(np.sum(y.astype(np.float64) ** 2), 1e-12))
    c = C[idx] / (np.sqrt(ex * ey) + 1e-12)
    if not c.size:
        return 0.0, 0, 0.0
    # 只在「参考信号本身有能量」的位置比较：播放历史里大片是静音（填 0），
    # 那些位置归一化后会被噪声顶成假峰（实测能量差 +141 dB 就是这种假峰）。
    good = ex >= float(ex.max()) * 0.02
    if good.any():
        c = np.where(good, c, 0.0)
    j = int(np.argmax(np.abs(c)))
    k = int(idx[j]) - pad_l
    gap = 10.0 * float(np.log10(ey / float(ex[j])))
    return float(c[j]), k, round(gap, 1)
