# -*- coding: utf-8 -*-
r"""vrdub C 链 OCR 引擎：det 跑一次 + ch rec 主判 + ja rec 兜底（14 版实测调优）。

沿用上一代实测结论（D:\工作区2\vrchat-live-dub\docs\20-读屏幕文字气泡OCR-v0.1.md）：
  * det 必须 limit_type=max：默认 min 会把小气泡按短边放大到 736，单帧 75ms → 1.3~7s。
  * **假名只有 ja 模型读得出**：ch 模型对日语气泡给 0.51~0.83 且假名全丢（只剩「！の」）。
  * **汉字只有 ch 模型读得准**：ja 模型把「不安与孤独」读成「不安与珂独」。
  * 路由：det 只跑一次 → 全量 ch rec → score < thr 的行用 ja 重跑 → 取分高的。
这版新增（本机 24 核 3.12 实测）：
  * ORT enable_cpu_mem_arena=True（上游 OrtInferSession 写死 False）：det -25%，rec -8%。
  * 三个模型各自定线程数（det 8 / ch 8 / ja 10），一次跑两个模型并行反而更慢，已放弃。
  * crop 放大 3× 再喂 rec：rec 无论如何都 resize 到 48 高，输入宽度不变 = 免费，但
    13px 小字准确率从「ちよつと／ワ-ルド」变成全对。
  * rec 按宽高比分桶再 batch：一条超宽行把整批 padding 撑大，分桶省 ~15%。
  * load() 里 warmup：ORT 动态 shape 首次 300~640ms，不预热第一次真抓帧会假超时。

本模块只依赖 rapidocr_onnxruntime 的 TextDetector / TextRecognizer 子类，不实例化 RapidOCR
（那个类会把 det 和两个 rec 一起加载、还自带一层会改分辨率的 preprocess）。
"""
from __future__ import annotations

import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import paths as _paths

# ---------------------------------------------------------------- 依赖路径
# 第三方库目录：环境变量 > 程序目录下的 .pylibs > 已安装（import 能找到就算）
def _pylib_dirs() -> list:
    dirs = []
    env = os.environ.get("VRDUB_PYLIBS") or ""
    if env:
        dirs.append(env)
    dirs.append(os.path.join(_paths.ROOT, ".pylibs"))
    dirs.append(os.path.join(_paths.ROOT, "_pylibs"))
    return [d for d in dirs if d]


PYLIBS_CANDIDATES = tuple(_pylib_dirs())


def ensure_pylibs() -> str:
    """把第三方库目录塞进 sys.path（Lead 的进程不一定带 PYTHONPATH）。"""
    for cand in PYLIBS_CANDIDATES:
        if cand and os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:
        for cand in PYLIBS_CANDIDATES:
            if cand and cand not in sys.path:
                sys.path.append(cand)
        import rapidocr_onnxruntime  # noqa: F401
    import rapidocr_onnxruntime as _r

    return os.path.dirname(os.path.dirname(os.path.abspath(_r.__file__)))


# ---------------------------------------------------------------- 默认模型
# 一律相对程序目录（models\ocr\）；找不到再回到依赖目录里翻（源码跑、没把模型放进去时）。
def _pick(*rels: str) -> str:
    for rel in rels:
        p = os.path.join(_paths.ROOT, rel)
        if os.path.isfile(p):
            return p
    for d in _pylib_dirs():
        for rel in rels:
            p = os.path.join(d, rel)
            if os.path.isfile(p):
                return p
    return os.path.join(_paths.ROOT, rels[0])


_MODEL_DIR = os.path.join(_paths.ROOT, "models", "ocr")
DEFAULT_MODELS = {
    "det": _pick("models/ocr/ch_PP-OCRv4_det_infer.onnx",
                 "rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx"),
    "rec_ch": _pick("models/ocr/ch_PP-OCRv4_rec_infer.onnx",
                    "rapidocr_onnxruntime/models/ch_PP-OCRv4_rec_infer.onnx"),
    "rec_ja": _pick("models/ocr/japan_rec.onnx"),
    "dict_ja": _pick("models/ocr/japan_dict.txt"),
    "yaml_ch": _pick("models/ocr/config_ch.yaml"),
    "yaml_ja": _pick("models/ocr/config_ja.yaml"),
}

# 上一代调好的两份 yaml 的内联副本（yaml 丢了也还能跑；数值与文件一致）
_BUILTIN_DET = {
    "model_path": DEFAULT_MODELS["det"],
    "limit_side_len": 736,
    "limit_type": "max",
    "std": [0.5, 0.5, 0.5],
    "mean": [0.5, 0.5, 0.5],
    "thresh": 0.3,
    "box_thresh": 0.4,
    "max_candidates": 200,
    "unclip_ratio": 1.6,
    "use_dilation": True,
    "score_mode": "fast",
    "intra_op_num_threads": 8,
    "inter_op_num_threads": 1,
    "use_cuda": False,
    "use_dml": False,
}
_BUILTIN_REC = {
    "model_path": DEFAULT_MODELS["rec_ch"],
    "rec_img_shape": [3, 48, 320],
    "rec_batch_num": 6,
    "intra_op_num_threads": 8,
    "inter_op_num_threads": 1,
    "use_cuda": False,
    "use_dml": False,
}


def _load_yaml(path: str) -> Optional[dict]:
    if not path or not os.path.isfile(path):
        return None
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _cfg_from_yaml(path: str) -> Tuple[dict, dict]:
    """返回 (det_cfg, rec_cfg)。yaml 拿到就用 yaml，拿不到用内联副本。"""
    det = dict(_BUILTIN_DET)
    rec = dict(_BUILTIN_REC)
    data = _load_yaml(path)
    if data:
        try:
            for k, v in (data.get("Det") or {}).items():
                if k not in ("model_path",):  # 模型路径以 DEFAULT_MODELS/调用方为准
                    det[k] = v
            for k, v in (data.get("Rec") or {}).items():
                if k not in ("model_path", "rec_keys_path"):
                    rec[k] = v
        except Exception:
            pass
    return det, rec


# ---------------------------------------------------------------- 文字工具
_KANA_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u31f0-\u31ff\uff66-\uff9f]")
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_WS_RE = re.compile(r"[\s\u3000\u200b]+")
_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


def norm_text(text: str) -> str:
    """NFKC + 折叠空白（半角假名/全角数字会归一，方便 MT 和去重指纹）。"""
    if not text:
        return ""
    import unicodedata

    out = unicodedata.normalize("NFKC", str(text).replace("\u200b", " "))
    out = _WS_RE.sub(" ", out).strip()
    return out


def detect_lang(text: str) -> str:
    """ja = 含假名；ch = 含汉字且无假名；en = 只有拉丁字母/数字。"""
    if not text:
        return "ch"
    if _KANA_RE.search(text):
        return "ja"
    if _HAN_RE.search(text):
        return "ch"
    if _LATIN_RE.search(text):
        return "en"
    return "ch"


def has_cjk(text: str) -> bool:
    return bool(text) and bool(_KANA_RE.search(text) or _HAN_RE.search(text))


def is_only_punct(text: str) -> bool:
    if not text:
        return True
    return bool(_PUNCT_ONLY_RE.match(text)) or not any(ch.isalnum() for ch in text)


def fingerprint(text: str) -> str:
    """去重指纹：NFKC + 去掉所有空白 + 小写。"""
    return _WS_RE.sub("", norm_text(text)).lower()


# ---------------------------------------------------------------- det / rec 子类
def _build_classes():
    """惰性构造子类（import 早了就吃不到 sys.path 的改动）。"""
    ensure_pylibs()
    from rapidocr_onnxruntime.ch_ppocr_det import TextDetector
    from rapidocr_onnxruntime.ch_ppocr_det.utils import DBPostProcess, DetPreProcess
    from rapidocr_onnxruntime.ch_ppocr_rec import TextRecognizer
    from rapidocr_onnxruntime.ch_ppocr_rec.utils import CTCLabelDecode
    from rapidocr_onnxruntime.utils import OrtInferSession

    class ArenaSession(OrtInferSession):
        """arena 打开 + 允许覆盖 graph opt；线程数仍走 config。"""

        ARENA = True

        @staticmethod
        def _init_sess_opts(config):
            so = OrtInferSession._init_sess_opts(config)
            so.enable_cpu_mem_arena = ArenaSession.ARENA
            return so

    class FastDet(TextDetector):
        """固定 limit_type=max + 用 config 的 limit_side_len。

        上游 get_preprocess() 在 limit_type=max 分支下**忽略 yaml 的 limit_side_len**，
        一律按 960/1500/2000 走；这里覆盖掉。
        """

        def __init__(self, config):
            self.limit_side_len = int(config.get("limit_side_len") or 640)
            self.limit_type = "max"
            self.mean = config.get("mean")
            self.std = config.get("std")
            self.preprocess_op = None
            self.postprocess_op = DBPostProcess(
                thresh=config.get("thresh", 0.3),
                box_thresh=config.get("box_thresh", 0.4),
                max_candidates=config.get("max_candidates", 200),
                unclip_ratio=config.get("unclip_ratio", 1.6),
                use_dilation=config.get("use_dilation", True),
                score_mode=config.get("score_mode", "fast"),
            )
            self.infer = ArenaSession(config)

        def get_preprocess(self, max_wh=None):
            return DetPreProcess(self.limit_side_len, "max", self.mean, self.std)

    class FastRec(TextRecognizer):
        def __init__(self, config):
            self.session = ArenaSession(config)
            character = self.session.get_character_list() if self.session.have_key() else None
            self.postprocess_op = CTCLabelDecode(
                character=character, character_path=config.get("rec_keys_path", None)
            )
            self.rec_batch_num = int(config["rec_batch_num"])
            self.rec_image_shape = list(config["rec_img_shape"])

    return FastDet, FastRec


def rotate_crop_image(img: np.ndarray, points: np.ndarray) -> np.ndarray:
    """四点透视裁切；竖排（h/w>=1.5）转 90°。与上游 get_rotate_crop_image 等价。"""
    import cv2

    w = int(max(np.linalg.norm(points[0] - points[1]), np.linalg.norm(points[2] - points[3])))
    h = int(max(np.linalg.norm(points[0] - points[3]), np.linalg.norm(points[1] - points[2])))
    w = max(1, w)
    h = max(1, h)
    pts_std = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(points.astype(np.float32), pts_std)
    dst = cv2.warpPerspective(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if dst.shape[0] * 1.0 / max(1, dst.shape[1]) >= 1.5:
        dst = np.rot90(dst)
    return np.ascontiguousarray(dst)


def box_to_rect(box: np.ndarray) -> Tuple[int, int, int, int]:
    xs = box[:, 0]
    ys = box[:, 1]
    x0, x1 = int(math.floor(xs.min())), int(math.ceil(xs.max()))
    y0, y1 = int(math.floor(ys.min())), int(math.ceil(ys.max()))
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


# ---------------------------------------------------------------- 引擎
class DualOcrEngine:
    """det 一次 + 双 rec 路由。线程安全由调用方保证（OcrChain 里加了锁）。"""

    def __init__(self, cfg: Optional[dict] = None, log=print):
        cfg = _cfg_dict(cfg)
        self.log = log or (lambda *a, **k: None)
        self.models = dict(DEFAULT_MODELS)
        for k in ("det", "rec_ch", "rec_ja", "dict_ja", "yaml_ch", "yaml_ja"):
            v = cfg.get("ocr_model_" + k) or cfg.get("ocr_" + k)
            if v:
                self.models[k] = str(v)
        base_thr = int(cfg.get("ocr_threads", 0) or 0)
        self.threads_det = int(cfg.get("ocr_threads_det") or base_thr or 8)
        self.threads_ch = int(cfg.get("ocr_threads_ch") or base_thr or 8)
        self.threads_ja = int(cfg.get("ocr_threads_ja") or base_thr or 10)
        self.det_limit = int(cfg.get("ocr_det_limit", 0) or 0)  # 0 = 按区域自适应
        self.crop_scale = float(cfg.get("ocr_crop_scale", 3.0) or 3.0)
        self.batch_num = int(cfg.get("ocr_batch_num", 6) or 6)
        self.thr_ja = float(cfg.get("ocr_thr_ja", cfg.get("ocr_rerun_thr", 0.9)))
        self.primary = str(cfg.get("ocr_primary", "auto") or "auto")  # auto|ja|ch
        self._auto_primary = "ja"  # C 链默认对方说日语；按上一帧主导语言自适应
        self.last_primary = self._auto_primary
        self.min_conf = float(cfg.get("ocr_min_conf", 0.45))
        self.min_h = int(cfg.get("ocr_min_h", 9))
        self.max_ratio = float(cfg.get("ocr_max_ratio", 40.0))
        self.warmup = bool(cfg.get("ocr_warmup", True))
        self.kana_fix = bool(cfg.get("ocr_kana_fix", True))
        self._rec_cache: Dict[tuple, Any] = {}
        self._rec_order: List[tuple] = []
        self._det = None
        self._rec_ch = None
        self._rec_ja = None
        self.loaded = False
        self.load_error = ""
        self.load_ms = 0.0
        self.warmup_ms = 0.0
        self._mono = 1

    # --- 加载 -------------------------------------------------------
    def load(self) -> bool:
        if self.loaded:
            return True
        if self.load_error:
            return False
        t0 = time.perf_counter()
        try:
            FastDet, FastRec = _build_classes()
            det_cfg, rec_ch_cfg = _cfg_from_yaml(self.models["yaml_ch"])
            _, rec_ja_cfg = _cfg_from_yaml(self.models["yaml_ja"])

            det_cfg = dict(det_cfg)
            det_cfg.update(
                model_path=self.models["det"],
                limit_type="max",
                limit_side_len=self.det_limit or 640,
                intra_op_num_threads=self.threads_det,
                inter_op_num_threads=1,
                use_cuda=False,
                use_dml=False,
            )
            rec_ch_cfg = dict(rec_ch_cfg)
            rec_ch_cfg.update(
                model_path=self.models["rec_ch"],
                rec_batch_num=self.batch_num,
                intra_op_num_threads=self.threads_ch,
                inter_op_num_threads=1,
                use_cuda=False,
                use_dml=False,
            )
            rec_ja_cfg = dict(rec_ja_cfg)
            rec_ja_cfg.update(
                model_path=self.models["rec_ja"],
                rec_keys_path=self.models["dict_ja"],
                rec_batch_num=self.batch_num,
                intra_op_num_threads=self.threads_ja,
                inter_op_num_threads=1,
                use_cuda=False,
                use_dml=False,
            )

            self._det = FastDet(det_cfg)
            self._rec_ch = FastRec(rec_ch_cfg)
            self._rec_ja = FastRec(rec_ja_cfg)
            self.loaded = True
            self.load_ms = (time.perf_counter() - t0) * 1000.0
            if self.warmup:
                self._do_warmup()
            return True
        except Exception as exc:
            self.load_error = "%s: %s" % (type(exc).__name__, exc)
            self.log("[ocr] 模型加载失败 %s" % self.load_error)
            self.loaded = False
            return False

    def _do_warmup(self):
        """ORT 动态 shape 首帧要 300~640ms：先用假图跑热，别让第一帧真人气泡背这个锅。"""
        t0 = time.perf_counter()
        try:
            import cv2

            dummy = np.full((420, 540, 3), 24, dtype=np.uint8)
            for i in range(3):
                cv2.rectangle(dummy, (30, 60 + i * 90), (480, 92 + i * 90), (235, 235, 235), -1)
            for _ in range(2):
                boxes, _ = self._det(dummy)
            crops = []
            for _ in range(3):
                crops.append(np.full((int(48 * 1.6), 48 * 8, 3), 200, dtype=np.uint8))
            self._rec_ch(crops)
            self._rec_ja(crops)
            self.warmup_ms = (time.perf_counter() - t0) * 1000.0
        except Exception as exc:  # 预热失败不影响主流程
            self.warmup_ms = -1.0
            self.log("[ocr] warmup 跳过 %s: %s" % (type(exc).__name__, exc))

    # --- 缓存 -------------------------------------------------------
    @staticmethod
    def _crop_sig(crop: np.ndarray) -> bytes:
        """行像素签名：24x6 灰度缩略的字节串（同一气泡不动 → 同一签名）。"""
        import cv2

        small = cv2.resize(crop, (24, 6), interpolation=cv2.INTER_AREA)
        return small.astype(np.uint8).tobytes()

    # --- det --------------------------------------------------------
    def _det_limit_for(self, img: np.ndarray) -> int:
        if self.det_limit:
            return self.det_limit
        long_side = max(img.shape[0], img.shape[1])
        # 736 以内不缩放（VRChat 气泡框 540x420 就是原尺寸跑），更大才缩
        return int(max(448, min(736, long_side)))

    def detect(self, img: np.ndarray):
        self._det.limit_side_len = self._det_limit_for(img)
        boxes, _ = self._det(img)
        if boxes is None or len(boxes) == 0:
            return []
        out = []
        for box in boxes:
            x, y, w, h = box_to_rect(box)
            if h < self.min_h or w < 3:
                continue
            if w / float(max(1, h)) > self.max_ratio:
                continue
            out.append((box, (x, y, w, h)))
        return out

    # --- rec --------------------------------------------------------
    def _batches(self, crops: Sequence[np.ndarray]) -> List[List[int]]:
        """按宽高比分桶：一条超宽行会把同批所有图 padding 到它的宽度。"""
        buckets: Dict[int, List[int]] = {}
        for i, c in enumerate(crops):
            r = c.shape[1] / float(max(1, c.shape[0]))
            key = 0 if r <= 4 else 1 if r <= 8 else 2 if r <= 14 else 3
            buckets.setdefault(key, []).append(i)
        order: List[List[int]] = []
        for key in sorted(buckets):
            idx = buckets[key]
            for k in range(0, len(idx), self.batch_num):
                order.append(idx[k : k + self.batch_num])
        return order

    def _rec_all(self, model, crops: Sequence[np.ndarray]):
        n = len(crops)
        out = [("", 0.0)] * n
        for idxs in self._batches(crops):
            res, _ = model([crops[i] for i in idxs])
            for i, item in zip(idxs, res):
                out[i] = item
        return out

    def recognize(self, crops: Sequence[np.ndarray], primary: Optional[str] = None):
        """主模型全量 → 低分/空串的行用另一个模型重跑 → 逐行取分高。

        为什么不是固定「先 ch 再 ja」：谁当主模型，成本差一倍。
          * 日语气泡（VRChat 日服主流）：ja 全量 ~70ms 且几乎不用重跑；
            反过来 ch 全量 66ms + 4 行 ja 55ms = 121ms，白花一倍。
          * 中语气泡：「今天也很热闹啊」ja 只给 0.6~0.8 乱码（"今天也限熟祠喩"），
            所以 ja 主判后 ch 重跑；ch 主判则整帧一次过。
        自适应：primary="auto" 时用上一帧的主导语言（第一帧 ja），每帧结束后按结果更新。
        正确性不受影响：低分/空串的行一定会被另一个模型重跑并取分高的。
        返回 [(text, score, lang, src, s_ch, s_ja)], t_primary, t_second（ms）。
        """
        if not crops:
            return [], 0.0, 0.0
        prim = primary or self.primary
        if prim in ("auto", "", None):
            prim = self._auto_primary
        if prim not in ("ja", "ch"):
            prim = "ja"
        self.last_primary = prim
        model_p = self._rec_ja if prim == "ja" else self._rec_ch
        model_s = self._rec_ch if prim == "ja" else self._rec_ja
        other = "ch" if prim == "ja" else "ja"

        t0 = time.perf_counter()
        res_p = self._rec_all(model_p, crops)
        t_p = (time.perf_counter() - t0) * 1000.0
        texts = [norm_text(t) for t, _ in res_p]
        scores = [float(s) for _, s in res_p]
        s_p = list(scores)
        s_s: List[Optional[float]] = [None] * len(crops)
        src = [prim] * len(crops)
        # 低分或空串才值得换模型；纯拉丁行（0.95+）不必
        need = [
            i
            for i, s in enumerate(scores)
            if (s < self.thr_ja or not texts[i])
            and not (texts[i] and not has_cjk(texts[i]) and s >= self.min_conf)
        ]
        t_s = 0.0
        if need:
            t1 = time.perf_counter()
            res_s = self._rec_all(model_s, [crops[i] for i in need])
            t_s = (time.perf_counter() - t1) * 1000.0
            for slot, (txt, sc) in zip(need, res_s):
                tx = norm_text(txt)
                sc = float(sc)
                s_s[slot] = sc
                if tx and sc > scores[slot]:
                    texts[slot] = tx
                    scores[slot] = sc
                    src[slot] = other

        out = []
        nj = 0
        for i, text in enumerate(texts):
            lang = detect_lang(text)
            if lang == "ja":
                nj += 1
            if prim == "ja":
                out.append((text, scores[i], lang, src[i], s_s[i], s_p[i]))
            else:
                out.append((text, scores[i], lang, src[i], s_p[i], s_s[i]))
        # 下一帧的主导语言：过半是日语就继续 ja 主判，否则切 ch
        self._auto_primary = "ja" if (nj * 2 >= max(1, len(texts))) else "ch"
        return out, t_p, t_s

    # --- 整条流水线 -------------------------------------------------
    def run(self, frame: np.ndarray, merge: bool = True, scale: Optional[float] = None) -> dict:
        """整条流水线。返回 {'lines': [...], 'det_ms', 'rec_ms', 'ja_ms', 'n_boxes', 'ms'}。"""
        stats = {
            "det_ms": 0.0,
            "rec_ms": 0.0,
            "ja_ms": 0.0,
            "n_boxes": 0,
            "ms": 0.0,
            "scaled": bool(scale and abs(scale - 1.0) > 1e-3),
            "det_limit": 0,
            "primary": self.primary,
        }
        t0 = time.perf_counter()
        if not self.load() or frame is None or getattr(frame, "size", 0) == 0:
            stats["ms"] = (time.perf_counter() - t0) * 1000.0
            return {"lines": [], **stats}

        img = frame
        if stats["scaled"]:
            import cv2

            img = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        stats["det_limit"] = self._det_limit_for(img)
        t1 = time.perf_counter()
        det_res = self.detect(img)
        stats["det_ms"] = (time.perf_counter() - t1) * 1000.0
        stats["n_boxes"] = len(det_res)
        if not det_res:
            stats["ms"] = (time.perf_counter() - t0) * 1000.0
            return {"lines": [], **stats}

        import cv2

        crops = []
        rects = []
        for box, rect in det_res:
            crop = rotate_crop_image(img, box)
            if crop.size == 0 or crop.shape[0] < 5:
                continue
            if self.crop_scale and abs(self.crop_scale - 1.0) > 1e-3:
                crop = cv2.resize(
                    crop, None, fx=self.crop_scale, fy=self.crop_scale, interpolation=cv2.INTER_CUBIC
                )
            crops.append(crop)
            rects.append(rect)

        # --- 逐行缓存：位置 + 像素签名都没变就不重跑 rec。
        # VRChat 气泡停在那里不动时，整帧只花 det 的钱（~40ms），不在原地重复算 rec。
        rec_res: List[Any] = [None] * len(crops)
        need: List[int] = []
        keys: List[tuple] = []
        for i, crop in enumerate(crops):
            r = rects[i]
            key = (r[0] // 4, r[1] // 4, r[2] // 4, r[3] // 4, self._crop_sig(crop))
            keys.append(key)
            hit = self._rec_cache.get(key)
            if hit is not None:
                rec_res[i] = hit
            else:
                need.append(i)
        t_ch = t_ja = 0.0
        if need:
            sub, t_ch, t_ja = self.recognize([crops[i] for i in need])
            for slot, item in zip(need, sub):
                rec_res[slot] = item
                self._rec_cache[keys[slot]] = item
                self._rec_order.append(keys[slot])
            if len(self._rec_order) > 256:
                for k in self._rec_order[:128]:
                    self._rec_cache.pop(k, None)
                self._rec_order = self._rec_order[128:]
        stats["rec_reuse"] = len(crops) - len(need)
        stats["rec_ms"] = t_ch
        stats["ja_ms"] = t_ja
        stats["primary"] = self.last_primary

        if stats["scaled"]:
            inv = 1.0 / scale
            rects = [
                (int(round(x * inv)), int(round(y * inv)), int(round(w * inv)), int(round(h * inv)))
                for x, y, w, h in rects
            ]

        lines = []
        for (text, score, lang, src, s_ch, s_ja), (x, y, w, h) in zip(rec_res, rects):
            if not text or is_only_punct(text):
                continue
            if self.kana_fix and lang == "ja":
                text = fix_kana(text)
            if score < self.min_conf:
                continue
            lines.append(
                {
                    "text": text,
                    "score": round(float(score), 4),
                    "box": [x, y, w, h],
                    "lang": lang,
                    "rec": src,
                    "score_ch": None if s_ch is None else round(float(s_ch), 4),
                    "score_ja": None if s_ja is None else round(float(s_ja), 4),
                    "h": h,
                }
            )

        if merge:
            lines = merge_rows(lines, self.log)
        lines.sort(key=lambda r: (r["box"][1], r["box"][0]))
        stats["ms"] = (time.perf_counter() - t0) * 1000.0
        return {"lines": lines, **stats}


def _cfg_dict(cfg) -> dict:
    """把任何「dict 风格」的配置对象摊平成普通 dict。

    vrdub.cfg.Cfg 不是 dict 子类（只有 get/update/save），直接 dict(Cfg()) 会抛
    TypeError: cannot convert dictionary update sequence element #0 to a sequence
    —— 探针里传的是 plain dict 所以一直没暴露，真机 -m vrdub.ocr --test 才炸。
    """
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        return dict(cfg)
    for name in ("to_dict", "as_dict", "snapshot"):
        fn = getattr(cfg, name, None)
        if callable(fn):
            try:
                got = fn()
            except Exception:
                continue
            if isinstance(got, dict):
                return dict(got)
    for name in ("data", "_data", "_d", "d"):
        got = getattr(cfg, name, None)
        if isinstance(got, dict):
            return dict(got)
    try:
        keys = list(cfg.keys())
    except Exception:
        keys = []
    out = {}
    for k in keys:
        try:
            out[k] = cfg[k]
        except Exception:
            try:
                out[k] = cfg.get(k)
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- 小 kana 修复
# 实测（本机，13/18/26/40px × rec 输入高 32/48/64 全部复现）：japan_rec 会系统性
# 把小写假名读成大写 —— ちょっと→ちよつと / いっしょに→いつしよに / きゃべつ→きやべつ。
# 这不是分辨率问题（放大到 40px 照样错），也不是 input 高度问题（32/48/64 都一样）：
# 模型训练分布里小写假名被当成了同形字符。直接给 logits 加偏置只会得到「ぢよぢれ」，
# 所以只能做**后处理修复**，并且必须保守（宁可不修，不可改错）：
#   1) 规则：假名 + つ + て/た → 假名 + っ + て/た（まつて→まって、わかつた→わかった）
#      真实存在的「かつて」等放进 _SOKUON_SKIP 白名单排除。
#   2) 词表：完整词级替换，只放入「误读形态不是真词」的常用词（ちよつと / いつしよ …）。
# 这两步都不影响翻译：Qwen 读「ちよつと待っててね」也能正确译出，修复是为了字幕原文好看。
_SOKUON_RE = re.compile(
    r"(?<=[あいうえおかきくけこさしすせそたちてとなにぬねのはひふへほまみむめもやゆよ"
    r"らりるれろがぎぐげござじずぜぞだでどばびぶべぼぱぴぷぺぽゃゅょっ])つ(?=[てた])"
)
# kana+つ+た/て 本身就是真词的极少数情况
_SOKUON_SKIP = {"かつて", "いつて", "ふつて"}
_KANA_WORDS = {
    # 字形近邻误读：小字号下「こ」被 rec 认成「二」（实测 13px 气泡 0.995 置信度）
    "二んにちは": "こんにちは",
    "二んにちわ": "こんにちは",
    "二んばんは": "こんばんは",
    "二んにち": "こんにち",
    "こんにちわ": "こんにちは",
    "こんばんわ": "こんばんは",
    "ちよつと": "ちょっと",
    "ちよっと": "ちょっと",
    "ちょうと": "ちょっと",
    "いつしよ": "いっしょ",
    "いつしょ": "いっしょ",
    "いししよ": "いっしょ",
    "ちやん": "ちゃん",
    "ちやんと": "ちゃんと",
    "しやしん": "しゃしん",
    "しやべる": "しゃべる",
    "しやべつた": "しゃべった",
    "きよう": "きょう",
    "ちよう": "ちょう",
    "きゆう": "きゅう",
    "ぎゆう": "ぎゅう",
    "しゆみ": "しゅみ",
    "じゆう": "じゅう",
    "りよく": "りょく",
    "じようず": "じょうず",
    "しよつぷ": "しょっぷ",
    "おちや": "おちゃ",
    "こちや": "こちゃ",
    "ゆつくり": "ゆっくり",
    "びつくり": "びっくり",
    "きつと": "きっと",
    "もつと": "もっと",
    "ずつと": "ずっと",
    "やつぱり": "やっぱり",
    "すこしつ": "すこしっ",
}


# ja 模型另一个系统性误读：把平假名读成形近的片假名（实测「ありがとう」→「あリがとう」）。
# 只在**左右都是平假名**时替换，平假名句子里夹一个片假名几乎不可能是原文。
_KATA_IN_HIRA = {"リ": "り", "ン": "ん", "シ": "し", "ス": "す", "ソ": "そ", "ツ": "つ",
                 "カ": "か", "タ": "た", "ナ": "な", "ハ": "は", "マ": "ま", "ヤ": "や",
                 "レ": "れ", "ワ": "わ", "エ": "え", "ニ": "に"}
_HIRA = "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをん"


def fix_kana(text: str) -> str:
    """修复 japan_rec 系统性丢失的小写假名（保守：只改确知误读形态）。"""
    if not text:
        return text
    out = text
    for bad, good in _KANA_WORDS.items():
        if bad in out:
            out = out.replace(bad, good)

    def _sub(m):
        i = m.start()
        win = out[max(0, i - 1) : i + 2]
        if win in _SOKUON_SKIP:
            return m.group(0)
        return "っ"

    out = _SOKUON_RE.sub(_sub, out)

    chars = list(out)
    for i, ch in enumerate(chars):
        rep = _KATA_IN_HIRA.get(ch)
        if not rep or i == 0 or i + 1 >= len(chars):
            continue
        if chars[i - 1] in _HIRA and chars[i + 1] in _HIRA:
            chars[i] = rep
    # 片假名还原之后可能出现新的「つ+て」组合（まツて → まつて），再走一遍促音规则
    return _SOKUON_RE.sub(_sub, "".join(chars))


# ---------------------------------------------------------------- 行合并
def _join_text(a: str, b: str) -> str:
    """CJK 直接拼；两侧都是拉丁字母且上一行不以连字符结尾时补一个空格。"""
    if not a:
        return b
    if not b:
        return a
    if a.endswith("-"):
        return a[:-1] + b

    def latin(ch):
        return ("a" <= ch.lower() <= "z") or ch.isdigit()

    if latin(a[-1]) and latin(b[0]):
        return a + " " + b
    return a + b


_SENT_END = ("。", "！", "？", "!", "?", "…", "」", "』", "）", ")")


_LABEL_BAN = set("，,。.？?！!、；;：:…—~～「」『』()（）[]【】")


def _is_label(row: dict, nxt: dict) -> bool:
    """这一行是不是「说话人名字」而不是正文。

    VRChat 聊天窗里名字独占一行、很短、宽度远小于下面那行正文。不挡住的话
    merge_rows 会把它和正文粘成一句，译文前面就多出一个音译名（实测 Aoi → 「葵」）。
    """
    t = (row.get("text") or "").strip()
    if not t or len(t) > 24:
        return False
    if t.endswith(_SENT_END):
        return False
    if any(ch in _LABEL_BAN for ch in t):
        return False
    w = float(row["box"][2])
    nw = float(nxt["box"][2] or 0)
    if nw <= 0:
        return False
    return w <= 0.45 * nw


def _can_continue(prev: dict, row: dict) -> bool:
    """折行合并的第二道闸：光靠 y 间距会把「两句不同的消息」粘成一句。

    实测（13px + 1.5x DPI 的 700x300 假窗）：第 4 行日文和第 5 行中文 y 间距只有
    18px，gap 阈值 0.55*h 放行，于是合成「…おねがいします今天也很热闹啊」。
    """
    t = (prev.get("text") or "").rstrip()
    if t.endswith(_SENT_END):
        return False  # 上一行已经收句：折行不会在句号后继续
    if prev.get("lang") != row.get("lang"):
        return False  # 日文行后面跟中文行 = 两条不同消息，不是同一句折行
    return True


def merge_same_row(lines: List[dict], log=None) -> List[dict]:
    """同一行被切成几块（横向碎片）时合成一条。

    实测：气泡里一行日文被 det 切成「!今日一游嬉」「嬉、明会。」两块，
    下游当成三句话各自翻译 → 屏幕上同一处叠三个气泡、越叠越乱。
    判据：竖直方向重叠 >= 55% 且水平间隙 <= 1.4 倍字高 → 同一行，按 x 排序拼接。
    """
    if not lines:
        return []
    rows = sorted(lines, key=lambda r: (r["box"][1], r["box"][0]))
    groups: List[List[dict]] = []
    for row in rows:
        placed = False
        for grp in groups:
            gx, gy, gw, gh = grp[0]["box"]
            x, y, w, h = row["box"]
            hm = max(1.0, (gh + h) / 2.0)
            top = max(gy, y)
            bot = min(gy + gh, y + h)
            ov = (bot - top) / float(hm)
            if ov >= 0.55:
                gap = x - (gx + gw) if x >= gx else gx - (x + w)
                if gap <= 1.4 * hm:
                    grp.append(row)
                    placed = True
                    break
        if not placed:
            groups.append([row])

    merged: List[dict] = []
    for grp in groups:
        if len(grp) == 1:
            merged.append(dict(grp[0]))
            continue
        grp = sorted(grp, key=lambda r: r["box"][0])
        text = ""
        for g in grp:
            text = _join_text(text, g["text"])
        x0 = min(g["box"][0] for g in grp)
        y0 = min(g["box"][1] for g in grp)
        x1 = max(g["box"][0] + g["box"][2] for g in grp)
        y1 = max(g["box"][1] + g["box"][3] for g in grp)
        nchar = sum(len(g["text"]) for g in grp) or 1
        score = sum(float(g.get("score") or 0.0) * len(g["text"]) for g in grp) / nchar
        langs = [g.get("lang") for g in grp]
        lang = "ja" if "ja" in langs else max(set(langs), key=langs.count)
        merged.append({"text": norm_text(text), "box": [x0, y0, x1 - x0, y1 - y0],
                       "score": score, "lang": lang})
    if log and len(merged) < len(lines):
        log("[ocr] 同行碎片合并 %d → %d 条" % (len(lines), len(merged)))
    return merged


def merge_rows(lines: List[dict], log=None) -> List[dict]:
    """把 VRChat 气泡折行合并成一条：y 间距小 + 左边对齐 + 字高接近。

    气泡里长句会被折成 2~4 行，det 每行给一个框；不合并的话下游会当 4 句翻译。
    阈值必须紧（13px + 2.6 倍行距实测）：gap <= max(5, 0.55*h)，|dx| <= max(6, 0.6*h)。
    """
    if not lines:
        return []
    rows = sorted(lines, key=lambda r: (r["box"][1], r["box"][0]))
    groups: List[List[dict]] = []
    cur: List[dict] = [rows[0]]
    for row in rows[1:]:
        prev = cur[-1]
        px, py, pw, ph = prev["box"]
        x, y, w, h = row["box"]
        hmed = max(1.0, (ph + h) / 2.0)
        gap = y - (py + ph)
        same_col = abs(x - px) <= max(6.0, 0.6 * hmed)
        height_ok = 0.55 <= (h / float(max(1, ph))) <= 1.8
        gap_ok = -0.35 * hmed <= gap <= max(5.0, 0.55 * hmed)
        if _is_label(prev, row):
            # 说话人名字单独一行（「Aoi」）不能被卷进正文，否则译文里会冒出「葵嘿，…」
            prev["label"] = True
            groups.append(cur)
            cur = [row]
            continue
        if same_col and height_ok and gap_ok and _can_continue(prev, row):
            cur.append(row)
        else:
            groups.append(cur)
            cur = [row]
    groups.append(cur)

    merged = []
    for grp in groups:
        if len(grp) == 1:
            merged.append(dict(grp[0]))
            continue
        text = ""
        for g in grp:
            text = _join_text(text, g["text"])
        x0 = min(g["box"][0] for g in grp)
        y0 = min(g["box"][1] for g in grp)
        x1 = max(g["box"][0] + g["box"][2] for g in grp)
        y1 = max(g["box"][1] + g["box"][3] for g in grp)
        nchar = sum(len(g["text"]) for g in grp) or 1
        score = sum(g["score"] * len(g["text"]) for g in grp) / nchar
        langs = [g["lang"] for g in grp]
        lang = "ja" if "ja" in langs else max(set(langs), key=langs.count)
        merged.append(
            {
                "text": norm_text(text),
                "score": round(float(score), 4),
                "box": [x0, y0, x1 - x0, y1 - y0],
                "lang": lang,
                "rec": "merged",
                "score_ch": round(sum((g.get("score_ch") or 0.0) * len(g["text"]) for g in grp) / nchar, 4),
                "score_ja": None,
                "h": y1 - y0,
                "parts": len(grp),
            }
        )
    merged = [m for m in merged if not m.get("label")]
    merged.sort(key=lambda r: (r["box"][1], r["box"][0]))
    return merged
