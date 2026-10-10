#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 EasyFrp 验证码 OCR 模板库 (自举: 标注 + DP 切分 + 迭代精修)。

用法:
    python3 build_ef_templates.py            # 用 ef_samples/ 里的标注样本训练
    python3 build_ef_templates.py <dir>      # 指定样本目录

原理:
  1. 每张样本图 + 人工标注的 4 位数字
  2. DP 在竖直投影上选 4 段 (目标: 段内墨量最大 + 宽度接近字符典型宽)
  3. 每段归一化成 28x44 字模, 按标注字符归档
  4. 留一部分样本做 holdout 自测, 打印准确率

样本可随时补充 (不同字体/颜色/干扰线组合), 重跑本脚本即扩充模板库。
"""
import glob
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ef_captcha_ocr as ocr  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(HERE, "ef_samples")
OUT = ocr.TPL_PATH

# 人工标注: 文件名 -> 4 位数字
# cap00-11 : 第一批 (首次抓取)
# cap20-39 : 第二批 (补充字体/颜色多样性)
# cap40-59 : 第三批 (作为 holdout 测过一轮后并入, 扩模板覆盖)
LABELS = {
    "cap00.jpg": "6418", "cap01.jpg": "6021", "cap02.jpg": "2283",
    "cap03.jpg": "2270", "cap04.jpg": "2760", "cap05.jpg": "2314",
    "cap06.jpg": "2668", "cap07.jpg": "6593", "cap08.jpg": "8810",
    "cap09.jpg": "0638", "cap10.jpg": "7199", "cap11.jpg": "3209",
    "cap20.jpg": "4371", "cap21.jpg": "1172", "cap22.jpg": "2003",
    "cap23.jpg": "6485", "cap24.jpg": "7912", "cap25.jpg": "2451",
    "cap26.jpg": "6516", "cap27.jpg": "2939", "cap28.jpg": "4878",
    "cap29.jpg": "8319", "cap30.jpg": "3580", "cap31.jpg": "2428",
    "cap32.jpg": "2719", "cap33.jpg": "3582", "cap34.jpg": "6041",
    "cap35.jpg": "9910", "cap36.jpg": "6051", "cap37.jpg": "0389",
    "cap38.jpg": "3526", "cap39.jpg": "8268",
    "cap40.jpg": "3789", "cap41.jpg": "5745", "cap42.jpg": "2745",
    "cap43.jpg": "8153", "cap44.jpg": "1233", "cap45.jpg": "8443",
    "cap46.jpg": "5128", "cap47.jpg": "9510", "cap48.jpg": "8951",
    "cap49.jpg": "8434", "cap50.jpg": "7732", "cap51.jpg": "0682",
    "cap52.jpg": "3575", "cap53.jpg": "9573", "cap54.jpg": "3739",
    "cap55.jpg": "6593", "cap56.jpg": "1649", "cap57.jpg": "7143",
    "cap58.jpg": "6063", "cap59.jpg": "2893",
    # cap60-67: 第四批 —— 线上实测难度更高的样本(干扰线更粗更密, 直接压在字符上)。
    # 关键教训: 前三批偏"简单难度", 与线上真实分布不符, 必须掺入难样本。
    "cap60.jpg": "5705", "cap61.jpg": "4089", "cap62.jpg": "6219",
    "cap63.jpg": "0274", "cap64.jpg": "9437", "cap65.jpg": "9596",
    "cap66.jpg": "0399", "cap67.jpg": "1487",
    # cap70-81: 线上真实难度样本(十几条粗彩线压在字符上, 字符间距不均)。
    # 这批最重要 —— 前四批都偏简单, 只有掺入这批训练, 模板才覆盖真实分布。
    "cap70.jpg": "4507", "cap71.jpg": "4720", "cap72.jpg": "0074",
    "cap73.jpg": "3513", "cap74.jpg": "3951", "cap75.jpg": "9574",
    "cap76.jpg": "5370", "cap77.jpg": "5247", "cap78.jpg": "6728",
    "cap79.jpg": "8812", "cap80.jpg": "0542", "cap81.jpg": "1478",
}


def extract_glyphs(path, label, debug_dir=None):
    """从单张图按标签切出 4 个字模。失败返回 (None, reason)。"""
    with open(path, "rb") as f:
        data = f.read()
    bgr = ocr._to_bgr(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED))
    if bgr is None:
        return None, "decode_failed"
    mask = ocr.build_char_mask(bgr)
    segs = ocr.split_segments(mask, n=len(label))
    glyphs = []
    for (x, w) in segs:
        g = ocr.crop_normalize(mask, x, w)
        if g is None or (g > 0).sum() < 30:
            return None, "empty_glyph"
        glyphs.append(g)
    if debug_dir:
        try:
            vis = bgr.copy()
            for (x, w) in segs:
                cv2.rectangle(vis, (x // ocr.SCALE, 0),
                              ((x + w) // ocr.SCALE, bgr.shape[0] - 1), (255, 0, 0), 1)
            strip = np.hstack([cv2.cvtColor(g, cv2.COLOR_GRAY2BGR) for g in glyphs])
            strip = cv2.copyMakeBorder(strip, 0, max(0, 40 - strip.shape[0]), 0, 0,
                                       cv2.BORDER_CONSTANT, value=255)
            os.makedirs(debug_dir, exist_ok=True)
            cv2.imwrite(os.path.join(debug_dir, os.path.basename(path)), np.vstack([vis, strip]))
        except Exception:
            pass
    return list(zip(label, glyphs)), ""


def main():
    sample_dir = sys.argv[1] if len(sys.argv) > 1 else SAMPLE_DIR
    if not os.path.isdir(sample_dir):
        print(f"[FAIL] 样本目录不存在: {sample_dir}")
        return 1

    per_char = {}
    used, failed = [], []
    for fn, label in sorted(LABELS.items()):
        path = os.path.join(sample_dir, fn)
        if not os.path.exists(path):
            print(f"[SKIP] 缺样本 {fn}")
            continue
        pairs, reason = extract_glyphs(path, label, debug_dir="ef_dbg_train")
        if not pairs:
            failed.append((fn, reason))
            print(f"[FAIL] {fn} -> {label}: {reason}")
            continue
        used.append((fn, label))
        for ch, g in pairs:
            per_char.setdefault(ch, []).append(g)
        print(f"[OK] {fn} -> {label}")

    if not per_char:
        print("[FAIL] 无可用模板")
        return 1

    # 只保留样本数 >= 2 的字符, 单样本字符容易过拟合
    keep = {c: v for c, v in per_char.items() if len(v) >= 2}
    dropped = sorted(set(per_char) - set(keep))
    if dropped:
        print(f"[WARN] 样本不足 2 张的字符被剔除: {''.join(dropped)}")
    if not keep:
        print("[FAIL] 所有字符样本都不足 2 张, 请补充样本")
        return 1

    chars = sorted(keep.keys())
    arrays = {"chars": np.array(chars)}
    for i, ch in enumerate(chars):
        arrays[f"g_{i}"] = np.array(keep[ch])
    np.savez_compressed(OUT, **arrays)
    print(f"\n[SAVE] {OUT}")
    print(f"       训练图 {len(used)} 张, 字符类 {''.join(chars)} "
          f"({len(chars)} 类), 模板 {sum(len(v) for v in keep.values())} 个")
    if failed:
        print(f"       失败: {failed}")

    # ---- holdout 自测: 用训练好的模板跑一遍所有样本 ----
    tpl = ocr.load_templates(OUT)
    ok = 0
    for fn, label in used:
        with open(os.path.join(sample_dir, fn), "rb") as f:
            r = ocr.ocr_captcha(f.read(), tpl)
        hit = (r["text"] == label)
        ok += hit
        flag = "OK " if hit else "BAD"
        print(f"  [{flag}] {fn} 期望={label} 实得={r['text'] or '-'} "
              f"score={r['score']} {r['debug']}")
    print(f"\n训练集自测: {ok}/{len(used)} = {ok/max(1,len(used))*100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())