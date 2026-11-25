# 架构地图（给要改代码的人）

一句话：**一个后端进程 + 四个 Qt 子进程 + 一个网页**，中间靠 HTTP/SSE 和 stdin 行 JSON 连起来。

```
                    ┌──────────────── 浏览器控制台（vrdub/web）────────────────┐
                    │ 状态灯 / p50 / 三链卡片 / 设备 / 音色 / SSE 日志        │
                    └───────────────▲───────────────────────┬─────────────────┘
                            /api/state│SSE /api/events        │POST /api/cfg|inject|ocr/once|…
                    ┌───────────────┴───────────────────────▼─────────────────┐
   麦克风 #23 ─────►│                     vrdub/app.py（单进程）              │
   回环/进程 ──────►│  App: 设备、配置、热键、看门狗、fanout、四个子进程管理  │
                    └──┬────────┬────────┬────────┬────────┬────────────────-┘
                       │        │        │        │        │
              chains.StreamChain ×2   bus（内存总线）  osc（chatbox）  metrics
                 （A 链 / B 链）          │
                       │                 ├──► hud 子进程（右下浮窗）
          vad → asr → mt → tts → audio    ├──► danmaku 子进程（弹幕）
                       │                 └──► ocr_overlay 子进程（C 链原位浮现）
                 ocr.OcrChain（C 链，可选）────────────────────────────┘
```

## 模块表

| 文件 | 干什么 | 改之前要知道 |
|---|---|---|
| `vrdub/app.py` | 总装：设备枚举、apply(cfg)、热键、日志、看门狗、子进程、`/api/*` 转发给 server | 所有跨模块接线都在这；改配置字段要同步 `cfg.DEFAULTS` |
| `vrdub/server.py` | `http.server.ThreadingHTTPServer` + 路由表 + SSE | 只用标准库；UTF-8 必须显式 |
| `vrdub/cfg.py` | 默认值、校验、`%LOCALAPPDATA%\VRDub\config\settings.json` 持久化、`PERSIST_SKIP` | **正确性开关不许落盘**（out_on/in_on/echo_guard/门限） |
| `vrdub/chains.py` | 一条链的全部状态机：断句、投机识别、翻译、排队、TTS、收尾重译、回声/漏音闸 | 最复杂的一块；`e2e_ms`/`open_ms` 语义在这定义 |
| `vrdub/vad.py` | 48k→16k、10 ms hop、门限迟滞、投机/收尾分段 | 输出**一律 int16 16 kHz** |
| `vrdub/asr.py` | 本地 SenseVoice（onnx）+ 云端 qwen-audio-asr，hybrid 融合 | `MIN_PEAK`/`MIN_SEC` 挡非语音；400 不算错误 |
| `vrdub/mt.py` | MT：非流式整句 + 流式子句；回声重试、语言闸、强约束重翻、同语言直通 | 目标语不对宁可不出声，别把中文当日语念 |
| `vrdub/tts.py` | qwen-audio-tts-plus，SSE 收 PCM 24k | 首包 ~750 ms 是 API 地板，与文本长度无关 |
| `vrdub/net.py` | 「借出即独占」连接池（每请求一条连接，LIFO，空闲 30 s 换新） | 流式必须独占，否则串包 |
| `vrdub/audio.py` | Player（主路回调 + 监听常开流）、Capture、Through、resample、电平 | **WASAPI 回调会给 out_data=None**，必须自己返回缓冲（踩过，见注释） |
| `vrdub/procloop.py` | 进程级回环：只采 VRChat.exe 的声音（纯 ctypes + COM） | 踩坑清单在 `docs/PROCLOOP.md`，改之前必读 |
| `vrdub/ocr.py` + `_ocr_engine.py` + `_ocr_capture.py` | C 链：抓屏（BitBlt→PIL 兜底）、检测+识别、视觉抄字兜底、单帧/轮询 | 抓屏有两条路；ROI 是**物理像素** |
| `vrdub/vlocr.py` | 视觉抄字（把裁图直接丢多模态模型） | 只在本地念不出来 / 覆盖不全时用 |
| `vrdub/hotkey.py` | 热键：RegisterHotKey 优先 + 粘滞位轮询兜底 | 非活动桌面下 GetAsyncKeyState 全 0 |
| `vrdub/hud.py` / `danmaku.py` / `ocr_overlay.py` / `region_picker.py` | 四个独立 Qt 进程，靠 stdin 行 JSON 驱动 | 都调了 `util.hide_from_capture`，否则会被自己抓屏再 OCR 一遍 |
| `vrdub/bus.py` / `metrics.py` / `util.py` / `persona.py` | 内存总线、延迟统计、工具（文本清洗/语言判定/重采样）、人设卡 | `util.lang_ok` 是所有语言闸的地基 |

## 线程模型（一个进程里）

```
主线程        server.serve_forever
tick          100 ms：ping 子进程、看门狗、重连回环
mic/loop      PortAudio 回调 → VAD（每 10 ms 一块）→ chains
proc-loop     进程回环事件线程 → queue → _proc_writer → VAD
链线程        A/B 各一条；翻译与 TTS 各自在队列里串行
refine        Semaphore(2) 限流的云端校正
mem-watch     2 s 一次 RSS，超软线卸本地模型，超硬线自杀交给 run.bat
hotkey        消息循环 + 20 ms 轮询
```

## 数据契约（改代码时别破坏）

1. **音频**：`vad`、`asr` 之间是 `np.int16`、单声道、16 kHz、-32768..32767。
   浮点必须在 `vad._emit` 里乘 32767 转掉（踩过：直接 astype 会全变 0）。
2. **`e2e_ms`**：段尾 → 第一声落地，**可以为负**（说完之前就开口 = 真同传）。
   `open_ms`：起话 → 第一声落地（含说话时长）。统计时 `pct(keep_neg=True)`。
3. **字幕只增不减**：`dst_acc` 累加每片译文；收尾整句重译后只补尾巴（LCP 对齐）。
4. **chatbox 只在 A 链真出声后发一次整句**；B 链 `self.osc = None`，永远不进。
5. **门限自标定**：每次开麦/开回环头 0.9 s 采本底，`thr = floor + margin`，不落盘。
6. **回声判定只看声学证据**：`Player` 维护 15 s / 8 kHz 的**连续**环形缓冲（`_hist_buf` + 累计采样数 + 平滑时钟锚点，绝不用回调时间戳摆放），`hist_window(t0,t1)` 取窗后与麦克风段做 `util.xcorr_norm`（FFT 归一化互相关，负时移必须支持，零能量位置要打掉）。判据：相关 ≥0.15 **且** 能量差 <+6 dB。文本相似度**不再**参与回声判定（旧版 >0.6 就把用户的复述吃掉）。
   `leak_gate` / `bleed_lvl` 这两道电平/包络闸默认 False：B 链比麦克风响时它们必然触发，实测一天误杀 40+ 段。
7. **语向字段别搞混**：
   - A 链 `src_lang = cfg.src_lang`（auto）、`dst_lang = cfg.dst_lang`（ja/en/ko/zh）
   - B 链 `src_lang = cfg.in_lang`（**对方说的语言**，auto）、`dst_lang = cfg.sub_lang`（zh）
   - C 链 `src_lang = cfg.ocr_src`、`dst_lang = zh`
   B 链**曾经**把 `sub_lang`（=zh）当成源语，导致「同语言直通」一上线整条 B 链不翻译；
   现在 `mt` 的直通判据也要求 `util.lang_ok(text, dst)` 为真，语向配错最多慢一点，不会再静默不译。

8. **设备按名字绑定，不认号码**：Windows/PortAudio 每次进程启动的 WASAPI 枚举顺序不固定，实测同一台机器 #19 从「CABLE Input」变成「扬声器 (Rapoo)」——**号码还活着、通道数也对，`resolve()` 拦不住**，结果日语放给了自己听、对方一句没收到，B 链还从阵列麦取声。
   `cfg.dev_names = {in,out,mon,loop}` 存名字，启动时 `app._bind()` → `audio.resolve_named()`：先 `find_by_name`（全等 → 去括号前缀包含 → 多命中取名最短），找不到才退回号码，然后**把解析结果和名字都回写**。
   坑：`find_by_name` 的 kind 是 `in/out/loop`，而 `list_devices()` 的桶叫 `input/output/loop`——不映射就永远查到空列表、静默退回旧号码（`audio._bucket` 专治这个）；`loop` 还要同时搜 `input` 桶，因为 VB-Cable 的录音端叫 `CABLE Output`，不在回环桶里。
   `_coerce` 的类型基准是 `DEFAULTS[k]`，不是当前值：否则 `dev_names` 一旦被旧版存成字符串 `"{'out': ...}"`，就永远判成 str、写 dict 又被打回字符串，自愈不了。
9. **精修字段独立**：`refine_ms` 是第二遍听的耗时，`engine` 记精修用的引擎（cloud/whisper），**绝不覆盖 `asr_ms`**（快路出字耗时）；文本变了写 `state="refined"` 与 `note="旧字数→新字数字"`。精修只改字幕/译文，不动已排队的 TTS。
   跳过条件：`asr_refine=off`、主引擎本来就是 cloud、音频 <0.9 s（实测云端对「喂」要 9.1–9.5 s）、单飞槽被占。
10. **引擎不写死在代码里，写进注册表**：`vrdub/asr_engines.py` 的 `SPECS` 每条声明 `langs / size_mb / vram_mb / ram_mb / cpu_min / lat / quality / offline`；`probe()` 读机器条件（显存空闲走 `nvidia-smi`、内存走 `GlobalMemoryStatusEx`、核数 `os.cpu_count`，缓存 30 s）；`pick(lang, prefer)` 先按语种筛、再按装不装得下筛、最后按「实测质量优先、延迟次之」排序。覆盖顺序：`cfg.asr_engine_map[lang]` > `cfg.asr_local_engine` > 自动。
    **三条硬约束**：① 注册表模块不 import 任何模型库（没有依赖的机器也能算出该用谁）；② 只有实测过才写 `quality`，没测写 `None`，不许猜；③ 自动选档永远可被手写覆盖，且覆盖失败要说明原因再回落，不许静默改人的决定。
11. **质量数字必须来自真话**：`out/clips/*.wav` 是 TTS 合成素材，用它比相似度所有引擎都是 1.00（测的是「会不会读清晰合成音」）。真判据只能来自 `cfg.asr_capture=true` 落的 `out/corpus/<日期>/<时间>_<lang>_<chain>.{wav,json}`（真人真麦真房间），用 `tools/asr_ab.py` A/B。任何「准确率提升」的结论如果没有 corpus 数据支撑，只能写成「待验证」。

## 延迟预算（实测，2026-11）

| 环节 | p50 | 说明 |
|---|---|---|
| 采集+分段 | 0.50 s | `end_silence`，可调到 0.3（会切碎句子） |
| 本地识别 | 0.08–0.35 s | B 链默认 hybrid，p50 79 ms（实测 5 条日语素材与云端文本相似 1.00） |
| 云端识别 | 0.34–13.2 s | 只做校正，不阻塞字幕；抖动极大，别当主引擎 |
| 翻译首包 | 0.28–0.45 s | 流式子句 |
| TTS 首包 | 0.72–1.20 s | **API 地板 ~750 ms** |
| **A 链（说完→开口）** | **0.46–1.38 s** | 8 次评测区间 |
| **B 链（对方说完→字幕）** | **0.22–0.44 s** | 无 TTS；改 hybrid 后实测 219 ms / p90 311 ms |
| 精修（后台第二遍） | 云端 1.4–9.5 s · whisper 0.31–0.35 s | **不进上面的数**：只刷字幕/译文，不阻塞出声 |

## 已知限制（欢迎接手）

- **本地 whisper 要显存**：`asr_refine=whisper` 时 anime-whisper 常驻 1.1 GB（8 GB 卡与 VRChat 同跑实测仍剩 3.5 GB 空闲）；首次推理 13.9 s 是建 CUDA 上下文，已用静音热身挡在前台之外。常驻策略是 `auto` 下不载、只在云端失守时临时拉起来，`whisper_idle_s` 到点释放。
- **本地模型关不掉**：`asr_engine=hybrid` 时本地 SenseVoice 一旦载入就常驻；
  想彻底不占内存只能把 `asr_engine_a/b` 设成 `cloud` 或重启。OCR 的 det/rec
  同理（`ocr_on=false` 只是停轮询，模型仍在内存里）。→ 待做：`unload()` 接到开关上。
- **合成长文本会排长**：`speak_q_max=1` 会主动跳片，长句靠收尾整句补。
- **热键依赖活动桌面**：锁屏 / UAC 安全桌面 / 独占全屏时收不到（RegisterHotKey 也一样）。
- **C 链常驻模式会读桌面上所有文字**（框小一点更干净）。
- **多显示器**：框选器只取主屏几何，跨屏选不了。
- **单实例**：`out/vrdub.lock` 用 `msvcrt.locking`，删掉锁文件才能起第二个。
