# -*- coding: utf-8 -*-
r"""人设卡真值表（离线）。

两件必须钉死的事：
  1. 自定义人设只改口吻 —— 提示词里必须带「不许改内容」的约束，且完整性条款永远在最后；
  2. 人设是「我」的，B/C 链不许套用（否则等于替对方改人格）。

用法：call scripts\_env.bat 后跑  %PY% -m tests.test_persona
"""
import sys

sys.path.insert(0, ".")
from vrdub import cfg as C          # noqa: E402
from vrdub import persona as P      # noqa: E402
from vrdub.chains import StreamChain as Chain   # noqa: E402

ok = True


def chk(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


print("[1] 预设卡：各有各的口吻，但都要带完整性条款")
ja = P.system_prompt("ja", "natural")
chk("默认卡是日语句", "中日同声传译" in ja)
chk("完整性条款在最后", ja.rstrip().endswith(P.FIDELITY.rstrip()))
teto = P.system_prompt("ja", "teto")
chk("换卡真的换提示词", teto != ja)
chk("teto 也带完整性", teto.endswith(P.FIDELITY))
for dst in ("ja", "zh", "en", "ko"):
    body = P.system_prompt(dst, "natural")
    chk(f"{dst} 有独立提示词", bool(body) and body.index(P.FIDELITY) > 0)
chk("dst=none 不翻译 → 空提示词", P.system_prompt("none") == "")

print("[2] 自定义人设：插在卡后面、完整性条款前面")
P.set_custom("")
plain = P.system_prompt("ja", "natural", custom="")
chk("空自定义 = 无块", "口吻设定" not in plain)
withc = P.system_prompt("ja", "natural", custom="你是傲娇的女高中生，说话带刺")
chk("有块", "口吻设定" in withc)
chk("块里含原文", "傲娇的女高中生" in withc)
chk("块在完整性之前", withc.index("口吻设定") < withc.index(P.FIDELITY))
chk("完整性仍在最末", withc.rstrip().endswith(P.FIDELITY.rstrip()))
chk("明确写了不许改内容", "不许" in withc and "内容" in withc)
chk("custom='' 明确不用（即使模块级有值）", "口吻设定" not in P.system_prompt("ja", "natural", custom=""))

print("[3] 模块级：set_custom 真生效 / 去空白 / 截 1200 字")
n = P.set_custom("  简短人设  ")
chk("返回去空白后的文本", n == "简短人设", repr(n))
chk("None→用模块值", "简短人设" in P.system_prompt("ja"))
chk("get_custom 一致", P.get_custom() == "简短人设")
long = "字" * 1300
P.set_custom(long)
chk("截到 1200", len(P.get_custom()) == P.CUSTOM_MAX, len(P.get_custom()))
chk("describe 报长度", P.describe()["custom_len"] == P.CUSTOM_MAX)
chk("describe 带卡列表", len(P.describe()["cards"]) == len(P.CARDS))
P.set_custom("")

print("[4] cfg 收口：非法卡回 natural、非字符串自定义归空、超长截断")
c = C.clamp_cfg({"card": "bogus", "persona_custom": ["x"]})
chk("卡白名单", c["card"] == "natural", c["card"])
chk("非字符串归空", c["persona_custom"] == "")
c = C.clamp_cfg({"card": "teto", "persona_custom": "  " + "a" * 1300 + "  "})
chk("合法卡保留", c["card"] == "teto")
chk("自定义截 1200", len(c["persona_custom"]) == 1200, len(c["persona_custom"]))

print("[5] 人设只属于「我」：A 链带、B/C 链不带")
c = Chain.__new__(Chain)


class _Cfg:
    def __init__(self, **kw):
        self.d = kw

    def get(self, k, dv=None):
        return self.d.get(k, dv)


c.cfg = _Cfg(persona_custom="你是傲娇")
c.src_key = "mic"
chk("A 链拿到人设", c._persona() == "你是傲娇", c._persona())
c.src_key = "loop"
chk("B 链为空", c._persona() == "")
c.src_key = "ocr"
chk("C 链为空", c._persona() == "")
c.src_key = "mic"
c.cfg = _Cfg(persona_custom=None)
chk("没设过就是空串", c._persona() == "", repr(c._persona()))

print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
