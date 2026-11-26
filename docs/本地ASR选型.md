# 本地 ASR 选型（2026-11-26 调研）

约束：**显存 ≤ 2 GB**（本机 RTX 5060 Laptop 8 GB 总，实测 VRChat+桌面常占 1.5–3 GB），
延迟要能进 A 链（说话→出声 ≤ 1 s，其中 TTS 首包地板 ≈750 ms）；
质量要能报数（与云端 qwen-audio-3.0-asr-flash 的文本相似度 / 公开 WER）。

## 一、候选总表（体积与运行时都是实测查到的，不是抄介绍）

| 模型 | 参数 | 磁盘≈ | 运行时 | 显存≈ | CPU 独自跑 | 语言 | 质量证据 |
|---|---|---|---|---|---|---|---|
| **SenseVoice-Small int8**（现状） | 234M | 228 MB | onnxruntime（已在用） | 0 | ✅ 80–350 ms/句 | zh ja en yue | 与本机云端文本相似度 1.00（ja 5 条） |
| **Qwen3-ASR-0.6B** | 0.6B | 767 MB + 204 MB 音频塔 | llama.cpp（GGUF，ggml-org 官方） | ≈1.1 GB | ⚠️ AR 解码，1–3 s/句 | 30 语 + 22 中文方言 | WenetSpeech net 4.97 / Fleurs 多语均 7.57 |
| **Qwen3-ASR-1.7B** | 1.7B | Q8 2065+339 MB；自量化 Q5_K_M ≈1.55 GB | llama.cpp | 1.6–2.4 GB | ⚠️ 2–5 s/句 | 同上 | **开源第一档**：WenetSpeech net/meeting 6.35/5.97→1.7B 更好、Fleurs 多语 4.90（胜 whisper-large-v3 的 5.27） |
| **Fun-ASR-Nano-2512** | 0.8B | 997 MB (q8) | audio.cpp / FunASR(torch) | ≈1.1 GB | ⚠️ 1–3 s/句 | zh(7 方言+26 口音) en ja | WenetSpeech net 4.66、meeting 4.84（比 Qwen0.6B 略好） |
| Fun-ASR-MLT-Nano | 0.8B | ≈1 GB | 同上 | 1.1 GB | ⚠️ | 31 语 | Fleurs 多语 10.03（明显弱于 Qwen1.7B） |
| **FireRedASR2-AED** | 1.1B | 4.41 GB fp32（ONNX 移植版同体积） | onnxruntime（要自己写 KV 解码循环）/ torch | int8 后 ≈1.2 GB | ❌ | zh en（**无日语**） | 中文 SOTA 档，但 B 链是日语 → 单独用不了 |
| GLM-ASR-Nano-2512 | 1.5B | 4.31 GB fp32 | torch / GGUF | int8 ≈1.6 GB | ❌ | 多语 | Fleurs 多语 16.08（弱） |
| VibeVoice-ASR-Streaming-1.5B | 1.5B | q4 2.02 GB / q8 3.19 GB | llama.cpp 分支 | 2.0–3.2 GB | ❌ | 多语 | 主打流式，体积卡线 |
| whisper-large-v3-turbo | 809M | CT2 int8 ≈800 MB | ctranslate2（已有 wheel） | 0.8 GB | ❌ RTF≈1.5–3 | 多语 | Fleurs 5.27 |
| anime-whisper CT2 int8（已有） | 155M | 736 MB | ctranslate2 | 1.1 GB | ❌ 10–26 s/句 | ja | 与本机云端相似度 0.968（不如 SenseVoice） |
| Paraformer-zh（sherpa-onnx int8） | 220M | 217 MB（+标点模型） | onnxruntime | 0 | ✅ RTF 0.03–0.08 | zh only | 中文老牌，同档 CER 与 SenseVoice 相仿 |
| TeleSpeech-ASR1.0 CTC int8（sherpa-onnx） | 0.9B | ≈1 GB | onnxruntime | 0 | ✅ CTC 非自回归，CPU 友好 | zh + 方言 | 中文方言强，无日语 |
| Dolphin-base / small | 0.4B / 1B | 535 MB / 1.42 GB | torch（sherpa-onnx 有 CTC 版） | 0–1 GB | ✅（CTC 版） | 多语 + 中文方言 | 社区常用，中文强于 whisper |
| Moonshine / Parakeet | — | 小 | onnxruntime | 0 | ✅ | **英文 only** | 不适用 |

## 二、CPU 能不能扛：分三类看

1. **非自回归 / CTC**（SenseVoice、Paraformer、Zipformer、TeleSpeech、Dolphin-CTC、Moonshine）
   → CPU 一波流，10–60 ms/秒音频，**完全够实时**。现状 SenseVoice 就是这一类（p50 79 ms）。
2. **编码器-解码器 AR**（Whisper、FireRedASR-AED）
   → CPU RTF 0.5–3，勉强能当「精修通道」，不能进 A 链。
3. **LLM 式 AR**（Qwen3-ASR、Fun-ASR、GLM-ASR、VibeVoice）
   → CPU 每句 1–4 s；24 核能跑但不实时。**只适合有 2–5 s 预算的通道**（B 链字幕、chatbox、定稿精修）。

## 三、两条落地方案

- **方案甲（推荐先试）**：双通道。SenseVoice 继续秒出字幕/出声草稿；Qwen3-ASR-**0.6B Q8**（971 MB，占显存 ≈1.1 GB）挂 llama.cpp 做「精修」——出字后用大模型重识别一遍，差异大就改字幕（chatbox 预算 5 s 正好吃得下）。
- **方案乙（质量优先）**：把 1.7B 的 BF16 下一次（3.88 GB），本地用 llama-quantize 量化到 **Q5_K_M ≈1.55 GB**，显存仍在 2 GB 内，质量是开源第一档（Fleurs 多语 4.90 > whisper-large-v3）。

## 四、下载预算（这次只拉最小验证集，共 ≈1.0 GB）

- llama.cpp Windows Vulkan 构建 37.3 MB（已下）
- Qwen3-ASR-0.6B Q8_0 767 MB + mmproj Q8 204 MB（hf-mirror 实测 9.6 MB/s，≈100 s）

以下都**未下**，等点头：Qwen3-ASR-1.7B BF16 3.88 GB、Fun-ASR-Nano q8 997 MB、VibeVoice-1.5B q4 2.0 GB、FireRedASR2 ONNX 4.41 GB、Dolphin 0.5–1.4 GB。

## 五、要拿数字说话的地方（下完就测）

同一批真实素材（out/clips zh0–zh4 / ja0–ja4，另加本次新录）跑三方对照：
1. 文本相似度：候选 vs 云端 qwen-audio-3.0-asr-flash（口径与现有 `quality` 字段一致）；
2. 延迟：句尾→出字 p50/p90（含 llama.cpp 冷/热）；
3. 显存：nvidia-smi 峰值；
4. 错误形态：看错字、漏句、语种串台（B 链是日语，串台比错字更致命）。

## 六、更正与补充（同日复核，2026-11-26 晚）

上表「Qwen3-ASR-1.7B：自量化 Q5_K_M」这句已过时——**Q5_K_M 有现成发布**，来源是另一个生态：

**transcribe.cpp**（`handy-computer/transcribe.cpp`，v0.5.0，2026-11-26，2211★）是 ggml 上的 STT 推理库，README 声明支持 16 个模型家族 / 60+ 变体，含 Qwen3-ASR、**Fun-ASR-Nano**、Moonshine（含 zh/ja 变体）、GigaAM、Canary、Granite Speech、Cohere Transcribe。

| 事实 | 数字 |
|---|---|
| Windows 运行时 | `transcribe-native-0.5.0-windows-x86_64-cpu-vulkan.tar.gz` **17.0 MB**；CUDA 版 248.3 MB |
| Python 绑定 | PyPI `transcribe_cpp` 0.5.0（requires-python ≥3.9，纯 wrapper 0.0 MB，原生库来自上面的 tar） |
| Qwen3-ASR-1.7B 各量化体积 | BF16 3893.9 / F16 3901.9 / Q8_0 2083.8 / Q6_K 1614.1 / **Q5_K_M 1447.0** / Q4_K_M 1258.7 MB（单文件含音频塔） |
| 量化代价（LibriSpeech clean WER） | bf16 1.62 / f16 1.62 / q8_0 1.62 / q6_k 1.65 / **q5_k_m 1.65** / q4_k_m 1.81 ⇒ Q5 基本无损，Q4 开始掉 |
| 语言分数（q8_0） | CER Fleurs **ja 5.29** / zh 7.14 / yue 6.13 |
| CPU / GPU 速度（作者实测 RTF） | Ryzen 4750U：cpu **2.47×**、vulkan **4.08×**；M4 Max：cpu 9.59×、metal 41.08× |
| 能力 | streaming=false、timestamps=none、lang_detect=true、translate=false |

结论：本机 24 核 CPU 按同量级推算约 5–8× 实时，1.7B **CPU 也跑得动**（3 s 语音约 0.4–0.6 s），Vulkan 再快 1.6 倍。所以「大模型只配做精修」这句话要改：1.7B 在 CPU 上做 B 链字幕/chatbox 完全够。

另外两个运行时（别混用，GGUF 互不通）：
- `llama.cpp` + `ggml-org/Qwen3-ASR-*`（官方，只发 BF16/Q8_0，需要含 PR #32571 的构建）
- `audio.cpp`（`0xShug0/audio.cpp`）+ `audio-cpp/*-GGUF`：Fun-ASR / VibeVoice / SenseVoice 等；注意它在 CUDA 上默认把 decoder 存 BF16（比 Q8 更吃显存）

