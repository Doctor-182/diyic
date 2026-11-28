/* ============================================================================
   VRDUB 控制台 · app.js
   契约：docs/PROTOCOL.md v1（字段名一个字都不改）
   纯原生 JS / 无框架 / 无 CDN / 离线可用
   ========================================================================== */
(function () {
  "use strict";

  /* ------------------------------ 小工具 ------------------------------ */
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var clamp = function (v, a, b) { return v < a ? a : (v > b ? b : v); };
  var rnd = function (n) { return Math.random() * n; };
  var pick = function (a) { return a[Math.floor(Math.random() * a.length)]; };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function pad2(n) { return n < 10 ? "0" + n : "" + n; }
  function fmtClock(ts) {
    if (!ts) return "–";
    var n = Number(ts);
    if (!isFinite(n) || n <= 0) return "–";
    // 协议写的是 epoch 秒，后端实际推的是毫秒；两种都吃
    var d = new Date(n > 1e11 ? n : n * 1000);
    if (isNaN(d.getTime())) return "–";
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }
  function fmtInt(v) { return (v === null || v === undefined || isNaN(v)) ? "–" : String(Math.round(v)); }
  function fmtSec(ms) { return (ms === null || ms === undefined || isNaN(ms) || ms <= 0) ? "–" : (ms / 1000).toFixed(2); }
  function fmtRtf(v) { return (v === null || v === undefined || isNaN(v) || v <= 0) ? "–" : Number(v).toFixed(2); }
  function setTx(el, v) { if (el && el.textContent !== v) el.textContent = v; }
  function fmtUp(sec) {
    sec = Math.max(0, Math.floor(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + "h " : "") + (h || m ? m + "m " : "") + s + "s";
  }
  function langName(l) {
    return ({ zh: "中文", ja: "日语", en: "英语", auto: "自动" })[l] || (l ? String(l) : "?");
  }

  /* ------------------------------ 状态 ------------------------------ */
  var MAX_ROWS = 400;
  var MAX_LOGS = 200;

  var st = {
    demo: false,
    base: "",                 // 后端 base（来自 state.url）
    cfg: {},
    devices: { input: [], output: [], loop: [] },
    voices: [],
    metrics: { A: {}, B: {} },
    hud: {},
    micOn: true,
    levels: { mic: -90, loop: -90 },
    last: { A: null, B: null, C: null },   // 每条链最后一段（链卡片实时显示）
    segs: new Map(),
    filter: "all",
    sse: null,
    retryT: 0,
    pid: 0,
    ver: "",
    uptime0: 0,
    upAt: 0,
    drop: 0,
    speed: 1,
    speedKb: false,           // 后端 cfg 里有 speed 字段才能写回去
    mic: { on: true, live: true, dev: 0, reason: "" },   // 后端 state.mic：on=开麦意愿 live=真在采音 reason=打不开的原因
    ocr: {},                  // 后端 state.ocr：{ready,region,region_inferred,...}
    ocrNote: { text: "", kind: "", until: 0 },   // C 卡那行小字：「识别到 3 行 · 123 ms」之类
    openTxt: "",              // 「起话→开口」那行的文本（open_ms 来自 /api/metrics rows）
    openT: 0,
    demoN: 0,
    demoTimers: [],
    deb: {},
    demoT0: 0
  };

  var els = {};

  /* ------------------------------ 文案表 ------------------------------ */
  var STATE_TXT = {
    asr: "识别中", mt: "翻译中", tts: "合成中", play: "播放中",
    done: "完成", refined: "已校正", drop: "已丢弃", err: "出错"
  };
  var NOTE_MAP = {
    "预合成命中": "预合成", "本地草稿": "草稿", "云端校正": "已校正",
    "字幕上屏": "已上屏", "出声中": "出声中", "回声": "回声"
  };
  function noteText(n) { return n ? (NOTE_MAP[n] || n) : ""; }
  function stateText(s) { return STATE_TXT[s] || s || "–"; }

  /* ------------------------------ 日志 ------------------------------ */
  function log(lv, msg) {
    var t = new Date();
    var item = { t: pad2(t.getHours()) + ":" + pad2(t.getMinutes()) + ":" + pad2(t.getSeconds()), lv: lv || "info", msg: String(msg) };
    if (!els.logs) { return; }
    var div = document.createElement("div");
    div.className = "logline " + item.lv;
    div.innerHTML = '<span class="lt">' + item.t + '</span><span class="lv">' + esc(item.lv) +
                    '</span><span class="lm">' + esc(item.msg) + "</span>";
    var el = els.logs;
    var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
    el.appendChild(div);
    while (el.childElementCount > MAX_LOGS) el.removeChild(el.firstChild);
    if (els.logAuto && els.logAuto.checked && atBottom) el.scrollTop = el.scrollHeight;
  }

  /* ------------------------------ 连接状态 ------------------------------ */
  function setConn(state, text) {
    if (!els.conn) return;
    els.conn.dataset.state = state;
    setTx(els.connText, text);
  }
  function liveText() { return st.pid ? "在线 · pid " + st.pid : "在线"; }
  function setBadge(on) { if (els.badge) els.badge.hidden = !on; }

  /* ------------------------------ API ------------------------------ */
  function api(p) { return (st.base || "") + p; }

  /* 浏览器自己出问题时报一句到后端日志（服务端只能看到连接开合，看不到 ERR_*） */
  function report(msg, extra) {
    try { console.error("[vrdub]", msg, extra || ""); } catch (e0) {}
    try {
      /* 后端正在重启时这条 fetch 必然失败——必须自己吞掉，否则会再抛一个 unhandledrejection，
         变成「上报失败 → 又上报」的雪崩 */
      fetch(api("/api/clientlog"), {
        method: "POST", cache: "no-store", keepalive: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ msg: msg, extra: (extra || "") + " · " + location.href })
      }).catch(function () {});
    } catch (e) {}
  }

  /* 密钥卡：安装版里唯一必须手填的东西，写 %LOCALAPPDATA%\VRDub\config\.env.local。
     必须放在模块作用域：tryState 连上后端后要调 loadKey()，先前它被写在 bind() 里，
     外面调不到 → ReferenceError 被 fetch 的 catch 吃掉 → 每 3 秒重试一次、
     每次重开一条事件通道，界面就永远停在「重连中…」。 */
  function renderKey(j) {
    if (!j) return;
    if (els.keyState) {
      setTx(els.keyState, j.has_key
        ? ("已配置 …" + (j.tail || "") + (j.probe === "ok" ? " · 自检正常"
          : (j.probe === "fail" ? " · 自检失败" : "")))
        : "未配置 · 云端功能不可用");
      els.keyState.dataset.state = j.has_key ? (j.probe === "fail" ? "warn" : "ok") : "err";
    }
    if (els.keyHost && !els.keyHost.value && j.host) els.keyHost.value = j.host;
  }
  function loadKey() {
    if (st.demo) return;
    apiGet("/api/env").then(function (j) {
      renderKey(j);
      if (j && !j.has_key) {
        log("warn", "还没配密钥：云端识别/翻译/合成都不可用（本地识别照常出字）。去右边「密钥」卡粘一个再点保存。");
      }
    }).catch(function () {});
  }

  function postJson(path, body) {
    return fetch(api(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function apiGet(path) {
    if (st.demo) return Promise.resolve(null);
    return fetch(api(path), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .catch(function (e) {
        setConn("err", "接口失败 " + path);
        log("err", "GET " + path + " 失败：" + e.message);
        return null;
      });
  }

  function apiPost(path, body) {
    if (st.demo) {
      log("info", "离线预览：未发送 " + path + " " + JSON.stringify(body || {}));
      return Promise.resolve(null);
    }
    return postJson(path, body).catch(function (e) {
      setConn("err", "接口失败 " + path);
      log("err", "POST " + path + " 失败：" + e.message);
      return null;
    });
  }

  /* ------------------------------ 参数表 ------------------------------ */
  var MAIN_PARAMS = [
    { k: "mic_db",      label: "麦克风门限",    min: -70, max: -10, step: 0.5,  unit: "dBFS", dec: 1 },
    { k: "loop_db",     label: "对面声音门限",  min: -70, max: -10, step: 0.5,  unit: "dBFS", dec: 1 },
    { k: "min_speech",  label: "最短有效语音",  min: 0.05, max: 1.5, step: 0.01, unit: "s", dec: 2 },
    { k: "max_speech",  label: "最长一句",      min: 2,   max: 30,  step: 0.5,  unit: "s", dec: 1 },
    { k: "end_silence", label: "句尾停顿确认",  min: 0.1, max: 2,   step: 0.01, unit: "s", dec: 2 },
    { k: "pre_roll",    label: "开口前预录",    min: 0,   max: 1,   step: 0.01, unit: "s", dec: 2 },
    { k: "duck",        label: "说话时压低原声", min: 0,  max: 1,   step: 0.02, unit: "×", dec: 2 },
    { k: "gain",        label: "播放音量",      min: 0,   max: 2,   step: 0.02, unit: "×", dec: 2 },
    { k: "rate_min",    label: "播放语速下限",      min: 0.5, max: 1.5, step: 0.01, unit: "×", dec: 2 },
    { k: "rate_max",    label: "播放语速上限",      min: 0.5, max: 2.0, step: 0.01, unit: "×", dec: 2 }
  ];
  // 延迟阀 / 窗口：用户自己拧，全部热生效（改一项下一段就按新阈值走）
  var LAT_PARAMS = [
    { k: "end_silence",    label: "句尾停顿确认（定稿阀）", min: 0.1, max: 1.5, step: 0.01, unit: "s", dec: 2 },
    { k: "spec_silence",   label: "投机静音（开口多快）",   min: 0.12, max: 0.6, step: 0.01, unit: "s", dec: 2 },
    { k: "min_speech",     label: "最短算一句",             min: 0.15, max: 2,   step: 0.01, unit: "s", dec: 2 },
    { k: "max_speech",     label: "最长一句（A 链）",        min: 4,    max: 30,  step: 0.5,  unit: "s", dec: 1 },
    { k: "max_speech_b",   label: "最长一句（B 链）",        min: 2.5,  max: 30,  step: 0.5,  unit: "s", dec: 1 },
    { k: "asr_spec_gap",   label: "投机识别间隔",            min: 0.4,  max: 8,   step: 0.1,  unit: "s", dec: 1 },
    { k: "asr_spec_timeout", label: "投机识别超时",          min: 1,    max: 20,  step: 0.5,  unit: "s", dec: 1 },
    { k: "cloud_wait",     label: "本地听串时等云端",        min: 0,    max: 8,   step: 0.1,  unit: "s", dec: 1 },
    { k: "a_cloud_wait",   label: "A 链云端优先等待",        min: 0,    max: 5,   step: 0.05, unit: "s", dec: 2 },
    { k: "chatbox_budget", label: "聊天框云端预算",          min: 0.5,  max: 15,  step: 0.5,  unit: "s", dec: 1 },
    { k: "refine_timeout", label: "精修超时",                min: 2,    max: 20,  step: 0.5,  unit: "s", dec: 1 },
    { k: "commit_chars",   label: "凑够几字就开口",          min: 1,    max: 40,  step: 1,    unit: "字", dec: 0 },
    { k: "partial_every",  label: "半截字幕刷新间隔（0=关）", min: 0,    max: 2,   step: 0.05, unit: "s", dec: 2 },
    { k: "speak_q_max",    label: "播放队列上限",            min: 1,    max: 8,   step: 1,    unit: "句", dec: 0 },
    { k: "valley_win",     label: "谷切回看窗口",            min: 0.8,  max: 8,   step: 0.1,  unit: "s", dec: 1 },
    { k: "valley_gap",     label: "谷要低多少 dB 才算谷",     min: 2,    max: 20,  step: 0.5,  unit: "dB", dec: 1 },
    { k: "active_ms",      label: "有效发声至少",            min: 40,   max: 1500, step: 10,  unit: "ms", dec: 0 },
    { k: "speech_ratio",   label: "占空比至少（挡咔哒）",     min: 0.02, max: 0.9, step: 0.01, unit: "", dec: 2 },
    { k: "spk_th",         label: "分人：同一人门限（余弦）",  min: 0.4,  max: 0.98, step: 0.01, unit: "", dec: 2 },
    { k: "spk_max",        label: "分人：最多几个人",          min: 2,    max: 12,  step: 1,    unit: "人", dec: 0 },
    { k: "spk_secs",       label: "分人：每段取前几秒",        min: 0.6,  max: 6,   step: 0.1,  unit: "s", dec: 1 }
  ];
  var HUD_PARAMS = [
    { k: "hud_alpha", label: "不透明度", min: 10, max: 100, step: 1, unit: "%", dec: 0 },
    { k: "hud_scale", label: "大小",     min: 60, max: 200, step: 5, unit: "%", dec: 0 },
    { k: "hud_rows",  label: "行数",     min: 1,  max: 16,  step: 1, unit: "行", dec: 0 }
  ];

  // 弹幕：大小/透明度/速度/泳道/条数/高度起点（样式是上面的下拉）
  var DK_PARAMS = [
    { k: "danmaku_scale", label: "弹幕大小",       min: 60,  max: 200, step: 5,   unit: "%", dec: 0 },
    { k: "danmaku_alpha", label: "不透明度",       min: 40,  max: 100, step: 1,   unit: "%", dec: 0 },
    { k: "danmaku_speed", label: "横穿整屏时间",   min: 2.5, max: 20,  step: 0.5, unit: "s", dec: 1 },
    { k: "danmaku_lanes", label: "泳道数",         min: 2,   max: 6,   step: 1,   unit: "道", dec: 0 },
    { k: "danmaku_max",   label: "同屏最多",       min: 1,   max: 10,  step: 1,   unit: "条", dec: 0 },
    { k: "danmaku_top",   label: "起点高度（屏高比例）", min: 0, max: 0.5, step: 0.01, unit: "", dec: 2 }
  ];

  function buildParams(host, list) {
    if (!host) return;
    host.innerHTML = list.map(function (p) {
      return '<div class="prow">' +
        '<div class="phead"><span>' + esc(p.label) + '</span><b data-for="' + p.k + '">–</b></div>' +
        '<input type="range" data-cfg="' + p.k + '" min="' + p.min + '" max="' + p.max + '" step="' + p.step + '">' +
        "</div>";
    }).join("");
  }

  function paramSpec(k) {
    var all = MAIN_PARAMS.concat(HUD_PARAMS).concat(LAT_PARAMS).concat(DK_PARAMS);
    for (var i = 0; i < all.length; i++) if (all[i].k === k) return all[i];
    return null;
  }

  function refreshParamVisual(el) {
    var k = el.dataset.cfg, spec = paramSpec(k);
    if (!spec) return;
    var v = Number(el.value);
    var pct = ((v - spec.min) / (spec.max - spec.min)) * 100;
    el.style.setProperty("--p", clamp(pct, 0, 100).toFixed(1) + "%");
    var lab = $('[data-for="' + k + '"]', el.parentNode);
    if (lab) setTx(lab, v.toFixed(spec.dec) + (spec.unit === "%" ? "%" : " " + spec.unit));
  }

  /* ------------------------------ 控件同步 ------------------------------ */
  function syncControls() {
    $$("[data-cfg]").forEach(function (el) {
      var k = el.dataset.cfg;
      if (!(k in st.cfg)) return;
      var v = st.cfg[k];
      if (el.type === "checkbox") { el.checked = !!v; return; }
      if (el.type === "range") {
        if (document.activeElement === el) return;
        el.value = String(v);
        refreshParamVisual(el);
        return;
      }
      if (document.activeElement === el) return;
      if (el.tagName === "SELECT") { pickOption(el, v); return; }
      el.value = String(v);
    });
    $$("[data-sw]").forEach(function (el) {
      var k = el.dataset.sw;
      if (!(k in st.cfg)) return;
      el.setAttribute("aria-pressed", st.cfg[k] ? "true" : "false");
    });
    renderMic();
    renderMaster();

    if (els.levelHint) {
      setTx(els.levelHint, "门限 A " + (st.cfg.mic_db != null ? Number(st.cfg.mic_db).toFixed(1) : "–") +
        " / B " + (st.cfg.loop_db != null ? Number(st.cfg.loop_db).toFixed(1) : "–") + " dB");
    }
    if (els.ocrX && document.activeElement !== els.ocrX) {
      var rg = st.cfg.ocr_region || [0, 0, 0, 0];
      els.ocrX.value = rg[0] | 0; els.ocrY.value = rg[1] | 0;
      els.ocrW.value = rg[2] | 0; els.ocrH.value = rg[3] | 0;
    }
    if ("speed" in st.cfg) {
      st.speedKb = true;
      st.speed = Number(st.cfg.speed) || 1;
      if (els.speed && document.activeElement !== els.speed) els.speed.value = String(st.speed);
      renderSpeed();
    }
    renderVoices();
    renderPersona();
    renderVoiceJob(st.voiceJob);
    renderChains();
  }

  function renderMic() {
    var on = !!st.micOn;
    var mi = st.mic || {};
    var reason = mi.reason ? String(mi.reason) : "";
    if (els.btnMic) {
      els.btnMic.dataset.on = on ? "1" : "0";
      setTx(els.micTxt, on ? "麦克风开" : "麦克风闭");
      els.btnMic.title = on ? "当前开麦，点击闭麦（等价 CapsLock）" : "当前闭麦，点击开麦（等价 CapsLock）";
    }
    if (els.capBadge) {
      if (st.demo) {
        // 离线预览：这个角标比「麦克风已闭」更重要，先占住它
        els.capBadge.hidden = false;
        els.capBadge.classList.add("pulsebadge");
        setTx(els.capBadge, "离线预览");
        els.capBadge.title = "没连上后端（/api/state 取不到）：这一页用的是内置假数据，" +
          "所有按钮都不会真的发请求，只写日志。";
      } else {
        els.capBadge.classList.remove("pulsebadge");
        els.capBadge.hidden = on;
        setTx(els.capBadge, "麦克风已闭");
        els.capBadge.title = "闭麦中：不采集你的声音，A 链暂停";
      }
    }

    // 打不开麦克风是真实会发生的状态，必须看得见（这是后端 mic.reason 里的原话）
    if (els.micWarn) {
      els.micWarn.hidden = !reason;
      if (reason) {
        setTx(els.micWarnTxt, "麦克风打不开：" + reason);
        els.micWarn.title = "后端 mic.reason：" + reason +
          "\n先换个输入设备，或看日志卡里的报错；A 链此时收不到声音。";
      }
    }
    if (els.sysMic) {
      var live = (mi.live === undefined || mi.live === null) ? null : !!mi.live;
      var dev = (mi.dev !== undefined && mi.dev !== null) ? " · 设备 " + mi.dev : "";
      setTx(els.sysMic, (on ? "开" : "闭") + (live === true ? " · 正在采音" :
        (live === false ? " · 没在采音" : "")) + (reason ? " · " + reason : "") + dev);
    }
  }

  function masterOn() { return !!(st.cfg.out_on || st.cfg.in_on); }
  function renderMaster() {
    if (!els.btnMaster) return;
    var on = masterOn();
    els.btnMaster.dataset.on = on ? "1" : "0";
    setTx(els.masterTxt, on ? "全部停止" : "开始转译");
    els.btnMaster.title = on ? "点击停止 A / B 两条链" : "点击开始 A / B 两条链";
  }

  function fillSelect(sel, list, cur) {
    if (!sel) return;
    if (!list || !list.length) { sel.innerHTML = '<option value="">— 没找到设备 —</option>'; return; }
    sel.innerHTML = list.map(function (d) {
      var tag = d.loop ? " ⟲" : "";
      var sr = d.sr ? (Math.round(d.sr / 100) / 10) + "k" : "";
      var meta = (d.ch || sr) ? " · " + (d.ch || "?") + "ch/" + sr : "";
      return '<option value="' + esc(d.i != null ? d.i : d.name) + '" data-name="' + esc(d.name) + '">' +
             esc(d.name) + tag + meta + "</option>";
    }).join("");
    pickOption(sel, cur);
  }

  // cfg 里的设备可能是「序号」也可能是「名字」，两种都要能选中；都匹配不上就补一个「（当前）」项，别让下拉空着
  function pickOption(sel, cur) {
    if (!sel) return;
    if (cur === undefined || cur === null || cur === "") { if (sel.options.length) sel.selectedIndex = 0; return; }
    var want = String(cur), hit = -1, i, op;
    for (i = 0; i < sel.options.length; i++) { if (sel.options[i].value === want) { hit = i; break; } }
    if (hit < 0) {
      for (i = 0; i < sel.options.length; i++) {
        op = sel.options[i];
        var nm = op.getAttribute("data-name") || "";
        if (nm && (nm === want || nm.indexOf(want) === 0 || want.indexOf(nm) === 0)) { hit = i; break; }
      }
    }
    if (hit < 0) {
      op = document.createElement("option");
      op.value = want; op.textContent = want + "（当前）";
      sel.appendChild(op); hit = sel.options.length - 1;
    }
    sel.selectedIndex = hit;
  }

  // 后端给的 dev 可能是序号，翻成设备名给人看
  function devName(d) {
    if (d === undefined || d === null || d === "") return "?";
    var all = (st.devices.output || []).concat(st.devices.input || [], st.devices.loop || []);
    for (var i = 0; i < all.length; i++) {
      if (String(all[i].i) === String(d)) return all[i].name + (all[i].loop ? " ⟲" : "");
    }
    return String(d);
  }

  // 设备下拉的取值：后端 cfg 存名字就发名字，存序号就发序号（不擅自改协议）
  function devValueFor(el, k) {
    var cur = st.cfg[k];
    var nameMode = cur != null && cur !== "" && !/^\d+$/.test(String(cur));
    var op = el.options[el.selectedIndex];
    if (nameMode && op) {
      var nm = op.getAttribute("data-name");
      if (nm) return nm;
    }
    return /^\d+$/.test(el.value) ? Number(el.value) : el.value;
  }

  function renderDevices() {
    fillSelect(els.selMicDev, st.devices.input, st.cfg.mic_dev);
    fillSelect(els.selOutDev, st.devices.output, st.cfg.out_dev);
    fillSelect(els.selLoopDev, st.devices.loop, st.cfg.loop_dev);
    if (els.selMonDev) fillSelect(els.selMonDev, st.devices.output, st.cfg.mon_dev);
    if (els.chkMonOn) els.chkMonOn.checked = st.cfg.mon_on !== false;
  }

  function voiceLabel(v, i) {
    var s = String(v.id || "") + " " + String(v.name || "");
    var m = /(?:^|[^a-z0-9])(t[1-9])(?:[^0-9]|$)/.exec(s);
    return m ? m[1] : ("t" + (i + 1));
  }
  function voiceShort(v, i) {
    var nm = String(v.name || v.id || "");
    var parts = nm.split(/[·|]/);
    return (parts.length > 1 ? parts[1] : parts[0]).trim() || ("音色 " + (i + 1));
  }

  function renderVoices() {
    if (!els.voiceChips) return;
    if (!st.voices || !st.voices.length) { els.voiceChips.innerHTML = '<span class="hint">暂无音色</span>'; return; }
    els.voiceChips.innerHTML = st.voices.map(function (v, i) {
      var on = String(v.id) === String(st.cfg.voice);
      var tip = (v.name || v.id) + (v.note ? " · " + v.note : "");
      return '<button class="vchip' + (on ? " on" : "") + '" data-voice="' + esc(v.id) + '" title="' + esc(tip) + '">' +
        "<b>" + esc(voiceLabel(v, i)) + "</b><span>" + esc(voiceShort(v, i)) + "</span>" +
        (v.custom ? '<em class="vdel" data-del="' + esc(v.id) + '" title="删掉这个克隆音色">×</em>' : "") +
        "</button>";
    }).join("");
    var cur = st.voices.filter(function (v) { return String(v.id) === String(st.cfg.voice); })[0];
    if (els.voiceNote) {
      setTx(els.voiceNote, cur ? (voiceLabel(cur, st.voices.indexOf(cur)) + " · " + voiceShort(cur, 0) + (cur.note ? " · " + cur.note : "")) : "未选");
    }
  }

  function renderSpeed() {
    if (els.speedVal) setTx(els.speedVal, Number(st.speed).toFixed(2) + "×");
    if (els.speed) {
      var pct = ((Number(st.speed) - 0.6) / (1.6 - 0.6)) * 100;
      els.speed.style.setProperty("--p", clamp(pct, 0, 100).toFixed(1) + "%");
    }
  }

  /* 人设卡：预设 chips + 自定义口吻（只影响语气） */
  function renderPersona() {
    if (!els.personaCards) return;
    var p = st.persona || {};
    var cards = p.cards || [];
    // 后端没连上（离线预览/刚启动）时也要有三张卡，否则这块看起来像坏了
    if (!cards.length) {
      cards = [{ id: "natural", name: "自然口语（默认）", note: "朋友间随性口吻" },
               { id: "teto", name: "テト · 活泼", note: "短句、语气跳" },
               { id: "polite", name: "敬语 · 正式", note: "です・ます" }];
    }
    els.personaCards.innerHTML = cards.map(function (c) {
      var on = String(c.id) === String(st.cfg.card || "natural");
      var nm = String(c.name || c.id);
      var parts = nm.split(/[·|]/);
      var head = (parts[0] || nm).trim();
      var tail = (parts[1] || c.note || "").trim();
      return '<button class="vchip' + (on ? " on" : "") + '" data-card="' + esc(c.id) + '" title="' + esc(c.note || nm) + '">' +
        "<b>" + esc(head) + "</b><span>" + esc(tail) + "</span></button>";
    }).join("");
    if (els.personaText && document.activeElement !== els.personaText && !st.personaDirty) {
      els.personaText.value = st.cfg.persona_custom || "";
    }
    updatePersonaHint();
  }
  function updatePersonaHint() {
    if (!els.personaHint || !els.personaText) return;
    setTx(els.personaHint, String(els.personaText.value.length) + " / 1200");
  }

  /* 专名表：只给 GGUF 档喂 vocabulary，压住「ロックマン→動画」这类音近乱写 */
  function renderVocab(list) {
    if (!els.ggufVocab) return;
    if (document.activeElement === els.ggufVocab || st.vocabDirty) return;
    var arr = (list && list.length !== undefined) ? list : (st.cfg.asr_gguf_vocab || []);
    els.ggufVocab.value = (arr || []).join(", ");
  }

  /* 克隆音色：录音 → 试听 → 上传训练 → 自动切过去（全程不离开这个页面） */
  var cloneT = null;
  function setClone(txt, state) {
    if (!els.cloneOut) return;
    setTx(els.cloneOut, txt);
    if (state) els.cloneOut.dataset.state = state;
    else delete els.cloneOut.dataset.state;
  }
  function setBar(pct) {
    if (els.cloneBar) els.cloneBar.style.width = clamp(pct, 0, 100).toFixed(1) + "%";
  }
  function renderVoiceJob(j) {
    if (!j) return;
    if (j.on) {
      var pct = j.tries_max ? (j.tries / j.tries_max) * 100 : 10;
      setBar(Math.max(6, pct));
      setClone((j.phase || "克隆中") + (j.tries ? " " + j.tries + "/" + j.tries_max : "") +
        (j.endpoint ? " · " + j.endpoint : ""), "warn");
      pollClone();
    } else if (j.phase === "完成") {
      setBar(100);
      setClone((j.msg || "完成") + (j.secs ? "（" + j.secs + " s）" : ""), "ok");
    } else if (j.phase === "失败") {
      setBar(0);
      setClone("失败：" + (j.err || "未知原因"), "err");
    }
  }
  function pollClone() {
    if (cloneT) return;
    cloneT = setInterval(function () {
      apiGet("/api/voice_clone").then(function (j) {
        if (!j) return;
        if (j.voices) { st.voices = j.voices; renderVoices(); }
        if (j.cur) st.cfg.voice = j.cur;
        if (!j.job) return;
        renderVoiceJob(j.job);
        if (!j.job.on) {
          clearInterval(cloneT); cloneT = null;
          apiGet("/api/state").then(applyState);
        }
      });
    }, 1000);
  }

  /* ------------------------------ 三链卡片 ------------------------------ */
  function engineText(chain, seg) {
    if (chain === "C") return "屏幕识别";
    var e = seg && seg.engine ? String(seg.engine) : "";
    if (e) {
      if (/whisper/.test(e)) return "本地+精修";
      if (/local/.test(e) && /cloud/.test(e)) return "本地+云端";
      if (/cloud/.test(e)) return "云端";
      if (/local/.test(e)) return "本地";
      return e;
    }
    var c = st.cfg.asr_engine || "hybrid";
    return c === "cloud" ? "云端" : (c === "local" ? "本地" : "本地+云端");
  }

  function setChainLive(chain, seg) {
    if (!seg) return;
    var prev = st.last[chain];
    if (prev && prev.id === seg.id) {
      st.last[chain] = Object.assign({}, prev, seg);
    } else if (prev && prev.ts && seg.ts && seg.ts < prev.ts) {
      return;
    } else {
      st.last[chain] = Object.assign({}, seg);
    }
    var m = st.last[chain];
    var box = els["live" + chain];
    if (box) {
      var ls = box.querySelector(".ls"), ld = box.querySelector(".ld");
      var src = m.src ? "[" + langName(m.src_lang) + "] " + m.src : "—";
      setTx(ls, src);
      if (ls) ls.title = m.src || "";
      setTx(ld, m.dst || (stateText(m.state) + "…"));
      if (ld) ld.title = m.dst || "";
      var busy = /^(asr|mt|tts|play)$/.test(m.state || "");
      box.className = "clive" + (busy ? " busy" : "") +
        ((m.state === "err" || m.state === "drop") ? " bad" :
         ((m.state === "done" || m.state === "refined") ? " ok" : ""));
    }
    var eng = els["eng" + chain];
    if (eng) setTx(eng, engineText(chain, m));
  }

  function renderChains() {
    ["A", "B", "C"].forEach(function (c) {
      var eng = els["eng" + c];
      if (eng) setTx(eng, engineText(c, st.last[c]));
      if (st.last[c] && els["live" + c]) setChainLive(c, st.last[c]);
    });
    renderOcrMode();
    renderOcrHint();
  }

  /* ---- C 链模式（单帧 / 常驻）、框选与单帧识别那几行反馈 ---- */
  // 后端没给 ocr_mode 时按 hotkey 显示：单帧是推荐用法，常驻是老的轮询
  function ocrMode() { return (st.cfg && st.cfg.ocr_mode) === "poll" ? "poll" : "hotkey"; }

  function ocrRange() {
    var r = (st.cfg && st.cfg.ocr_region) || [0, 0, 0, 0];
    return { x: Math.round(Number(r[0]) || 0), y: Math.round(Number(r[1]) || 0),
             w: Math.round(Number(r[2]) || 0), h: Math.round(Number(r[3]) || 0) };
  }
  function ocrFramed() { var g = ocrRange(); return g.w > 0 && g.h > 0; }

  function setOcrNote(text, kind) {
    st.ocrNote.text = text || "";
    st.ocrNote.kind = kind || "";
    var el = els.ocrNote;
    if (!el) return;
    el.textContent = st.ocrNote.text || "—";
    el.className = "ocrnote" + (st.ocrNote.kind ? " " + st.ocrNote.kind : "");
    el.title = st.ocrNote.text || "";
  }
  // 一闪而过的反馈（「识别到 3 行 · 123 ms」）：几秒后自动回到「范围 W×H」
  function flashOcrNote(text, kind, ms) {
    st.ocrNote.until = Date.now() + (ms || 9000);
    setOcrNote(text, kind);
  }

  function renderOcrNote() {
    var n = st.ocrNote;
    if (n.kind && n.until && Date.now() < n.until) {
      if (els.ocrNote && els.ocrNote.textContent !== (n.text || "—")) setOcrNote(n.text, n.kind);
      return;
    }
    n.kind = ""; n.until = 0;
    var g = ocrRange();
    if (!ocrFramed()) { setOcrNote("还没框选范围 · 按 小键盘 -（或点「框选范围」）", "warn"); return; }
    setOcrNote("范围 " + g.w + "×" + g.h + " · " + g.x + "," + g.y +
               (st.ocr && st.ocr.region_inferred ? "（自动推断）" : ""), "ok");
  }

  function renderOcrMode() {
    var m = ocrMode();
    if (els.ocrModes) {
      Array.prototype.forEach.call(els.ocrModes.querySelectorAll("[data-ocrmode]"), function (b) {
        b.setAttribute("aria-pressed", b.dataset.ocrmode === m ? "true" : "false");
      });
    }
    if (els.ocrSrc) {
      var v = (st.cfg && st.cfg.ocr_src) || "auto";
      if (els.ocrSrc.value !== v) els.ocrSrc.value = v;
    }
    renderOcrNote();
  }

  function renderOcrHint() {
    var el = els.hintC;
    if (!el) return;
    var on = !!st.cfg.ocr_on;
    var m = ocrMode();
    if (!on) {
      setTx(el, (m === "poll" ? "常驻识别已关" : "屏幕识别已关") + " · 按 F7 打开");
      el.className = "chint";
      el.title = "C 链关着，不读屏幕。";
      return;
    }
    if (!ocrFramed()) {
      setTx(el, "还没框选范围 · 按 小键盘 -");
      el.className = "chint warn";
      el.title = "后端在没框选时不再兜底抓全屏（之前会把桌面文字全当气泡）。" +
        "\n点 C 卡「框选范围」或按全局热键 小键盘 -，框住聊天框就恢复。";
      return;
    }
    if (m === "poll") {
      setTx(el, "常驻 · 轮询 " + (st.cfg.ocr_poll != null ? Number(st.cfg.ocr_poll).toFixed(2) : "0.35") +
        " s/帧 · F7 开关");
      el.title = "常驻模式每 0.35 s 抓一帧，聊天框不动时白费 CPU；要省就切「按一下识别一帧」。";
    } else {
      setTx(el, "单帧模式 · 小键盘 + 识别一帧 · 小键盘 - 改范围");
      el.title = "不常驻抓屏：按 小键盘 +（或点「识别一帧」）才抓一次。";
    }
    el.className = "chint ok";
  }

  // 右栏那块小黑板：把单帧结果的 src → dst 打出来
  function ocrRowsOut(j) {
    if (!els.ocrOut) return;
    var items = (j && j.items) || [];
    if (!items.length) { setTx(els.ocrOut, "这一帧没读到字（聊天框可能是空的）"); return; }
    var out = "耗时 " + fmtInt(j.ms) + " ms · " + items.length + " 行\n";
    for (var i = 0; i < items.length; i++) {
      out += "· " + (items[i].src || "?") + (items[i].dst ? "  →  " + items[i].dst : "") + "\n";
    }
    setTx(els.ocrOut, out.trim());
  }

  function runOcrOnce(btn, fromRight) {
    if (st.demo) {
      flashOcrNote("离线预览：识别到 2 行 · 118 ms", "ok", 12000);
      if (els.ocrOut) setTx(els.ocrOut,
        "离线预览：\n· 久しぶり！  →  好久不见！\n· 待って、ロード中  →  等一下，正在加载");
      log("info", "离线预览：单帧识别返回假数据");
      return;
    }
    if (btn && btn.disabled) return;                       // 防重复点
    var old = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "识别中…"; }
    if (!fromRight) flashOcrNote("识别中…", "busy", 60000);
    apiPost("/api/ocr/once", {}).then(function (j) {
      if (btn) { btn.disabled = false; btn.textContent = old; }
      if (!j) { flashOcrNote("识别失败 · 看右栏日志", "warn", 12000); return; }
      if (j.ok === false) {
        if (j.error === "no_region") flashOcrNote("还没框选范围 · 按 小键盘 -", "warn", 12000);
        else flashOcrNote("识别失败：" + (j.error || "未知错误"), "warn", 12000);
        log("err", "单帧识别失败：" + (j.error || JSON.stringify(j)));
        return;
      }
      var n = (j.items || []).length;
      flashOcrNote("识别到 " + n + " 行 · " + fmtInt(j.ms) + " ms", n ? "ok" : "", 12000);
      ocrRowsOut(j);
      log("info", "单帧识别 " + fmtInt(j.ms) + " ms / " + n + " 行（屏幕 → " + (st.cfg.ocr_src || "auto") + "）");
    });
  }

  function doPickRegion() {
    if (st.demo) { flashOcrNote("离线预览：框选要有后端才能用", "warn", 6000); return; }
    var b = els.btnPick;
    if (b && b.disabled) return;
    var old = b ? b.textContent : "";
    if (b) { b.disabled = true; b.textContent = "在屏幕上拖好按回车…"; }
    flashOcrNote("屏幕上已弹出遮罩：拖好范围按回车，Esc 取消", "busy", 62000);
    log("info", "框选范围：等你框住聊天框（回车确认 / Esc 取消）");
    apiPost("/api/region/pick", {}).then(function (j) {
      if (b) { b.disabled = false; b.textContent = old; }
      if (!j) { flashOcrNote("框选失败 · 看日志", "warn", 12000); return; }
      if (j.ok === false) {
        if (j.cancel) { flashOcrNote("框选已取消", "", 5000); log("info", "框选已取消"); }
        else { flashOcrNote("框选失败：" + (j.error || "未知"), "warn", 12000); log("err", "框选失败：" + (j.error || "")); }
        return;
      }
      // 后端返回的 region 已经是**物理像素**（它自己按 dpr 换算过），logical 是选区器原始值
      var r = j.region || [], lg = j.logical || null;
      var done = function () {
        if (r.length !== 4) { flashOcrNote("框选返回的数据看不懂", "warn", 12000); return; }
        flashOcrNote("框选完成 · 物理 " + Math.round(r[2]) + "×" + Math.round(r[3]) +
          (lg && lg.length === 4 ? "（逻辑 " + Math.round(lg[2]) + "×" + Math.round(lg[3]) + "）" : ""),
          "ok", 12000);
      };
      if (r.length === 4) log("info", "框选范围 → 物理 " + r.join(",") + (lg ? "（逻辑 " + lg.join(",") + "）" : ""));
      else log("err", "/api/region/pick 返回：" + JSON.stringify(j));
      renderOcrHint();
      // 后端已经把区域写进 cfg（物理像素），拉一次 state 对齐；拉完再把「框选完成」重新亮出来
      apiGet("/api/state").then(function (s) {
        if (s) applyState(s);
        done();
        renderOcrMode(); renderOcrHint();
      });
    });
  }

  /* ------------------------------ 电平 ------------------------------ */
  var LV_MIN = -60;

  function setLevel(k, db, gate) {
    if (db === null || db === undefined || isNaN(db)) return;
    st.levels[k] = db;
    var pct = clamp(((db - LV_MIN) / (0 - LV_MIN)) * 100, 0, 100);
    var fill = k === "mic" ? els.fillMic : els.fillLoop;
    var lab = k === "mic" ? els.dbMic : els.dbLoop;
    var g = k === "mic" ? els.gateMic : els.gateLoop;
    if (!fill) return;
    fill.style.width = pct.toFixed(1) + "%";
    var open = (gate === null || gate === undefined) ? false : db > Number(gate);
    fill.classList.toggle("open", open);
    fill.classList.toggle("hot", db > -20);
    if (lab) {
      setTx(lab, db <= -69 ? "–∞ dB" : db.toFixed(1) + " dB");
      lab.classList.toggle("open", open);
    }
    if (g && gate !== null && gate !== undefined) {
      g.style.left = clamp(((Number(gate) - LV_MIN) / (0 - LV_MIN)) * 100, 0, 100).toFixed(2) + "%";
    }
  }

  /* ------------------------------ 延迟 / 丢段 ------------------------------ */
  function renderSpk() {
    var el = document.getElementById("spkStat");
    if (!el) return;
    var k = st.spk || {};
    if (!st.cfg || st.cfg.spk_on === false) {
      el.textContent = "分人：关着";
      el.title = "关掉就不算向量（省 ~100 ms CPU/段），字幕前不会标号";
      return;
    }
    if (k.err) { el.textContent = "分人：模型没就位"; el.title = k.err; return; }
    if (!k.ready) { el.textContent = "分人：待命"; el.title = "第一次有声音时才载入模型（84 MB）"; return; }
    el.textContent = "分人：" + (k.clusters || 0) + " 人 · " + (k.seen || 0) + " 段 · " +
      fmtInt(k.ms_p50) + " ms";
    el.title = "簇人数：" + JSON.stringify(k.counts || []) +
      "\n门限 " + k.th + " · 上限 " + k.max + " 人" +
      "\n新声音 " + (k.new || 0) + " 次 · 失败 " + (k.fail || 0) + " · 丢弃 " + (k.dropped || 0);
  }

  function renderMetrics() {
    var A = st.metrics.A || {}, B = st.metrics.B || {}, C = st.metrics.C || {};
    var p50 = A.e2e_p50, p90 = A.e2e_p90;
    setTx(els.p50, fmtInt(p50));
    setTx(els.p90, fmtInt(p90));
    if (els.p50) els.p50.classList.toggle("hi", p50 > 1800);
    if (els.p90) els.p90.classList.toggle("hi", p90 > 2400);
    if (els.latB) {
      setTx(els.latB, "B 链 P50 " + fmtInt(B.e2e_p50) + " · P90 " + fmtInt(B.e2e_p90));
      els.latB.title = "B 链端到端（对方说完 → 我看到字幕）：P50 " + fmtInt(B.e2e_p50) +
        " / P90 " + fmtInt(B.e2e_p90) + " ms · 共 " + fmtInt(B.n || 0) + " 句";
    }
    if (els.mtHint) {
      setTx(els.mtHint, "P50 · A " + fmtInt(A.n || 0) + " / B " + fmtInt(B.n || 0) +
        " / C " + fmtInt(C.n || 0) + " 句");
    }
    if (!els.mtab) return;
    var rows = [{ c: "a", name: "A", d: A }, { c: "b", name: "B", d: B }, { c: "c", name: "C", d: C }];
    var e2e = rows.map(function (r) {
      return '<div class="me2erow ' + r.c + '"><span>' + r.name + " 端到端</span><b>" +
        fmtInt(r.d.e2e_p50) + " / " + fmtInt(r.d.e2e_p90) + " ms</b></div>";
    }).join("");
    els.mtab.innerHTML =
      '<div class="mhead"><span>链</span><span>识别</span><span>翻译</span><span>合成</span></div>' +
      rows.map(function (r) {
        return '<div class="mrow ' + r.c + '">' +
          '<span class="mname">' + r.name + "</span>" +
          '<div class="mcell"><b>' + fmtInt(r.d.asr_p50) + "</b><span>ms</span></div>" +
          '<div class="mcell"><b>' + fmtInt(r.d.mt_p50) + "</b><span>ms</span></div>" +
          '<div class="mcell"><b>' + (r.d.tts_p50 ? fmtInt(r.d.tts_p50) : "–") + "</b><span>ms</span></div>" +
          "</div>";
      }).join("") +
      '<div class="me2e">' + e2e + "</div>" +
      '<div class="mopen" id="openMs">' + esc(st.openTxt || "起话→开口：还没数据") + "</div>";
  }

  // 指标最全的地方是 /api/metrics（带 A/B/C 的 P50/P90 + 每句 open_ms）：
  // 定时拉一次，独立于 SSE——st 事件不来的时候顶栏那两个大数字就不会僵在 0。
  function applyMetrics(j) {
    if (!j) return;                      // 附属指标，取不到就当没发生
    if (j.A || j.B || j.C) {
      var cur = st.metrics || {};
      st.metrics = {
        A: j.A || cur.A || {}, B: j.B || cur.B || {}, C: j.C || cur.C || {}
      };
    }
    var rows = j.rows || [];
    var ops = rows.map(function (r) { return Number(r.open_ms); })
      .filter(function (v) { return isFinite(v) && v > 0; });
    if (!ops.length) {
      if (!st.openTxt) st.openTxt = "起话→开口：还没数据（要真说一句）";
    } else {
      ops.sort(function (a, b) { return a - b; });
      var p50 = ops[Math.floor((ops.length - 1) * 0.5)];
      st.openTxt = "起话→开口 P50 " + fmtInt(p50) + " ms · 最慢 " + fmtInt(ops[ops.length - 1]) +
        " ms · 最近 " + ops.length + " 句";
    }
    renderMetrics();
  }

  function pollMetrics() {
    if (st.demo) return;
    fetch(api("/api/metrics"), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .catch(function () { return null; })
      .then(applyMetrics);
  }

  var metricsTimer = null;
  function startMetricsPoll() {
    if (st.demo || metricsTimer) return;
    metricsTimer = setInterval(pollMetrics, 2500);
  }

  // 句子落定后节流补一刀，让「起话→开口」跟上表里刚出现的那一行
  function refreshOpen() {
    if (st.demo) return;
    clearTimeout(st.openT);
    st.openT = setTimeout(pollMetrics, 900);
  }

  function renderDrop() {
    setTx(els.dropN, String(st.drop));
    if (els.dropWrap) els.dropWrap.classList.toggle("bad", st.drop > 0);
  }

  /* ------------------------------ 句子表 ------------------------------ */
  function createRow(seg) {
    var tr = document.createElement("tr");
    tr.className = "c" + (seg.chain || "A");
    tr.dataset.id = seg.id;
    var cells = {};
    var defs = [
      ["t", "t"], ["chain", ""], ["src", "src"], ["dst", "dst"],
      ["asr", "num"], ["mt", "num"], ["tts", "num"],
      ["dur", "num"], ["rtf", "num"], ["note", "note"]
    ];
    defs.forEach(function (d) {
      var td = document.createElement("td");
      if (d[1]) td.className = d[1];
      if (d[0] === "chain") td.innerHTML = '<span class="chainpill">' + esc(seg.chain || "?") + "</span>";
      if (d[0] === "note") td.innerHTML = '<span class="note"><i class="st"></i><span class="notetx"></span></span>';
      tr.appendChild(td);
      cells[d[0]] = td;
    });
    return { tr: tr, cells: cells, data: {} };
  }

  function updateRow(rec, seg, isNew) {
    var d = rec.data;
    for (var k in seg) if (Object.prototype.hasOwnProperty.call(seg, k) && seg[k] !== undefined) d[k] = seg[k];
    var c = rec.cells, tr = rec.tr;

    tr.className = "c" + (d.chain || "A") + " st-" + (d.state || "asr");
    setTx(c.t, fmtClock(d.ts));
    setTx(c.src, d.src || "…");
    setTx(c.dst, d.dst || "");
    if (c.src.title !== (d.src || "")) c.src.title = d.src || "";
    if (c.dst.title !== (d.dst || "")) c.dst.title = d.dst || "";
    setTx(c.asr, fmtInt(d.asr_ms));
    setTx(c.mt, fmtInt(d.mt_ms));
    setTx(c.tts, d.tts_ms ? fmtInt(d.tts_ms) : "–");
    setTx(c.dur, fmtSec(d.audio_ms));
    setTx(c.rtf, fmtRtf(d.rtf));
    if (c.rtf) c.rtf.style.color = (d.rtf > 1 ? "#fca5a5" : (d.rtf > 0 ? "#8ee6c8" : ""));

    var chip = c.note.querySelector(".st");
    setTx(chip, stateText(d.state));
    chip.className = "st " + (d.state || "asr");
    var ntx = c.note.querySelector(".notetx");
    var note = noteText(d.note) || (d.echo ? "回声" : "");
    setTx(ntx, note);
    ntx.classList.toggle("echo", !!d.echo || /回声/.test(note));

    var pill = c.chain.querySelector(".chainpill");
    setTx(pill, d.chain || "?");

    if (!isNew) {
      tr.classList.remove("flash");
      void tr.offsetWidth;
      tr.classList.add("flash");
    }
    applyRowFilter(rec);
  }

  function applyRowFilter(rec) {
    var show = st.filter === "all" || rec.data.chain === st.filter;
    if (rec.tr.hidden === show) rec.tr.hidden = !show;
  }

  function onSeg(seg) {
    if (!seg || !seg.id) return;
    var rec = st.segs.get(seg.id);
    var isNew = !rec;
    var prevState = rec ? rec.data.state : null;
    if ((seg.state === "drop" || seg.state === "err") && prevState !== seg.state) {
      st.drop++;
      renderDrop();
    }
    if (isNew) {
      rec = createRow(seg);
      st.segs.set(seg.id, rec);
      var body = els.segBody;
      body.insertBefore(rec.tr, body.firstChild);
      while (body.childElementCount > MAX_ROWS) {
        var last = body.lastElementChild;
        if (!last) break;
        if (last.dataset.id) st.segs.delete(last.dataset.id);
        body.removeChild(last);
      }
    }
    updateRow(rec, seg, isNew);
    if (seg.chain) setChainLive(seg.chain, seg);
    if (seg.state === "done" || seg.state === "refined") refreshOpen();
    if (els.empty) els.empty.hidden = st.segs.size > 0;
    setTx(els.segCount, st.segs.size + " 句");
  }

  function applyFilter() {
    $$("#filters .chip").forEach(function (c) { c.classList.toggle("on", c.dataset.f === st.filter); });
    st.segs.forEach(applyRowFilter);
  }

  function clearRows(note) {
    els.segBody.innerHTML = "";
    st.segs.clear();
    setTx(els.segCount, "0 句");
    if (els.empty) els.empty.hidden = false;
    if (note) log("info", note);
  }

  /* ------------------------------ 事件分发 ------------------------------ */
  function handleEvent(raw) {
    var m;
    try { m = JSON.parse(raw); } catch (e) { return; }
    if (!m) return;
    if (!m.k && m.id) { onSeg(m); return; }

    switch (m.k) {
      case "hb": break;
      case "level":
        setLevel("mic", m.mic, st.cfg.mic_db);
        setLevel("loop", m.loop, st.cfg.loop_db);
        break;
      case "seg": onSeg(m.seg || m); break;
      case "log": log(m.lv || "info", m.msg || ""); break;
      case "mic":
        st.micOn = !!m.on;
        st.mic.on = st.micOn;
        if (m.live !== undefined) st.mic.live = !!m.live;
        if (m.reason !== undefined) st.mic.reason = m.reason ? String(m.reason) : "";
        if ("mic_on" in st.cfg) st.cfg.mic_on = st.micOn;
        renderMic();
        log(st.mic.reason ? "err" : "info", st.micOn
          ? ("麦克风已打开" + (st.mic.reason ? "，但打不开：" + st.mic.reason : ""))
          : "麦克风已闭（CapsLock）");
        break;
      case "tts_test":
        if (els.ttsOut) {
          setTx(els.ttsOut, fmtInt(m.ms) + " ms · 首包 " + fmtInt(m.first_pkt_ms) +
            " · " + (m.audio_s != null ? Number(m.audio_s).toFixed(2) : "–") + " s");
        }
        break;
      case "st": {
        var s = m.st || m;
        if (s.cfg) { st.cfg = Object.assign({}, st.cfg, s.cfg); syncControls(); }
        if (s.metrics) { st.metrics = s.metrics; renderMetrics(); }
        if (s.hud) { st.hud = s.hud; renderHudInfo(); }
        if (s.mic) { st.mic = Object.assign({}, st.mic, s.mic); st.micOn = !!s.mic.on; renderMic(); }
        else if (s.mic_on !== undefined) { st.micOn = !!s.mic_on; renderMic(); }
        break;
      }
      default:
        if (m.seg) onSeg(m.seg);
        break;
    }
  }

  /* 事件通道：一条就够。
     浏览器对同一个 host 只给 6 条连接；控制台开两三个标签页、每个标签页再挂一条
     EventSource，排不上的那条就一直「重连中…」（本机实测：7 条 established 每 12 秒
     全部换新端口，就是重连风暴）。所以：① 多标签页只留一条长连接，其余靠
     BroadcastChannel 转发；② 断线退避 1.5→3→6→10 s，不再每 2 秒死磕。 */
  var SSECH = null;
  try { if (typeof BroadcastChannel !== "undefined") SSECH = new BroadcastChannel("vrdub-sse"); }
  catch (e0) { SSECH = null; }
  var TAB_ID = Math.random().toString(36).slice(2);
  var sseLeader = false, sseFails = 0, sseLastEv = 0, leaderTimer = 0, followTimer = 0;

  function sseBroadcast(msg) {
    if (!SSECH) return;
    try { SSECH.postMessage(msg); } catch (e) {}
  }

  function connectSSE() {
    if (st.demo) return;
    disconnectSSE();
    sseLeader = false;
    st.follow = false;
    if (!SSECH) { openSSE(); return; }
    leaderTimer = setTimeout(function () {          // 250 ms 内没人应声就我来当主
      sseLeader = true;
      openSSE();
      sseBroadcast({ t: "leader", id: TAB_ID });
    }, 250);
    sseBroadcast({ t: "who", id: TAB_ID });
  }

  if (SSECH) {
    SSECH.onmessage = function (e) {
      var m = e.data || {};
      if (m.id === TAB_ID) return;
      if (m.t === "who") { if (sseLeader) sseBroadcast({ t: "leader", id: TAB_ID }); return; }
      if (m.t === "leader" && !sseLeader) {         // 已经有标签页连着后端
        clearTimeout(leaderTimer);
        st.follow = true;
        sseLastEv = Date.now();
        setConn("ok", liveText() + " · 共用事件通道");
        return;
      }
      if (m.t === "ev" && !sseLeader) {
        clearTimeout(leaderTimer);
        st.follow = true;
        sseLastEv = Date.now();
        handleEvent(m.d);
        setConn("ok", liveText() + " · 共用事件通道");
      }
    };
  }

  function openSSE() {
    var es;
    try { es = new EventSource(api("/api/events")); }
    catch (e) { log("err", "事件通道建立失败：" + e.message); return; }
    st.sse = es;
    es.onopen = function () {
      sseFails = 0;
      stopPollFallback();
      setConn("ok", liveText());
      log("info", "事件通道已连接");
    };
    es.onmessage = function (ev) {
      sseLastEv = Date.now();
      handleEvent(ev.data);
      if (sseLeader) sseBroadcast({ t: "ev", id: TAB_ID, d: ev.data });
    };
    es.onerror = function () {
      var d = Math.min(fbTimer ? 30000 : 10000, 1500 * Math.pow(2, Math.min(4, sseFails)));
      sseFails++;
      if (sseFails === 1) report("事件通道断开", "ready=" + es.readyState + " tab=" + TAB_ID);
      else if (sseFails === 3) {
        report("事件通道连断三次，转轮询", "ready=" + es.readyState + " tab=" + TAB_ID);
        startPollFallback();          // 事件通道废了也别让控制台变成「一直重连」
      }
      setConn("warn", fbTimer ? "轮询模式（事件通道连不上）" : "重连中…（" + Math.round(d / 1000) + " s）");
      if (sseFails === 1 || sseFails % 5 === 0) {
        log("warn", "事件通道断开第 " + sseFails + " 次，" + Math.round(d / 1000) + " 秒后重试");
      }
      try { es.close(); } catch (e2) {}
      st.sse = null;
      clearTimeout(st.retryT);
      st.retryT = setTimeout(connectSSE, d);
    };
  }

  function disconnectSSE() {
    clearTimeout(leaderTimer);
    if (st.sse) { try { st.sse.close(); } catch (e) {} st.sse = null; }
    clearTimeout(st.retryT);
  }

  /* 兜底：事件通道连断 3 次就切成 1.5 s 轮询 /api/state。
     界面照常用（段、指标、设备都能刷），只是不再是逐条推送。 */
  var fbTimer = 0;
  function startPollFallback() {
    if (fbTimer) return;
    log("warn", "事件通道连不上，改用 1.5 秒轮询：控制台功能不变，状态刷新慢一点");
    fbTimer = setInterval(function () {
      fetch(api("/api/state"), { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (s) { st.pollOk = true; applyState(s); setConn("warn", "轮询模式（事件通道连不上）"); })
        .catch(function () { setConn("err", "后端没响应"); });
    }, 1500);
  }
  function stopPollFallback() {
    if (!fbTimer) return;
    clearInterval(fbTimer);
    fbTimer = 0;
  }

  // 主的标签页被关掉后，跟随者 20 s 内收不到事件就重新选主
  followTimer = setInterval(function () {
    if (st.demo || sseLeader) return;
    if (st.follow && Date.now() - sseLastEv > 20000) { st.follow = false; connectSSE(); }
  }, 5000);

  /* ------------------------------ 全量状态 ------------------------------ */
  function applyState(s) {
    if (!s) return;
    if (s.url) st.base = String(s.url).replace(/\/+$/, "");
    st.cfg = Object.assign({}, st.cfg, s.cfg || {});
    if (s.devices) st.devices = s.devices;
    if (s.voices) st.voices = s.voices;
    if (s.persona) st.persona = s.persona;
    st.vocabFromState = (s.cfg && s.cfg.asr_gguf_vocab) || [];
    if (s.voice_job) st.voiceJob = s.voice_job;
    if (s.metrics) st.metrics = s.metrics;
    if (s.hud) st.hud = s.hud;
    if (s.danmaku) st.danmaku = s.danmaku;
    if (s.spk) st.spk = s.spk;
    if (!("danmaku" in st.cfg)) st.cfg.danmaku = false;
    if (s.mic) st.mic = Object.assign({}, st.mic, s.mic);
    if (s.ocr) st.ocr = Object.assign({}, st.ocr, s.ocr);
    if (s.asr) st.asr = Object.assign({}, st.asr, s.asr);
    st.micOn = (st.mic && st.mic.on !== undefined) ? !!st.mic.on : !!st.cfg.mic_on;
    st.pid = s.pid || 0;
    st.ver = s.ver || "";
    st.uptime0 = s.uptime_s || 0;
    st.upAt = Date.now();

    renderDevices();
    syncControls();
    renderMetrics();
    renderSpk();
    renderHudInfo();
    renderSys();
    renderAsr();

    var last = s.last || [];
    last.slice().sort(function (a, b) { return (a.ts || 0) - (b.ts || 0); })
      .forEach(function (seg) { onSeg(seg); });
    renderChains();
    renderVocab(st.vocabFromState);
  }

  /* 识别引擎徽标：A/B 各一格（本地/云端），title 里带精修模型现状 */
  var ASR_TXT = { hybrid: "本地+云", local: "本地", cloud: "云端",
    qwen17: "Qwen1.7B", qwen06: "Qwen0.6B", fun: "FunASR" };
  var GGUF_IDS = { qwen17: 1, qwen06: 1, fun: 1 };
  /* GGUF 档的驻留说明：同一时刻只有一个模型在显存里，徽标上直接写清「是不是它、占多少」
     ——换档要 2–4 s 载入，看见「待载入」就知道下一句会慢一点。 */
  function ggufNote(a, eid) {
    if (!GGUF_IDS[eid]) return "";
    var g = a.gguf || {};
    if (g.err) return " · 载入失败：" + g.err;
    if (!g.key) return " · 待载入";
    if (g.key !== eid) return " · 现驻留 " + (ASR_TXT[g.key] || g.key);
    return " · " + Math.round(g.vram_mb || 0) + " MiB";
  }
  function renderAsr() {
    var a = st.asr || {};
    if (els.engA) {
      setTx(els.engA, (ASR_TXT[a.a] || "–") + ggufNote(a, a.a));
      els.engA.title = "A 链识别：" + (ASR_TXT[a.a] || "–") + ggufNote(a, a.a);
    }
    if (els.engB) {
      setTx(els.engB, (ASR_TXT[a.b] || "–") + ggufNote(a, a.b));
      var ja = a.ja || {};
      var rf = a.refine === "off" ? "关闭"
        : (ja.device && ja.device !== "-" ? ("本地 " + ja.device) : (a.refine === "cloud" ? "云端" : "云端（本地待命）"));
      els.engB.title = "B 链识别：" + (ASR_TXT[a.b] || "–") + ggufNote(a, a.b) +
        " · 精修：" + rf + (ja.note ? " · " + ja.note : "");
      els.engB.classList.toggle("ok", !!(ja.device && ja.device !== "-"));
    }
  }

  function renderSys() {
    setTx(els.sysPid, st.pid ? String(st.pid) : "–");
    setTx(els.sysVer, st.ver || "–");
    /* GGUF 档：显存里现在是谁、吃了多少；没驻留就报「装了哪几个」。
       换档有 2–4 s 载入，用户看见「未驻留」就知道下一句会慢一点。 */
    if (els.sysGguf) {
      var aa = st.asr || {}, g = aa.gguf || {};
      var cat = aa.gguf_models || [];
      var have = cat.filter(function (m) { return m.present; })
        .map(function (m) { return ASR_TXT[m.id] || m.id; });
      var txt = g.key
        ? ((ASR_TXT[g.key] || g.key) + " 驻留 " + Math.round(g.vram_mb || 0) + " MiB")
        : ("未驻留 · 已装 " + (have.length ? have.join(" / ") : "无"));
      setTx(els.sysGguf, txt);
      els.sysGguf.title = cat.map(function (m) {
        return m.name + (m.present ? " · " + m.mb + " MB" : " · 未下载") +
          " · 显存约 " + m.vram_mb + " MiB · " + m.backend;
      }).join("\n") || "没有可选档";
    }
  }
  function renderHudInfo() {
    var h = st.hud || {};
    var alive = h.alive === undefined ? !!h.on : !!h.alive;
    setTx(els.sysHud, alive ? ("运行中 · pid " + (h.pid || "?")) : (st.cfg.hud_on ? "已启动未就绪" : "关闭"));
    // 弹幕进程：后端有 st.danmaku 就用它，没有就按开关状态说
    if (els.sysDanmaku) {
      var d = st.danmaku || st.hud_danmaku || null;
      var dOn = d ? (d.alive === undefined ? !!d.on : !!d.alive) : !!st.cfg.danmaku;
      setTx(els.sysDanmaku,
        d && d.pid ? ("运行中 · pid " + d.pid)
          : (dOn ? "已开启（进程未就绪）" : "关闭"));
    }
    if (els.danmHint) {
      var on = !!st.cfg.danmaku;
      setTx(els.danmHint, on
        ? "全屏透明 · 鼠标穿透 · 上 1/4 区域 · 最多 6 条 · A 青 / B 紫 / C 橙"
        : "全屏透明 · 鼠标穿透 · 上 1/4 区域 · 最多 6 条");
    }
  }

  /* ------------------------------ 演示模式 ------------------------------ */
  var DEMO_LIB = [
    { chain: "A", src: "今天有点迟到了，路上堵车", dst: "今日はちょっと遅れちゃった、道が渋滞してて", src_lang: "zh", dst_lang: "ja" },
    { chain: "B", src: "ちょっと待って、今行くから", dst: "等一下，我马上过去", src_lang: "ja", dst_lang: "zh" },
    { chain: "A", src: "你那边现在几点", dst: "そっちは今何時？", src_lang: "zh", dst_lang: "ja" },
    { chain: "C", src: "[聊天框] teto: 久しぶり！元気？", dst: "[聊天框] teto：好久不见！还好吗？", src_lang: "ja", dst_lang: "zh" },
    { chain: "B", src: "そのワールド、重くない？", dst: "那个世界不卡吗？", src_lang: "ja", dst_lang: "zh" },
    { chain: "A", src: "这个副本我们上周打过了", dst: "このレイド、先週やったよね", src_lang: "zh", dst_lang: "ja" },
    { chain: "B", src: "I will be there in five minutes", dst: "我五分钟就到", src_lang: "en", dst_lang: "zh" },
    { chain: "A", src: "等一下，我先调一下麦", dst: "ちょっと待って、先にマイク調整する", src_lang: "zh", dst_lang: "ja" },
    { chain: "C", src: "[聊天框] kira: 待って、ロード中…", dst: "[聊天框] kira：等等，加载中…", src_lang: "ja", dst_lang: "zh" },
    { chain: "A", src: "你声音有点小，能大一点吗", dst: "声が小さいかも、もう少し大きくできる？", src_lang: "zh", dst_lang: "ja" },
    { chain: "B", src: "この曲いいね、誰の？", dst: "这歌不错，谁的？", src_lang: "ja", dst_lang: "zh" },
    { chain: "A", src: "别走传送门，我们先去集合点", dst: "ポータル行かないで、先に集合場所に行こう", src_lang: "zh", dst_lang: "ja" }
  ];

  function demoCfg() {
    return {
      mode: "trans", dst_lang: "ja", src_lang: "auto", in_lang: "auto", sub_lang: "zh",
      voice: "qwen-audio-3.0-tts-plus-t3", card: "teto",
      out_on: true, in_on: true, ocr_on: false, mic_on: true,
      mic_dev: 22, out_dev: 19, loop_dev: 25,
      mic_db: -32, loop_db: -24,
      min_speech: 0.35, max_speech: 12, end_silence: 0.5, pre_roll: 0.25,
      duck: 0, gain: 1,
      asr_engine: "hybrid", asr_race: false, presynth: true, echo_guard: true,
      hud_on: true, hud_alpha: 92, hud_scale: 100, hud_rows: 8, hud_click: false, danmaku: false,
      hud_x: -1, hud_y: -1, ocr_region: [0, 0, 0, 0],
      rate_min: 0.9, rate_max: 1.15
    };
  }

  function demoDevices() {
    return {
      input: [
        { i: 22, name: "麦克风阵列 (Realtek(R) Audio)", loop: false, ch: 2, sr: 48000 },
        { i: 23, name: "Rapoo Gaming Headset 麦克风", loop: false, ch: 2, sr: 48000 }
      ],
      output: [
        { i: 19, name: "CABLE Input (VB-Audio Virtual Cable)", loop: false, ch: 2, sr: 48000 },
        { i: 21, name: "扬声器 (Rapoo Gaming Headset)", loop: false, ch: 2, sr: 48000 }
      ],
      loop: [
        { i: 25, name: "扬声器 (Rapoo Gaming Headset) [Loopback]", loop: true, ch: 2, sr: 48000 },
        { i: 26, name: "CABLE Output (VB-Audio Virtual Cable) [Loopback]", loop: true, ch: 2, sr: 48000 }
      ]
    };
  }

  function demoVoices() {
    return [
      { id: "qwen-audio-3.0-tts-plus-t1", name: "t1 · 温柔", note: "语速偏慢" },
      { id: "qwen-audio-3.0-tts-plus-t2", name: "t2 · 明亮", note: "谱包络 cos 0.9512" },
      { id: "qwen-audio-3.0-tts-plus-t3", name: "t3 · 最像", note: "谱包络 cos 0.9693" },
      { id: "qwen-audio-3.0-tts-plus-t4", name: "t4 · 沉稳", note: "适合长时间说话" }
    ];
  }

  function demoMetrics() {
    return {
      A: { n: 20, e2e_p50: 1420, e2e_p90: 2100, asr_p50: 130, mt_p50: 320, tts_p50: 1020 },
      B: { n: 12, e2e_p50: 900, e2e_p90: 1400, asr_p50: 700, mt_p50: 300, tts_p50: 0 }
    };
  }

  function demoHistory() {
    var out = [], now = Date.now() / 1000;
    for (var i = 13; i >= 0; i--) {
      var lib = DEMO_LIB[(i * 5) % DEMO_LIB.length];
      var asr = 90 + Math.round(rnd(120));
      var mt = 240 + Math.round(rnd(180));
      var tts = lib.chain === "A" ? 780 + Math.round(rnd(420)) : 0;
      var dur = 1200 + Math.round(rnd(2600));
      var e2e = asr + mt + tts + 60 + Math.round(rnd(260));
      out.push({
        id: lib.chain + "-" + (70 + i), chain: lib.chain,
        ts: now - (14 - i) * 7.4 - rnd(2),
        state: i % 9 === 4 ? "refined" : "done",
        src: lib.src, dst: lib.dst, src_lang: lib.src_lang, dst_lang: lib.dst_lang,
        asr_ms: asr, mt_ms: mt, tts_ms: tts, audio_ms: dur, e2e_ms: e2e,
        rtf: tts ? +(dur / e2e).toFixed(2) : 0,
        engine: lib.chain === "C" ? "ocr" : (i % 4 === 0 ? "local+cloud" : "local"),
        note: i % 7 === 3 ? "预合成命中" : (i % 11 === 6 ? "本地草稿" : ""),
        echo: i === 5, speaker: null
      });
    }
    return out;
  }

  function enterDemo(reason) {
    st.demo = true;
    destroyDemoTimers();
    setBadge(true);
    setConn("demo", "离线预览");
    log("warn", "进入演示模式：" + reason);
    log("info", "界面用内置假数据渲染，所有写操作不会真的发出");

    applyState({
      ok: true, ver: "1.0", pid: 0, uptime_s: 137.4,
      cfg: demoCfg(), devices: demoDevices(), voices: demoVoices(),
      metrics: demoMetrics(), hud: { on: true, pid: 4321, alive: true },
      mic: { on: true, live: true, dev: 24, reason: "" },
      last: demoHistory()
    });
    renderSys();
    setTx(els.sysPid, "—（离线）");

    st.demoT0 = performance.now();
    st.demoTimers.push(setInterval(demoLevels, 100));
    st.demoTimers.push(setInterval(demoSpawn, 2900));
    st.demoTimers.push(setTimeout(demoSpawn, 900));
    st.demoTimers.push(setInterval(function () {
      log("info", "演示心跳 · 电平 " + st.levels.mic.toFixed(1) + " dB");
    }, 24000));
  }

  function destroyDemoTimers() {
    st.demoTimers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
    st.demoTimers = [];
  }

  function env(t, period, duty) {
    var p = t % period;
    return p < duty ? Math.sin(Math.PI * (p / duty)) : 0;
  }

  function demoLevels() {
    var t = (performance.now() - st.demoT0) / 1000;
    var e1 = env(t, 9.5, 4.2), e2 = env(t + 3.1, 11.4, 5.0);
    var mic = -58 + 46 * e1 + (Math.random() * 5 - 2.5);
    var loop = -58 + 40 * e2 + (Math.random() * 5 - 2.5);
    if (e1 <= 0) mic = -67 + Math.random() * 5;
    if (e2 <= 0) loop = -67 + Math.random() * 5;
    setLevel("mic", clamp(mic, -70, 0), st.cfg.mic_db);
    setLevel("loop", clamp(loop, -70, 0), st.cfg.loop_db);
  }

  function demoSpawn() {
    var lib = DEMO_LIB[st.demoN % DEMO_LIB.length];
    st.demoN++;
    var id = lib.chain + "-" + (100 + st.demoN);
    var asr = lib.chain === "C" ? 310 + Math.round(rnd(90)) : 100 + Math.round(rnd(90));
    var mt = 240 + Math.round(rnd(180));
    var tts = lib.chain === "A" ? 820 + Math.round(rnd(380)) : 0;
    var dur = 1300 + Math.round(rnd(2400));
    var e2e = asr + mt + tts + 70 + Math.round(rnd(220));
    var base = {
      id: id, chain: lib.chain, ts: Date.now() / 1000, src_lang: lib.src_lang,
      dst_lang: lib.dst_lang, engine: lib.chain === "C" ? "ocr" : "local+cloud",
      echo: false, speaker: null
    };

    onSeg(Object.assign({}, base, { state: "asr", src: lib.src, dst: "", asr_ms: asr, note: "" }));
    st.demoTimers.push(setTimeout(function () {
      onSeg({ id: id, state: "mt", src: lib.src, dst: "", mt_ms: mt });
    }, 420));
    st.demoTimers.push(setTimeout(function () {
      onSeg({ id: id, state: lib.chain === "B" ? "done" : "tts", src: lib.src, dst: lib.dst, tts_ms: tts, audio_ms: dur });
      if (lib.chain === "B") {
        onSeg({ id: id, state: "done", e2e_ms: e2e, note: "字幕上屏" });
        log("info", "B 链 " + id + " 字幕已上屏 · " + e2e + " ms");
      }
    }, 980));
    if (lib.chain !== "B") {
      st.demoTimers.push(setTimeout(function () {
        onSeg({ id: id, state: "play", e2e_ms: e2e, rtf: +(dur / e2e).toFixed(2), note: "出声中" });
      }, 1650));
      st.demoTimers.push(setTimeout(function () {
        onSeg({ id: id, state: "done", e2e_ms: e2e, rtf: +(dur / e2e).toFixed(2), note: pick(["", "", "预合成命中", "本地草稿", "云端校正"]) });
        log("info", lib.chain + " 链 " + id + " 完成 · 端到端 " + e2e + " ms");
        var A = st.metrics.A;
        A.n = (A.n || 0) + 1;
        A.e2e_p50 = Math.round((A.e2e_p50 || 1400) * 0.85 + e2e * 0.15);
        A.e2e_p90 = Math.max(A.e2e_p90 || 2000, e2e);
        renderMetrics();
      }, 2900));
    }
  }

  /* ------------------------------ 写配置 ------------------------------ */
  function sendCfgDebounced(k, v, el) {
    clearTimeout(st.deb[k]);
    if (el) { var lab = $('[data-for="' + k + '"]', el.parentNode); if (lab) lab.classList.add("pending"); }
    st.deb[k] = setTimeout(function () {
      if (el) { var lab2 = $('[data-for="' + k + '"]', el.parentNode); if (lab2) lab2.classList.remove("pending"); }
      sendCfg(k, v);
    }, 120);
  }

  function sendCfg(k, v) {
    st.cfg[k] = v;
    if (st.demo) {
      log("info", "离线预览：设置 " + k + " = " + JSON.stringify(v));
      syncControls();
      return;
    }
    var body = {};
    body[k] = v;
    apiPost("/api/cfg", body).then(function (j) {
      if (j && j.cfg) { st.cfg = Object.assign({}, st.cfg, j.cfg); syncControls(); }
      log("info", "设置 " + k + " = " + JSON.stringify(v));
    });
  }

  function postChains() {
    var body = { out_on: !!st.cfg.out_on, in_on: !!st.cfg.in_on, ocr_on: !!st.cfg.ocr_on,
                 asr_on: st.cfg.asr_on !== false, mt_on: st.cfg.mt_on !== false };
    if (st.demo) { log("info", "离线预览：链路 " + JSON.stringify(body)); return; }
    apiPost("/api/chains", body).then(function (j) {
      if (j && j.cfg) st.cfg = Object.assign({}, st.cfg, j.cfg);
      syncControls();
      log("info", "链路 A " + (body.out_on ? "开" : "关") + " / B " + (body.in_on ? "开" : "关") + " / C " + (body.ocr_on ? "开" : "关"));
    });
  }

  function toggleMic() {
    var want = !st.micOn;
    if (st.demo) {
      st.micOn = want; st.cfg.mic_on = want; renderMic();
      log("info", "离线预览：麦克风 " + (want ? "开" : "闭"));
      return;
    }
    apiPost("/api/mic", { on: want }).then(function (j) {
      if (j && j.mic_on !== undefined) { st.micOn = !!j.mic_on; st.cfg.mic_on = st.micOn; }
      else { st.micOn = want; st.cfg.mic_on = want; }
      renderMic();
      log("info", "麦克风 → " + (st.micOn ? "开" : "闭"));
    });
  }

  function toggleMaster() {
    var on = masterOn();
    st.cfg.out_on = !on;
    st.cfg.in_on = !on;
    renderMaster();
    syncControls();
    postChains();
    log("info", on ? "已全部停止（A / B 两条链）" : "已开始转译（A + B 两条链）");
  }

  /* ------------------------------ TTS 试听 ------------------------------ */
  function runTts(text, lang, play, btn, silent) {
    if (btn) btn.disabled = true;
    var body = { text: text, lang: lang, play: !!play };
    if (st.cfg.voice) body.voice = st.cfg.voice;
    var useSpeed = (st.speed && Math.abs(st.speed - 1) > 0.001);
    if (useSpeed) body.speed = Number(st.speed);

    function done(j) {
      if (btn) btn.disabled = false;
      if (!j) return;
      var txt = fmtInt(j.ms) + " ms · 首包 " + fmtInt(j.first_pkt_ms) +
                " · " + (j.audio_s != null ? Number(j.audio_s).toFixed(2) : "–") + " s";
      if (els.ttsOut) setTx(els.ttsOut, txt);
      if (!silent) log("info", "试听完成 " + fmtInt(j.ms) + " ms（" + text.length + " 字）");
    }

    if (st.demo) {
      var fake = 900 + Math.round(rnd(900));
      setTimeout(function () {
        if (btn) btn.disabled = false;
        if (els.ttsOut) setTx(els.ttsOut, fake + " ms · 首包 " + Math.round(fake * 0.6) + " · 4.00 s");
        log("info", "离线预览：试听返回假数据 " + fake + " ms");
      }, 340);
      return;
    }

    postJson("/api/tts_test", body).then(done).catch(function (e) {
      if (body.speed) {
        var b2 = Object.assign({}, body);
        delete b2.speed;
        postJson("/api/tts_test", b2).then(function (j) {
          log("warn", "后端不接受 speed 字段，语速只在前端生效");
          done(j);
        }).catch(function (e2) {
          if (btn) btn.disabled = false;
          setConn("err", "试听失败");
          log("err", "试听失败：" + e2.message);
        });
        return;
      }
      if (btn) btn.disabled = false;
      setConn("err", "试听失败");
      log("err", "试听失败：" + e.message);
    });
  }

  /* ------------------------------ 事件绑定 ------------------------------ */
  function bind() {
    els.conn = $("#conn"); els.connText = $("#connText");
    els.badge = $("#badge");
    els.btnMaster = $("#btnMaster"); els.masterTxt = $("#masterTxt");
    els.btnMic = $("#btnMic"); els.micTxt = $("#micTxt"); els.capBadge = $("#capBadge");
    els.hintC = $("#hintC");
    els.ocrModes = $("#ocrModes"); els.ocrSrc = $("#ocrSrc"); els.ocrNote = $("#ocrNote");
    els.btnPick = $("#btnPick"); els.btnOnce = $("#btnOnce");
    els.micWarn = $("#micWarn"); els.micWarnTxt = $("#micWarnTxt");
    els.injectText = $("#injectText"); els.btnInject = $("#btnInject"); els.injectOut = $("#injectOut");
    els.p50 = $("#p50"); els.p90 = $("#p90"); els.latB = $("#latB");
    els.dropN = $("#dropN"); els.dropWrap = $("#dropWrap");
    els.fillMic = $("#fillMic"); els.fillLoop = $("#fillLoop");
    els.gateMic = $("#gateMic"); els.gateLoop = $("#gateLoop");
    els.dbMic = $("#dbMic"); els.dbLoop = $("#dbLoop");
    els.levelHint = $("#levelHint");
    els.selMicDev = $("#selMicDev"); els.selOutDev = $("#selOutDev"); els.selLoopDev = $("#selLoopDev");
  els.selMonDev = $("#selMonDev"); els.chkMonOn = $("#chkMonOn");
    els.voiceChips = $("#voiceChips"); els.voiceNote = $("#voiceNote");
    els.cloneName = $("#cloneName"); els.btnVoiceRec = $("#btnVoiceRec");
    els.btnVoicePlay = $("#btnVoicePlay"); els.btnVoiceClone = $("#btnVoiceClone");
    els.cloneOut = $("#cloneOut"); els.cloneBar = $("#cloneBar");
    els.personaCards = $("#personaCards"); els.personaText = $("#personaText");
    els.personaHint = $("#personaHint"); els.personaOut = $("#personaOut");
    els.btnPersonaSave = $("#btnPersonaSave"); els.btnPersonaClear = $("#btnPersonaClear");
    els.ggufVocab = $("#ggufVocab"); els.btnVocabSave = $("#btnVocabSave"); els.vocabOut = $("#vocabOut");
    els.speed = $("#speed"); els.speedVal = $("#speedVal");
    els.engA = $("#engA"); els.engB = $("#engB"); els.engC = $("#engC");
    els.liveA = $("#liveA"); els.liveB = $("#liveB"); els.liveC = $("#liveC");
    els.segBody = $("#segBody"); els.segCount = $("#segCount"); els.empty = $("#empty");
    els.mtab = $("#mtab"); els.mtHint = $("#mtHint");
    els.logs = $("#logs"); els.logAuto = $("#logAuto");
    els.ttsText = $("#ttsText"); els.ttsLang = $("#ttsLang"); els.ttsPlay = $("#ttsPlay");
    els.btnTts = $("#btnTts"); els.ttsOut = $("#ttsOut");
    els.ocrX = $("#ocrX"); els.ocrY = $("#ocrY"); els.ocrW = $("#ocrW"); els.ocrH = $("#ocrH");
    els.ocrOut = $("#ocrOut");
    els.keyVal = $("#keyVal"); els.keyHost = $("#keyHost"); els.keyState = $("#keyState");
    els.keyOut = $("#keyOut"); els.btnKeySave = $("#btnKeySave");
    els.sysPid = $("#sysPid"); els.sysVer = $("#sysVer"); els.sysUp = $("#sysUp"); els.sysHud = $("#sysHud");
    els.sysDanmaku = $("#sysDanmaku"); els.danmHint = $("#danmHint");
    els.sysMic = $("#sysMic");
    els.sysGguf = $("#sysGguf");

    buildParams($("#params"), MAIN_PARAMS);
    buildParams($("#latparams"), LAT_PARAMS);
    buildParams($("#hudparams"), HUD_PARAMS);
    buildParams($("#dkparams"), DK_PARAMS);

    // 滑杆
    $$('input[type=range][data-cfg]').forEach(function (el) {
      el.value = String(st.cfg[el.dataset.cfg] != null ? st.cfg[el.dataset.cfg] : (el.min || 0));
      refreshParamVisual(el);
      el.addEventListener("input", function () {
        refreshParamVisual(el);
        sendCfgDebounced(el.dataset.cfg, Number(el.value), el);
        if (el.dataset.cfg === "mic_db" || el.dataset.cfg === "loop_db") {
          var g = el.dataset.cfg === "mic_db" ? els.gateMic : els.gateLoop;
          if (g) g.style.left = clamp(((Number(el.value) + 60) / 60) * 100, 0, 100).toFixed(2) + "%";
        }
      });
    });

    // 下拉 / 复选框
    $$("select[data-cfg], input[type=checkbox][data-cfg]").forEach(function (el) {
      el.addEventListener("change", function () {
        var k = el.dataset.cfg, v;
        if (el.type === "checkbox") v = el.checked;
        else if (/_dev$/.test(k)) v = devValueFor(el, k);
        else v = el.value;
        sendCfg(k, v);
        if (k === "mic_dev" || k === "out_dev" || k === "loop_dev") log("info", "切换设备 " + k + " → " + v);
        if (k === "asr_engine") { st.last.A = null; st.last.B = null; renderChains(); }
        if (/^danmaku_/.test(k)) log("info", "弹幕 " + k + " → " + v);
        if (k === "asr_gguf_lid") log("info", "分语种探测 → " + (v === "zh" ? "只救中文" : v === "any" ? "连英语也照顾" : "关"));
      });
    });

    // 链开关 / 浮窗开关
    $$("[data-sw]").forEach(function (el) {
      el.addEventListener("click", function () {
        var k = el.dataset.sw;
        var v = el.getAttribute("aria-pressed") !== "true";
        el.setAttribute("aria-pressed", v ? "true" : "false");
        st.cfg[k] = v;
        if (k === "out_on" || k === "in_on" || k === "ocr_on" ||
            k === "asr_on" || k === "mt_on") {
          postChains(); renderMaster();
          var nm = { out_on: "A 出", in_on: "B 入", ocr_on: "C 屏",
                     asr_on: "识别总闸", mt_on: "翻译总闸" }[k] || k;
          log("info", nm + " → " + (v ? "开" : "关"));
          if (k === "asr_on" && !v) log("warn", "识别总闸关了：两条链都不再识别，在途的段已作废");
          if (k === "mt_on" && !v) log("warn", "翻译总闸关了：只出原文，不合成、不发聊天框");
        } else {
          sendCfg(k, v);
          if (k === "danmaku") {
            log("info", "弹幕浮窗 → " + (v ? "开（全屏透明 · 鼠标穿透）" : "关"));
            if (els.danmHint) renderHudInfo();
          }
        }
      });
    });

    els.btnMaster.addEventListener("click", toggleMaster);
    els.btnMic.addEventListener("click", toggleMic);

    if (els.voiceChips) {
      els.voiceChips.addEventListener("click", function (e) {
        var del = e.target.closest("[data-del]");
        if (del) {
          var did = del.dataset.del;
          if (!window.confirm("删掉这个克隆音色？云端那份也一起删。")) return;
          apiPost("/api/voice_del", { id: did }).then(function (j) {
            if (!j || j.ok === false) { log("err", "删除音色失败"); return; }
            st.voices = j.voices || st.voices;
            st.cfg.voice = j.cur || st.cfg.voice;
            renderVoices();
            log("info", "已删除克隆音色");
          });
          return;
        }
        var b = e.target.closest(".vchip");
        if (!b) return;
        var id = b.dataset.voice;
        st.cfg.voice = id;
        renderVoices();
        sendCfg("voice", id);
        log("info", "音色 → " + b.textContent.replace(/\s+/g, " ").trim());
      });
    }

    // 音色克隆：录音（最长 30 s，默认 15）→ 试听 → 上传训练 → 自动切过去
    if (els.btnVoiceRec) {
      els.btnVoiceRec.addEventListener("click", function () {
        var b = els.btnVoiceRec;
        b.disabled = true;
        setBar(0);
        setClone("录音中… 现在开始，自然说 15 秒", "warn");
        apiPost("/api/voice_sample", { action: "record", seconds: 15 }).then(function (j) {
          b.disabled = false;
          if (!j || j.ok === false) {
            setClone("录音失败：" + ((j && (j.err || j.error)) || "接口没回"), "err");
            return;
          }
          st.sampleSecs = Number(j.secs) || 0;
          setBar(Math.min(100, (st.sampleSecs / 20) * 100));
          setClone("样本 " + j.secs + " s · " + j.rms_db + " dBFS" +
            (j.hint ? " · " + j.hint : ""), st.sampleSecs >= 3 ? "ok" : "warn");
          log("info", "音色样本 " + j.secs + " s / " + j.sr + " Hz");
        });
      });
    }
    if (els.btnVoicePlay) {
      els.btnVoicePlay.addEventListener("click", function () {
        apiPost("/api/voice_sample", { action: "play" }).then(function (j) {
          if (!j || j.ok === false) { setClone("还没有能放的样本", "warn"); return; }
          setClone("正在回放样本（" + j.secs + " s）", "ok");
        });
      });
    }
    if (els.btnVoiceClone) {
      els.btnVoiceClone.addEventListener("click", function () {
        if (!st.sampleSecs) { setClone("先点「录音」，录一段再克隆", "warn"); return; }
        var name = (els.cloneName && els.cloneName.value || "").trim();
        setBar(4);
        setClone("提交样本…", "warn");
        apiPost("/api/voice_clone", { name: name, secs: st.sampleSecs }).then(function (j) {
          if (!j || j.ok === false) {
            setClone("提交失败：" + ((j && (j.err || j.error)) || "接口没回"), "err");
            return;
          }
          renderVoiceJob(j.job);
        });
      });
    }

    // 人设：预设卡点一下就换；自定义那段要点「保存」才生效（避免边打字边改提示词）
    if (els.personaCards) {
      els.personaCards.addEventListener("click", function (e) {
        var b = e.target.closest(".vchip");
        if (!b) return;
        var id = b.dataset.card;
        st.cfg.card = id;
        renderPersona();
        sendCfg("card", id);
        log("info", "人设卡 → " + b.textContent.replace(/\s+/g, " ").trim());
      });
    }
    if (els.personaText) {
      els.personaText.addEventListener("input", function () {
        st.personaDirty = true;
        updatePersonaHint();
      });
    }
    if (els.btnPersonaSave) {
      els.btnPersonaSave.addEventListener("click", function () {
        var txt = (els.personaText && els.personaText.value || "").trim();
        apiPost("/api/cfg", { persona_custom: txt }).then(function (j) {
          if (j && j.cfg) { st.cfg = Object.assign({}, st.cfg, j.cfg); syncControls(); }
          st.personaDirty = false;
          renderPersona();
          if (els.personaOut) setTx(els.personaOut, txt ? ("已保存 " + txt.length + " 字，下一句就用新口吻") : "已清空，回到预设卡的口吻");
          log("info", "人设口吻 → " + (txt ? txt.length + " 字" : "空"));
        });
      });
    }
    // 专名表：打字不打扰，点「保存专名」才提交（后端 clamp 去重、单条 24 字、最多 8 条）
    if (els.ggufVocab) {
      els.ggufVocab.addEventListener("input", function () { st.vocabDirty = true; });
    }
    if (els.btnVocabSave) {
      els.btnVocabSave.addEventListener("click", function () {
        var raw = (els.ggufVocab && els.ggufVocab.value || "").replace(/[，;；]/g, ",");
        var list = raw.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
        var btn = els.btnVocabSave;
        if (btn) btn.disabled = true;
        apiPost("/api/cfg", { asr_gguf_vocab: list }).then(function (j) {
          if (btn) btn.disabled = false;
          if (!j || j.ok === false) {
            if (els.vocabOut) setTx(els.vocabOut, "保存失败：" + ((j && (j.err || j.error)) || "接口没回"));
            log("err", "专名表保存失败");
            return;
          }
          if (j.cfg) { st.cfg = Object.assign({}, st.cfg, j.cfg); syncControls(); }
          st.vocabDirty = false;
          var got = (st.cfg.asr_gguf_vocab || []);
          if (els.ggufVocab) els.ggufVocab.value = got.join(", ");
          if (els.vocabOut) {
            setTx(els.vocabOut, got.length
              ? ("已保存 " + got.length + " 条：" + got.join(" / ") + "。下一段 GGUF 识别就带上，压不住就删掉那条。")
              : "已清空专名表，回到纯识别。");
          }
          log("info", "专名表 → " + (got.length ? got.join(" / ") : "空"));
        }).catch(function (e) {
          if (btn) btn.disabled = false;
          if (els.vocabOut) setTx(els.vocabOut, "保存失败：" + e.message);
          log("err", "POST /api/cfg（专名表）失败：" + e.message);
        });
      });
    }
    if (els.btnPersonaClear) {
      els.btnPersonaClear.addEventListener("click", function () {
        if (els.personaText) els.personaText.value = "";
        st.personaDirty = false;
        updatePersonaHint();
        apiPost("/api/cfg", { persona_custom: "" }).then(function () {
          st.cfg.persona_custom = "";
          if (els.personaOut) setTx(els.personaOut, "已清空自定义口吻");
        });
      });
    }

    if (els.speed) {
      els.speed.addEventListener("input", function () {
        st.speed = Number(els.speed.value);
        renderSpeed();
        if (st.speedKb) sendCfgDebounced("speed", st.speed, null);
      });
      els.speed.addEventListener("change", function () {
        if (!st.speedKb && !st.demo) log("warn", "后端没有 speed 字段，语速只会随试听请求一起发");
      });
    }

    if (els.btnKeySave) {
      els.btnKeySave.addEventListener("click", function () {
        var key = (els.keyVal && els.keyVal.value || "").trim();
        var host = (els.keyHost && els.keyHost.value || "").trim();
        if (!key && !host) { log("warn", "密钥为空，没保存"); return; }
        els.btnKeySave.disabled = true;
        if (els.keyOut) setTx(els.keyOut, "保存并自检中…");
        postJson("/api/env", { key: key, host: host }).then(function (j) {
          els.btnKeySave.disabled = false;
          if (els.keyVal) els.keyVal.value = "";
          renderKey(j);
          if (els.keyOut) setTx(els.keyOut, j.has_key ? ("已写入 " + (j.path || "配置目录") + (j.probe === "ok" ? " · 自检通过" : (j.probe === "fail" ? " · 自检失败，看日志" : ""))) : "没写进去，看日志");
          log(j.probe === "fail" ? "err" : "info", "密钥已保存 · 自检 " + (j.probe || "未测"));
        }).catch(function (e) {
          els.btnKeySave.disabled = false;
          if (els.keyOut) setTx(els.keyOut, "保存失败：" + e.message);
          log("err", "保存密钥失败：" + e.message);
        });
      });
    }

    $("#btnReconnect").addEventListener("click", function () {
      log("info", "手动重连…");
      if (st.demo) { location.reload(); return; }
      disconnectSSE();
      apiGet("/api/state").then(function (s) {
        if (!s) { setConn("err", "后端无响应"); return; }
        applyState(s);
        setConn("ok", liveText());
        connectSSE();
      });
    });

    $("#btnDev").addEventListener("click", function () {
      if (st.demo) { renderDevices(); log("info", "离线预览：设备列表是内置假数据"); return; }
      apiGet("/api/devices").then(function (d) {
        if (!d) return;
        st.devices = d.devices || d;
        renderDevices();
        log("info", "设备列表已刷新");
      });
    });

    // 回环自测：后端自己合成一句中文从「对方听到」的设备播出去，让 A 链真的走一圈
    $("#btnInject").addEventListener("click", function () {
      var text = (els.injectText && els.injectText.value || "").trim();
      var btn = els.btnInject;
      if (btn) btn.disabled = true;
      if (els.injectOut) setTx(els.injectOut, "正在合成并往对方的设备播出去…（A 链会自己接住这句）");

      if (st.demo) {
        setTimeout(function () {
          if (btn) btn.disabled = false;
          if (els.injectOut) setTx(els.injectOut, "离线预览：已假装播了 2.80 s 音频 · 首包 180 ms · 总耗时 2900 ms");
          log("info", "离线预览：回环自测（假数据）");
        }, 400);
        return;
      }

      var body = text ? { text: text, lang: "zh" } : {};
      postJson("/api/inject", body).then(function (j) {
        if (btn) btn.disabled = false;
        if (!j || j.ok === false) {
          if (els.injectOut) setTx(els.injectOut, "回环自测失败，看日志");
          setConn("err", "回环自测失败");
          log("err", "回环自测失败：" + JSON.stringify(j || {}));
          return;
        }
        var line = "已播出 " + (j.audio_s != null ? Number(j.audio_s).toFixed(2) : "–") + " s 音频 · 首包 " +
          fmtInt(j.first_pkt_ms) + " ms · 总耗时 " + fmtInt(j.ms) + " ms" +
          (j.dev ? " · → " + devName(j.dev) : "");
        if (els.injectOut) setTx(els.injectOut, line + "\n等 A 链把它接住，句子表会自己多一行");
        log("info", "回环自测 → " + line);
        // A 链接住要几秒，指标要跟着刷新，不然顶栏会僵在旧数字
        setTimeout(pollMetrics, 2500);
        setTimeout(pollMetrics, 6000);
      }).catch(function (e) {
        if (btn) btn.disabled = false;
        setConn("err", "回环自测失败");
        log("err", "POST /api/inject 失败：" + e.message);
        if (els.injectOut) setTx(els.injectOut, "回环自测失败：" + e.message);
      });
    });

    $("#btnDevTest").addEventListener("click", function () {
      var lang = ["ja", "en", "zh"].indexOf(st.cfg.dst_lang) >= 0 ? st.cfg.dst_lang : "ja";
      var t = lang === "ja" ? "テスト音です、一二三四五。" : (lang === "en" ? "This is a test, one two three four five." : "测试音，一二三四五。");
      if (els.ttsOut) setTx(els.ttsOut, "正在往对方听到的设备播测试音…");
      runTts(t, lang, true, null, false);
    });

    $("#btnHudReset").addEventListener("click", function () {
      st.cfg.hud_x = -1; st.cfg.hud_y = -1;
      if (st.demo) { log("info", "离线预览：浮窗复位"); return; }
      apiPost("/api/cfg", { hud_x: -1, hud_y: -1 });
      log("info", "浮窗位置 → 屏幕底部中间");
    });

    // 飘一条：只丢一段假字幕到总线，不合成不出声（调样式专用）
    var dkTry = [
      { chain: "A", src: "测试，这个字幕看得见吗？", dst: "テスト、この字幕は見えますか？" },
      { chain: "B", src: "这个副本我们上周就打过了。", dst: "このダンジョン先週クリアしたよ。" },
      { chain: "C", src: "别走传送门，在集合点等我。", dst: "ポータルはやめて、集合場所で待って。" }
    ];
    var dkTryI = 0;
    $("#btnDkTry").addEventListener("click", function () {
      var t = dkTry[dkTryI++ % dkTry.length];
      if (!st.cfg.danmaku) { log("warn", "弹幕开关是关的，先在下面打开"); return; }
      if (st.demo) { log("info", "离线预览：飘一条 " + t.src); return; }
      postJson("/api/dk_preview", t).then(function () {
        log("info", "弹幕试放 → " + t.dst);
      }).catch(function (e) { log("err", "弹幕试放失败：" + e.message); });
    });

    $("#filters").addEventListener("click", function (e) {
      var b = e.target.closest(".chip");
      if (!b) return;
      st.filter = b.dataset.f;
      applyFilter();
    });

    $("#btnClearRows").addEventListener("click", function () {
      if (st.demo) { clearRows("视图已清空（离线预览）"); return; }
      apiPost("/api/clear", {}).then(function (j) {
        if (!j) return;
        clearRows("句子表和指标已在后端清空（日志保留）");
        st.drop = 0; renderDrop();
        st.metrics = { A: {}, B: {}, C: {} };
        st.last = { A: null, B: null, C: null };
        st.openTxt = "";
        renderMetrics(); renderChains();
      });
    });

    $("#btnLogClear").addEventListener("click", function () {
      els.logs.innerHTML = "";
    });

    els.btnTts.addEventListener("click", function () {
      var text = (els.ttsText.value || "").trim();
      if (!text) { log("warn", "试听文本是空的"); return; }
      runTts(text, els.ttsLang.value, !!els.ttsPlay.checked, els.btnTts, false);
    });

    // OCR 标定
    $("#btnOcrSave").addEventListener("click", function () {
      var region = [Number(els.ocrX.value) | 0, Number(els.ocrY.value) | 0,
                    Number(els.ocrW.value) | 0, Number(els.ocrH.value) | 0];
      if (region[2] <= 0 || region[3] <= 0) {
        log("warn", "区域宽高必须大于 0");
        if (els.ocrOut) setTx(els.ocrOut, "区域无效：宽和高都要大于 0");
        return;
      }
      st.cfg.ocr_region = region;
      if (st.demo) {
        if (els.ocrOut) setTx(els.ocrOut, "离线预览：区域 " + region.join(",") + "\n不会真的截图");
        log("info", "离线预览：OCR 区域 " + region.join(","));
        return;
      }
      apiPost("/api/ocr/region", { region: region, capture: true }).then(function (j) {
        if (els.ocrOut) setTx(els.ocrOut, j ? ("已记住区域 " + region.join(",") + "\n截图存到 out/ocr_crop.png") : "保存失败，看日志");
        log(j ? "info" : "err", "OCR 区域 → " + region.join(","));
      });
    });

    // ---- C 链：单帧识别 / 框选 / 模式 & 语种 ----
    $("#btnOcrTest").addEventListener("click", function () { runOcrOnce(els.btnOcrTest, true); });
    if (els.btnOnce) els.btnOnce.addEventListener("click", function () { runOcrOnce(els.btnOnce, false); });
    if (els.btnPick) els.btnPick.addEventListener("click", doPickRegion);

    if (els.ocrModes) els.ocrModes.addEventListener("click", function (e) {
      var b = e.target.closest("[data-ocrmode]");
      if (!b) return;
      var v = b.dataset.ocrmode;
      if (v === ocrMode()) return;
      st.cfg.ocr_mode = v;
      renderOcrMode(); renderOcrHint();
      sendCfg("ocr_mode", v);
      // 切到常驻而 C 链是关的：一句话什么都不做，顺手把 C 开上
      if (v === "poll" && !st.cfg.ocr_on) { st.cfg.ocr_on = true; postChains(); }
      log("info", "C 链模式 → " + (v === "poll" ? "常驻识别（0.35 s/帧 · 费 CPU）" : "按一下识别一帧"));
    });

    if (els.ocrSrc) els.ocrSrc.addEventListener("change", function () {
      sendCfg("ocr_src", els.ocrSrc.value);
      var opt = els.ocrSrc.options[els.ocrSrc.selectedIndex];
      log("info", "屏幕上是什么语言 → " + (opt ? opt.text : els.ocrSrc.value));
    });

    // 运行时长
    setInterval(function () {
      if (!els.sysUp) return;
      if (st.demo) { setTx(els.sysUp, "—（预览）"); return; }
      setTx(els.sysUp, fmtUp(st.uptime0 + (Date.now() - st.upAt) / 1000));
    }, 1000);
  }

  /* ------------------------------ 启动 ------------------------------ */
  var bootTries = 0;
  function tryState(cands, i) {
    if (i >= cands.length) {
      // 只有「本地文件直开」才进离线预览；从 http 打开时连不上就老实报错并重试，
      // 绝不拿假数据顶上（假数据会让人以为设备/延迟是真实读数）。
      if (location.protocol === "file:") { enterDemo("本地文件直开，连不到后端"); return; }
      bootTries++;
      setConn("err", "连不上后端 · 第 " + bootTries + " 次重试");
      log("warn", "连不上后端（" + (cands.join(" / ") || "同源") + "）：3 秒后重试。后端没起就双击桌面「VRDub 无言势」快捷方式。");
      setTimeout(function () { tryState(cands, 0); }, 3000);
      return;
    }
    var base = cands[i] ? String(cands[i]).replace(/\/+$/, "") : "";
    setConn("warn", "连接中…");
    fetch(base + "/api/state", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (s) {
        st.base = (s && s.url) ? String(s.url).replace(/\/+$/, "") : base;
        applyState(s);
        setConn("ok", liveText());
        log("info", "已连接后端 v" + (s.ver || "?") + " · pid " + (s.pid || "?") +
          " · 已运行 " + Math.round(s.uptime_s || 0) + " 秒" + (st.base ? " · " + st.base : ""));
        // 状态拿到了就是连上了：后面这些是界面初始化，出 bug 只报错，绝不回落到「连不上」重试
        try {
          connectSSE();
          startMetricsPoll();
          pollMetrics();
          loadKey();
          if (st.autoInject && els.btnInject) {
            log("info", "自测模式：2 秒后自动按一次「回环自测」");
            setTimeout(function () { els.btnInject.click(); }, 2000);
          }
        } catch (e3) {
          report("连上后端但初始化出错", String((e3 && e3.message) || e3));
          log("err", "连上后端了，但界面初始化出错：" + ((e3 && e3.message) || e3));
        }
      })
      .catch(function (e) {
        log("warn", (base || "同源") + " 连不上（" + e.message + "）");
        tryState(cands, i + 1);
      });
  }

  function boot() {
    bind();
    renderMetrics();
    renderDrop();
    renderMaster();
    renderSpeed();
    renderChains();
    setTx(els.segCount, "0 句");

    if (location.protocol === "file:") {
      enterDemo("本地文件直开，连不到后端");
      return;
    }
    var qs = location.search.replace(/^\?/, "");
    var m = /(?:^|&)api=([^&]+)/.exec(qs);
    st.autoInject = /(?:^|&)autoinject=1(?:&|$)/.test(qs);
    var cands = m ? [decodeURIComponent(m[1])] : ["", "http://127.0.0.1:8899", "http://127.0.0.1:8900"];
    log("info", "控制台启动，正在找后端…");
    tryState(cands, 0);
  }

  window.addEventListener("error", function (e) {
    report("页面异常", (e && e.message ? e.message + " @" + e.filename + ":" + e.lineno : String(e)));
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r2 = e && e.reason;
    report("未处理的 Promise 拒绝", String((r2 && (r2.stack || r2.message)) || r2).slice(0, 300));
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();