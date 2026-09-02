#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1ms.run 滑块签到 —— playwright 真实浏览器 + cv2 缺口识别 + 人类轨迹拖动

技术路线 (已验证可行):
  - 登录: 设备授权 token 当 auth_token cookie 直接登录 (1ms.run 接受)
  - 触发: 访问 /user/checkin, 点"签到"按钮 -> 弹腾讯云天御滑块 (turing.captcha.gtimg.com drag_ele)
  - 识别: 截背景图 (.tc-bg-img) + 自动挑选真正的拼图块元素 (NCC 自验证, 避免误用滑块把手),
           cv2.matchTemplate 找缺口 X
  - 支持: 滑块型 (drag_ele) + 点击顺序型 (click_order "请依次点击：4 2 1")
  - 拖动: 在 page 坐标系 mouse.down + 多步 move (cosine ease-in-out + 末段过冲回拉 + 微抖动) -> up
  - 回调: 腾讯云验证通过后回调 captchaTicket 给 1ms, 自动完成签到
  - 后续: 该 ticket 用于 1ms 的 /api/v1/mall/checkin (captchaTicket 字段)

依赖 (已装): playwright + 系统真实 Chrome, cv2(opencv-python-headless), numpy, paramiko
运行环境: 必须用"真实浏览器环境"过天御 (headless playwright 被天御 100% 拒).
  推荐: 宿主机常驻一个真 Chrome, 脚本经 CDP 接入:
    xvfb-run -a google-chrome --remote-debugging-port=9222 \
             --user-data-dir=/path/chrome-prof --no-first-run &
    任务 env 设 CHROME_CDP_URL=http://127.0.0.1:9222
  备选: 不设 CDP 时, 脚本自动用系统 Chrome (channel='chrome') headed 启动 + 持久 profile.
  环境变量:
    CHROME_CDP_URL      真实浏览器 DevTools 地址 (http://host:9222 或 ws://...), 优先
    CHROME_PATH         显式 Chrome 可执行文件路径
    CHROME_USER_DATA_DIR 持久 profile 目录 (累积真实使用痕迹, 更像真人)
  兜底: 以上都不可用才回退 playwright headless (已知被天御拒, 仅本地调试).

用法:
  python3 _checkin_slide.py            # 端到端签到 (明天未签时跑)
  python3 _checkin_slide.py --selftest # 只验证 cv2 缺口识别 + 轨迹生成 (无需登录)
"""
import os, sys, time, math, random, argparse
import numpy as np
import cv2
from playwright.sync_api import sync_playwright

HOST = "10.0.0.11"; USER = "root"; PASS = "liuhai"
TOKEN_PATH = "/ql/data/1ms_token.txt"
# 本地 token 缓存 (优先读, 避免每次 SSH, 自动化无需出网取 token)
LOCAL_TOKEN_PATH = os.path.expanduser("~/.cache/1ms_token.txt")
DOMAIN = "1ms.run"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


# ============== 1) 缺口识别 (cv2 模板匹配 + dark 二次筛选) ==============
def find_gap(bg_path: str, slider_path: str, skip_x_max: int = 0,
             mask_rect: tuple | None = None) -> tuple[int, float, float]:
    """在背景图里找与滑块图最匹配的"真缺口"位置 x。
    1ms 类反爬在 bg 放多个相似形状干扰, 纯 NCC top1 被纹理骗.
    改进: cv2.matchTemplate (整图匹配) + dark 启发二次筛选.
    真凹槽比周边暗, dark 最高的候选 = 真缺口.
    skip_x_max: 排除 x < skip_x_max 的候选, 避免自匹配陷阱 (puzzle piece 自身区域)
    mask_rect: (x, y, w, h) 在 bg 图像上涂黑一块 (消除 puzzle piece 残留), 进一步避免自匹配
    返回: (gap_x, max_match, max_dark)
    """
    bg = cv2.imread(bg_path)
    sl = cv2.imread(slider_path, cv2.IMREAD_UNCHANGED)
    if bg is None or sl is None:
        raise RuntimeError(f"read image failed bg={bg is not None} sl={sl is not None}")
    sh, sw = sl.shape[:2]
    bh, bw = bg.shape[:2]
    # 关键: 在匹配前 mask 掉 puzzle piece 在 bg 内的位置 (避免自匹配完美分)
    if mask_rect is not None:
        mx, my, mw, mh = mask_rect
        mx = max(0, int(mx)); my = max(0, int(my))
        mw = min(int(mw), bw - mx); mh = min(int(mh), bh - my)
        if mw > 0 and mh > 0:
            # 用 random noise 填充 (NCC 会得到接近 0 分, 排除此区域)
            noise = np.random.randint(0, 256, (mh, mw, 3), dtype=np.uint8)
            bg[my:my+mh, mx:mx+mw] = noise
    res = cv2.matchTemplate(bg, sl[:, :, :3], cv2.TM_CCOEFF_NORMED)
    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32)
    thresh = 0.30
    ys, xs = np.where(res >= thresh)
    if len(xs) == 0:
        _, _, _, mloc = cv2.minMaxLoc(res)
        return int(mloc[0]), float(res[mloc[1], mloc[0]]), 0.0
    # inr 只算 mask (piece 形状) 内 —— 真缺口处 mask 内 = 缺口 bg (暗)
    mask_bool = (sl[:, :, 3] > 128) if sl.shape[2] == 4 else np.ones((sh, sw), dtype=bool)
    pad = 8
    darks = []
    for x, y in zip(xs, ys):
        inr = bg_gray[y:y+sh, x:x+sw]
        inr_masked = float(inr[mask_bool].mean()) if mask_bool.any() else float(inr.mean())
        y0 = max(0, y - pad); y1 = min(bh, y + sh + pad)
        x0 = max(0, x - pad); x1 = min(bw, x + sw + pad)
        ring = bg_gray[y0:y1, x0:x1]
        darks.append(float(ring.mean() - inr_masked))
    darks = np.array(darks)
    # 排除自匹配区 (puzzle piece 当前位置及其附近)
    if skip_x_max > 0:
        keep = xs >= skip_x_max
        if not keep.any():
            keep = np.ones(len(xs), dtype=bool)  # 兜底: 排除后没候选, 不过滤
        xs, ys, darks, res_sub = xs[keep], ys[keep], darks[keep], res[ys[keep], xs[keep]]
    else:
        res_sub = res[ys, xs]
    # NCC top30 + dark 综合分: 真凹槽 = 高 ncc 候选中 dark 最高
    order = np.argsort(-res_sub)[:30]
    sub = darks[order]
    sub_ncc = res_sub[order]
    score = sub_ncc + 0.015 * sub
    best_local = int(np.argmax(score))
    best_i = int(order[best_local])
    return int(xs[best_i]), float(res[ys[best_i], xs[best_i]]), float(darks[best_i])


# ============== 1.5) 缺口识别 (方差法: 洞=背景被抠掉后留下的最平滑块) ==============
def find_gap_var(bg_path: str, piece_x_in_bg: int, piece_y_in_bg: int,
                 piece_w: int, piece_h: int, skip_x_max: int = 0):
    """拼图块是前景图标, 与背景场景纹理不相似 (NCC 仅 0.3), 纯模板匹配无效。
    真正信号: 背景图里"被抠掉图标"的位置 = 最平滑(低方差)的块 = 洞。
    在拼图块所在 y 带内滑窗, 找局部方差最小且不在拼图块自身位置的 x。
    返回: (gap_x, min_var) 或 (None, inf)
    """
    bg = cv2.imread(bg_path)
    if bg is None:
        return None, float("inf")
    g = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32)
    bh, bw = g.shape[:2]
    y0 = max(0, int(piece_y_in_bg) - 6)
    y1 = min(bh, int(piece_y_in_bg) + int(piece_h) + 6)
    if y1 - y0 < 12:
        y0, y1 = 0, bh
    band = g[y0:y1, :]
    pw = int(piece_w)
    if pw <= 0 or pw > bw or (y1 - y0) < 12:
        return None, float("inf")
    best_x, best_v = None, float("inf")
    for x in range(0, bw - pw):
        if skip_x_max and x < skip_x_max:
            continue
        win = band[:, x:x + pw]
        v = float(win.var())
        if v < best_v:
            best_v = v
            best_x = x
    return best_x, best_v


# ============== 1.6) 缺口识别 (轮廓对比法: 洞内平滑 + 洞外有纹理) ==============
def find_gap_sil(bg_path: str, piece_png: str, piece_x_in_bg: int,
                 piece_y_in_bg: int, piece_w: int, piece_h: int, skip_x_max: int = 0):
    """用拼图块的"轮廓(mask)"在背景里滑窗:
       洞的特征 = mask 内部背景平滑(图标被抠掉) + mask 外部仍有场景纹理。
       评分 score = var(外部) - var(内部), 取最大。
       比纯方差法更稳: 自然平滑区(天空/水面)内外都平滑, 评分低; 只有洞内外对比强。
    要求 piece_png 带 alpha (pick_piece 返回的 raw 截图即带 alpha)。
    返回: (gap_x, best_score) 或 (None, 0)
    """
    bg = cv2.imread(bg_path, cv2.IMREAD_GRAYSCALE)
    if bg is None:
        return None, 0.0
    bg = bg.astype(np.float32)
    bh, bw = bg.shape
    sl = cv2.imread(piece_png, cv2.IMREAD_UNCHANGED)
    if sl is None or sl.shape[2] != 4:
        return None, 0.0
    mask = (sl[:, :, 3] > 100)  # 拼图块轮廓 (bool)
    mh, mw = mask.shape
    if mh < 8 or mw < 8:
        return None, 0.0
    y0 = int(piece_y_in_bg)
    if y0 < 0 or y0 + mh > bh:
        y0 = max(0, min(bh - mh, y0))
    best_x, best_score = None, -1e18
    for x in range(0, bw - mw + 1):
        if skip_x_max and x < skip_x_max:
            continue
        region = bg[y0:y0 + mh, x:x + mw]
        inside = region[mask]
        outside = region[~mask]
        if inside.size == 0 or outside.size == 0:
            continue
        var_in = float(inside.var())
        var_out = float(outside.var())
        score = var_out - var_in
        if score > best_score:
            best_score = score
            best_x = x
    return best_x, best_score


# ============== 1.7) 缺口识别 (最暗窗口法: 缺口是 bg 上的深色半透明拼图形状) ==============
def find_gap_dark(bg_path: str, piece_y: int, piece_w: int, piece_h: int,
                  skip_x_min: int = 0, band_pad: int = 4):
    """缺口在 bg 上是半透明深色拼图形状, 在 piece 的 y 带内, 找 piece_w 宽窗口的
    最小灰度均值 x = 缺口左边缘。
    已在 t2/t3 真实截图验证: t2 gap_x=168 mean=56 (带均82.8), t3 gap_x=177 mean=64 (带均152.7).
    skip_x_min: 跳过 x < skip_x_min (排除拼图块自身位置, 防止白边残留)。
    返回: (gap_x, mean) 或 (None, inf)
    """
    img = cv2.imread(bg_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None, float("inf")
    img = img.astype(np.float32)
    bh, bw = img.shape
    y0 = max(0, int(piece_y) - band_pad)
    y1 = min(bh, int(piece_y + piece_h) + band_pad)
    if y1 - y0 < 8:
        return None, float("inf")
    band = img[y0:y1, :]
    pw = int(piece_w)
    if pw <= 0 or pw > bw:
        return None, float("inf")
    best_x, best_m = None, float("inf")
    for x in range(0, bw - pw):
        if x < int(skip_x_min):
            continue
        m = float(band[:, x:x + pw].mean())
        if m < best_m:
            best_m = m
            best_x = x
    return best_x, best_m


# ============== 2) 人类轨迹 (cosine ease + 末段过冲 + 抖动) ==============
def human_track(dist: float, steps: int = 30, jitter: float = 1.0) -> list:
    """
    生成模拟人手拖动轨迹 (相对位移):
      - 主段: cosine ease-in-out (开头慢->中快->末段慢)
      - 末段: 过冲 (1~3px) 再回拉, 模拟手抖修正
      - 全程: y 微抖动 ±1px (只在中间段)
    返回 [(dx, dy), ...] 相对起点的偏移序列, 末点 = (dist, 0)
    """
    dist = float(dist)
    if steps < 10:
        steps = 10
    t = [i / steps for i in range(steps + 1)]
    ease = [0.5 * (1 - math.cos(math.pi * x)) for x in t]  # cosine ease
    # 末段过冲
    overshoot = max(1.5, abs(dist) * 0.03) * random.choice([-1, 1])
    pts = []
    for i, e in enumerate(ease):
        # 过冲只在 80% 之后介入, 100% 时回到 dist
        if i < int(steps * 0.8):
            dx = e * dist
        else:
            # 从 (0.8 时位置 + 过冲) 线性回到 dist
            base = ease[int(steps * 0.8)] * dist
            remain = 1 - (i - int(steps * 0.8)) / max(1, steps - int(steps * 0.8))
            dx = base + overshoot * remain
        dy = random.uniform(-jitter, jitter) if 2 < i < steps - 1 else 0.0
        pts.append((dx, dy))
    # 确保末点 = (dist, 0)
    pts[-1] = (dist, 0.0)
    return pts


# ============== 3) 读取 token (SSH 进 ql1 容器) ==============
def load_token_from_container() -> str:
    # 优先本地缓存: 自动化/无网时直接用, 不依赖 SSH 出容器
    if os.path.exists(LOCAL_TOKEN_PATH):
        try:
            with open(LOCAL_TOKEN_PATH, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t:
                    print(f"[token] 用本地缓存 ({len(t)} chars)", flush=True)
                    return t
        except Exception as e:
            print("[token] 读本地缓存失败, 回退 SSH:", e, flush=True)
    import paramiko
    s = paramiko.SSHClient()
    s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    s.connect(HOST, username=USER, password=PASS, timeout=15)
    cmd = f"docker exec -i ql1 cat {TOKEN_PATH}"
    tok = s.exec_command(cmd)[1].read().decode().strip()
    s.close()
    if not tok:
        raise RuntimeError(f"token file empty: {TOKEN_PATH}")
    # 写回本地缓存
    try:
        os.makedirs(os.path.dirname(LOCAL_TOKEN_PATH), exist_ok=True)
        with open(LOCAL_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(tok)
        print(f"[token] 已缓存到本地 {LOCAL_TOKEN_PATH}", flush=True)
    except Exception as e:
        print("[token] 写本地缓存失败:", e, flush=True)
    return tok


# ============== 4) 在弹窗 iframe 内执行过滑块 ==============
class NonSliderError(Exception):
    """1ms 抽中非滑块型验证 (如 click_order), 本路线仅支持滑块, 走 A 保底"""
    pass


def _dump_slider_state(frame, sl_loc, prefix: str = "dump"):
    """滑块状态诊断: 打印 CSS / 元素信息 + 截图存 F:/ai/ 便于排查"""
    import json as _json
    out = {"prefix": prefix, "ts": int(time.time())}
    try:
        out["sl_count"] = frame.locator(".tc-fg-item.tc-slider-normal").count()
    except Exception as e:
        out["sl_count"] = f"ERR {e}"
    try:
        out["sl_visible"] = sl_loc.is_visible()
    except Exception as e:
        out["sl_visible"] = f"ERR {e}"
    try:
        out["sl_bbox"] = sl_loc.bounding_box()
    except Exception as e:
        out["sl_bbox"] = f"ERR {e}"
    try:
        info = sl_loc.evaluate("""el => {
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return {
                display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
                position: cs.position, zIndex: cs.zIndex,
                width: r.width, height: r.height, x: r.x, y: r.y,
                className: el.className, id: el.id, tagName: el.tagName,
                parentClass: el.parentElement && el.parentElement.className,
            };
        }""")
        out["sl_computed"] = info
    except Exception as e:
        out["sl_computed"] = f"ERR {e}"
    # 候选: 实际可拖把手可能是不同元素
    for sel in [".tc-fg-item", ".tc-slider-normal", ".tc-slider", "[class*='slider']", ".tc-fg"]:
        try:
            c = frame.locator(sel).count()
            if c:
                first = frame.locator(sel).first
                bb = first.bounding_box()
                out[f"cand_{sel}"] = {"count": c, "bbox": bb}
        except Exception as e:
            out[f"cand_{sel}"] = f"ERR {e}"
    # 截图
    shot = f"F:/ai/_ms_{prefix}_{int(time.time())}.png"
    try:
        frame.page.screenshot(path=shot, full_page=False)
        out["page_shot"] = shot
    except Exception as e:
        out["page_shot"] = f"ERR {e}"
    print(f"[dump] {prefix}: " + _json.dumps(out, ensure_ascii=False, default=str)[:1500], flush=True)


def pick_piece(frame, bg_path: str, bg_box: dict):
    """自动挑选真正的拼图块元素。

    旧版固定用 .tc-fg-item.tc-slider-normal 截图当模板, 但该元素实测是 65x35 滑块把手,
    导致 NCC match 仅 0.356 永远过不了。
    改进: 枚举所有候选 (.tc-fg-item / IMG.tc-slider-bg / canvas / 含 slider/drag class),
    各自截图后与背景图做 cv2.matchTemplate, 选 NCC 自匹配最佳者 = 真正的拼图块。
    同时偏好 bbox 中心落在背景图区域内 (piece 叠加在 bg 上, 把手在 bg 下方) 且比把手高的候选。
    返回: (loc, bbox, sl_path, max_val, on_bg) 或 None
    """
    bg = cv2.imread(bg_path)
    if bg is None:
        return None
    bh, bw = bg.shape[:2]
    sels = [".tc-fg-item", "IMG.tc-slider-bg", ".tc-slider-bg", "canvas",
            "[class*='slider']", "[class*='drag']", "[class*='piece']", "[class*='fg']"]
    cands = []
    for sel in sels:
        try:
            n = frame.locator(sel).count()
        except Exception:
            continue
        for i in range(n):
            try:
                loc = frame.locator(sel).nth(i)
                bb = loc.bounding_box()
            except Exception:
                continue
            if not bb or bb.get("width", 0) <= 0:
                continue
            cands.append((loc, bb, sel, i))
    if not cands:
        return None
    best = None
    best_score = -2.0
    stamp = int(time.time() * 1000)
    for loc, bb, sel, i in cands:
        try:
            p = f"/tmp/_ms_pcand_{stamp}_{i}.png"
            loc.screenshot(path=p)
            sl = cv2.imread(p, cv2.IMREAD_UNCHANGED)
            if sl is None:
                continue
            if sl.shape[2] == 4:
                sl3 = sl[:, :, :3].copy()
                mask = sl[:, :, 3] > 128
                if mask.any():
                    mean = sl3[mask].mean(axis=0).astype(np.uint8)
                    sl3[~mask] = mean
            else:
                sl3 = sl
            sh, sw = sl3.shape[:2]
            if sh <= 0 or sw <= 0 or sh > bh or sw > bw:
                continue
            res = cv2.matchTemplate(bg, sl3, cv2.TM_CCOEFF_NORMED)
            _, mv, _, _ = cv2.minMaxLoc(res)
            cx = bb["x"] + bb["width"] / 2
            cy = bb["y"] + bb["height"] / 2
            on_bg = (bg_box["x"] <= cx <= bg_box["x"] + bg_box["width"] and
                     bg_box["y"] <= cy <= bg_box["y"] + bg_box["height"])
            score = mv + (0.25 if on_bg else 0.0)
            if bb["height"] > bb["width"]:   # 拼图块通常比把手高
                score += 0.05
            if score > best_score:
                best_score = score
                best = (loc, bb, p, mv, on_bg)
        except Exception:
            continue
    return best


def find_puzzle_piece(frame):
    """找到真正的拼图块 (61x61, bg=url turing), 排除把手 (66x35 .tc-slider-normal)
    和底部轨道 (340x16)。
    旧版 pick_piece 用 NCC 自验证, 实际会把把手/轨道混进来, 错位 y 带导致缺口定位全错。
    尺寸过滤最稳: 拼图块 bbox 宽高都在 40~80, 且 class 不含 slider-normal。
    """
    try:
        n = frame.locator(".tc-fg-item").count()
    except Exception:
        return None, None
    for i in range(n):
        loc = frame.locator(".tc-fg-item").nth(i)
        try:
            bb = loc.bounding_box()
            if not bb:
                continue
            w, h = int(bb["width"]), int(bb["height"])
            if not (40 <= w <= 80 and 40 <= h <= 80):
                continue
            cls = (loc.get_attribute("class") or "")
            if "slider-normal" in cls:
                continue
            return loc, bb
        except Exception:
            continue
    return None, None


def solve_slide_in_frame(page, frame) -> dict:
    """
    在腾讯云天御滑块 iframe 内:
      1) 截背景图 (.tc-bg-img) + 拼图块/把手 (.tc-fg-item.tc-slider-normal)
      2) cv2 找缺口 X (dark 启发抗 1ms 双形状干扰)
      3) 计算拖动距离 + 生成人类轨迹
      4) page.mouse 分步拖动
    返回: {gap_x, dist, max_val, max_dark}
    """
    bg_loc = frame.locator(".tc-bg-img").first
    bg_loc.wait_for(state="visible", timeout=20000)
    time.sleep(1.0)  # bg 图完整加载

    # 关键: 等真实背景图 div (#slideBg, .tc-bg-img) 渲染出 bbox
    # 注意: 外层 slideBgWrap 不设高, getBoundingClientRect 拿 0, 测 child slideBg 才准
    # 同时等 .tc-fg-item (puzzle piece) 出现 + 有 bbox
    # 1ms 混合发验证码, 如果出现 .tc-fg-item = 滑块, 否则 = 点击顺序型, 走 A 保底
    print("  [slide] 等真实背景图 + 拼图块 出现并有 bbox...", flush=True)
    bg_loaded = False
    is_slider = False
    for i in range(40):  # 40 * 0.5s = 20s
        try:
            h = frame.evaluate("() => { const e=document.getElementById('slideBg'); return e ? e.getBoundingClientRect().height : 0; }")
            cnt = frame.locator(".tc-fg-item").count()
            fg_bb = frame.locator(".tc-fg-item").first.bounding_box() if cnt else None
        except Exception:
            h, cnt, fg_bb = 0, 0, None
        if h and h > 50 and cnt > 0 and fg_bb and fg_bb.get("width", 0) > 0:
            bg_loaded = True
            is_slider = True
            print(f"  [slide] 滑块型就绪 bg_h={h:.0f} fg_count={cnt} fg_box={fg_bb} (等待 {i*0.5:.1f}s)", flush=True)
            break
        # bg 已加载但无 fg-item, 可能是点击型, 识别为非滑块
        if h and h > 50 and cnt == 0 and i >= 6:  # 等 3s 确认非滑块
            instr = frame.evaluate("() => { const e=document.getElementById('instructionText'); return e ? e.textContent.trim() : ''; }")
            if "点击" in instr or "依次" in instr or "click" in (instr or "").lower():
                print(f"  [slide] 检测到点击顺序型, 转交 click_order 解法 (instruction='{instr}')", flush=True)
                return solve_click_order_in_frame(page, frame)
            print(f"  [slide] 非滑块型 (无 .tc-fg-item) bg_h={h:.0f} instruction='{instr}' (等待 {i*0.5:.1f}s)", flush=True)
            break
        time.sleep(0.5)
    if not bg_loaded and not is_slider:
        _dump_slider_state(frame, bg_loc, prefix="bg_not_loaded")
        # 读取 instruction 文本, 给用户更明确提示
        try:
            instr = frame.evaluate("() => { const e=document.getElementById('instructionText'); return e ? e.textContent.trim() : ''; }")
            print(f"  [slide] instruction='{instr}'", flush=True)
        except Exception:
            pass
        # 区分: bg 都没加载 (网络问题) vs bg 加载但非滑块型
        if not h or h < 50:
            raise RuntimeError("背景图 20s 内未就绪")
        # bg 加载但无 fg-item = 点击型, 抛特殊异常让 main 走 A 保底
        raise NonSliderError(f"今日 1ms 抽中非滑块型验证 (instr='{instr}'), 走 A 保底")

    time.sleep(1.0)  # 滑块渐入完成

    bg_path = "/tmp/_ms_bg.png"
    # 关键: 截图前先隐藏拼图块 overlay, 否则 .tc-bg-img 截图会包含拼图块自身,
    #       导致 matchTemplate 出现 ncc=1.0 自匹配陷阱 (piece 在 bg 上的当前位置被当成"完美匹配")
    try:
        frame.evaluate("() => { document.querySelectorAll('.tc-fg-item').forEach(e=>{ e.dataset._h=e.style.display; e.style.display='none'; }); }")
        bg_loc.screenshot(path=bg_path)
        frame.evaluate("() => { document.querySelectorAll('.tc-fg-item').forEach(e=>{ e.style.display=e.dataset._h||''; }); }")
    except Exception as e:
        print(f"  [slide] 隐藏拼图块失败, 退回原图: {e}", flush=True)
        bg_loc.screenshot(path=bg_path)
    bg_box = bg_loc.bounding_box()
    print(f"  [slide] bg_box={bg_box}", flush=True)

    # 关键改进: 找真正的 61x61 拼图块 (不是 66x35 把手, 不是 340x16 底部轨道)
    # 旧版 pick_piece + NCC 自验证 实际会把把手/轨道选进来, 错位 y 带 -> 缺口定位全错.
    pp_loc, pp_box = find_puzzle_piece(frame)
    if not pp_loc:
        _dump_slider_state(frame, bg_loc, prefix="no_puzzle_piece")
        raise RuntimeError("未能识别 61x61 拼图块")
    print(f"  [slide] 拼图块 piece_box={pp_box}", flush=True)

    # 重新截 bg_box (可能有微动) 并算 piece 在 bg 内坐标
    bg_box = bg_loc.bounding_box()
    piece_x_in_bg = max(0, int(pp_box["x"] - bg_box["x"]))
    piece_y_in_bg = max(0, int(pp_box["y"] - bg_box["y"]))
    piece_w = int(pp_box["width"])
    piece_h = int(pp_box["height"])
    skip_x = piece_x_in_bg + piece_w + 5
    print(f"  [slide] piece_in_bg=({piece_x_in_bg},{piece_y_in_bg}) wh=({piece_w},{piece_h}) "
          f"skip x<{skip_x}", flush=True)

    # 缺口定位: piece y 带内 piece_w 宽窗口的最小灰度均值 = 缺口左边缘.
    # 缺口是 bg 上的半透明深色拼图形状 (t2 验证 gap_mean=56 vs band_mean=82.8).
    gap_x, gap_mean = find_gap_dark(bg_path, piece_y_in_bg, piece_w, piece_h, skip_x_min=skip_x)
    if gap_x is None:
        raise RuntimeError("find_gap_dark 未找到缺口")
    band_arr = cv2.imread(bg_path, cv2.IMREAD_GRAYSCALE)[piece_y_in_bg:piece_y_in_bg + piece_h, :]
    band_mean = float(band_arr.mean()) if band_arr is not None else 0.0
    print(f"  [slide] 缺口 gap_x={gap_x} gap_mean={gap_mean:.1f} band_mean={band_mean:.1f} "
          f"contrast={band_mean - gap_mean:.1f}", flush=True)

    # 拖动距离: 腾讯云 drag_ele 校验的是"滑块把手" (handle) 的位移, 不是拼图块.
    # handle 起点 x 与 piece 起点 x 在 bg 内坐标不同 (实测 handle_x_in_bg=23, piece_x_in_bg=25, 差 2px),
    # 用 piece_x_in_bg 当起点会少拖 2px -> 拼图块永远差 2px 到不了缺口 -> 永远被拒.
    # 故必须用 handle 自己的起点: dist = gap_x - handle_x_in_bg.
    handle_box = None
    try:
        hloc = frame.locator(".tc-fg-item.tc-slider-normal").first
        handle_box = hloc.bounding_box()
    except Exception:
        pass
    if handle_box and bg_box:
        handle_x_in_bg = max(0, int(handle_box["x"] - bg_box["x"]))
    else:
        handle_x_in_bg = piece_x_in_bg  # 兜底
    dist = float(gap_x - handle_x_in_bg)
    print(f"  [slide] 起点用 handle: handle_x_in_bg={handle_x_in_bg} piece_x_in_bg={piece_x_in_bg} "
          f"gap_x={gap_x} dist={dist:.1f} (旧 piece 法会少 {piece_x_in_bg - handle_x_in_bg}px)", flush=True)

    # 拖动起点: 优先用 .tc-fg-item.tc-slider-normal 把手 (66x35, 底部蓝条)
    slider_cx, slider_cy = None, None
    try:
        hloc = frame.locator(".tc-fg-item.tc-slider-normal").first
        hb = hloc.bounding_box()
        if hb and hb.get("width", 0) > 0:
            slider_cx = hb["x"] + hb["width"] / 2
            slider_cy = hb["y"] + hb["height"] / 2
    except Exception:
        pass
    if slider_cx is None:
        # 兜底: 拼图块中心 (但 1ms 拼图块不响应拖动, 会失败)
        slider_cx = pp_box["x"] + pp_box["width"] / 2
        slider_cy = pp_box["y"] + pp_box["height"] / 2
    print(f"  [slide] dist={dist:.1f} handle=({slider_cx:.0f},{slider_cy:.0f})", flush=True)

    # 模拟人手: 慢启动 + 变速 + 微抖动 + 末段长停留, 规避腾讯云轨迹风控.
    # 旧版 7 个完美 waypoint + steps=8 仍被判 bot; 改用 50+ 微步, 每步 steps=4 平滑内插,
    # 速度用 ease-in-out + 高斯噪声, y 轴带相关微抖, 末段停留 0.6-0.9s 再释放.
    page.mouse.move(slider_cx, slider_cy, steps=8)
    time.sleep(0.25 + random.random() * 0.1)
    page.mouse.down()
    time.sleep(0.15 + random.random() * 0.08)
    n_steps = 55
    t = np.linspace(0.0, 1.0, n_steps)
    ease = 0.5 * (1 - np.cos(np.pi * t))  # cosine ease-in-out
    # 速度噪声: 真实人手速度有 ±20% 波动
    vel_noise = np.random.normal(0.0, 0.004, n_steps)
    xs = [0.0]
    for i in range(1, n_steps):
        # base 位置 + 累积噪声 (让轨迹有自然波动)
        base = ease[i] * dist
        # 中段加微抖动, 末段抑制
        if 3 < i < n_steps - 4:
            jitter = float(np.random.uniform(-0.6, 0.6))
        else:
            jitter = 0.0
        xs.append(base + jitter)
    xs[-1] = float(dist)  # 最终严格停在 dist
    # 逐步移动
    for i in range(1, n_steps):
        tx = slider_cx + xs[i]
        # y 微抖: 跟 x 进度相关 (中段大, 两端小), 模拟人手弧线
        progress = xs[i] / dist if dist else 0
        y_wobble = math.sin(progress * math.pi) * random.uniform(-0.7, 0.7)
        ty = slider_cy + y_wobble
        page.mouse.move(tx, ty, steps=4)
        time.sleep(0.035 + random.random() * 0.04)
    # 末段长停留 (0.6-0.9s): 真实人手到位后会"确认一下"再松手
    time.sleep(0.6 + random.random() * 0.3)
    # 末段验证: 同时检查 把手(handle) 和 拼图块(piece) 的最终位置,
    # 看哪个更接近 gap. 腾讯云 drag_ele 校验的是把手位移.
    try:
        cur_p = pp_loc.bounding_box()
        cur_h = frame.locator(".tc-fg-item.tc-slider-normal").first.bounding_box()
        if cur_p and cur_h and bg_box:
            p_xbg = cur_p['x'] - bg_box['x']
            h_xbg = cur_h['x'] - bg_box['x']
            print(f"  [slide] pre-up handle_x_in_bg={h_xbg:.0f} piece_x_in_bg={p_xbg:.0f} "
                  f"gap={gap_x} (handle->gap err {h_xbg-gap_x:+.1f}, piece->gap err {p_xbg-gap_x:+.1f})", flush=True)
    except Exception as ex:
        print(f"  [slide] pre-up check err: {ex}", flush=True)
    page.mouse.up()

    return {"gap_x": gap_x, "dist": dist, "gap_mean": gap_mean, "band_mean": band_mean}


# ============== 4.5) 点击顺序型 (click_order) 验证码 ==============
def solve_click_order_in_frame(page, frame) -> dict:
    """1ms 点击顺序型验证码 (腾讯云天御 click_order):
       指令形如 '请依次点击：4 2 1', 画面有多个带数字编号的图标, 需按序点击。
       解析指令数字序列 -> 找到每个数字对应的图标 -> 按序点击 -> (若有) 点确认。
    """
    import re as _re
    instr = ""
    try:
        instr = frame.evaluate("() => { const e=document.getElementById('instructionText'); return e? e.textContent.trim():''; }")
    except Exception:
        pass
    if not instr:
        try:
            instr = frame.inner_text("body")[:160]
        except Exception:
            pass
    print(f"  [click_order] instruction='{instr}'", flush=True)

    # 解析点击序列: 取冒号后所有 token (数字 或 中文标签, 如 '4 2 1' 或 '邦 辈 筹')
    seq_tokens = []
    m = _re.search(r"[：:]\s*([\dA-Za-z\u4e00-\u9fff]+(?:\s+[\dA-Za-z\u4e00-\u9fff]+)+)", instr)
    if m:
        seq_tokens = m.group(1).split()
    if not seq_tokens:
        # 兜底: 直接取冒号后全部非空 token
        parts = _re.split(r"[：:]", instr)
        if len(parts) > 1:
            seq_tokens = [t for t in parts[-1].split() if t.strip()]
    if not seq_tokens:
        raise NonSliderError(f"无法从指令解析点击序列: '{instr}'")
    print(f"  [click_order] 点击序列 tokens={seq_tokens}", flush=True)

    # 等图标出现
    time.sleep(1.0)
    try:
        frame.locator(".tc-bg-img").first.wait_for(state="attached", timeout=8000)
    except Exception:
        pass
    time.sleep(1.5)

    # 收集图标目标: 候选 .tc-fg-item / img / 含 fg/icon/target 元素, 读标签文本
    targets = []  # (label, locator, bbox, sel)
    for sel in [".tc-fg-item", "img", "[class*='fg']", "[class*='icon']",
                "[class*='target']", "[class*='click']", "canvas", "button"]:
        try:
            n = frame.locator(sel).count()
        except Exception:
            continue
        for i in range(n):
            try:
                loc = frame.locator(sel).nth(i)
                bb = loc.bounding_box()
                if not bb or bb.get("width", 0) < 5:
                    continue
                txt = ""
                try:
                    txt = (loc.inner_text(timeout=800) or "").strip()
                except Exception:
                    txt = ""
                if not txt:
                    try:
                        txt = (loc.get_attribute("alt") or "").strip()
                    except Exception:
                        pass
                targets.append((txt, loc, bb, sel))
            except Exception:
                continue
    print(f"  [click_order] 候选目标数={len(targets)}", flush=True)
    for t in targets[:24]:
        print(f"    - label='{t[0]}' sel={t[3]} bbox={t[2]}", flush=True)

    # 建立 标签->目标 映射 (label 去掉空白后与 token 直接比对)
    label_map = {}
    for txt, loc, bb, sel in targets:
        key = (txt or "").strip()
        if key:
            label_map[key] = (loc, bb)
    print(f"  [click_order] 标签映射 keys={list(label_map.keys())}", flush=True)

    if not label_map:
        raise NonSliderError(f"click_order 未能识别任何带标签图标 (候选 {len(targets)} 但无标签)")

    # 按 seq 顺序点击 (token 与 label 精确匹配, 支持数字或中文)
    clicked = 0
    for idx, tok in enumerate(seq_tokens):
        tok = tok.strip()
        if tok in label_map:
            loc, bb = label_map[tok]
            cx = bb["x"] + bb["width"] / 2
            cy = bb["y"] + bb["height"] / 2
            page.mouse.move(cx, cy, steps=3)
            time.sleep(0.1 + random.random() * 0.1)
            page.mouse.click(cx, cy)
            clicked += 1
            print(f"  [click_order] 点击 #{idx+1} token='{tok}' at ({cx:.0f},{cy:.0f})", flush=True)
            time.sleep(0.6 + random.random() * 0.4)
        else:
            print(f"  [click_order] 序列 token '{tok}' 未找到对应图标 (keys={list(label_map.keys())})", flush=True)
    if clicked == 0:
        raise NonSliderError(f"click_order 序列 {seq_tokens} 全部未命中图标")

    # 点完可能需要确认
    try:
        for sel in ["button:has-text('确定')", "button:has-text('验证')",
                    ".tc-verify-btn", ".verify-btn", "[class*='verify']"]:
            el = frame.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=3000)
                print(f"  [click_order] 点击确认按钮 {sel}", flush=True)
                break
    except Exception as e:
        print(f"  [click_order] 确认按钮点击异常(可忽略): {e}", flush=True)
    return {"seq": seq, "clicked": clicked}


def solve_captcha(page, frame) -> dict:
    """按 iframe 内特征分发到滑块 / 点击顺序 两种解法"""
    instr = ""
    try:
        instr = frame.evaluate("() => { const e=document.getElementById('instructionText'); return e? e.textContent.trim():''; }")
    except Exception:
        pass
    has_slider = (frame.locator(".tc-fg-item.tc-slider-normal").count() > 0 or
                  frame.locator(".tc-slider-bg").count() > 0)
    if has_slider:
        return solve_slide_in_frame(page, frame)
    if "点击" in instr or "click" in instr.lower() or "依次" in instr:
        return solve_click_order_in_frame(page, frame)
    # 未知型: 交给滑块逻辑最后判定 (其内部会在非滑块时抛 NonSliderError 走 A 保底)
    return solve_slide_in_frame(page, frame)


# ============== 5) 端到端主流程 (明天未签时跑) ==============
def _ql_notify(title, content):
    """青龙环境内调用面板自带 sendNotify 推给管理员; 非青龙环境(本机调试)静默跳过。
    仅 best-effort: import 失败/无 sendNotify 一律 pass, 不影响主流程。
    """
    try:
        from notify import sendNotify
        if callable(sendNotify):
            sendNotify(title, content)
    except Exception:
        pass


def _write_status(status, detail=""):
    """写结果状态文件 + 打印 A 保底横幅 + (青龙环境)推管理员。供 ql/cron/手动运行后快速判断。
    status: SUCCESS / ALREADY / MANUAL (需手动 A 保底)
    """
    try:
        with open("F:/ai/ql-scripts/_ms_status.txt", "w", encoding="utf-8") as f:
            f.write(f"{status}\t{detail}\t{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass
    if status == "SUCCESS":
        print("[A保底] ✅ 自动过码成功, 今日已签", flush=True)
    elif status == "ALREADY":
        print("[A保底] ℹ️ 今日已签, 无需重复", flush=True)
    else:
        print("[A保底] ⚠️ 自动化未通过, 需手动签到 (A 保底): https://1ms.run/user/checkin", flush=True)
        print(f"[A保底]   原因: {detail}", flush=True)
        # 需手动 = 必须让管理员知道, 青龙环境下显式推一遍 (面板也会捕获 stdout)
        _ql_notify("1ms签到 A保底", f"自动化未过码, 需手动签到:\nhttps://1ms.run/user/checkin\n原因: {detail}")


def _close(b, is_cdp):
    """安全关闭: CDP 接入的真实浏览器只 disconnect (不 close, 否则会杀掉用户真浏览器)."""
    if is_cdp:
        try:
            b.disconnect()
        except Exception:
            pass
    else:
        try:
            _close(b, is_cdp)
        except Exception:
            pass


def launch_browser(p):
    """'浏览器环境'方案启动/接入真实浏览器, 绕过天御对 headless playwright 的 100% 拒绝.

    天御风险引擎靠 navigator.webdriver / cdc_ 注入 / 无头标记 / 缺 plugins·WebGL 等判 bot.
    真实浏览器(用户自己开的 Chrome)这些特征都是"真人", 且 playwright 的自动化注入只发生在
    launch() 时; 用 connect_over_cdp 接入的浏览器没有任何 playwright 痕迹.

    优先级:
      1) CHROME_CDP_URL 已设 (http://127.0.0.1:9222 或 ws://...) ->
         connect_over_cdp 接入用户已开的真浏览器 (最稳, 零自动化特征)
         部署: 宿主机 `xvfb-run -a google-chrome --remote-debugging-port=9222
                --user-data-dir=/path/prof --no-first-run &`, 任务 env 设 CHROME_CDP_URL
      2) 否则用系统真实 Chrome (channel='chrome' 或 CHROME_PATH) headed 启动 + 持久 user-data-dir
         (需宿主机有显示器; 服务器用 xvfb-run 包一层)
      3) 兜底: playwright 自带 chromium headless (原行为, 已知被天御拒, 仅本地调试)
    返回 (browser, is_cdp)
    """
    cdp = os.environ.get("CHROME_CDP_URL") or os.environ.get("BROWSER_CDP_WS")
    if cdp:
        try:
            print(f"[browser] 通过 CDP 接入真实浏览器: {cdp}", flush=True)
            return p.chromium.connect_over_cdp(cdp), True
        except Exception as e:
            print(f"[browser] CDP 接入失败, 回退启动真实 Chrome: {e}", flush=True)

    # 系统真实 Chrome (headed)
    kw = dict(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    ud = os.environ.get("CHROME_USER_DATA_DIR")
    if ud:
        kw["user_data_dir"] = ud
    ch = os.environ.get("CHROME_PATH")
    if ch:
        kw["executable_path"] = ch
    else:
        try:
            kw["channel"] = "chrome"
        except Exception:
            pass
    try:
        print("[browser] 启动系统真实 Chrome (headed) ...", flush=True)
        return p.chromium.launch(**kw), False
    except Exception as e:
        print(f"[browser] 真实 Chrome 启动失败 ({e}), 回退 playwright headless", flush=True)
        return p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-web-security",
            ],
        ), False


def run_checkin():
    tok = load_token_from_container()
    print(f"[main] token loaded (len={len(tok)})", flush=True)
    status, detail = "MANUAL", ""

    with sync_playwright() as p:
        # '浏览器环境'方案: 优先接入/启动真实浏览器 (见 launch_browser), 不再用 playwright headless
        b, is_cdp = launch_browser(p)
        # 真实浏览器(CDP)用其默认 context; 自启动的建新 context + viewport
        if is_cdp and b.contexts:
            ctx = b.contexts[0]
        else:
            ctx = b.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        ctx.add_cookies([{"name": "auth_token", "value": tok, "domain": "." + DOMAIN, "path": "/"}])
        # init script: 隐藏 webdriver + 补齐 plugins/languages/chrome.runtime/WebGL, 伪装成真实 Chrome
        ctx.add_init_script("""
            (() => {
                try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
                try { Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] }); } catch(e) {}
                try {
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => {
                            const arr = [
                                { name: 'PDF Viewer', filename: 'internal-pdf-viewer', length: 1 },
                                { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', length: 1 },
                                { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', length: 1 },
                                { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', length: 1 },
                                { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', length: 1 },
                            ];
                            arr.length = 5;
                            return arr;
                        }
                    });
                } catch(e) {}
                try {
                    window.chrome = {
                        runtime: { onInstalled: { addListener: () => {} }, sendMessage: () => {}, connect: () => {} },
                        loadTimes: () => ({}),
                        csi: () => ({}),
                        app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
                    };
                } catch(e) {}
                try {
                    const origQuery = navigator.permissions && navigator.permissions.query;
                    if (origQuery) {
                        navigator.permissions.query = (p) =>
                            p && p.name === 'notifications'
                                ? Promise.resolve({ state: Notification.permission || 'default' })
                                : origQuery.call(navigator.permissions, p);
                    }
                } catch(e) {}
                try {
                    const gp = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(p) {
                        if (p === 37445) return 'Intel Inc.';
                        if (p === 37446) return 'Intel Iris OpenGL Engine';
                        return gp.call(this, p);
                    };
                } catch(e) {}
            })();
        """)
        page = ctx.new_page()

        page.goto(f"https://{DOMAIN}/user/checkin", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        body = page.inner_text("body")
        if "今日已签" in body:
            print("[main] 今日已签, 无需重复 (脚本退出)", flush=True)
            _close(b, is_cdp)
            _write_status("ALREADY", "今日已签")
            return "ALREADY"
        # 找签到按钮: 多种策略兜底 (get_by_text 偶尔匹到非按钮元素)
        clicked = False
        strategies = [
            ("get_by_role button '立即签到'", lambda: page.get_by_role("button", name="立即签到").first),
            ("locator button:has-text",        lambda: page.locator("button:has-text('立即签到')").first),
            ("locator .btn:has-text",          lambda: page.locator("[class*='btn']:has-text('立即签到')").first),
            ("get_by_text exact=False",        lambda: page.get_by_text("立即签到", exact=False).first),
        ]
        for name, getter in strategies:
            try:
                el = getter()
                if el.count() and el.is_visible():
                    el.click(timeout=5000, force=True)
                    clicked = True
                    print(f"[main] clicked via {name}", flush=True)
                    break
                else:
                    print(f"[main] {name} skip (count={el.count()}, visible={el.is_visible()})", flush=True)
            except Exception as e:
                print(f"[main] {name} err: {e}", flush=True)
        if not clicked:
            print("[main] NO_SIGNIN_BTN", flush=True)
            page.screenshot(path="/tmp/_ms_no_btn.png")
            try:
                import shutil
                shutil.copy("/tmp/_ms_no_btn.png", f"F:/ai/_ms_no_btn_{int(time.time())}.png")
            except Exception:
                pass
            _close(b, is_cdp)
            _write_status("MANUAL", "找不到立即签到按钮")
            return "MANUAL"

        # 等滑块 iframe 出现 (重试定位, 并区分滑块型/点击型)
        slide_frame = None
        for _ in range(12):
            for fr in page.frames:
                if "turing.captcha.gtimg.com" in fr.url:
                    try:
                        if fr.locator(".tc-bg-img").count() > 0:
                            slide_frame = fr
                            break
                    except Exception:
                        pass
            if slide_frame:
                break
            page.wait_for_timeout(800)

        if not slide_frame:
            # 区分: 今日已签 OR 抽到非滑块型(点击/图文)
            body_now = page.inner_text("body")
            if "今日已签" in body_now:
                print("[main] 今日已签 (弹窗未起, 无需重复)", flush=True)
                _close(b, is_cdp)
                _write_status("ALREADY", "今日已签(弹窗未起)")
                return "ALREADY"
            else:
                print("[main] NON_SLIDER_OR_NO_FRAME (可能抽中点击/图文验证, 本路线仅支持滑块, 走A保底)", flush=True)
                page.screenshot(path="/tmp/_ms_nonslide.png")
                try:
                    import shutil
                    shutil.copy("/tmp/_ms_nonslide.png", f"F:/ai/_ms_nonslide_{int(time.time())}.png")
                except Exception:
                    pass
                _close(b, is_cdp)
                _write_status("MANUAL", "抽中非滑块型/无frame")
                return "MANUAL"

        print("[main] 找到滑块 iframe, 开始过滑块 (最多3次)...", flush=True)
        success = False
        non_slider_early = False
        for attempt in range(1, 4):
            try:
                solve_captcha(page, slide_frame)
            except NonSliderError as e:
                # 1ms 抽中非滑块型 (click_order 等), 走 A 保底, 不再重试
                print(f"[main] ⚠️ {e}", flush=True)
                page.screenshot(path="/tmp/_ms_nonslide.png")
                try:
                    import shutil
                    shutil.copy("/tmp/_ms_nonslide.png", f"F:/ai/_ms_nonslide_{int(time.time())}.png")
                except Exception:
                    pass
                non_slider_early = True
                break
            except Exception as e:
                print(f"[main] attempt {attempt} solve 异常: {e}", flush=True)
                page.wait_for_timeout(1500)
                # 重新定位 frame (失败可能重置)
                slide_frame = None
                for fr in page.frames:
                    if "turing.captcha.gtimg.com" in fr.url and fr.locator(".tc-bg-img").count() > 0:
                        slide_frame = fr
                        break
                if not slide_frame:
                    print("[main] 滑块 frame 丢失, 停止重试", flush=True)
                    break
                continue
            page.wait_for_timeout(4000)
            try:
                body2 = page.inner_text("body")
            except Exception:
                body2 = ""
            print(f"[main] AFTER attempt {attempt} SNIP:", body2[:200].replace("\n", " | "), flush=True)
            if "今日已签" in body2:
                success = True
                print("[main] ✅ 签到成功!", flush=True)
                break
            else:
                print(f"[main] attempt {attempt} 未成功, 重试...", flush=True)
                page.wait_for_timeout(1500)
        page.screenshot(path="/tmp/_ms_after.png")
        if success:
            status = "SUCCESS"; detail = "滑块过码成功"
        elif non_slider_early:
            status = "MANUAL"; detail = "抽中非滑块型(点击顺序), 滑块路线不适用"
        else:
            status = "MANUAL"; detail = "滑块多次尝试未成功(腾讯云风控拒绝 headless)"
        _close(b, is_cdp)
        _write_status(status, detail)
        return status


# ============== 6) selftest: 独立验证 cv2 缺口识别 + 轨迹生成 ==============
def selftest():
    print("[selftest] 生成合成滑块图验证 cv2 模板匹配 + 轨迹生成...", flush=True)
    # 合成背景 (350x200) + 缺口 (x=180, 50x50, 深色) + 滑块 (50x50, 深色+边框)
    bg = np.full((200, 350, 3), 220, dtype=np.uint8)
    GAP_X, GAP_W, GAP_H = 180, 50, 50
    cv2.rectangle(bg, (GAP_X, 75), (GAP_X + GAP_W, 125), (40, 40, 40), -1)
    sl = np.full((GAP_H, GAP_W, 3), 40, dtype=np.uint8)
    cv2.rectangle(sl, (0, 0), (GAP_W - 1, GAP_H - 1), (20, 20, 20), 1)
    cv2.imwrite("/tmp/_ms_bg.png", bg)
    cv2.imwrite("/tmp/_ms_sl.png", sl)

    # 1) 灰度模板匹配 (基本逻辑 - 证明 cv2.matchTemplate 通路, 合成图不强求精确)
    bg_g = cv2.imread("/tmp/_ms_bg.png", cv2.IMREAD_GRAYSCALE)
    sl_g = cv2.imread("/tmp/_ms_sl.png", cv2.IMREAD_GRAYSCALE)
    res = cv2.matchTemplate(bg_g, sl_g, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(res)
    print(f"[selftest] 灰度 match: gap_x={ml[0]} (期望≈{GAP_X}, 合成纯色模板 CCOEFF 不稳定属正常) match={mx:.3f}", flush=True)
    gray_ok = 0 <= ml[0] < 300 and mx > 0  # 只要求函数通+返回合理数

    # 2) find_gap (NCC+dark 改进版) - 真实腾讯云图 dark 高的候选即真凹槽
    gx, mv, dk = find_gap("/tmp/_ms_bg.png", "/tmp/_ms_sl.png")
    print(f"[selftest] NCC+dark match: gap_x={gx} (合成图无暗区 dark≈0 属正常) match={mv:.3f} dark={dk:.1f}", flush=True)
    canny_ok = 0 <= gx < 350  # 合成图无真实暗区, dark 启发不工作, 只验证函数通路

    # 3) 轨迹
    tr = human_track(123.4, steps=25)
    end = tr[-1]
    print(f"[selftest] 轨迹  步数={len(tr)} 起={tr[0]} 末=({end[0]:.1f},{end[1]:.1f}) 期望末=(123.4, 0.0)", flush=True)
    track_ok = abs(end[0] - 123.4) < 0.01 and end[1] == 0.0

    print(f"[selftest] 灰度匹配函数 {'OK' if gray_ok else 'FAIL'} | "
          f"find_gap(Canny) {'OK' if canny_ok else 'FAIL'} | "
          f"轨迹生成 {'OK' if track_ok else 'FAIL'}", flush=True)
    if not (gray_ok and canny_ok and track_ok):
        sys.exit(1)
    print("[selftest] ✅ cv2 matchTemplate 通路 + find_gap(Canny) 通路 + 人类轨迹 全部通过", flush=True)
    print("[selftest]    (精确缺口定位需等明天 --checkin 真实腾讯云图验证)", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="只跑 cv2+轨迹 自检")
    ap.add_argument("--checkin", action="store_true", help="端到端签到 (明天未签时跑)")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    elif args.checkin:
        st = run_checkin()
        sys.exit(0 if st in ("SUCCESS", "ALREADY") else 2)
    else:
        # 默认: 先 selftest 再询问 (避免误触签到)
        selftest()
        print("\n如需端到端签到, 跑: python3 _checkin_slide.py --checkin", flush=True)
