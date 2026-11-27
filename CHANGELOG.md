# 更新记录

格式：日期 · 版本 · 一句话。详细取舍见 docs/。

## 0.9.0-dev（开发中，2026-11 起）

首版可用的三条链，全部走云端套餐端点（识别 / 翻译 / 合成），本地只做兜底。

### 功能
- **A 链** 我说中文 → 对方听到日语，音色用 t3 克隆；说话中途就开口（投机识别 + 子句流式翻译）。
- **B 链** 对方说日语 → 我这边中文字幕；输入走 Windows 进程回环（只听 VRChat.exe），译文绝不回灌 chatbox。
- **C 链** 对方打字 → OCR 后中文浮在原气泡位置；本地 PP-OCRv4 双模型路由，认不出时用视觉模型兜底。
- **弹幕** 四种样式（卡片 / 纯描边 / 色条 / 发光）、大小与透明度可调，走独立浮窗进程。
- **闭麦** CapsLock 一键静音，浮窗右上角红字提示。
- 网页控制台（SSE 实时曲线 + 全部参数热改），无需构建步骤。

### 功能（音色与人设）
- **音色克隆**：控制台里录 10–20 秒样本 → 试听 → 克隆，10–40 秒拿到自己的日语声音；样本自动转 24 kHz 单声道并归一化，近乎静音直接拒（不花冤枉钱）。端点自适应：套餐端点没有复刻模型（404 Model not exist.）就记住并转按量端点。音色表可存 24 个、可删（本地 + 云端尽力）。
- **人设（口吻）**：三张内置卡 + 最多 1200 字自定义口吻，热生效；硬约束（全译 / 不增 / 不减 / 不省略 / 人称照原文 / 只出译文 / 禁标签）永远追加在末尾，且只作用于 A 链。
- 新增真值表 `tests/test_voices.py`（12 组，含端点记忆）、`tests/test_persona.py`。

### 功能（GGUF 本地大模型档）
- 两条链的「识别」下拉多了三个**离线档**：`qwen17`（Qwen3-ASR-1.7B Q5_K_M，1447 MB / 显存 1441 MiB）、`qwen06`（0.6B Q8_0，811 MB / 805 MiB）、`fun`（Fun-ASR-Nano-2512 Q8_0，850 MB / 844 MiB）。走 [transcribe.cpp](https://github.com/handy-computer/transcribe.cpp) 的 CUDA/Vulkan 后端，**自带标点、Qwen 系还能判语种**——SenseVoice 这两样都没有。
- 真机对照（16 段真人日语纯对话，对云端参考）：1.7B 与 SenseVoice 的 CER 同为 29.32%，但整句全对 **3 : 0**；0.6B 36.73% / 全对 2；Fun 37.04% / 全对 2 且会把日语听成中文。VRChat 里切 `qwen17` 后现场字幕 159.8–356.3 ms、无空段（`ありえない。ねえ、これねプヨプヨするね。`）。
- **换档独占**：`asr_gguf.Resident` 带锁串行，换档先卸载旧模型再载新的（日志「GGUF 切换：卸载 X → 载入 Y」）；两条链选了不同档只驻留 B 链那个并 warn。**聊天框继承 B 链**：B 链用 GGUF 档时聊天框直接用它自己的输出，不再花云端钱重听。GGUF 档自动跳过 refine（它自带标点，anime-whisper 0.968 只会擦掉好的）。
- 运维：`tools/fetch_asr_models.py --check/--download/--runtime/--all/--proxy`（HF + GitHub release，纯标准库）；`tools/build_exe.py --with-gguf` 才把运行库与模型装进安装包（默认不带，否则包从 264 MB 涨到 1.5 GB 以上）。
- 踩过的坑写进 README §2：CUDA 版 `ggml-cuda.dll` 用裸名 `LoadLibraryA` 找 cublas，**只认 PATH**；直接摊开的 wheel 没有 dist-info 就没有 entry point，必须显式设 `TRANSCRIBE_LIBRARY`。
- 新增真值表 `tests/test_gguf_engine.py`（8 组：换档卸载、同档不重载、缺文件、解码自重复、没出字回退 local(兜底)、B 链聊天框不重听、clamp 白名单）。

### 修正（语种漂移与文档）
- **语种漂移守卫**：GGUF 档（尤其 1.7B 唱歌/叠 BGM 时）会把日语吐成英文或罗马字（极端条件实测 10 段唱段里 6 段漂移），然后被当英文翻成英文腔中文。现在出字后先过 `util.lang_why`：假名/汉字/拉丁占比不达标 → 用 SenseVoice 重听一次，重听合格就用它（`engine` 记 `local(漂移)`），不合格保留原判（**绝不变空字幕**）。判据在 **194 段极端素材上拦下 107/117 段坏输出、误杀 0 段**（21 组阈值组合结果完全一致 ⇒ 阈值不敏感，本质是「假名≈0 且拉丁占多数」）。
- **`/sil` 清理**：Fun 会把静音标记当字吐出来（`/sil`、`<sil>`、`[sil]`），`asr_gguf.tidy()` 统一抹掉并压空格；`silly`/`すし` 这类正常词不误伤。
- 新增真值表 `tests/test_drift_guard.py`（7 组 18 项：英文漂移回退、正常日语不回退、两边都漂保留原文、韩文漂移、`lang=auto` 不判、中文链漂英文、tidy 边界）。
- **文档大清理**：删掉过期的与写给你个人的（`docs/UI.md` 手写说明书、`docs/SETUP.md`、`docs/软件化路线.md`、`docs/选型-小显存方案.md`、`docs/前端AB-交接.md`、`docs/proto/` 三份向导原型、`CONTRIBUTING.md`）。选型资料合并成 `docs/识别引擎.md`（三档 GGUF + 两批实测 + 守卫 + 换机器命令 + 七条坑，选型只信这份）；`docs/PROTOCOL.md` 从 14 条路由补到全部 22 条 + SSE 八种事件 + 段 schema；`docs/待修清单.md` 只留未完成项。
- **打包入口改名副实**：`scripts\打包.bat` 以前调的是「打源码 zip 发资料」的 `tools/pack.py`（还列着已删的文件），现在真跑 `tools/build_exe.py` + `tools/build_installer.py`；`tools/pack.py` 删除（发资料式分发已废）。
- **识别口径修正 + B 链主档改成 qwen06/qwen17**：01:00–01:20 的 KTV 直播窗口全量 194 段、四家模型全上。第一版脚本漏传 language 提示，1.7B 才整段吐英文（中位 CER 100%）；补传 language=ja 后同一批素材 1.7B 中位 36.4%、整句全对 34/154、英漂 97→2、p50 158 ms——**旧结论「1.7B 唱段灾难」是测量错误，已推翻**。生产口径总表：qwen06 中位 33.3% / 全对 31 / 空 2 / 125 ms / 805 MiB；qwen17 全对 34 / 轻声层 11/19 全对 / 1441 MiB；SenseVoice 45.5% / 0 英漂（继续做兜底）；fun 唱段中位 81%（四家最差）且 29 段英漂 ⇒ **不是「听歌特化」，只当低延迟兜底档**。真值表口径不变，运行态 asr_engine_b 已切 qwen06。
- **云端空返回的重试次数定死为 2**：153 段一次就出字，重试 5 次只多救回 1 段，40 段重试用尽仍空（纯伴奏/咳嗽）⇒ 第 3 次起只烧时间和额度。
### 功能（子密钥网关）
- **给别人用但不交出主密钥**：新增 `tools/relay.py` —— 主密钥留在自己机器上，对方拿 `sub-…` 子密钥打网关；校验、路径白名单、每日配额、记账、SSE 原样转发（首块不攒包）。端点支持 `http://host:port`（`vrdub/cfg.py` 的 `_host_of()` 与 `vrdub/net.py` 的 `_parse_host()`），控制台「密钥」卡可直接填网关地址。真值表 `tests/test_relay.py` 7 组 20 项全过；本机回环实测 chat 364 ms（直连 ~340 ms）、SSE 首块 292 ms。官方原生多 key（账号+地域最多 50 把、可设 IP/模型白名单）作为首选方案写进 `docs/子密钥网关.md`。

### 修正
- **控制台不再「一直重连」**：`tryState` 连上后端后会调 `loadKey()` 拉密钥状态，可它当时被写在 `bind()` 里（局部函数），从外面调是 `ReferenceError`；这个异常被 `fetch` 的 `.catch` 吃掉，被当成「后端连不上」→ 每 3 秒重试一次、每次重开一条事件通道，服务端日志里就是开-关成对地涨、界面永远停在「重连中…」。修法：`renderKey`/`loadKey` 提到模块作用域；`tryState` 拿到 `/api/state` 之后的界面初始化整段套 `try/catch`，出 bug 只报错、不再回落到「连不上」重试；另加 `window.onerror` / `unhandledrejection` 上报到 `/api/clientlog`。实测：新建无头浏览器挂 20 秒只有 1 次打开、0 次关闭（此前 3 秒一次）。
- **识别没出字不再假装 done**：云端整段没出字（线上实测 `ali HTTP 400: {}`，一次能连丢十几段）先用本地兜一次；本地也没字就把段落标 `drop` 并写明原因，不再以空 `src/dst` 的 `done` 静默收尾。真值表 `tests/test_empty_asr.py` 4 组全过。
- **开关关得干净了**：新增识别总闸 `asr_on` 与翻译总闸 `mt_on`（前端「总闸」行，热生效不落盘）；A 链在采集出口加硬闸，关掉即不采集不识别；`mode=pass`（直通）终于真的直通；改开关那一刻三层队列（识别 / 待播 / 播放缓冲）里的在途全部作废并留 `drop` 原因。真值表 `tests/test_gates.py` 7 组全过。

### 交付（打包成软件）
- **安装包**：PyInstaller onedir + Inno Setup，单文件 installer 264.5 MB，装完 591 MB，内置 252.7 MB 识别模型；朋友双击装完即可运行，不需要 Python / pip / PYTHONPATH。
- **密钥面板**：网页控制台底部填 key → 写 `%LOCALAPPDATA%\VRDub\config\.env.local` 并当场自检；exe 与仓库里永远没有 key。
- **子进程调度**：hud / 弹幕 / OCR 浮窗统一走 `VRDub.exe --child <名字>`，打包后不闪黑框、不依赖外部 python。

### 工程
- 配置与日志搬到 `%LOCALAPPDATA%\VRDub\`（config / logs / cache / out / recordings），程序目录只读也能跑；老配置首次启动自动迁移。
- 依赖与模型路径全部相对程序目录，不再引用任何本机绝对路径。
- 启动脚本统一走 `scripts\_env.bat` 探测解释器（VRDUB_PYTHON → .runtime → py -3.12 → python）。
- 日志 8 MB × 1 份自动轮转；单实例文件锁；内存看门狗（900 MB 放模型 / 1500 MB 自杀重启）。
