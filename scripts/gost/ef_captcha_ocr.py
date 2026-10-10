#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyFrp 图形验证码 OCR (纯 cv2, 无第三方 ML 依赖)

验证码形态 (2026-10-10 实测 12 张样本): 120x40 JPEG, 4 位数字, 深色粗体 + 多条彩色细干扰线。

识别思路 (切分 + 模板匹配):
  1. 暗度二值化 (V <= 205) + 距离变换笔画宽度过滤 -> 字符掩膜
     (实测数据: 图像 S 中位数仅 ~11, 字符是"低饱和深色", 判据必须以暗度为主)
  2. 已知位数=4, DP 在竖直投影上切 4 段 (目标: 段内墨量 - 宽度偏离惩罚),
     再在 ±radius 内把切点挪到墨量低谷, 使每段贴紧一个完整字符
  3. 每段裁剪 -> 去稀疏行 + 等比归一化到 28x44
  4. 与内置模板库 cv2.matchTemplate (TM_CCOEFF_NORMED) 逐字匹配
  5. 任一字分数 < MIN_CHAR_SCORE -> 判为不可信, 上层换图重试

模板库由 build_ef_templates.py 从人工标注样本生成, 落盘 templates_ef.npz。
验证码 key 一次性且可重复提交, 识别不可信时换图重试成本极低。
"""
import base64
import os

import cv2
import numpy as np

TPL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates_ef.npz")

EXPECT_LEN = 4            # 期望位数
GLYPH_W, GLYPH_H = 28, 44  # 模板/候选的归一化尺寸
SCALE = 3                 # 内部放大倍数 (越大越慢, 3 在 120x40 上够用)

V_TH = 205               # 暗度阈值: 字符是最深的一批像素 (背景 V≈250)
WIDTH_MIN = 1.5          # 笔画半宽下限, 低于此视为 1-3px 细干扰线

# 匹配可信度阈值 (两者任一不满足即判为不可信 -> 换图重试)
MIN_CHAR_SCORE = 0.45    # 单字符匹配分下限
MIN_MARGIN = 0.045       # top1 与 top2 的最小分差(区分度); 低于说明字模没吃准

# DP 切分: 切点"低墨量"奖励系数 (字符之间的空白处切才是合理的)
CUT_BONUS = 2.0


def _to_bgr(img):
    """统一成 3 通道 BGR (JPEG 解码可能是 BGRA)。"""
    if img is None:
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img[:, :, :3]


def build_char_mask(bgr):
    """提取字符掩膜 (255=字符)。返回 SCALE 倍放大的单通道图。

    实测要点: 字符主体是"低饱和深色"(S 中位数仅 ~11), 所以判据必须以暗度为主。
    干扰线多为浅色(V 接近背景); 少数深色线靠"笔画宽度"剔除。
    """
    up = cv2.resize(bgr, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    val = cv2.cvtColor(up, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.int32)
    cand = (val <= V_TH).astype(np.uint8) * 255
    # 笔画宽度过滤: 半宽 >= WIDTH_MIN 才是字符笔画
    dist = cv2.distanceTransform((cand > 0).astype(np.uint8), cv2.DIST_L2, 3)
    mask = ((dist >= WIDTH_MIN) & (cand > 0)).astype(np.uint8) * 255
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = _drop_thin_lines(mask)
    return _strip_thin_pixels(mask)


def _drop_thin_lines(mask):
    """剔除细长线状连通域 (深色干扰线残留)。

    干扰线在掩膜里表现为"细长条": 面积不小(与字符交叉处变粗)但**平均笔画宽度**很小。
    用"面积 / 外接框周长"近似平均宽度, 阈值取字符笔画宽度的一半左右。
    比单纯看 w/h 更稳 —— 斜线(长且斜)的 w/h 接近 1, 靠宽高比筛不掉。
    """
    bw = (mask > 0).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(bw, 8)
    out = np.zeros_like(bw)
    H = mask.shape[0]
    # 参考: 掩膜高度约 3x 原图(40px) -> 字符高约 0.5H, 笔画宽约 6-9px(SCALE=3)
    min_stroke = max(2.0, H * 0.045)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 40:
            continue
        # 平均笔画宽度 = 面积 / (外接框半周长), 对斜线/横线/竖线都适用
        avg_w = area / max(1.0, (w + h))
        ar = w / max(h, 1)
        # 规则: 平均宽度过细 => 线; 或极端扁长 => 线
        if avg_w < min_stroke and (ar > 1.6 or ar < 0.55):
            continue
        if ar > 5.0 and h < 0.55 * H:
            continue
        if ar < 0.10 and h > 0.85 * H:
            continue
        out[lab == i] = 1
    return out * 255


def _strip_thin_pixels(mask, min_half=2.2):
    """剔除掩膜里"细线状"像素, 只保留足够粗的笔画。

    背景: 干扰线常常**穿过字符内部**, 在列区间切分后依然留在字模里。
    行投影裁剪对它无效(线在图中间), 连通域过滤也无效(线与字粘连)。
    但形态学尺度可分 —— 干扰线宽1-3px, 字符笔画宽 6-10px(SCALE=3)。
    做法: 距离变换求每像素"到背景的距离"(≈笔画半宽), 低于 min_half 的判为细线。
    之后用原图灰度重建这些像素(而非置黑), 避免在字符上挖出空洞。
    """
    bw = (mask > 0).astype(np.uint8)
    dist = cv2.distanceTransform(bw, cv2.DIST_L2, 3)
    thin = (dist < min_half) & (bw > 0)
    if not thin.any():
        return mask
    out = mask.copy()
    # 细线像素直接置黑: 干扰线本就与背景同色, 置黑不会破坏字符完整性
    # (字符笔画半宽 >= 3, 不会被 thin 覆盖)
    out[thin] = 0
    return out


def _trim_rows(sub, keep_ratio=0.22):
    """裁掉干扰线残留, 只保留字符主体所在的行带。

    根因(实测): 字模里混着斜向干扰线碎片, 导致模板被污染 -> 换新图就匹配不上。
    斜线碎片在"整段列区间"的行投影上墨量很低(每行仅几个像素),
    而字符笔画行墨量高得多。用峰值比例阈值即可把两端斜线切掉。

    keep_ratio 需足够大以免削掉字符的上下笔画(如"2"的顶横、"7"的顶横),
    实测 0.22~0.30 区间效果最好。
    """
    rows = (sub > 0).sum(axis=1).astype(np.float32)
    if rows.max() <= 0:
        return sub
    thr = max(3.0, rows.max() * keep_ratio)
    ys = np.where(rows >= thr)[0]
    if len(ys) == 0:
        return sub
    return sub[ys.min():ys.max() + 1, :]


def crop_normalize(mask, x, w, size=(GLYPH_W, GLYPH_H)):
    """裁 (x, w) 列区间 -> 去稀疏行 + 左右空白 -> 等比缩放居中到 size。"""
    sub = mask[:, x:x + w]
    if sub.size == 0 or (sub > 0).sum() == 0:
        return None
    sub = _trim_rows(sub)
    cols = np.where((sub > 0).any(axis=0))[0]
    if len(cols) == 0:
        return None
    sub = sub[:, cols.min():cols.max() + 1]
    sh, sw = sub.shape
    inner_h, inner_w = size[1] - 4, size[0] - 4
    s = min(inner_w / sw, inner_h / sh)
    nw, nh = max(1, int(round(sw * s))), max(1, int(round(sh * s)))
    r = cv2.resize(sub, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((size[1], size[0]), np.uint8)
    canvas[(size[1] - nh) // 2:(size[1] - nh) // 2 + nh,
           (size[0] - nw) // 2:(size[0] - nw) // 2 + nw] = r
    return canvas


def normalize_glyph(mask, x, y, w, h, size=(GLYPH_W, GLYPH_H)):
    """兼容入口: 给定完整框 (x,y,w,h) 时按框裁剪。"""
    sub = mask[y:y + h, x:x + w]
    if sub.size == 0:
        return np.zeros((size[1], size[0]), np.uint8)
    sub = _trim_rows(sub)
    rows = np.where((sub > 0).any(axis=1))[0]
    cols = np.where((sub > 0).any(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return np.zeros((size[1], size[0]), np.uint8)
    sub = sub[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    sh, sw = sub.shape
    ih, iw = size[1] - 4, size[0] - 4
    s = min(iw / sw, ih / sh)
    nw, nh = max(1, int(round(sw * s))), max(1, int(round(sh * s)))
    r = cv2.resize(sub, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((size[1], size[0]), np.uint8)
    canvas[(size[1] - nh) // 2:(size[1] - nh) // 2 + nh,
           (size[0] - nw) // 2:(size[0] - nw) // 2 + nw] = r
    return canvas


def load_templates(path=TPL_PATH):
    """加载模板库 -> {char: [glyph, ...]}"""
    if not os.path.exists(path):
        return {}
    z = np.load(path)
    return {str(ch): [z[f"g_{i}"][j] for j in range(z[f"g_{i}"].shape[0])]
            for i, ch in enumerate(z["chars"])}


def col_profile(mask):
    """竖直投影: 每列的墨量, 归一化到 0~1。"""
    col = (mask > 0).sum(axis=0).astype(np.float64)
    m = col.max()
    return col / m if m > 0 else col


def split_segments(mask, n=EXPECT_LEN, refine=True):
    """把掩膜切成 n 段 (每段一个字符)。

    两步:
      1. DP 切分 —— 目标 = 段内墨量 - 宽度偏离惩罚 + 切点"低墨量"奖励。
         * 宽度惩罚必需: 纯最大化墨量会退化成均分(前缀和让切分点边际收益恒为 0)
         * 切点奖励是关键: 字符之间是空白, 切在墨量低谷才合理。
           缺了它时 DP 会为了多拿墨量而把切点放在字符笔画中间(实测导致字模残缺)
      2. 边界精修 —— 在 ±radius 内把切点挪到墨量最低处, 使段贴紧完整字符。

    返回 [(x, w), ...]; 切不出 n 段返回 []。
    """
    col = col_profile(mask)
    W = len(col)
    if W == 0 or col.max() <= 0:
        return []
    min_w = max(10, int(W * 0.045))
    max_w = max(min_w + 4, int(W * 0.28))
    target = W / n

    INF = -1e18
    pre = np.concatenate([[0.0], np.cumsum(col)])
    dp = np.full((n + 1, W + 1), INF, np.float64)
    back = np.full((n + 1, W + 1), -1, np.int32)
    dp[0][0] = 0.0
    for k in range(1, n + 1):
        i_max = W - (n - k) * min_w
        for i in range(1, i_max + 1):
            lo = 0 if k == 1 else (k - 1) * min_w
            hi = i - min_w
            if hi < lo:
                continue
            js = np.arange(lo, hi + 1)
            sw = (i - js).astype(np.float64)
            ok = (sw >= min_w) & (sw <= max_w)
            # 宽度偏离惩罚 + 切点低墨奖励
            pen = 0.9 * ((sw - target) / target) ** 2 * target
            cut_bonus = CUT_BONUS * col[js]
            vals = dp[k - 1][js] + (pre[i] - pre[js]) - pen + cut_bonus
            valid = (dp[k - 1][js] > INF / 2) & ok
            if not valid.any():
                continue
            vals = np.where(valid, vals, INF)
            j = int(np.argmax(vals))
            dp[k][i] = vals[j]
            back[k][i] = js[j]
    if dp[n][W] <= INF / 2:
        return []
    segs = []
    i = W
    for k in range(n, 0, -1):
        j = int(back[k][i])
        segs.append((j, i - j))
        i = j
    segs.reverse()

    if not refine:
        return segs
    # --- 边界精修: 在 ±radius 内找墨量低谷 ---
    radius = max(4, int(W * 0.035))
    out = []
    for idx, (x, w) in enumerate(segs):
        lo_b = segs[idx - 1][0] + segs[idx - 1][1] if idx > 0 else 0
        hi_b = segs[idx + 1][0] if idx + 1 < len(segs) else W
        a0, a1 = max(lo_b + 1, x - radius), min(hi_b - 1, x + radius)
        xs = int(a0 + np.argmin(col[a0:a1 + 1])) if a1 > a0 else x
        b0, b1 = max(x + 1, x + w - radius), min(hi_b, x + w + radius)
        xe = int(b0 + np.argmin(col[b0:b1 + 1])) if b1 > b0 else x + w
        out.append((xs, xe - xs) if xe - xs >= 8 else (x, w))
    return out


def match_glyph(glyph, templates):
    """单字符匹配 -> (best_char, best_score, margin)。

    margin = best - second, 反映"区分度"。字模来自掩膜, 粘连/残缺时
    top1 与 top2 会非常接近(实测常只差 0.01~0.07), 此时识别不可信,
    上层应换图重试, 而不是硬猜一个。
    """
    best_ch, best_s, second = "", -1.0, -1.0
    for ch, gls in templates.items():
        s = -1.0
        for g in gls:
            r = cv2.matchTemplate(glyph, g, cv2.TM_CCOEFF_NORMED)
            s = max(s, float(r.max()))
        if s > best_s:
            second = best_s
            best_s, best_ch = s, ch
        elif s > second:
            second = s
    return best_ch, best_s, best_s - second


def ocr_captcha(img_bytes, templates=None, expect=EXPECT_LEN, debug=False):
    """识别验证码。返回 {ok, text, score, min_score, min_margin, chars, debug}。

    ok=False -> 识别不可信 (无模板 / 切不出 expect 段 / 分数或区分度不足),
    上层应换一张验证码重试 (key 一次性且可重复提交, 重试成本极低)。
    """
    if templates is None:
        templates = load_templates()
    if not templates:
        return {"ok": False, "text": "", "score": 0.0, "min_score": 0.0,
                "min_margin": 0.0, "chars": [], "debug": "no_templates"}

    buf = np.frombuffer(img_bytes, np.uint8)
    bgr = _to_bgr(cv2.imdecode(buf, cv2.IMREAD_UNCHANGED))
    if bgr is None:
        return {"ok": False, "text": "", "score": 0.0, "min_score": 0.0,
                "min_margin": 0.0, "chars": [], "debug": "decode_failed"}

    mask = build_char_mask(bgr)
    segs = split_segments(mask, n=expect)
    glyphs = [crop_normalize(mask, x, w) for (x, w) in segs]

    if debug:
        try:
            vis = bgr.copy()
            for (x, w) in segs:
                cv2.rectangle(vis, (x // SCALE, 0),
                              ((x + w) // SCALE, bgr.shape[0] - 1), (255, 0, 0), 1)
            mdisp = cv2.resize(cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR), (bgr.shape[1], bgr.shape[0]))
            cv2.imwrite("/tmp/_ef_dbg.jpg", np.hstack([bgr, mdisp, vis]))
        except Exception:
            pass

    if len(segs) != expect or any(g is None for g in glyphs):
        return {"ok": False, "text": "", "score": 0.0, "min_score": 0.0,
                "min_margin": 0.0, "chars": [], "debug": f"seg={len(segs)}"}

    chars = []
    for g in glyphs:
        ch, s, mg = match_glyph(g, templates)
        chars.append((ch, round(s, 3), round(mg, 3)))
    text = "".join(c for c, _, _ in chars)
    scores = [s for _, s, _ in chars]
    margins = [m for _, _, m in chars]
    weak = (min(scores) < MIN_CHAR_SCORE) or (min(margins) < MIN_MARGIN)
    return {
        "ok": not weak,
        "text": text,
        "score": round(float(np.mean(scores)), 3),
        "min_score": round(float(min(scores)), 3),
        "min_margin": round(float(min(margins)), 3),
        "chars": chars,
        "debug": ("low_score" if min(scores) < MIN_CHAR_SCORE else
                  ("low_margin" if min(margins) < MIN_MARGIN else "")),
    }


def ocr_bs64(bs64, **kw):
    """直接识别接口返回的 data.bs64。"""
    return ocr_captcha(base64.b64decode(bs64), **kw)


if __name__ == "__main__":
    import glob
    import sys
    tpl = load_templates()
    print(f"模板库: {len(tpl)} 类 / {sum(len(v) for v in tpl.values())} 模板")
    pat = sys.argv[1] if len(sys.argv) > 1 else "ef_samples/*.jpg"
    for f in sorted(glob.glob(pat)):
        with open(f, "rb") as fh:
            r = ocr_captcha(fh.read(), tpl)
        print(f"{f}: ok={r['ok']} text={r['text']} score={r['score']} {r['debug']} {r['chars']}")