#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断脚本: 触发 1ms 滑块验证码, 保存真实截图 + DOM 结构 + 列统计, 不真正拖动(省尝试次数)。
目的: 看清"缺口(gap)在 .tc-bg-img 里到底怎么渲染", 以设计可靠的缺口定位。
"""
import os, sys, time, json
import numpy as np
import cv2
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checkin_slide import load_token_from_container
from playwright.sync_api import sync_playwright

HOST = "10.0.0.11"
DOMAIN = "1ms.run"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
OUT = "F:/ai"


def dump_frame_dom(frame):
    """Dump all elements in captcha frame: tag, id, class, rect, src/backgroundImage."""
    info = frame.evaluate("""() => {
        const out = [];
        const all = document.querySelectorAll('*');
        for (const el of all) {
            const r = el.getBoundingClientRect();
            if (r.width < 2 && r.height < 2) continue;
            const cs = getComputedStyle(el);
            let bg = '';
            if (cs.backgroundImage && cs.backgroundImage !== 'none') bg = cs.backgroundImage.slice(0, 80);
            const src = el.src ? el.src.slice(0, 60) : '';
            out.push({
                tag: el.tagName,
                id: el.id,
                cls: el.className && el.className.toString ? el.className.toString().slice(0, 80) : '',
                x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
                bg: bg, src: src,
            });
        }
        return out;
    }""")
    return info


def column_stats(bg_path, band_y0, band_y1, piece_w):
    """对 bg 灰度图在 [band_y0,band_y1] 带内逐列统计: 暗度 / 边缘密度。
    返回 top 候选列。"""
    bg = cv2.imread(bg_path, cv2.IMREAD_GRAYSCALE)
    if bg is None:
        return None
    bg = bg.astype(np.float32)
    bh, bw = bg.shape
    y0 = max(0, int(band_y0)); y1 = min(bh, int(band_y1))
    band = bg[y0:y1, :]
    # 边缘密度
    gx = cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(band, cv2.CV_32F, 0, 1, ksize=3)
    edge = np.sqrt(gx**2 + gy**2)
    means = band.mean(axis=0)            # 每列均值(暗度: 越小越暗)
    edges = edge.mean(axis=0)            # 每列边缘强度
    pw = int(piece_w)
    cand = []
    for x in range(0, bw - pw):
        cand.append((x, float(means[x:x+pw].mean()), float(edges[x:x+pw].mean())))
    cand.sort(key=lambda t: t[1])  # 按暗度升序(最暗在前)
    darkest = cand[:6]
    cand.sort(key=lambda t: t[2], reverse=True)  # 按边缘降序
    edgiest = cand[:6]
    return {"darkest": darkest, "edgiest": edgiest}


def main():
    tok = load_token_from_container()
    print(f"[diag] token len={len(tok)}", flush=True)
    captured = {"slider": 0, "click": 0}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        ctx.add_cookies([{"name": "auth_token", "value": tok, "domain": "." + DOMAIN, "path": "/"}])

        TRIALS = 4
        for trial in range(TRIALS):
            if captured["slider"] >= 3:
                break
            page = ctx.new_page()
            page.goto(f"https://{DOMAIN}/user/checkin", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            body = page.inner_text("body")
            if "今日已签" in body:
                print(f"[diag] trial {trial}: 今日已签, 停止", flush=True)
                page.close()
                break
            # 点签到
            clicked = False
            for name, getter in [
                ("btn立即签到", lambda: page.locator("button:has-text('立即签到')").first),
                ("get_by_role", lambda: page.get_by_role("button", name="立即签到").first),
            ]:
                try:
                    el = getter()
                    if el.count() and el.is_visible():
                        el.click(timeout=5000, force=True)
                        clicked = True
                        print(f"[diag] trial {trial}: clicked {name}", flush=True)
                        break
                except Exception as e:
                    print(f"[diag] trial {trial}: click {name} err {e}", flush=True)
            if not clicked:
                print(f"[diag] trial {trial}: 无签到按钮", flush=True)
                page.close()
                continue

            # 等 frame
            frame = None
            for _ in range(15):
                for fr in page.frames:
                    if "turing.captcha.gtimg.com" in fr.url and fr.locator(".tc-bg-img").count() > 0:
                        frame = fr
                        break
                if frame:
                    break
                page.wait_for_timeout(800)
            if not frame:
                print(f"[diag] trial {trial}: 无滑块 frame (可能点击型或非滑块)", flush=True)
                page.screenshot(path=f"{OUT}/_ms_diag_t{trial}_noslide.png")
                page.close()
                continue

            # 类型: 等到 instruction 渲染 + 滑块/点击元素就绪 (避免过早判定)
            instr = ""
            is_slider = False
            for _ in range(50):  # 25s
                try:
                    instr = frame.evaluate("() => { const e=document.getElementById('instructionText'); return e? e.textContent.trim():''; }")
                except Exception:
                    instr = ""
                if "拖动" in instr:
                    is_slider = True
                    break
                if "点击" in instr or "依次" in instr:
                    is_slider = False
                    break
                # instruction 还没出: 看 piece 是否已渲染
                try:
                    if frame.locator(".tc-fg-item").count() > 0:
                        is_slider = True
                        instr = instr or "拖动下方滑块完成拼图"
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            if not is_slider:
                print(f"[diag] trial {trial}: 点击型 instr='{instr}'", flush=True)
                captured["click"] += 1
                page.screenshot(path=f"{OUT}/_ms_diag_t{trial}_click.png")
                page.close()
                continue

            # 等到滑块就绪
            ready = False
            for i in range(40):
                try:
                    h = frame.evaluate("() => { const e=document.getElementById('slideBg'); return e? e.getBoundingClientRect().height:0; }")
                    cnt = frame.locator(".tc-fg-item").count()
                    fbb = frame.locator(".tc-fg-item").first.bounding_box() if cnt else None
                except Exception:
                    h, cnt, fbb = 0, 0, None
                if h and h > 50 and cnt > 0 and fbb and fbb.get("width", 0) > 0:
                    ready = True
                    print(f"[diag] trial {trial}: 滑块就绪 (等 {i*0.5:.1f}s)", flush=True)
                    break
                time.sleep(0.5)
            if not ready:
                print(f"[diag] trial {trial}: 滑块未就绪", flush=True)
                page.close()
                continue

            time.sleep(0.8)
            bg_loc = frame.locator(".tc-bg-img").first
            bg_box = bg_loc.bounding_box()
            piece_loc = frame.locator(".tc-fg-item").first
            piece_box = piece_loc.bounding_box()
            print(f"[diag] trial {trial}: bg_box={bg_box} piece_box={piece_box}", flush=True)

            # 1) fullpage
            page.screenshot(path=f"{OUT}/_ms_diag_t{trial}_full.png", full_page=False)
            # 2) bg with piece hidden
            try:
                frame.evaluate("() => { document.querySelectorAll('.tc-fg-item').forEach(e=>{ e.dataset._h=e.style.display; e.style.display='none'; }); }")
                time.sleep(0.3)
                bg_loc.screenshot(path=f"{OUT}/_ms_diag_t{trial}_bg_hidden.png")
                frame.evaluate("() => { document.querySelectorAll('.tc-fg-item').forEach(e=>{ e.style.display=e.dataset._h||''; }); }")
            except Exception as e:
                print(f"[diag] trial {trial}: hide piece err {e}", flush=True)
                bg_loc.screenshot(path=f"{OUT}/_ms_diag_t{trial}_bg_hidden.png")
            # 3) bg full (piece shown)
            bg_loc.screenshot(path=f"{OUT}/_ms_diag_t{trial}_bg_full.png")
            # 4) piece alone
            try:
                piece_loc.screenshot(path=f"{OUT}/_ms_diag_t{trial}_piece.png")
            except Exception as e:
                print(f"[diag] trial {trial}: piece shot err {e}", flush=True)

            # DOM dump
            dom = dump_frame_dom(frame)
            with open(f"{OUT}/_ms_diag_t{trial}_dom.json", "w", encoding="utf-8") as f:
                json.dump(dom, f, ensure_ascii=False, indent=1)
            print(f"[diag] trial {trial}: DOM 元素数={len(dom)}", flush=True)
            # 打印含 gap/shadow/slide/bg/piece/mask 的关键元素
            keys = ["gap", "shadow", "slide", "bg", "piece", "mask", "fg", "img", "canvas"]
            for d in dom:
                low = (d["cls"] + d["id"] + d["tag"] + d["bg"] + d["src"]).lower()
                if any(k in low for k in keys):
                    print(f"    {d['tag']:6} id='{d['id']}' cls='{d['cls'][:50]}' "
                          f"rect=({d['x']},{d['y']},{d['w']},{d['h']}) bg='{d['bg'][:30]}' src='{d['src'][:30]}'", flush=True)

            # 列统计 (piece y 带)
            py0 = max(0, int(piece_box["y"] - bg_box["y"]) - 6)
            py1 = min(int(bg_box["height"]), int(piece_box["y"] - bg_box["y"] + piece_box["height"]) + 6)
            stats = column_stats(f"{OUT}/_ms_diag_t{trial}_bg_hidden.png", py0, py1, piece_box["width"])
            if stats:
                print(f"[diag] trial {trial}: 列统计(y带 {py0}-{py1}) 最暗列(前6)={stats['darkest']}", flush=True)
                print(f"[diag] trial {trial}: 列统计 最高边缘列(前6)={stats['edgiest']}", flush=True)
            captured["slider"] += 1
            page.close()
            time.sleep(1.0)

        b.close()
    print(f"[diag] DONE captured={captured}", flush=True)


if __name__ == "__main__":
    main()
