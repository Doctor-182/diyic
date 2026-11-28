# -*- coding: utf-8 -*-
r"""配置：默认值 + 用户目录 settings.json 覆盖 + 运行期热改。

优先级（后者赢）：
  1. 代码里的 DEFAULTS
  2. %LOCALAPPDATA%\VRDub\config\settings.json（网页改过的值）
  3. %LOCALAPPDATA%\VRDub\config\.env.local（密钥/端点用，不参与界面开关）

老版本写在程序目录（data\settings.json、.env.local），首次启动自动搬到用户目录，
程序目录因此可以是只读的（装在 Program Files 里也能跑）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

from . import paths as _paths

_paths.ensure()
ROOT = _paths.ROOT          # 程序目录（只读也能跑）
DATA = _paths.CONFIG_DIR    # 用户配置目录
OUT = _paths.OUT            # 用户目录下的 out（日志/截图/语料）
SETTINGS = _paths.SETTINGS
# 这些键不落盘：它们是「正确性」而不是「偏好」，被旧的调试值覆盖过一次就出事
# （实测踩过：echo_guard=false 被持久化后，自己的日语被重新翻译了一遍）。
PERSIST_SKIP = ("echo_guard", "mic_db", "loop_db",
                # 开始/停止不该记到下次：调试时关了出声忘了打开，下次启动就「什么都听不到」
                "out_on", "in_on", "asr_on", "mt_on")

VOICE_T3 = "qwen-audio-3.0-tts-plus-t3-52873432237b464a9cb3f9439c5a80b6"

VOICES = [
    {"id": "qwen-audio-3.0-tts-plus-t3-52873432237b464a9cb3f9439c5a80b6",
     "name": "t3 · 最像（默认）", "note": "谱包络 cos 0.9693，F0 300Hz"},
    {"id": "qwen-audio-3.0-tts-plus-t1-1428a6a4dbce42749749a4bce4536e0a",
     "name": "t1 · 自然连贯", "note": "cos 0.9587"},
    {"id": "qwen-audio-3.0-tts-plus-t2-1e83ca4b929847e7a930218c270f6afa",
     "name": "t2 · 干净", "note": "cos 0.9578，高 SNR"},
    {"id": "qwen-audio-3.0-tts-plus-t4-514d483a0bc148009f5105c107061e83",
     "name": "t4 · 偏亮", "note": "cos 0.9644"},
]

DEFAULTS = {
    # 链路
    "mode": "trans",            # trans | pass
    "dst_lang": "ja",           # A 链目标语
    "src_lang": "auto",         # A 链识别语
    "in_lang": "auto",          # B 链识别语
    "sub_lang": "zh",           # B/C 译文
    "card": "natural",         # 保真优先；要角色感再切 teto
    "persona_custom": "",      # 自定义人设（只影响口吻；A 链专属，B/C 链不用）
    "voice": VOICE_T3,
    # 克隆出来的音色：voice_id → 名字/模型/时间，存在 settings 里（云端才是真身，这里只是索引）
    "voices_custom": [],
    # 音色复刻：样本走哪个模型训练、走哪个端点（套餐端点不一定开放复刻，auto = 先套餐再按量）
    "voice_clone_model": "qwen-audio-3.0-tts-plus",
    "voice_clone_endpoint": "auto",
    "out_on": True,
    "in_on": True,
    # 两个正交总闸：链开关关的是「这条链干不干活」，总闸关的是「整机允许它干什么」。
    # asr_on=False → 两条链都不再识别（麦克风/回环照采、电平照显示，只是不进识别器）；
    # mt_on=False  → 只出原文，不翻、不合成、不发 chatbox（用户 m05780：全部停止时还在翻译）。
    "asr_on": True,
    "mt_on": True,
    "ocr_on": False,
    # C 链工作方式：hotkey = 只在按 F6/点「识别一帧」时抓一帧（推荐，不占 CPU、不误读桌面）
    #               poll   = 老行为，常驻轮询（0.35 s/帧）
    "ocr_mode": "hotkey",
    "ocr_src": "auto",           # auto/ja/en/zh/ko/ru/other —— 非中日英走视觉抄字
    "ocr_vl": True,              # 允许用多模态模型抄字（韩/俄/泰…本地词表里没有）
    "ocr_vl_fallback": True,     # 本地一行没认出来时自动补一次视觉抄字
    "ocr_vl_model": "",          # 空 = 用 mt_model（qwen3.6-flash 本身支持图片）          # C 链源语言：auto/ja/en/ko/zh（auto = 按 OCR 结果逐行判断）
    "mic_on": True,
    # 设备
    "mic_dev": 23,
    "out_dev": 19,
    "loop_src": "process",      # B 链输入来源：process = 只采 VRChat 进程树（推荐），device = 采某个设备
    "loop_proc": "VRChat.exe",  # loop_src=process 时要采的进程名
    "loop_dev": 25,             # loop_src=device 时采的设备（扬声器回环 / 虚拟线回环）
    # 设备名（key: in/out/mon/loop）：开机按名字重新绑定，号码只做兜底。
    # Windows 每次启动枚举顺序都可能换（实测 #19 从 CABLE Input 变成扬声器），只认号码会把
    # 日语放给耳机、把耳机麦克风换成阵列麦，而且通道数校验拦不住，所以必须存名字。
    "dev_names": {},
    "mon_dev": 18,              # 测听/自听设备（耳机）
    "mon_on": True,             # 监听：合成音同时放一份到耳机，自己听得见
    "mon_gain": 1.0,            # 监听音量倍率
    "pass_on": True,            # VRChat 专线直通：VRChat 的声音原样放回耳机（否则改走虚拟线后自己听不见）
    "pass_gain": 1.0,
    "pass_duck": 0.35,          # 自己 TTS 出声时把对方压低到 35%，避免两句搅在一起
    "speak_q_max": 1,           # TTS 队列最多压几段（多了就丢碎片，等收尾整句）
    # 分段
    "mic_db": -32.0,
    "mic_gate_db": 6.0,         # 段内 95 分位要比门限高这么多 dB 才送识别（挡静音幻觉）
    "active_ms": 120,           # 段内「高过门限」的帧至少这么多毫秒才算真说话
    "speech_ratio": 0.12,       # 有效发声还要占整段这么多比例（挡咔哒/敲键盘）
    "loop_db": -24.0,
    "auto_noise": True,         # 开麦/开回环时自动测本底噪声定门限
    "noise_margin": 12.0,       # 门限 = 本底 + 这么多 dB
    "min_speech": 0.35,
    "max_speech": 12.0,
    "end_silence": 0.50,
    "valley_cut": False,        # 到了 max_speech 不按时间硬切，回看最近 valley_win 秒找能量谷切
    "valley_win": 2.5,          # 往回看多久找谷（s）
    "valley_gap": 6.0,          # 谷要比该窗口电平中位数低这么多 dB 才算「谷」
    "pre_roll": 0.25,
    "spec_silence": 0.18,       # 投机识别触发的静音
    "live": True,               # 边说边提交子句（同传核心，关掉=等整句）
    "partial_every": 0.55,      # 部分结果节奏 s
    "commit_chars": 8,          # 少于这么多字不提交（避免「嗯，」）
    "passthru": False,           # 直通麦（对方同时听到我的原声）
    # 播放
    "duck": 0.0,
    "gain": 1.0,
    "rate_min": 0.92,
    "rate_max": 1.12,
    "speed": 1.0,               # 主观语速（0.6–1.6），乘在自适应语速之上
    # 引擎
    "asr_engine": "hybrid",     # cloud | local | hybrid（全局兜底，两条链没单独设时用它）
    # 两条链分开选识别引擎（控制台里各有一条下拉）：
    #   A＝我说的话 → 对方语种：本地优先，延迟第一（本地 60-150 ms，云端校正只改字幕）
    #   B＝VRChat 里对方说唱 → 中文：默认云端，准度第一（本地日语对唱歌/远场/多人很弱）
    "asr_engine_a": "hybrid",
    # B 链（对方说话 → 中文字幕）也用 hybrid：本地 SenseVoice 143–351 ms，
    # 实测 5 条日语素材与云端文本相似度 1.00，而云端同批 0.34–13.2 s（抖动极大）。
    # 云端仍会在 _refine_async 里后台校正字幕，所以准确率不降、延迟稳。
    "asr_engine_b": "hybrid",
    # GGUF 档（transcribe.cpp）：qwen17 / qwen06 / fun —— 这三个和 hybrid/local/cloud 平级，
    # 直接写在 asr_engine_a / asr_engine_b 里就能用。换档时旧模型会被显式卸载，
    # 同一时刻只驻留一个（三引擎并存实测出现 12–20 s 离群延迟）。
    "asr_gguf_dir": "",          # 空 = <程序目录>\models\asr
    "asr_gguf_backend": "cuda",  # cuda | vulkan | cpu（小模型上 vulkan 反而更快）
    "asr_gguf_hint": "auto",     # 是否把语种提示传给模型（auto 传 / off 只让它自己判）
    "asr_gguf_lid": "zh",        # 语种探测：off 不探 / zh 只在明确判出中文时切（推荐）/ any 连 en、ko 也切
    "asr_gguf_vocab": [],        # 专名表：GGUF 档的 vocabulary（游戏名/人名），2–6 条最稳；空的等于不用
    # 云端引擎的投机识别（只在 asr_engine_* = cloud 时用到）：
    # 最小间隔、单次超时。云端长段识别会飘到十几秒，靠这两个值把并发和延迟关进笼子。
    "asr_spec_gap": 1.5,
    "asr_spec_timeout": 6.0,
    # 本地引擎不写死：vrdub/asr_engines.py 里有注册表（语言/体积/显存/内存/核数/实测延迟与质量），
    # 启动时按「这台机器的条件」自动选档；这两个键只用来覆盖它（人说了算）：
    #   asr_local_engine：全局指定一个本地引擎 id（sensevoice / whisper-anime）
    #   asr_engine_map：按语种分别指定，如 {"ja": "whisper-anime"}，优先于全局
    # 换到朋友那台机器上不需要改代码：注册表自己会算出该跑哪一个。
    "asr_local_engine": "",
    "asr_engine_map": {},
    # 本地 SenseVoice 权重目录（空 = models/sensevoice）。指向别的目录就能换成
    # 衍生微调版（如社区日语/BGM 微调版），必须含 model.int8.onnx + tokens.txt。
    "sv_dir": "",
    # 把每条定稿音频连同识别文本落盘（out/corpus/日期/），用来在自己的真实语音上 A/B 引擎。
    # 默认关：只在你打算「拿真话测模型」时打开。
    "asr_capture": False,
    # B 链（云端识别）的段长上限：云端识别耗时≈段长，唱歌时 12 s 一段要等 10-11 s，
    # 压到 6 s 后同一首歌的字幕能稳定在 2-4 s 内出来。A 链仍用全局 max_speech。
    "max_speech_b": 6.0,
    # 内存看门狗：软线到了就放掉本地识别模型/缓存，硬线到了自己退出让 run.bat 重启
    # （加进 anime-whisper 之后 Soft 抬到 2200：它 GPU 版常驻约 1.1 GB，旧值一上来就把
    #   刚载好的模型卸掉，等于白载。软线只「卸模型」，不重启，抬高的代价只是更晚释放。）
    "mem_soft_mb": 2200.0,
    "mem_hard_mb": 3600.0,
    "asr_race": False,
    "presynth": True,
    "dbg_timing": False,        # 打开后每次气口/排队都打时间戳（调延迟用）
    "cloud_wait": 2.4,          # 本地听串（中英混排乱码）时同步等云端的秒数，0=不等
    # A 链云端优先：本地和云端同时跑，云端在 a_cloud_wait 秒内回来就用云端的字（更准），
    # 超过就直接用本地结果往下走（不为了更准牺牲延迟）。0 = 关掉（等价 a_cloud_first=0）。
    "a_cloud_first": False,
    "a_cloud_wait": 1.0,        # 用户定的 1000 ms，后续按实测标定
    "refine": True,             # 定稿精修（后台跑，不算延迟）
    # 精修引擎：auto＝云端优先（准），云端挂了/没网才用本地 anime-whisper（GPU 0.35 s）；
    #           cloud＝只用云端；whisper＝只用本地显卡；off＝不精修。
    # 实测（5 条素材 × 4 种劣化，与干净素材云端文本比相似度）：
    #   云端 1.000（尾部长 1.4-5.2 s，最长 13 s）· SenseVoice 0.992（0.25-0.32 s）
    #   anime-whisper 0.968（0.36-0.52 s / 1.1 GB 显存，错在同音汉字）
    # 所以「本地大模型」在这台 8 GB 卡上的价值是「离线可用 + 有标点」，不是「更准」。
    "asr_refine": "auto",
    # 精修后把 B 链中文译文一起刷新（B 链不出声，改了不会「重念」）
    "refine_mt": True,
    # 本地精修参数
    "whisper_device": "auto",   # auto | cuda | cpu
    "whisper_vram_min_mb": 1300,  # 空闲显存低于这个就不占（VRChat 要 5-8 GB）
    "whisper_beam": 1,
    "whisper_threads": 6,       # 只有退回 CPU 才用得上；本机 CPU 实测 10-26 s，别指望
    "whisper_slow_ms": 2500.0,  # 单次超过这个就当本机不适合，永久退回云端
    "whisper_idle_s": 180.0,
    "refine_timeout": 6.0,      # 云端精修上限（s）：实测能飘到 13.2 s，那时候这段早念完了，等它没意义
    # 空闲多久把显存还回去
    "echo_guard": True,
    # 电平/包络类的「漏音」闸默认关：B 链=VRChat 声音比麦克风响时，它们会把
    # 用户自己的话当漏音丢掉（实测一天吃掉 40+ 段真语音）。内容式的 _bleed 才可靠。
    "leak_gate": False,         # 麦克风 vs B 链「电平包络相关」判漏音
    "bleed_lvl": False,         # 用「B 链那一刻更响」判漏音
    "mt_model": "qwen3.6-flash",
    # 浮窗
    "hud_on": True,
    "hud_alpha": 92,
    "hud_scale": 100,
    "hud_rows": 8,
    "hud_click": False,
    "hud_x": -1,
    "hud_y": -1,
    # VRChat chatbox（OSC UDP 9000）：只发 A 链日文译文，B 链绝不进
    "chatbox_on": True,
    # chatbox 的文本走云端识别（更准）。聊天框不需要抢在「我说话说完之前」发出去，
    # 所以不用省时间：云端识别给足 chatbox_budget 秒，等不到才退回本地译文。
    # 这条通路独立于播放在哪一句、播到第几秒（旧实现挂在 _finish 上，所以才「收完才发」）。
    "chatbox_cloud": True,
    "chatbox_budget": 5.0,      # 用户 m04624：可以等更久，让它走云端。实测云端识别 0.34–13.2 s
    "chatbox_notify": False,
    "osc_host": "127.0.0.1",
    "osc_port": 9000,
    # 弹幕浮窗（识别结果像弹幕一样划过屏幕）
    "danmaku": True,
    "danmaku_speed": 7.0,       # 秒/屏宽
    "danmaku_lanes": 4,
    "danmaku_font": 20,
    "danmaku_top": 0.12,        # 屏幕高度的起点比例
    "danmaku_max": 6,
    "danmaku_scale": 100,       # 弹幕整体大小 %（60–200，和字幕浮窗各自独立）
    "danmaku_alpha": 100,       # 弹幕不透明度 %（40–100）
    "danmaku_style": "card",    # card 卡片 / plain 纯描边 / bar 只留色条 / neon 发光
    "danmaku_src": True,        # 副行显示原文
    # 分人（说话人分离）：只给 B 链看「这句话像不像同一个人在说」
    "ball_on": True,            # 桌面悬浮球（点开就能改 A 链语言/设备、打一句中文发聊天框）
    "spkui_on": True,           # 说话人名单窗口（起名、删人、调门限）
    "spk_on": True,             # 开关；关掉 = 不算向量、不标号（省 100 ms CPU/段）
    "spk_th": 0.72,             # 判同一人的余弦门限（0.4–0.98）
    "spk_max": 12,              # 最多记几个人（2–24）；满了淘汰「没起名且最久没出现」的，起过名的永不淘汰
    "spk_secs": 2.0,            # 掐完静音后最多取前多久算向量（越长越准越慢）
    "spk_tta": True,            # 双窗平均：段 >= 2.4 s 时多算一次取平均（+80 ms，抖动减半）
    "spk_merge": 0.70,          # 碎簇合并：「没起名 + 只出现 1–2 次」的小簇，余弦超这个就并进更像的那位
                                # 为什么要低于 spk_th(0.72)：新簇只在「全员都 < 门限」时才诞生，
                                # 同一个人被拆成两号时两个碎簇彼此常在 0.6–0.7，按 0.82 永远并不掉；0 = 关
    "spk_model": "",            # 模型路径，空 = 项目根 models/spk/voxceleb.onnx
    # OCR
    "ocr_region": [0, 0, 0, 0],
    "ocr_fps": 6,
    "ocr_poll": 0.35,
    # C 链「原处浮现」覆盖层：翻出来的中文直接贴在原文那个位置，停 hold 秒再淡出
    "ocr_overlay": True,        # 总开关（关掉就只走右下浮窗/控制台）
    "ocr_overlay_src": True,    # 气泡里副行显示日文原文
    "ocr_hold": 3.5,            # 译文浮住多久（秒）
    "ocr_fade": 0.6,            # 淡出时长
    "ocr_font": 17,             # 译文基础字号（逻辑像素，会按气泡宽度自适应缩）
    "ocr_max_live": 12,         # 同时最多几块气泡
    "ocr_label_strip": False,   # 默认不剔任何行：认到几个字就翻几个字
}


def _host_of(v: str) -> str:
    """把环境变量里的端点写成 Net 能吃的形式。

    'https://host/path'  -> 'host'            （默认协议 https:443，保持旧行为）
    'https://host:8443/' -> 'https://host:8443'
    'http://127.0.0.1:8787' -> 'http://127.0.0.1:8787'  （自建子密钥网关常用）
    """
    s = (v or "").strip()
    if not s:
        return "token-plan.cn-beijing.maas.aliyuncs.com"
    scheme = "https"
    low = s.lower()
    if low.startswith("http://"):
        scheme, s = "http", s[7:]
    elif low.startswith("https://"):
        scheme, s = "https", s[8:]
    host = s.split("/")[0].strip()
    if not host:
        return "token-plan.cn-beijing.maas.aliyuncs.com"
    has_port = ":" in host and host.rsplit(":", 1)[-1].isdigit()
    if scheme == "https" and (not has_port or host.endswith(":443")):
        return host[:-4] if host.endswith(":443") else host
    return scheme + "://" + host


def _load_env(path):
    c = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip().strip('"').strip("'")
    return c


class Cfg:
    """线程安全的配置容器。所有模块拿同一个实例。"""

    def __init__(self, path=SETTINGS):
        self.path = path
        self._lk = threading.RLock()
        self._d = dict(DEFAULTS)
        self._subs = []
        # 老版本把密钥写在程序目录，搬到用户目录（只搬一次，不覆盖已有文件）
        self.migrated = _paths.migrate()
        self.env = _load_env(_paths.ENV_FILE)
        if not self.env:
            self.env = _load_env(_paths.LEGACY_ENV)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.makedirs(OUT, exist_ok=True)
        self._read()
        # 密钥 / 端点
        self.key = self.env.get("DASHSCOPE_TOKENPLAN_KEY", "")
        # 端点可换：填 https://xxx/ 或只填域名都行（这里的值是套餐端点默认地址，不含任何密钥）
        self.host = _host_of(self.env.get("DASHSCOPE_TOKENPLAN_BASE") or
                             "token-plan.cn-beijing.maas.aliyuncs.com")
        self.payg_key = self.env.get("DASHSCOPE_PAYG_KEY", "")
        self.payg_host = _host_of(self.env.get("DASHSCOPE_PAYG_BASE") or
                                  "dashscope.aliyuncs.com")

    # ---------------------------------------------------------------- 持久化
    def _read(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                disk = json.load(fh)
            if isinstance(disk, dict):
                dropped = [k for k in disk if k in PERSIST_SKIP]
                for k, v in disk.items():
                    if k in DEFAULTS and k not in PERSIST_SKIP:
                        self._d[k] = v
                if dropped:
                    for k in dropped:
                        self._d[k] = DEFAULTS[k]
                    try:
                        self.save()
                    except Exception:
                        pass
        except Exception:
            try:
                shutil.copy(self.path, self.path + ".bad")
            except Exception:
                pass

    def save(self):
        tmp = self.path + ".tmp"
        with self._lk:
            data = {k: v for k, v in self._d.items() if k not in PERSIST_SKIP}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------------------------------------------------------------- 读写
    def get(self, k, default=None):
        with self._lk:
            return self._d.get(k, DEFAULTS.get(k, default))

    def __getitem__(self, k):
        return self.get(k)

    def all(self) -> dict:
        with self._lk:
            return dict(self._d)

    def on_change(self, fn):
        with self._lk:
            self._subs.append(fn)

    def update(self, patch: dict) -> dict:
        """增量写。返回真正改变的键值对；越界的值被夹紧，非法键被忽略。"""
        changed = {}
        with self._lk:
            for k, v in (patch or {}).items():
                if k not in DEFAULTS:
                    continue
                v = _coerce(k, v, self._d.get(k))
                if self._d.get(k) != v:
                    self._d[k] = v
                    changed[k] = v
        if changed:
            try:
                self.save()
            except Exception:
                pass
            for fn in list(self._subs):
                try:
                    fn(changed)
                except Exception:
                    pass
        return changed


def _coerce(k, v, old):
    """按**默认值**的类型归一，并夹到合理范围（防止网页滑杆把东西弄死）。

    类型基准必须是 DEFAULTS[k] 而不是当前值：当前值可能是旧版本写坏的
    （实测 dev_names 被存成 "{'out': ...}" 字符串，于是「按当前值判类型」永远
    判成 str，dict 写进去又被 str() 回去，自愈不了）。
    """
    ref = DEFAULTS.get(k, old)
    try:
        if isinstance(ref, bool):
            if isinstance(v, str):
                return v.strip().lower() in ("1", "on", "true", "yes")
            return bool(v)
        if isinstance(ref, int) and not isinstance(ref, bool):
            return int(v)
        if isinstance(ref, float):
            return float(v)
        if isinstance(ref, dict):
            # dev_names 这类「名字→索引」的映射：必须是 dict。
            # 旧版 _coerce 没有这一支，dict 被 str() 成了 "{'out': ...}"，读回来直接炸。
            if isinstance(v, dict):
                return dict(v)
            if isinstance(v, str):
                try:
                    import ast
                    got = ast.literal_eval(v)
                    if isinstance(got, dict):
                        return dict(got)
                except Exception:
                    pass
            return dict(ref)
        if isinstance(ref, list):
            if not isinstance(v, (list, tuple)):
                return list(ref)
            # 纯数字列表（ocr_region 这类坐标）强转 int；其余（专名表、音色表）
            # 原样放行，结构由 clamp_cfg 校验——之前一律 int() 会把字符串表直接吞掉。
            if ref and all(isinstance(x, int) and not isinstance(x, bool) for x in ref):
                return [int(x) for x in v]
            return list(v)
        return str(v) if not isinstance(v, str) else v
    except Exception:
        return old


def clamp_cfg(d: dict) -> dict:
    """把明显不合法的值拉回边界（在 update 之后由 app 调用）。"""
    d = dict(d)
    d["mic_db"] = max(-70.0, min(-10.0, float(d.get("mic_db", -32))))
    d["loop_db"] = max(-70.0, min(-10.0, float(d.get("loop_db", -24))))
    d["noise_margin"] = max(4.0, min(24.0, float(d.get("noise_margin", 12.0))))
    d["end_silence"] = max(0.25, min(1.5, float(d.get("end_silence", 0.5))))
    d["min_speech"] = max(0.15, min(2.0, float(d.get("min_speech", 0.35))))
    d["max_speech"] = max(4.0, min(30.0, float(d.get("max_speech", 12.0))))
    d["max_speech_b"] = max(2.5, min(30.0, float(d.get("max_speech_b", 6.0))))
    d["active_ms"] = int(max(40, min(1500, int(d.get("active_ms", 120) or 120))))
    d["speech_ratio"] = max(0.02, min(0.9, float(d.get("speech_ratio", 0.12))))
    d["valley_win"] = max(0.8, min(8.0, float(d.get("valley_win", 2.5))))
    d["valley_gap"] = max(2.0, min(20.0, float(d.get("valley_gap", 6.0))))
    d["pre_roll"] = max(0.0, min(1.0, float(d.get("pre_roll", 0.25))))
    d["spec_silence"] = max(0.12, min(0.6, float(d.get("spec_silence", 0.18))))
    d["duck"] = max(0.0, min(1.0, float(d.get("duck", 0.0))))
    d["gain"] = max(0.1, min(3.0, float(d.get("gain", 1.0))))
    d["speed"] = max(0.6, min(1.6, float(d.get("speed", 1.0))))
    d["hud_alpha"] = max(20, min(100, int(d.get("hud_alpha", 92))))
    d["hud_scale"] = max(60, min(200, int(d.get("hud_scale", 100))))
    d["danmaku_scale"] = max(60, min(200, int(d.get("danmaku_scale", 100))))
    d["danmaku_alpha"] = max(40, min(100, int(d.get("danmaku_alpha", 100))))
    # 人设：预设卡只认白名单，自定义只留纯文本（超过 1200 字截断——模型吃不下也没意义）
    _card = str(d.get("card", "natural") or "natural").strip()
    d["card"] = _card if _card in ("natural", "teto", "polite") else "natural"
    _pc = d.get("persona_custom", "")
    d["persona_custom"] = (str(_pc) if isinstance(_pc, str) else "").strip()[:1200]
    # 克隆音色：只收我们自己写进去的形状（voice_id 必须像 qwen-audio-*-<t?>-<32位十六进制>）
    _vc = d.get("voices_custom")
    if not isinstance(_vc, list):
        d["voices_custom"] = []
    else:
        out = []
        for it in _vc:
            if not isinstance(it, dict):
                continue
            vid = str(it.get("id") or "").strip()
            if not vid or len(vid) > 200 or " " in vid:
                continue
            out.append({"id": vid,
                        "name": str(it.get("name") or vid)[:40],
                        "model": str(it.get("model") or "")[:60],
                        "ts": float(it.get("ts") or 0.0)})
        d["voices_custom"] = out[:24]
    if d.get("voice_clone_endpoint") not in ("auto", "plan", "payg"):
        d["voice_clone_endpoint"] = "auto"
    _vcm = str(d.get("voice_clone_model") or "").strip()
    d["voice_clone_model"] = _vcm[:60] if _vcm else "qwen-audio-3.0-tts-plus"
    _dkst = str(d.get("danmaku_style", "card") or "card").strip().lower()
    d["danmaku_style"] = _dkst if _dkst in ("card", "plain", "bar", "neon") else "card"
    d["danmaku_speed"] = max(2.5, min(20.0, float(d.get("danmaku_speed", 7.0) or 7.0)))
    d["danmaku_lanes"] = max(2, min(6, int(d.get("danmaku_lanes", 4) or 4)))
    d["danmaku_max"] = max(1, min(10, int(d.get("danmaku_max", 6) or 6)))
    d["danmaku_top"] = max(0.0, min(0.5, float(d.get("danmaku_top", 0.12) or 0.0)))
    d["spk_th"] = max(0.40, min(0.98, float(d.get("spk_th", 0.72) or 0.72)))
    d["spk_max"] = max(2, min(24, int(d.get("spk_max", 12) or 12)))
    d["spk_secs"] = max(0.6, min(6.0, float(d.get("spk_secs", 2.0) or 2.0)))
    d["spk_tta"] = bool(d.get("spk_tta", True))
    d["spk_merge"] = max(0.0, min(0.98, float(d.get("spk_merge", 0.70) or 0.0)))
    d["ball_on"] = bool(d.get("ball_on", True))
    d["spkui_on"] = bool(d.get("spkui_on", True))
    d["hud_rows"] = max(1, min(30, int(d.get("hud_rows", 8))))
    d["ocr_fps"] = max(1, min(12, int(d.get("ocr_fps", 6))))
    d["asr_spec_gap"] = max(0.4, min(8.0, float(d.get("asr_spec_gap", 1.5))))
    d["asr_spec_timeout"] = max(1.0, min(20.0, float(d.get("asr_spec_timeout", 6.0))))
    d["refine_timeout"] = max(2.0, min(20.0, float(d.get("refine_timeout", 6.0))))
    d["chatbox_budget"] = max(0.5, min(20.0, float(d.get("chatbox_budget", 5.0))))
    d["a_cloud_wait"] = max(0.0, min(5.0, float(d.get("a_cloud_wait", 1.0))))
    for k in ("asr_engine", "asr_engine_a", "asr_engine_b"):
        if d.get(k) not in ("hybrid", "local", "cloud", "qwen17", "qwen06", "fun"):
            d[k] = DEFAULTS[k]
    d["asr_gguf_dir"] = str(d.get("asr_gguf_dir") or "").strip()[:260]
    if d.get("asr_gguf_backend") not in ("cuda", "vulkan", "cpu"):
        d["asr_gguf_backend"] = "cuda"
    if d.get("asr_gguf_hint") not in ("auto", "off"):
        d["asr_gguf_hint"] = "auto"
    if d.get("asr_gguf_lid") not in ("off", "zh", "any"):
        d["asr_gguf_lid"] = "zh"
    raw_vocab = d.get("asr_gguf_vocab")
    if isinstance(raw_vocab, str):
        raw_vocab = re.split(r"[,，\n;；]+", raw_vocab)
    if not isinstance(raw_vocab, (list, tuple)):
        raw_vocab = []
    vocab, seen = [], set()
    for it in raw_vocab:
        w = str(it or "").strip()
        if not w or w in seen:
            continue
        seen.add(w)
        vocab.append(w[:24])
    d["asr_gguf_vocab"] = vocab[:8]
    if d.get("asr_refine") not in ("auto", "whisper", "cloud", "off"):
        d["asr_refine"] = "auto"
    if d.get("asr_local_engine") and d["asr_local_engine"] not in ("sensevoice", "whisper-anime", ""):
        d["asr_local_engine"] = ""
    d["sv_dir"] = str(d.get("sv_dir") or "").strip()[:300]
    raw_map = d.get("asr_engine_map")
    if not isinstance(raw_map, dict):
        d["asr_engine_map"] = {}
    else:
        d["asr_engine_map"] = {str(k).lower(): str(v) for k, v in raw_map.items()
                               if str(v) in ("sensevoice", "whisper-anime")}
    if d.get("whisper_device") not in ("auto", "cuda", "cpu"):
        d["whisper_device"] = "auto"
    d["whisper_vram_min_mb"] = max(400, min(7000, int(d.get("whisper_vram_min_mb", 1300))))
    d["whisper_beam"] = max(1, min(5, int(d.get("whisper_beam", 1))))
    d["whisper_threads"] = max(1, min(32, int(d.get("whisper_threads", 6))))
    d["whisper_slow_ms"] = max(300.0, min(60000.0, float(d.get("whisper_slow_ms", 2500.0))))
    d["whisper_idle_s"] = max(15.0, min(3600.0, float(d.get("whisper_idle_s", 180.0))))
    # A 链的目标语是「对方听到的语言」——中文是 B/C 链的输出，不该出现在这里。
    # 真机踩过：UI 上误选成「中文」并落盘，A 链变成中文→中文，模型原样回吐，
    # 于是 chatbox 里全是中文、字幕串英文，看着像翻译坏了。
    if d.get("dst_lang") not in ("ja", "en", "ko", "zh"):
        d["dst_lang"] = "ja"
    if d.get("sub_lang") not in ("zh", "ja", "en"):
        d["sub_lang"] = "zh"
    return d


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")