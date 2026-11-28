# -*- coding: utf-8 -*-
r"""离线验证：sv 档（本地 SenseVoice）与显存看门。

不碰显卡、不载真模型、不花钱：nvidia-smi 的读数直接注入，transcribe_cpp 换假的。

  1. 每个档「真正要多少显存」= 权重 + 运行库/上下文（1861 / 1225 / 1264 MiB）
  2. fits()：够 → True；差一点也不给过（留 256 MiB 余量）；读不到读数 → 放行
  3. free_after_switch()：换档时旧模型那份空间算进来（否则 qwen17→qwen06 会被误拒）
  4. acquire()：显存不够时**一个模型都不载**，返回原因，驻留器保持空
  5. catalog()：带 need_mb / fits / why，界面能直接标「装不下」
  6. App._gguf_fit_error()：拒绝文案带引擎名与替代建议；/api/cfg 在改 cfg 之前就返回
  7. sv 档：直接走本地 SenseVoice，不碰 GGUF、不兜云端，engine 记 sensevoice
  8. cfg 白名单：sv 不被 clamp 掉，乱写的值回落默认

用法：python -m tests.test_vram_guard
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pylibs"))

import numpy as np

from vrdub import asr_gguf, cfg as cfgmod, util as utilmod
from vrdub.asr import Transcriber

HERE = os.path.abspath(__file__)
LOADS = []


class _FakeSession:
    def run(self, x, **kw):
        return types.SimpleNamespace(text="テスト。", language="ja")


class _FakeModel:
    def __init__(self, path, backend="auto"):
        LOADS.append((os.path.basename(path), backend))

    def session(self, n_threads=0, kv_type="auto"):
        return _FakeSession()


sys.modules["transcribe_cpp"] = types.SimpleNamespace(Model=_FakeModel)

LOGS = []


class _U:
    @staticmethod
    def log(msg, lvl="info", who=""):
        LOGS.append(str(msg))

    clean_text = staticmethod(utilmod.clean_text)
    lang_ok = staticmethod(utilmod.lang_ok)
    lang_why = staticmethod(utilmod.lang_why)


asr_gguf.util = _U
asr_gguf._env_setup = lambda: True
asr_gguf.path_of = lambda eid, cfg=None: HERE          # 模型文件「存在」
asr_gguf.RES = asr_gguf.Resident()

_REAL_FREE = asr_gguf.vram.free_mb


def set_free(mb):
    asr_gguf.vram.free_mb = lambda: mb


ok = True


def chk(name, cond, extra=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + ("  " + extra if extra else ""))
    ok = ok and bool(cond)


print("[1] 每个档真正要多少显存")
chk("qwen17 = 1441 + 420 = 1861", asr_gguf.need_mb("qwen17") == 1861, str(asr_gguf.need_mb("qwen17")))
chk("qwen06 = 805 + 420 = 1225", asr_gguf.need_mb("qwen06") == 1225, str(asr_gguf.need_mb("qwen06")))
chk("fun = 844 + 420 = 1264", asr_gguf.need_mb("fun") == 1264, str(asr_gguf.need_mb("fun")))

print("[2] fits() 的阈值")
asr_gguf.RES = asr_gguf.Resident()
chk("空 5000 → 装得下", asr_gguf.fits("qwen17", free_mb=5000)[0])
chk("空 2200 → 装得下（1861+256=2117）", asr_gguf.fits("qwen17", free_mb=2200)[0])
b, why = asr_gguf.fits("qwen17", free_mb=2100)
chk("空 2100 → 不给过（差 17 MiB 也不行）", not b, why)
chk("理由里有具体数字", "1861" in why and "2117" in why, why)
chk("理由里给替代方案 sv", "sv" in why, why)
b2, why2 = asr_gguf.fits("qwen06", free_mb=1000)
chk("qwen06 空 1000 → 不给过", not b2, why2)
chk("读不到读数（-1）→ 放行", asr_gguf.fits("qwen17", free_mb=-1)[0],
    asr_gguf.fits("qwen17", free_mb=-1)[1])

print("[3] 换档时旧模型那份空间要算进来")
asr_gguf.RES = asr_gguf.Resident()
asr_gguf.RES.key = "qwen17"
asr_gguf.RES.session = object()
set_free(800)
chk("现驻留 qwen17、整机空 800 → 切 qwen06 的可用额度 = 2661",
    asr_gguf.free_after_switch("qwen06") == 2661, str(asr_gguf.free_after_switch("qwen06")))
chk("按这个额度 qwen06 装得下", asr_gguf.fits("qwen06")[0])
chk("同一档在显存里 → 直接 True", asr_gguf.fits("qwen17")[0], asr_gguf.fits("qwen17")[1])

print("[4] acquire()：装不下就一个都不载")
asr_gguf.RES = asr_gguf.Resident()
LOADS.clear()
set_free(500)
sess, err = asr_gguf.RES.acquire("qwen17")
chk("返回 None", sess is None)
chk("原因写清是显存", "装不下" in err and "显存" in err, err[:80])
chk("驻留器仍然是空的", asr_gguf.RES.key == "", asr_gguf.RES.key)
chk("一次模型载入都没发生", LOADS == [], str(LOADS))
chk("info() 里带着这条错误", "装不下" in (asr_gguf.RES.info().get("err") or ""))

print("[5] 显存够就正常载入")
set_free(6000)
sess, err = asr_gguf.RES.acquire("qwen17")
chk("载入成功", sess is not None and err == "", err)
chk("驻留 qwen17", asr_gguf.RES.key == "qwen17")
chk("记录权重显存", asr_gguf.RES.info().get("vram_mb") == 1441)

print("[6] catalog() 带看门信息")
asr_gguf.RES = asr_gguf.Resident()          # 上一步把 qwen17 载进来了，先清掉再看 catalog
cat = {m["id"]: m for m in asr_gguf.catalog()}
chk("qwen17 有 need_mb", cat["qwen17"]["need_mb"] == 1861, str(cat["qwen17"].get("need_mb")))
chk("有 fits 与 why 字段", "fits" in cat["qwen17"] and "why" in cat["qwen17"])
set_free(300)
cat2 = {m["id"]: m for m in asr_gguf.catalog()}
chk("空 300 时 qwen17 标成装不下", cat2["qwen17"]["fits"] is False, cat2["qwen17"]["why"][:40])
chk("理由说的是显存而不是没下载", "装不下" in cat2["qwen17"]["why"], cat2["qwen17"]["why"][:40])
chk("文件在就 present=True（不按 MB 四舍五入判）", cat2["qwen17"]["present"] is True)

print("[7] App._gguf_fit_error()：拒绝文案与拦截点")
asr_gguf.RES = asr_gguf.Resident()
set_free(300)
from vrdub.app import App


class _Cfg:
    def __init__(self, d):
        self._d = dict(d)

    def get(self, k, dv=None):
        return self._d.get(k, dv)

    def all(self):
        return dict(self._d)

    def update(self, patch):
        ch = {k: v for k, v in patch.items() if self._d.get(k) != v}
        self._d.update(patch)
        return ch


app = App.__new__(App)
app.cfg = _Cfg(cfgmod.DEFAULTS)
msg = app._gguf_fit_error({"asr_engine_b": "qwen17"})
chk("切 qwen17 被拦", bool(msg), msg[:60])
chk("文案点名键与档", "asr_engine_b" in msg and "qwen17" in msg, msg[:60])
chk("切 sv 不拦（不占显存）", app._gguf_fit_error({"asr_engine_b": "sv"}) == "")
chk("切 cloud 不拦", app._gguf_fit_error({"asr_engine_a": "cloud"}) == "")
set_free(6000)
chk("显存够了就放行", app._gguf_fit_error({"asr_engine_b": "qwen06"}) == "")

print("[8] api_cfg 在改 cfg 之前就返回（拒绝时配置不变）")
set_free(300)
app = App.__new__(App)
app.cfg = _Cfg(cfgmod.DEFAULTS)
import vrdub.app as appmod
appmod.util = _U
before = app.cfg.get("asr_engine_b")
res = app.api_cfg({"asr_engine_b": "qwen17"})
chk("返回 ok=False", res.get("ok") is False, str(res.get("ok")))
chk("带 err 与当前 cfg", bool(res.get("err")) and "cfg" in res)
chk("配置一个字都没改", app.cfg.get("asr_engine_b") == before, str(app.cfg.get("asr_engine_b")))

print("[9] sv 档：只走本地 SenseVoice，不碰 GGUF")


class _Local:
    note = "ok 载入 100ms"
    sv_dir = "models/sensevoice"
    loaded = True

    def ready(self):
        return True

    def transcribe(self, pcm, lang="auto"):
        self.last_lang = lang
        return "テスト、これは字幕です。"


def _boom(*a, **k):
    raise AssertionError("sv 档不该碰 GGUF")


_real_gguf_tr = asr_gguf.transcribe
asr_gguf.transcribe = _boom
tr = Transcriber.__new__(Transcriber)
tr.cfg = types.SimpleNamespace(get=lambda k, dv=None: dv)
tr.local = _Local()
tr.log = _U.log
txt, eng, ms = tr.transcribe_fast(np.zeros(3200, dtype=np.int16), "ja", e="sv")
chk("出字", txt.startswith("テスト"), txt)
chk("engine 记 sensevoice", eng == "sensevoice", eng)
chk("语种原样传下去", tr.local.last_lang == "ja", str(tr.local.last_lang))
chk("没有调用 GGUF", True)
asr_gguf.transcribe = _real_gguf_tr

print("[10] cfg 白名单")
full = dict(cfgmod.DEFAULTS)
full["asr_engine_b"] = "sv"
chk("sv 不被 clamp 掉", cfgmod.clamp_cfg(full)["asr_engine_b"] == "sv")
full2 = dict(cfgmod.DEFAULTS)
full2["asr_engine_a"] = "sv"
chk("A 链也认 sv", cfgmod.clamp_cfg(full2)["asr_engine_a"] == "sv")
full3 = dict(cfgmod.DEFAULTS)
full3["asr_engine"] = "nonsense"
chk("乱写回落默认", cfgmod.clamp_cfg(full3)["asr_engine"] == cfgmod.DEFAULTS["asr_engine"])

asr_gguf.vram.free_mb = _REAL_FREE
print()
print("RESULT: " + ("ALL PASS" if ok else "FAILED"))
sys.exit(0 if ok else 1)
