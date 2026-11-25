# 前端 A/B 交接（新会话第一句就用这个）

## 一句话任务

派一个子智能体，用 **ccc / claude-opus-5.5** 写「首次运行向导」第三版，输出到 `vrdub/web/wizard-opus.html`，然后渲染截图跟已有两版并排比。

## 为什么要开新会话

子代理的模型授权是**会话级**的，只在 `agent/created` 且 `firstLiveSeq === 0` 时写一次
（`@deepseek-ai/dsh-tool-subagent/lib/model-selection-settings.js` 的 `[Service.init]`）。
老会话永远拿不到 → 重开一个新对话即可，配置本身已经是对的
（`C:\Users\Lenovo\.dsh\profiles\desktop\cordis.patch.yml` 第 1142 行起 `enabled: true`，allowedModels 含 `ccc/claude-opus-5.5`）。

## 项目上下文（三行）

- 项目根 `D:\vrc无言势`：VRChat 实时同传（A 链我说话→日语出声 / B 链对方说话→中文字幕 / C 链对方打字→OCR）。
- 后端 Python（`vrdub/`），控制台 `http://127.0.0.1:8899/`，前端 `vrdub/web/{index.html,app.js,style.css}`。
- 正在做软件化 P0，向导页是 P2 的第一块；两版原型已就位，只缺「更强前端模型」的对照。

## 已有的两版（基线，别改它们）

| 文件 | 模型 | 字节/行 | 交互元件 | 复用变量 | 特点 |
|---|---|---|---|---|---|
| `vrdub/web/wizard.html` | 本机模型（继承父路由） | 37167 / 836 | 24 | 16 | 信息密度高：驱动/采样/缓冲栅格、右侧说明栏、底部提示条、纯 CSS 四步无 JS |
| `vrdub/web/wizard-codex.html` | codex gpt-6.2-sol | 23990 / 416 | 10 | 19 | 排版克制留白好，但每步只有一个控件，信息少 |

## 派发参数（照抄）

```
subagent({ name: "opus-wizard", cwd: "D:\\vrc无言势",
  provider: "ccc", model: "claude-opus-5.5",   // 点号；若报 not allowed 先试 ccc/claude-opus-5-5
  description: "向导第三版（opus）", prompt: <下面的 brief> })
```

## Brief（原样给它，只改输出文件名）

```
项目根 D:\vrc无言势（VRChat 实时同传工具：Python 后端 + 纯 HTML/CSS/JS 前端，控制台 http://127.0.0.1:8899/）。
任务：新建一个文件 D:\vrc无言势\vrdub\web\wizard-opus.html —— 「首次运行向导」静态原型（纯 HTML+CSS，不接后端、不发网络请求，不需要 JS 逻辑）。
先读 vrdub/web/style.css 与 vrdub/web/index.html，沿用同一套设计语言（配色变量、圆角、卡片渐变/阴影、按钮、select、滑块、开关、电平条），不要另起风格。
要求：单文件零外部依赖（无 CDN/字体/框架）；四步（①选麦克风带实时电平条动效 ②选输出设备默认 CABLE Input ③填 API key ④说一句试听，含「试听」按钮与「延迟/首包」两个数字位）；左步骤条+右内容区+底部上一步/下一步；1024x640 无滚动；全中文、文案要像产品。
业务口径必须正确：服务商=阿里云百炼 DashScope 兼容模式；音色=t3 克隆音色（默认）t1/t2/t4；TTS=云端 tts-plus；本地引擎只有 SenseVoice(ASR) 与 PP-OCR；配置路径=%LOCALAPPDATA%\VRDub\config\settings.json。
只新建这一个文件，不要修改/删除任何已有文件，不要启动后端，不要跑 pip。完成后回：文件路径、字节数、用到的设计变量清单。
```

## 验收四项（只看这四条，不看感觉）

1. 设计变量与 `style.css` 同源（`var(--…)` 计数 ≥16，不另造色板）；
2. 1024×640 四步零溢出、零滚动；
3. 零外部依赖：`<script>`=0、`<link>`=0、`http(s)://`=0；
4. 业务口径 8 项全中：阿里云百炼 / DashScope / tts-plus / %LOCALAPPDATA% / t3 / SenseVoice / PP-OCR / CABLE Input。

## 出图命令（PowerShell，Edge 无头）

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1024,640 --screenshot="D:\vrc无言势\out\wizard_opus.png" "file:///D:/vrc%E6%97%A0%E8%A8%80%E5%8A%BF/vrdub/web/wizard-opus.html"
```

切到第 4 步看试听后态：复制一份临时 html，把 `id="s4"` 换成 `id="s4" checked`、`id="tried"` 换成 `id="tried" checked`，再渲染。
三张图（`out/wizard.png` / `out/wizard_codex.png` / `out/wizard_opus.png`）并排给用户。

## 如果新会话里 ccc 仍然 not allowed

- 说明宿主进程没吃到 profile 补丁：确认 `C:\Users\Lenovo\.dsh\.credentials.yaml` 里有 `CCC_API_KEY`（有），然后彻底退出 DSH 桌面端（不是关窗口）再启动。
- 或者先测通道：`list_subagent_models({ provider: "ccc" })`，返回模型清单才算通。