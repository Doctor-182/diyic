# 进程级 WASAPI 回环（只监听 VRChat 的声音）

## 为什么换
原来从「扬声器 (Rapoo Gaming Headset) [Loopback]」采 B 链，等于把**一切**都录进去：自己的麦克风（本地监听）、自己 TTS 的回放、系统音。VB-CABLE 那几根线（19/20/24/26/27）实测互通，做不出第二条隔离线。Windows 10 2004 起有的**进程回环**能按 PID 抓声音，只能听到目标进程树，天生隔离。

## 原理
`ActivateAudioInterfaceAsync(VAD\Process_Loopback, IID_IAudioClient, params)` → 回调里 `GetActivateResult` 拿 `IAudioClient`；`params` 是 PROPVARIANT(VT_BLOB=65)，内层 `AUDIOCLIENT_ACTIVATION_PARAMS{ActivationType=1, ProcessLoopbackMode=0(INCLUDE_PROCESS_TREE), TargetProcessId}`。之后按普通 loopback 用法：`Initialize(SHARED, LOOPBACK|EVENTCALLBACK|AUTOCONVERTPCM|SRC_DEFAULT_QUALITY, 20ms)` → `SetEventHandle` → `GetService(IAudioCaptureClient)` → `Start`，事件驱动 `GetBuffer/ReleaseBuffer`。AUTOCONVERTPCM 让引擎把目标进程的任意格式转成我们要的 float32 48k stereo，所以不必跟它的 mix format 死磕。

## 接口（vrdub/procloop.py，纯 ctypes，无第三方依赖）
```python
from vrdub.procloop import ProcLoop, find_pid, list_pids
pl = ProcLoop(pid=None, name="VRChat.exe", on_pcm=cb, rate=48000, channels=2, log=util.log, mono=True)
pl.start()            # 找不到进程 / 任何一步失败 -> RuntimeError("进程回环开不了：0x........ 含义")
pl.pid                # 实际采的 PID
pl.running            # 线程活着且没出错
pl.stats              # {'frames','dur','drops','blocks','silent','errs','peak'}
pl.init_format        # 例如 "float32 48000Hz/2ch + AUTOCONVERTPCM 20ms"
pl.stop()             # 幂等；Stop/Release/CoUninitialize 各一次
```
- `on_pcm(pcm: np.ndarray, rate: int)`：float32 [-1,1]；默认**已下混单声道**（`mono=False` 则给 `(n, channels)`）。
- 静音帧（`AUDCLNT_BUFFERFLAGS_SILENT`）送全零数组，**不丢**——下游按时间轴处理更简单。
- 也可 `with ProcLoop(pid=...) as pl:`。

## 自验
```
Set-Location "D:\vrc无言势"; call scripts\_env.bat
%PY% -u tools\procloop_check.py
```
它会起一个子进程往 #18 播 4 s 440 Hz（peak 应为 gain≈0.25），再拿一个只 sleep 的进程做对照（peak 应≈0）。判据 peak>0.02 = 通；退出码 0 通过 / 1 结论不符 / 2 采集器起不来。

## 失败排查（HRESULT 表，完整表见 procloop.py 的 _HR）
| HRESULT | 含义 / 处置 |
| --- | --- |
| 0x80070005 | E_ACCESSDENIED，目标进程权限更高（管理员进程）：以同权限启动本项目 |
| 0x80070057 | E_INVALIDARG，激活参数/结构体尺寸不对（PROPVARIANT 必须 24 字节、VT_BLOB=65） |
| 0x8000000E | E_ILLEGAL_METHOD_CALL，完成回调的 QueryInterface 拒了 `IID_IAgileObject`（必须接受） |
| 0x80004005 | E_FAIL，目标进程已退出或没有音频流 |
| 0x88890008 | AUDCLNT_E_UNSUPPORTED_FORMAT，格式不支持（已自动兜底 PCM16/mix format） |
| 0x88890019 | BUFFER_SIZE_NOT_ALIGNED，已自动按 GetBufferSize 重算重试 |
| 0x88890026 | RESOURCES_INVALIDATED，目标进程退出/切设备：重建 ProcLoop |
| E_NOTFOUND(0x80070490) | 进程不存在（名字写错 / 还没启动） |
| 0x80010106 | RPC_E_CHANGED_MODE，本线程已初始化成 STA——采集线程必须自己 CoInitializeEx(MTA) |

## 已知限制
- 只能采**同一个登录会话**里的进程（跨 session / 服务进程采不到），拿不到就报 0x80070005 或 0x80004005。
- 采不到**独占模式**音频（目标进程用 WASAPI exclusive 时无声）。
- Windows 10 build < 19041 没有这个 API，start() 会直接报错退出。
- 目标进程重启后 PID 变，必须 stop() 后重建（不会自动跟）。
- 采集端自己不做重采样：下游按 `rate` 处理；需要 24k 就用 `vrdub.util.resample`。