# 从零把这套跑起来（换机器 / 给朋友）

> 目标：`run.bat` 双击起来，浏览器自动打开 http://127.0.0.1:8899/ ，
> VRChat 里对方听到日语、自己看到中文字幕。

## 0. 你需要

- Windows 10 2004+ / Windows 11（进程回环用的是 Win10 2004 才有的
  `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`）
- Python **3.10–3.12**（开发机是 3.12.14）
- VRChat（要听对方说话、要让对方听见你）
- 一根虚拟声卡：**VB-Audio Virtual Cable**（免费）或 Voicemeeter
- 一个阿里云百炼「Token 套餐」key（`.env.local` 里那三行）

## 1. 装依赖

```bat
pip install -r requirements.txt
```

如果这台机器上已经有别的 python 环境（比如本项目开发机用的
装进项目下的 `.pylibs`），`scripts\_env.bat` 会自动把 `.pylibs` 与程序目录拼进 `PYTHONPATH`。
解释器也不用自己指定：`_env.bat` 按 `VRDUB_PYTHON` → `.runtime\python.exe` → `py -3.12` → `py -3` → `python` 依次找。

## 2. 放模型

见 [`models/README.md`](../models/README.md)。合计约 238 MB。

## 3. 填 key

`.env.local`（本包已带一份能用 30 天的测试 key，公开发布前记得去后台禁用）：

```
DASHSCOPE_TOKENPLAN_KEY=sk-xxxx
DASHSCOPE_TOKENPLAN_BASE=https://token-plan.cn-beijing.maas.aliyuncs.com
```

## 4. 接声卡（关键，一步错就听不见）

1. 装 VB-Audio Virtual Cable，重启。
2. **VRChat → Settings → Audio**
   - `Microphone` = **CABLE Output (VB-Audio Virtual Cable)**
   → 对方的耳朵来自这根线
   - `Output` = 你的耳机（想听环境音/音乐就保持耳机；不用改）
3. 控制台 → 设备卡
   - 我的麦克风 = 你真正说话的那只（Realtek / Rapoo / 阵列都行）
   - 合成音输出 = **CABLE Input**
   - 我的监听 = 你的耳机（同一只；这里出的声音只有你自己听得到）
4. 点顶栏「开始」。日志应出现四行：

```
播放开 #19 CABLE Input
监听开 #18 扬声器 (Rapoo Gaming Headset)
采集开 mic #23 麦克风 (Rapoo Gaming Headset)
B 链只听 VRChat 进程 pid=xxxx（麦克风、系统音、自己的 TTS 都不会进 B 链）
```

## 5. 自检 & 评测

```bat
检查环境.bat     :: 依赖、设备、云端四件套（ASR/MT/TTS）、本地模型 → out\selftest.md
评测.bat         :: 跑 8 句真素材，出 out\bench.md（延迟 p50/p90 表）
```

## 6. 三条链分别怎么验

| 链 | 验法 | 期望 |
|---|---|---|
| A 你说 → 对方听日语 | 对着麦克风说一句中文，看日志 `[A]` 段 + 耳机里听到日语 | 说完 ~0.5–1.5 s 开口 |
| B 对方说 → 你看中文字幕 | 让朋友说日语，或用 `tools\api.py inject "日本語" ja` 模拟 | 字幕 0.3–0.9 s，且**不进** chatbox |
| C 屏幕文字 → 中文浮在原处 | 小键盘 `-` 框选聊天框 → 小键盘 `+` 识别一帧 | 0.4–1.2 s 浮出中文，3.5 s 后淡出 |

## 7. 常见坑

| 现象 | 原因 |
|---|---|
| 控制台打不开 | 后端崩了，看 `out\srv.log` 尾部；`run.bat` 会在 2 s 后自动重启 |
| 对方听不到 | VRChat 麦克风没选 CABLE Output；或 Windows 隐私设置禁止桌面应用用麦克风 |
| 自己听不到合成音 | 设备卡「监听开关」关着，或监听设备选成了 CABLE |
| B 链永远「太安静弃段」 | 回环源没选对：`loop_src=process` 要 VRChat 在跑；`device` 模式要选到对的回环设备 |
| C 链一直不出 | 还没框选范围（控制台会写「未框选区域 · 已暂停」），按小键盘 `-` 框一下 |
| 热键按了没反应 | 看日志有没有 `[key] 按下 …`；没有就是被别的软件占了，换 F5/F6 |

## 8. 关掉它

- 控制台顶栏「停止」= 停三条链（麦克风/回环保留）
- 右下角浮窗右键 → 退出；或任务管理器结束 `python.exe`
- 后端是单实例（`out/vrdub.lock`），重复双击 `run.bat` 只会把浏览器再打开一次
