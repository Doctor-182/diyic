# 给接手的人（备忘）

## 三条命令

```bat
检查环境.bat      :: 依赖 / 设备 / 云端四件套 / 本地模型 → out\selftest.md
评测.bat          :: 8 句真素材跑延迟表 → out\bench.md
python -m vrdub.app --force   :: 忽略单实例锁，前台起后端（调试用，能看 traceback）
```

单测式的小脚本都在 `tests/`：`test_language_gate.py`（语言闸）、`test_mt_quality.py`（翻译质量）、
`mon_player_check.py`（监听回路）、`cable_check.py`（虚拟声卡通路）、
`procloop_check.py`（进程回环）、`ocr_probe.py`（C 链单帧）。

## 改代码的几条硬规矩

1. **只用标准库做网络**：`http.client` + `http.server`。这台机器上没有
   requests/flask/aiohttp，加了就跑不起来。
2. **日志中文**，格式 `[模块] 内容`，带级别的用 `util.log(msg, "warn"|"err", "tag")`。
   日志同时写 `out/srv.log`（run.bat 起的进程控制台是最小化的，看 stdout 等于没有）。
3. **改任何 `.py` 都要重启后端**（8899 单实例）。
   任务管理器结束 `python.exe`（`vrdub` 相关那几个）再双击 `run.bat`。
4. **正确性开关不进 `settings.json`**：见 `cfg.PERSIST_SKIP`。
5. **抓屏相关的窗口必须 `util.hide_from_capture(widget)`**，否则会自反馈。
6. **改 `chains.py` 先读 `docs/ARCHITECTURE.md` 的「数据契约」**：e2e_ms 可负、
   字幕只增不减、chatbox 只发一次。
7. 音频一律 int16 / 16 kHz / 单声道；浮点转 int16 要 `* 32767` 再 `astype`。

## 提交到 GitHub

```bat
git init
git add .
git commit -m "feat: VRChat 实时同传（A/B/C 三链）"
git branch -M main
git remote add origin https://github.com/<你>/<仓库>.git
git push -u origin main
```

`.gitignore` 已经排掉 `out/`、`dist/`、`models/**/*.onnx`、`data/settings.json`、
`__pycache__`。模型两个文件（238 MB）超出 GitHub 单文件 100 MB 限制，
放 Release 附件或网盘，别硬塞。

> `.env.local` 里是**真实可用的 key**（作者同意随包分发，后续会去后台禁用）。
> 要公开仓库的话，建议先换成 `.env.example` 再推。

## 代码风格

- Python 3.10+，类型注解能写就写，但不强求；`from __future__ import annotations` 已用。
- 每 100 行左右一个 `# ---- 小节 ----` 分隔，函数 docstring 写「为什么」而不是「是什么」。
- 前端是零构建的原生 JS（`vrdub/web`），改完刷新页面即可，没有 npm。
