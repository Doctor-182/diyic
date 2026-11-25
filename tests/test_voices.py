# -*- coding: utf-8 -*-
r"""音色克隆真值表（离线，不联网、不花钱）。

音色复刻这条路最容易出的不是"接口报错"，而是"参数悄悄不对"：
样本形状不合法、prefix 带中文、端点选错、云端一直 DEPLOYING 却当成成功。
这张表把请求体逐字段钉死，并用假 Net 走完整轮询。

用法：call scripts\_env.bat 后跑  %PY% -m tests.test_voices
"""
import os
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, ".")
from vrdub import voices as V      # noqa: E402

ok = True


def chk(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


class FakeCfg:
    def __init__(self, **kw):
        self.d = {"voice_clone_model": "qwen-audio-3.0-tts-plus",
                  "voice_clone_endpoint": "auto"}
        self.d.update(kw)

    def get(self, k, dv=None):
        return self.d.get(k, dv)


class FakeNet:
    """按脚本回包，同时把请求体存下来给断言用。"""

    def __init__(self, script=None, fail=False, err=None):
        self.script = list(script or [])
        self.fail = fail
        self.err = err or "ali HTTP 400: {}"
        self.calls = []

    def post_json(self, path, payload, timeout=60.0):
        self.calls.append((path, payload))
        if self.fail:
            raise RuntimeError(self.err)
        return self.script.pop(0) if self.script else {"output": {"status": "OK"}}


def make_wav(path, secs=6.0, sr=48000, ch=2, amp=0.05):
    t = np.arange(int(sr * secs), dtype=np.float32) / sr
    a = (amp * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    x = np.stack([a, a], axis=1) if ch > 1 else a.reshape(-1, 1)
    i16 = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(i16.tobytes())


tmp = tempfile.mkdtemp(prefix="vdub_voice_")
src = os.path.join(tmp, "raw.wav")
dst = os.path.join(tmp, "sample.wav")
make_wav(src, secs=6.0)

print("[1] prefix 清洗：只留小写字母数字、≤16 位、中文退化成 vdub")
chk("中文名 → vdub", V.clean_prefix("小明") == "vdub", V.clean_prefix("小明"))
chk("大写与符号", V.clean_prefix("My_Voice!!") == "myvoice", V.clean_prefix("My_Voice!!"))
chk("超长截 16", len(V.clean_prefix("a" * 40)) == 16, V.clean_prefix("a" * 40))
chk("空串用 fallback", V.clean_prefix("", "ok123") == "ok123")

print("[2] 样本转码：48k 双声道 → 24k 单声道 16bit，并且做了峰值归一")
meta = V.to_clone_wav(src, dst, sr=V.SR_MIN)
chk("秒数基本不变", abs(meta["secs"] - 6.0) < 0.1, meta["secs"])
chk("采样率 24000", meta["sr"] == 24000, meta["sr"])
chk("单声道", meta["ch"] == 1)
chk("标了 changed", meta["changed"] is True)
chk("增益 >1（原始 0.05 很小）", meta["gain"] > 1.0, meta["gain"])
with wave.open(dst, "rb") as w:
    chk("落盘 = 单声道/16bit/24k", (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000),
        (w.getnchannels(), w.getsampwidth(), w.getframerate()))
a, ch, sr = V._read_wav(dst)
chk("峰值归一到 0.89 附近", abs(float(np.max(np.abs(a))) - 0.89) < 0.02, float(np.max(np.abs(a))))

print("[3] 样本闸门：太短 / 太大 / 文件不在")
try:
    V.check_sample(dst, secs=1.0)
    chk("太短要抛", False)
except V.VoiceError as e:
    chk("太短要抛", "至少要" in str(e), str(e))
try:
    V.check_sample(dst, secs=6.0, size=11 * 1024 * 1024)
    chk("超 10 MB 要抛", False)
except V.VoiceError as e:
    chk("超 10 MB 要抛", "10 MB" in str(e), str(e))
try:
    V.check_sample(os.path.join(tmp, "nope.wav"))
    chk("文件不存在要抛", False)
except V.VoiceError as e:
    chk("文件不存在要抛", "不在" in str(e))
chk("合规样本通过", V.check_sample(dst, secs=6.0) is True)

print("[4] data URI：wav → audio/wav，非法后缀拒绝")
uri, mime, size = V.data_uri(dst)
chk("mime", mime == "audio/wav", mime)
chk("是 data URI", uri.startswith("data:audio/wav;base64,"))
chk("大小对得上", size == os.path.getsize(dst), (size, os.path.getsize(dst)))
bad = os.path.join(tmp, "x.flac")
open(bad, "wb").write(b"x")
try:
    V.data_uri(bad)
    chk("flac 要拒绝", False)
except V.VoiceError:
    chk("flac 要拒绝", True)

print("[5] 请求体：action / target_model / prefix / audio 全对")
body = V.payload_create("qwen-audio-3.0-tts-plus", "My Voice", uri)
chk("model=voice-enrollment", body["model"] == "voice-enrollment")
chk("action=create_voice", body["input"]["action"] == "create_voice")
chk("target_model 原样", body["input"]["target_model"] == "qwen-audio-3.0-tts-plus")
chk("prefix 已清洗", body["input"]["prefix"] == "myvoice", body["input"]["prefix"])
chk("audio 是 URI", body["input"]["audio"].startswith("data:audio/wav"))

print("[6] create → 云端回 voice_id，prefix/模型都记下来")
net = FakeNet([{"output": {"voice_id": "qwen-audio-3.0-tts-plus-vdub-deadbeef", "status": "DEPLOYING"}}])
c = V.Cloner(FakeCfg(), net)
r = c.create(dst, name="小明", secs_hint=6.0)
chk("voice_id 取到", r["voice_id"].endswith("deadbeef"), r["voice_id"])
chk("走 plan 端点", r["endpoint"] == "plan", r["endpoint"])
chk("模型名写进结果", r["target_model"] == "qwen-audio-3.0-tts-plus")
chk("只发了一次", len(net.calls) == 1)
chk("打到 customization 端点", net.calls[0][0] == V.PATH, net.calls[0][0])

print("[7] 云端不给 voice_id → 直接抛（不能当成功）")
net = FakeNet([{"output": {}}])
try:
    V.Cloner(FakeCfg(), net).create(dst, secs_hint=6.0)
    chk("缺 voice_id 要抛", False)
except V.VoiceError as e:
    chk("缺 voice_id 要抛", "voice_id" in str(e), str(e)[:80])

print("[8] 轮询：DEPLOYING → DEPLOYING → OK，第三次返回")
net = FakeNet([{"output": {"status": "DEPLOYING"}}, {"output": {"status": "RUNNING"}},
               {"output": {"status": "OK"}}])
c = V.Cloner(FakeCfg(), net)
seen = []
res = c.wait("vid-1", tries=5, gap=0.0, progress=lambda i, n, st: seen.append(st))
chk("status=OK", res["status"] == "OK")
chk("跑了 3 次", res["tries"] == 3, res["tries"])
chk("progress 收到状态", seen == ["DEPLOYING", "RUNNING", "OK"], seen)
chk("查询体 action=query_voice", net.calls[0][1]["input"]["action"] == "query_voice")

print("[9] 轮询遇到 FAILED → 立刻抛，不干等")
net = FakeNet([{"output": {"status": "FAILED"}}])
try:
    V.Cloner(FakeCfg(), net).wait("vid-2", tries=10, gap=0.0)
    chk("FAILED 要抛", False)
except V.VoiceError as e:
    chk("FAILED 要抛", "FAILED" in str(e), str(e)[:80])
chk("只问了 1 次", len(net.calls) == 1, len(net.calls))

print("[10] 等到超时还没 OK → 抛，并说明等了多少秒")
net = FakeNet([{"output": {"status": "DEPLOYING"}}] * 3)
try:
    V.Cloner(FakeCfg(), net).wait("vid-3", tries=3, gap=0.0)
    chk("超时要抛", False)
except V.VoiceError as e:
    chk("超时要抛", "还是" in str(e), str(e)[:80])

print("[11] 端点策略：auto = 套餐先试失败退按量；显式选就只用那个")
plan, payg = FakeNet(), FakeNet()
c = V.Cloner(FakeCfg(), plan, payg)
chk("auto 顺序 plan→payg", [n for n, _ in c.nets()] == ["plan", "payg"])
chk("plan 只一个", [n for n, _ in V.Cloner(FakeCfg(voice_clone_endpoint="plan"), plan, payg).nets()] == ["plan"])
chk("payg 只一个", [n for n, _ in V.Cloner(FakeCfg(voice_clone_endpoint="payg"), plan, payg).nets()] == ["payg"])
plan.fail = True
net_payg = FakeNet([{"output": {"voice_id": "vid-payg"}}])
r = V.Cloner(FakeCfg(), plan, net_payg).create(dst, secs_hint=6.0)
chk("套餐失败自动退按量", r["endpoint"] == "payg", r["endpoint"])
chk("按量真的被调用", len(net_payg.calls) == 1)
try:
    V.Cloner(FakeCfg(), FakeNet(fail=True), FakeNet(fail=True)).create(dst, secs_hint=6.0)
    chk("两个端点都失败要抛", False)
except V.VoiceError as e:
    chk("两个端点都失败要抛", "plan" in str(e) and "payg" in str(e), str(e)[:120])

print("[12] 端点记忆：404/Model not exist 之后不再撞同一个端点")
plan = FakeNet(fail=True, err='ali HTTP 404: {"code":"InvalidParameter","message":"Model not exist."}')
payg = FakeNet([{"output": {"voice_id": "vid-x"}}, {"output": {"voice_id": "vid-y"}}])
c = V.Cloner(FakeCfg(), plan, payg)
try:
    c.create(dst, secs_hint=6.0)
except V.VoiceError:
    pass
chk("plan 被标记", bool(c._bad.get("plan")), c._bad)
before = len(plan.calls)
c.create(dst, secs_hint=6.0)
chk("第二次直接走 payg", len(plan.calls) == before, (len(plan.calls), before))
chk("payg 拿到了两次", len(payg.calls) == 2, len(payg.calls))

print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
