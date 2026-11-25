# 协议契约 v1 —— 后端 / 网页控制台 / 浮窗

这份文件是**唯一的接口真相**。三个进程各自照着它写，谁都不许改字段名。
后端是 `vrdub/`（Lead 写），网页是 `vrdub/web/`，浮窗是 `vrdub/hud.py`，OCR 是 `vrdub/ocr.py`。

端口：**8899**（旧项目占用 8770，别碰）。只用 `127.0.0.1`。

---

## 0. 名词

| 链 | 含义 | 方向 |
|---|---|---|
| **A 出** | 我说话 → 对方听到目标语（我的克隆音色） | 麦克风 → ASR → MT → TTS → VB-Cable |
| **B 入** | 对方说话 → 我看到中文 | 扬声器 Loopback → ASR → MT → 字幕 |
| **C 屏** | 对方打字（Chatbox）→ 我看到中文 | 屏幕 OCR → MT → 字幕浮在原文位置 |

---

## 1. HTTP API

所有响应 `Content-Type: application/json; charset=utf-8`，UTF-8。
所有 POST 请求体是 JSON（`ensure_ascii` 与否都接受）。

### GET /
返回控制台单页 `vrdub/web/index.html`。同目录的 `app.js`、`style.css` 走 `/static/<file>`。

### GET /api/state
一次拿全量状态。**网页启动先调它，之后靠 SSE 增量。**

```jsonc
{
  "ok": true,
  "ver": "1.0",
  "pid": 12345,
  "uptime_s": 12.3,
  "cfg": {
    "mode": "trans",              // "trans" 翻译模式 | "pass" 直通(不变声)
    "dst_lang": "ja",             // A 链目标语 ja|en|zh
    "src_lang": "auto",           // A 链识别语 auto|zh|ja|en
    "in_lang": "auto",            // B 链识别语
    "sub_lang": "zh",             // B/C 链译文语言
    "voice": "qwen-audio-3.0-tts-plus-t3-...",
    "card": "teto",               // 人设卡
    "out_on": true,               // A 链总开关
    "in_on": true,                // B 链总开关
    "ocr_on": false,              // C 链总开关
    "mic_on": true,               // 麦克风总闸（CapsLock 控制）
    "mic_dev": 23,                // 输入设备索引
    "out_dev": 19,                // 输出设备索引（VB-Cable 的 CABLE Input）
    "loop_dev": 25,               // B 链采集：扬声器 Loopback
    "mic_db": -32.0,              // 说话门限 dBFS
    "loop_db": -24.0,
    "min_speech": 0.35,           // 最短有效语音 s
    "max_speech": 12.0,
    "end_silence": 0.50,          // 句尾静音确认 s
    "pre_roll": 0.25,
    "duck": 0.0,                  // TTS 播放时直通麦的增益 0..1（0=全静音）
    "gain": 1.0,                  // TTS 播放增益
    "asr_engine": "hybrid",       // 全局兜底 cloud|local|hybrid(本地先出、云端校正)
    "asr_engine_a": "hybrid",     // A 链（我说→对方语种）单独选；hybrid=本地 60-150ms 先出字
    "asr_engine_b": "cloud",      // B 链（VRChat 里的人/歌→中文）单独选；cloud=云端准，1.8-3.0s
    "asr_spec_gap": 1.5,          // cloud 引擎投机识别最小间隔 s（后台线程，不占链线程）
    "asr_spec_timeout": 6.0,      // cloud 引擎投机单次超时 s
    "asr_race": false,            // 备用按量付费第二路（烧钱，默认关）
    "presynth": true,             // 停顿处预翻译+预合成（降延迟，风险开关）
    "echo_guard": true,           // 译文≈原文时重试并拦 TTS
    "hud_on": true,
    "hud_alpha": 92,
    "hud_scale": 100,
    "hud_rows": 8,
    "hud_click": false,           // 鼠标可拖动（默认穿透）
    "hud_x": -1, "hud_y": -1,     // -1 = 右下角默认位
    "ocr_region": [0,0,0,0],      // 屏幕物理像素 x,y,w,h（0 = 未标定）
    "rate_min": 0.9,              // TTS 语速范围（按说话快慢跟随）
    "rate_max": 1.15
  },
  "devices": {
    "input":  [{"i": 22, "name": "麦克风阵列 (…)", "loop": false, "ch": 2, "sr": 48000}],
    "output": [{"i": 19, "name": "CABLE Input (VB-Audio Virtual Cable)", "loop": false, "ch": 2, "sr": 48000}],
    "loop":   [{"i": 25, "name": "扬声器 (Rapoo Gaming Headset) [Loopback]", "loop": true, "ch": 2, "sr": 48000}]
  },
  "voices": [{"id": "qwen-audio-3.0-tts-plus-t3-…", "name": "t3 · 最像", "note": "谱包络 cos 0.9693"}],
  "metrics": {
    "A": {"n": 20, "e2e_p50": 1420, "e2e_p90": 2100, "asr_p50": 130, "mt_p50": 320, "tts_p50": 1020},
    "B": {"n": 12, "e2e_p50": 900,  "e2e_p90": 1400, "asr_p50": 700, "mt_p50": 300, "tts_p50": 0}
  },
  "hud": {"on": true, "pid": 4321, "alive": true},
  "last": [ /* 最近 50 条段，schema 见 §3 */ ]
}
```

### POST /api/cfg
增量改配置。请求体是 `cfg` 的子集，返回 `{"ok":true,"cfg":{...}}`。
改设备索引会自动重启对应采集/播放线程。改 `mic_db` 立即生效。

### POST /api/tts_test
`{"text":"…","lang":"ja","voice":"…(可选)","play":true}`
不占麦克风、不影响在跑的链路。返回 `{"ok":true,"ms":1720,"audio_s":4.0,"first_pkt_ms":1029}`。
同时在 SSE 推一条 `{"k":"tts_test", ...}`。

### POST /api/mic
`{"on": true|false}` 等价于按一次 CapsLock。返回 `{"ok":true,"mic_on":true}`。

### POST /api/chains
`{"out_on":true,"in_on":true,"ocr_on":false}` 批量开关。

### GET /api/devices
只返回 `devices` 段（便于网页刷新下拉框）。

### GET /api/metrics
`{"A":{...},"B":{...},"rows":[...最近50...]}`

### POST /api/ocr/region
`{"region":[x,y,w,h]}`，写进 cfg（屏幕物理像素）。同时 `{"capture":true}` 时立刻截一张图存 `out/ocr_crop.png` 并可被 `/api/ocr/test` 用。

### POST /api/ocr/test
`{}` → 立即对当前区域跑一次 OCR + 翻译，返回 `{"ok":true,"lines":["…"],"zh":["…"],"ms":420}`，并把结果画到浮窗（用于标定）。

### GET /api/health
`{"ok":true,"ts":…}`（给脚本轮询用）。

### GET /api/events
`text/event-stream`。每条 `data: <one-line json>\n\n`，心跳 15 s（`{"k":"hb"}`）。

---

## 2. SSE 事件

| k | 负载 | 频率 |
|---|---|---|
| `hb` | `{"ts":...}` | 15 s |
| `st` | 与 `/api/state` 同构的**瘦身版**（cfg+metrics+hud），不含 devices/voices | 状态变化时 |
| `level` | `{"mic":-38.2,"loop":-51.0,"gate":-32}` | 每 100 ms |
| `seg` | 段（§3），同一 `id` 会推多次（开始 → 识别 → 翻译 → 播放完） | 每句 2–4 次 |
| `log` | `{"lv":"info|warn|err","msg":"…"}` | 事件驱动 |
| `mic` | `{"on":true}` | 手动/热键切换后 |
| `tts_test` | `{"ms":…,"first_pkt_ms":…,"audio_s":…}` | 手动 |

---

## 3. 段（segment）schema —— 网页表格和浮窗共用

```jsonc
{
  "id": "A-172",                  // 链前缀 + 单调序号
  "chain": "A",                   // A|B|C
  "ts": 1732161234.5,             // 句尾定稿时刻（epoch 秒）
  "state": "asr",                 // asr|mt|tts|play|done|refined|drop|err
  "src": "今日はちょっと遅れちゃった",   // 识别原文
  "dst": "今天有点迟到了",              // 译文（A 链是目标语，B/C 链是中文）
  "src_lang": "ja", "dst_lang": "zh",
  "asr_ms": 128,                  // 识别耗时（真正产出这段文本的那一次：local 30-150ms / cloud 1800-3000ms）
  "cloud_ms": 780,                // 后台云端校正耗时（只在 hybrid 下出现；绝不覆盖 asr_ms）
  "mt_ms": 320,                   // 翻译耗时（首子句）
  "tts_ms": 1020,                 // TTS 首包耗时
  "audio_ms": 2100,               // 这段音频时长
  "e2e_ms": 1520,                 // 句尾 → 出声（A）或句尾 → 字幕上屏（B）
  "rtf": 0.72,                    // 播放实时率 = audio_ms / e2e_ms
  "engine": "local+cloud",        // 识别引擎：local | cloud（hybrid 时先 local，校正后变 refined）
  "note": ""                      // "疑似回声"、"本地草稿"、"预合成命中"…
  "echo": false,
  "speaker": null
}
```

网页表格列：时间 / 链 / 原文 / 译文 / 识别 ms / 翻译 ms / TTS ms / 句长 s / 实时率 / 备注。
**A 链的译文绝不进 chatbox**（B/C 的译文只在本机显示）。

---

## 4. 浮窗（hud.py）协议 —— 后端 → 子进程 stdin，一行一个 JSON

```jsonc
{"op":"cfg","alpha":92,"scale":100,"rows":8,"click":false,"x":-1,"y":-1}
{"op":"seg","seg":{ /* §3 */ }}          // 同 id 覆盖旧行
{"op":"mic","on":true}
{"op":"clear"}
{"op":"status","text":"A 链 运行中 · CapsLock 闭麦"}
{"op":"ping"}                            // 后端每 5 s 一次，浮窗 20 s 收不到自己退出
{"op":"quit"}
```

浮窗要求：
- 无边框、置顶、`WindowTransparentForInput`（默认穿透，`click:true` 才可拖）
- 半透明深色圆角底，`alpha` 是底不透明度百分数
- 每行：`[链] 原文 → 译文`，右侧小字 `识别 128 / 翻译 320 / TTS 1020 ms · 实时率 0.72`
- 最多 `rows` 行，新的在下方，超出滚掉；`done` 后 20 s 淡出
- 闭麦时右上角显示红色「麦已闭」
- 1536×864 逻辑分辨率下默认放右下角，`hud_scale` 缩放字体
- stdout 不要打印任何东西（后端会读；调试写 stderr）

---

# 增补 v1.1（已实现，以此为准）

## 1.1 新增 POST
| 路径 | 请求 | 返回 | 用途 |
|---|---|---|---|
| `/api/inject` | `{"text":"…","lang":"zh","dev":20}` | `{ok,text,lang,audio_s,dev,first_pkt_ms,ms}` | **回环自测**：后端自己合成这句从 `out_dev`（`dev` 可覆盖）播出去，让对面那条线真的收到声音。不接真人也能验完整链路 |
| `/api/clear` | `{}` | `{ok:true}` | 清空句子表 + 指标（日志流不动） |
| `/api/quit` | `{}` | `{ok:true}` | 优雅退出（等价 Ctrl+C） |

## 1.2 新增 cfg 键
| 键 | 默认 | 说明 |
|---|---|---|
| `speed` | 1.0 | **主观语速 0.6–1.6**，直接进 TTS `rate`，乘在自适应语速之上（实测 0.8→4.4s 的句子变 5.8s，1.4→2.9s） |
| `mic_gate_db` | 6.0 | 段内 95 分位要比 `mic_db` 高这么多 dB 才送识别（挡「静音被听出一句话」的幻觉） |
| `partial_every` | 0.55 | 流式部分结果节奏 s |
| `commit_chars` | 8 | 少于此字数不提交子句 |
| `dbg_timing` | false | 打开后日志里打「投机 / 气口 / 出队前排队」时间戳 |
| `refine` | true | 云端识别校正字幕（后台线程，不计入延迟） |
| `spec_silence` | 0.18 | 投机识别触发的静音 s |

## 1.4 B 链输入来源（进程回环）
| cfg 键 | 默认 | 说明 |
|---|---|---|
| `loop_src` | `process` | `process` = 只采 `loop_proc` 这个进程树（Windows 进程回环，无需额外驱动）；`device` = 退回采某个设备（旧的 `loop_dev` 那条路） |
| `loop_proc` | `VRChat.exe` | `loop_src=process` 时要采的进程名（`start()` 时解析一次 PID，之后由守护线程每 2 s 检查、断了自动重建） |
| `loop_dev` | 25 | 只在 `loop_src=device` 时用 |

`GET /api/state` 新增：

```json
"loop": {"src":"process","proc":"VRChat.exe","pid":26688,"alive":true,
         "stat":{"frames":1498080,"dur":31.21,"drops":0,"blocks":3121,"silent":0,"errs":0,"peak":0.1452}}
```

`blocks` 每秒应涨约 50（20 ms 一块）；`drops` 涨说明 `on_pcm` 回调里有阻塞操作（严禁在回调里跑 ASR/重采样/写盘）。改成进程回环后，自己的麦克风、系统音、自己的 TTS 监听都不再进 B 链，因此不需要再靠回声拦截挡自己的日语。

## 1.5 C 链单帧识别 / 框选 / 多语种（hotkey 模式）
| 路径 | 请求 | 返回 | 用途 |
|---|---|---|---|
| `POST /api/ocr/once` | `{}` 或 `{"region":[x,y,w,h]}`（物理像素） | `{ok,ms,lines,items:[{src,dst,box,lang}]}`；没框选 → `{ok:false,error:"no_region"}` | **只抓这一帧**（F6 / 控制台「识别一帧」）。不走帧差、不去重、不看「区域未标定」，抓到什么报什么；同时走 on_raw/on_text → 原位浮现 + 段逻辑 |
| `POST /api/region/pick` | `{}` | `{ok,region:[物理像素],logical:[逻辑像素],dpr}`；用户取消 → `{ok:false,cancel:true}` | 起 `vrdub/region_picker.py`（全屏遮罩 + 8 手柄），后端自己乘 dpr 写 `cfg.ocr_region` 并推给 OcrChain |

`GET /api/state` 新增 `"ocr": {mode, src, on, region, ready}`。

| cfg 键 | 默认 | 说明 |
|---|---|---|
| `ocr_mode` | `hotkey` | `hotkey` = 只在 F6/按钮时抓一帧（不占 CPU、不误读桌面）；`poll` = 老的常驻轮询（`ocr_poll` 秒一帧） |
| `ocr_src` | `auto` | 屏幕上的语种：`auto/ja/en/zh/ko/ru/other`。`auto` 先本地认，认不出来才升级视觉抄字 |
| `ocr_vl` | true | 允许用多模态模型「视觉抄字」（`qwen3.6-flash`；该端点上 `qwen3-vl-*` 全 404） |
| `ocr_vl_fallback` | true | auto 模式下允许自动升级：①字数比框宽应有的稀得多（换字符集了）②det 框住宽正文但一个字都没认出来 |
| `ocr_vl_model` | ""（空=用 `mt_model`） | 视觉抄字用哪个模型 |
| `ocr_overlay` | true | 译文浮在原气泡位置（独立 Qt 子进程，`WDA_EXCLUDEFROMCAPTURE` 防自反馈） |
| `ocr_hold` / `ocr_fade` | 4.0 / 0.6 | 停留秒数 / 淡出秒数 |
| `ocr_overlay_src` | true | 副行显示原文 |
| `ocr_font` / `ocr_max_live` | 17 / 12 | 字号 / 同时在屏的气泡上限 |
| `ocr_label_strip` | **false** | 默认一行都不剔：认到几个字就翻几个字。设 true 才剔气泡左上角那种「窄、短、无句读」的说话人名字行 |

**热键映射**（`vrdub/hotkey.py`，`/api/state` 的 `key` 报最后一次）：小键盘 `+`(0x6B) = `/api/ocr/once`；小键盘 `-`(0x6D) = `/api/region/pick`；`F7` = 切 `ocr_on`；`F8` = 切 `in_on`；`F9` = 清屏；`F5`/`F6` 与小键盘 `-`/`+` 同功能。实现是 `RegisterHotKey`（MOD_NOREPEAT）优先 + `GetAsyncKeyState` 粘滞位轮询兜底，注册失败的键自动落到轮询。

**语向字段**：A 链 = `src_lang`→`dst_lang`；B 链 = `in_lang`（对方说的语言）→`sub_lang`（默认 zh）；C 链 = `ocr_src`→`zh`。三者互不覆盖：把 `dst_lang` 设成 `zh` 只影响 A 链，B 链照样翻译。

**`dst_lang` 允许 `ja/en/ko/zh`**：选 `zh` 就是「中文模式」——源语与目标语相同（或 `auto` 模式下原文本来就是中文）时 MT 直通（`same_lang: true`，0 ms，不过模型），TTS 仍照常念。

**视觉抄字的落点规则**：det 继续本地（25–40 ms，与语种无关），只把「认字」换成模型。模型回的行数与本地行数相等 → 逐行替换并保留原框；与 det 框数相等 → 按框给；都不等 → 并成一条、框用并集。默认**不改动行集合**（`ocr_label_strip=false`）。

**热键与状态**：`F5`/`F6`/`F7` 是全局热键，用 `GetAsyncKeyState` 的粘滞位（bit0）判按下，因此 40 ms 的轻点也不会漏；`F6` 通过 `_ensure_ocr()`（`OcrChain.start(snap_only=True)`）临时建链，**不受 `ocr_on` 影响**。每次按下都会更新 `/api/state` 的 `key`：`{"name":"F6","ts":1794981591.9,"n":1}`。

实测（真机 1050×450 区域）：日/英/中单帧 190–350 ms 识别 + ~300 ms 翻译（480–580 ms 到中文浮现）；韩文 `안녕! 오늘 같이 게임할래? 나 지금 접속했어` → 视觉抄字 891 ms → 单帧合计 1.19 s → 「你好!今天一起玩游戏吗?我现在已经上线了。」；多行并行翻译（≤4 线程）会把 2–5 行的总耗时压到接近单行。

## 1.3 段字段修正
- `e2e_ms` = **句尾 → 第一声落地**（A 链）或句尾 → 字幕上屏（B/C）。**可以是负数**：负数表示我还没说完，日语已经开口了（真同传）。
- `open_ms` = **开口（起话）→ 第一声落地**，含我说话时长，网页/报表里用来解释「为什么 e2e 很小但体感还是有一句的间隙」。
- `state` 增加了 `asr`（收尾识别改写字幕）与 `refined`（云端校正后的字幕）两个中间态。
- `dst` 只增不减：字幕永远以**整句重译**为准，碎翻出来的中间译文只用于「先出声」。
