# 接口契约

控制台只跟本机后端说话：`http://127.0.0.1:8899/`（端口可用 `VRDUB_PORT` 覆盖，只绑 127.0.0.1）。所有 JSON 都是 UTF-8，失败一律 `{"ok":false,"err":"…"}`。

## 0. 名词

| 词 | 意思 |
|---|---|
| 链 | A＝我说话→对方听日语；B＝对方说话→中文字幕；C＝对方打字→屏幕 OCR→中文字幕 |
| 段（seg） | 一次「一段语音 / 一帧屏幕」的处理记录，见 §4 |
| 引擎 | `local`（SenseVoice）/ `cloud`（qwen-audio-3.0-asr-flash）/ `hybrid` / `qwen17` / `qwen06` / `fun` |

## 1. GET

| 路径 | 返回 | 用途 |
|---|---|---|
| `/`、`/static/*` | 网页控制台、app.js、style.css | 断网可用，无框架 |
| `/api/state` | 见下 | 前端主状态：cfg + metrics + 段表 + 设备 + 音色 + 人设 + hud/弹幕/回环 |
| `/api/devices` | `{ok, devices:{input:[…],output:[…],loop:[…]}}` | WASAPI 设备枚举（每项 `{idx,name,ch,sr}`） |
| `/api/metrics` | `{ok, A:{…}, B:{…}, C:{…}, rows:[…]}` | 分链统计 + 最近 50 段 |
| `/api/health` | `{ok, ts, sse, ver, ver_full, pid, asr:{refine, ja}}` | 存活探针；`sse` 是当前事件通道条数 |
| `/api/env` | `{ok, has_key, tail, host, path, probe}` | 密钥状态（`tail` 只给后 6 位，永不回明文） |
| `/api/voice_clone` | `{ok, job, voices, cur}` | 克隆任务进度 + 本地音色表 |
| `/api/events` | `text/event-stream` | 事件通道，见 §3 |
| `/api/ocr/crop` | PNG | 最近一次裁的屏幕图（调框选时看） |
| `/shot/<文件>` | PNG | 浮窗自截图（`/api/hud` 带 `shot` 时生成） |

`/api/state` 的骨架：

    {
      "ok": true, "ver": "0.9.0-dev", "ver_full": "…", "frozen": false,
      "cfg": { …全部配置… },
      "metrics": { "A": {…}, "B": {…}, "C": {…} },
      "last": [seg, …最近 50 段…],
      "devices": {…}, "voices": […], "voice_job": null,
      "persona": {"custom_len":0,"max":1200,"cards":[…]},
      "asr": {"engine":"hybrid","local":"ok 载入 1705ms","a":"hybrid","b":"hybrid",
              "pick":{"zh":"sensevoice","ja":"sensevoice"},"gguf":{…}},
      "loop": {"src":"process","proc":"VRChat.exe","pid":17860,"alive":true,"stat":{…}},
      "hud": {…}, "danmaku": {…}, "key": {}
    }

（`key` 里是最新热键，不含密钥。）

## 2. POST

body 一律是 JSON；**`/api/cfg` 的 body 就是 patch 本身**（`{"a_cloud_first": true}`），未知键会被丢弃。

| 路径 | 请求 | 返回 | 用途 |
|---|---|---|---|
| `/api/cfg` | 任意 cfg 键的 patch | `{ok, cfg}` | 热生效；只有 PERSIST_SKIP 里的键不落盘 |
| `/api/chains` | `{out_on,in_on,ocr_on,asr_on,mt_on}` | `{ok, …五项当前值}` | 链开关与两个总闸 |
| `/api/mic` | `{on:true}` | `{ok, mic_on}` | 开/闭麦 |
| `/api/env` | `{key, host}` | `{ok, has_key, tail, host, probe}` | 写 %LOCALAPPDATA%\VRDub\config\.env.local，热重载 + 1 token 自检 |
| `/api/tts_test` | `{text, voice, dev}` | `{ok, ms, first_pkt_ms, audio_s}` | 合成一句直接放出来 |
| `/api/inject` | `{text, lang, dev}` | `{ok, text, lang, audio_s, dev, first_pkt_ms, ms}` | 回环自测：自己合成、从 out_dev 播出去，对面那条线真的收到声音 |
| `/api/voice_sample` | `{action:"record"|"play", secs}` | `{ok, secs, sr, ch, peak, gain, rms_db, recorded, path, hint}` | 录音 / 试听克隆样本（默认 15 s；太安静当场提示） |
| `/api/voice_clone` | `{name, model}` | `{ok, job}` | 起克隆任务（上一个没跑完会拒） |
| `/api/voice_use` | `{id}` | `{ok, voice}` | 切音色（内置 t1–t4 + 自定义） |
| `/api/voice_del` | `{id}` | `{ok, voices}` | 删自定义音色（正在用的会回落 t3） |
| `/api/dk_preview` | `{dst, src, chain}` | `{ok, id}` | 弹幕试放一条假段，不合成不花钱 |
| `/api/ocr/region` | `{region:[x,y,w,h]}` | `{ok, region, crop}` | 设框 + 立刻裁一张 |
| `/api/ocr/once` | `{}` | `{ok, lines, …}` | 单帧识别（F6 / 按钮） |
| `/api/region/pick` | `{}` | `{ok, region}` | 起框选器（F2） |
| `/api/ocr/test` | `{}` | `{ok, …}` | OCR 引擎自检 |
| `/api/hud` | `{hud_on|hud_alpha|hud_scale|hud_rows|hud_click|hud_x|hud_y, shot}` | `{ok, hud}` | 浮窗参数 + 可选截图 |
| `/api/clear` | `{}` | `{ok}` | 清空段表与指标（日志不动） |
| `/api/clientlog` | `{msg, extra}` | `{ok}` | 浏览器把异常报进后端日志（`[web]` 前缀），排查一直重连靠它 |
| `/api/quit` | `{}` | `{ok}` | 优雅退出 |

## 3. SSE（`/api/events`）

必须用 **HTTP/1.1 chunked** 发（`Transfer-Encoding: chunked` + 每条 flush）。只写裸 body 又不给 `Content-Length`，浏览器判非法 → `EventSource` 报错 → 每 2 s 重连一次，页面永远停在“重连中”。

| k | 负载 | 频率 |
|---|---|---|
| `hello` | `{ts, pid}` | 连上时一次 |
| `hb` | `{ts}` | 15 s |
| `level` | `{mic, loop, gate}`（dBFS） | 100 ms |
| `st` | 与 `/api/state` 同构的瘦身版（cfg+metrics+hud） | 状态变化时 |
| `seg` | 段（§4），同一 `id` 会推多次 | 每句 2–4 次 |
| `log` | `{lv:"info|warn|err", msg}` | 事件驱动 |
| `mic` | `{on}` | 切换后 |
| `tts_test` | `{ms, first_pkt_ms, audio_s}` | 手动 |

## 4. 段（segment）

```jsonc
{
  "id": "A-172",              // 链前缀 + 单调序号
  "chain": "A",               // A|B|C
  "ts": 1732161234.5,         // 段尾定稿时刻（epoch 秒）
  "state": "asr",             // asr|mt|speak|play|done|refined|drop|err
  "src": "今日はちょっと遅れちゃった",
  "dst": "今天有点迟到了",
  "src_lang": "ja", "dst_lang": "zh",
  "asr_ms": 128,              // 真正产出这段文本的那一次识别
  "vad_ms": 640,              // 段尾 → 定稿（停顿等待）
  "mt_ms": 320,               // 翻译首子句
  "tts_ms": 1020,             // 合成首包
  "audio_ms": 2100,           // 这段音频时长
  "e2e_ms": 1520,             // 说完最后一个字 → 听见第一声
  "open_ms": 5900,            // 开口 → 第一声（含说话本身，别当延迟看）
  "cb_ms": 1750,              // 定稿 → 聊天框发出
  "engine": "local",          // local|cloud|local(兜底)|local(漂移)|qwen17|qwen06|fun|preview|inject
  "echo": false,              // 是否判成自己的回声
  "speaker": null,            // A=我，B=对方
  "note": ""                  // 整段没识别出字… / 只出原文 / 预合成命中
}
```

字段只会增加不会改名；前端遇到不认识的键直接忽略。

## 5. 浮窗协议（后端 → `hud.py` / `danmaku.py` stdin，一行一个 JSON）

```jsonc
{"op":"cfg","alpha":92,"scale":100,"rows":8,"click":false,"x":-1,"y":-1,"style":"card"}
{"op":"seg","seg":{ /* §4 */ }}
{"op":"mic","on":true}
{"op":"clear"}
{"op":"status","text":"A 链 运行中 · CapsLock 闭麦"}
{"op":"ping"}     // 后端每 5 s 一次，20 s 收不到自己退出
{"op":"quit"}
```

要求：无边框、置顶、默认 `WindowTransparentForInput`（穿透，`click:true` 才可拖）；stdout 不许打印任何东西（后端在读，调试写 stderr）。

## 6. 三条铁律

1. **SSE 必须 chunked**（见 §3），否则浏览器每 2 s 重连一次。
2. **密钥永不进响应**：`/api/env` 只回后 6 位；仓库、安装包、日志里都没有明文 key。
3. **A 链的译文绝不进 chatbox**（对方听到的就是日语，聊天框只放 B/C 的中文）。
