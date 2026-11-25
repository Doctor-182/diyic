# models/ —— 本地模型权重（不进 git）

代码靠这三份文件在本地跑识别；onnx 太大（合计约 238 MB），
所以仓库里只有配置和下载脚本，权重单独打包成 `vrc-dub-models-*.zip`，
或者按下面自己下。

## 需要什么

| 文件 | 大小 | 用途 |
|---|---|---|
| `models/sensevoice/model.int8.onnx` | ~228 MB | 本地语音识别（SenseVoice-Small int8） |
| `models/sensevoice/tokens.txt` | ~300 KB | 上面那个模型的词表 |
| `models/ocr/japan_rec.onnx` | ~9.3 MB | OCR 日文识别（PP-OCRv4 mobile） |
| `models/ocr/japan_dict.txt` | ~17 KB | 日文字典 |
| `models/ocr/config_ch.yaml` / `config_ja.yaml` | 1 KB | 中日两套 OCR 参数（已在本仓库里） |

OCR 的**检测**模型和**中英识别**模型不用单独下——`rapidocr-onnxruntime`
包里自带，`vrdub/_ocr_engine.py` 直接按包内路径引用。

## 怎么下

```bat
:: 1) OCR 日文模型（9.3 MB，走 ModelScope，脚本自带）
python models\ocr\fetch_models.py

:: 2) SenseVoice（228 MB，sherpa-onnx 导出版本）
::    HuggingFace 原仓库：
::    https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
::    需要两个文件：model.int8.onnx、tokens.txt
::    国内可换镜像 hf-mirror.com（把域名换掉，路径不变）
```

放好后目录长这样：

```
models/
  sensevoice/
    model.int8.onnx
    tokens.txt
  ocr/
    japan_rec.onnx
    japan_dict.txt
    config_ch.yaml
    config_ja.yaml
    fetch_models.py
```


## 可选：日语精修模型 anime-whisper（GPU）

`asr_refine=whisper`（或 `auto` 且云端失守）时才会载入，用来「定稿后后台再听一遍」。

| 文件 | 大小 | 说明 |
|---|---|---|
| `models/anime-whisper-ct2-int8/model.bin` | ~732 MB | quantumcookie/anime-whisper-ct2-int8（CTranslate2 int8 转换版） |
| 同目录 `tokenizer.json` / `vocabulary.json` / `normalizer.json` | ~2.5 MB | 与模型配套的分词/归一化 |

运行条件（实测，这台机器 8 GB 卡 + VRChat 同跑）：

- 依赖装在 `_pylibs`（faster-whisper 1.2.1 / ctranslate2 4.9.0 / av / tokenizers）与 `_pylibs_cuda`（nvidia-cublas-cu12 12.9 + nvidia-cudnn-cu12 9.28 + cuda-nvrtc，共 1.3 GB），代码里由 `vrdub/asr_whisper.py` 的 `_ensure_path()` 加 `os.add_dll_directory` 挂上。
- **必须走 GPU**：同一段 4 s 日语在 CPU 上 10.5–26.5 s（ct2 int8 在这颗 i7-14650HX 上有效算力只有 ~55 GFLOPS，8 线程也一样），显卡上稳态 0.31–0.35 s。
- 显存约 1.1 GB；`whisper_vram_min_mb=1300` 低于这个数会直接只排 CPU（等于不可用），载入成功后先用 1 s 静音热身，把首次 13.9 s 的 CUDA 上下文建立挡在用户第一句话之前。
- 缺文件不会崩：`asr_refine=auto` 退回云端，`whisper` 模式记一条 warn 后按 `off` 处理。

下载：

```bat
:: 走 HF 镜像（官方 snapshot_download 在 xet 通道会卡 0 B）
python tools\_hf_get.py quantumcookie/anime-whisper-ct2-int8 models\anime-whisper-ct2-int8
```

## 没有模型会怎样

不会崩：`vrdub/asr.py` 里本地识别是懒加载，缺文件时记一条 warn，
自动只用云端识别（多 0.3–0.8 s 延迟）；OCR 没有日文模型时，
C 链的日文会退到「视觉抄字」（把图直接丢给多模态模型，慢 0.6–1.0 s）。
