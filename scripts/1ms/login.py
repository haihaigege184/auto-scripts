#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 取 token 工具 —— 在本机运行 (需 playwright + chromium)

用途: 登录 1ms.run, 取出 auth_token, 供青龙脚本 (checkin.py) 使用。
      本脚本运行在用户 PC, 不在青龙里跑 (青龙无浏览器)。

配置 (环境变量或命令行参数):
  MS_PHONE     手机号
  MS_PASSWORD  密码
  也可命令行: python login.py --phone 159... --password xxx [--print-token]

用法:
  python login.py --print-token
  输出: MS_TOKEN=eyJhbGci...   ← 复制该值填入青龙环境变量 MS_TOKEN

流程 (Logto OIDC, 密码登录无图形验证码):
  填手机号 → 继续 → 改用密码登录 → 填密码 → 登录
  → OIDC 授权确认页点"授权" → 授权 → 跳回 1ms.run → 导出 auth_token

注意: 不勾"记住账号"(避免触发滑块验证)。
"""
import os
import sys
import json
import time
from playwright.sync_api import sync_playwright

TARGET = "https://1ms.run/user/domain"
SESSION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")
AUTH_COOKIE_NAME = "auth_token"

LOG = lambda *a: print("[login]", *a, flush=True)


def get_args():
    phone = os.environ.get("MS_PHONE", "").strip()
    password = os.environ.get("MS_PASSWORD", "").strip()
    i = 1
    args = sys.argv[1:]
    while i < len(args):
        if args[i] in ("--phone", "-u"):
            phone = args[i + 1]; i += 2
        elif args[i] in ("--password", "-p"):
            password = args[i + 1]; i += 2
        elif args[i] == "--print-token":
            i += 1  # handled by caller via sys.argv
        else:
            i += 1
    return phone, password


def main():
    phone, password = get_args()
    if not phone or not password:
        LOG("ERROR: 请提供 MS_PHONE / MS_PASSWORD (环境变量或 --phone/--password)。")
        sys.exit(1)

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context()
        pg = ctx.new_page()
        pg.goto(TARGET, wait_until="networkidle", timeout=20000)
        try:
            pg.get_by_text("统一认证登录", exact=False).first.click(timeout=6000)
            LOG("clicked 统一认证登录")
        except Exception as e:
            LOG("login btn (maybe already redirected):", e)
        time.sleep(2.5)

        pg.get_by_placeholder("请输入手机号", exact=False).first.fill(phone)
        time.sleep(0.4)
        pg.get_by_text("继续", exact=True).first.click(timeout=6000)
        LOG("clicked 继续")
        time.sleep(3)

        try:
            pg.get_by_text("改用密码登录", exact=False).first.click(timeout=6000)
            LOG("clicked 改用密码登录")
        except Exception as e:
            LOG("switch to password (maybe already there):", e)
        time.sleep(1.5)

        pg.get_by_placeholder("请输入密码", exact=False).first.fill(password)
        time.sleep(0.4)
        pg.get_by_text("登录", exact=True).first.click(timeout=6000)
        LOG("clicked 登录, waiting for redirect...")

        t0 = time.time()
        reached = False
        while time.time() - t0 < 25:
            url = pg.url
            if "1ms.run" in url and "login.wang" not in url:
                reached = True
                break
            try:
                btn = pg.get_by_role("button", name="授权", exact=True)
                if btn.count() > 0 and btn.first.is_visible(timeout=2000):
                    btn.first.click()
                    LOG("clicked 授权 consent")
                    time.sleep(2)
                    continue
            except Exception:
                pass
            for label in ["允许", "Authorize", "Allow", "同意并继续"]:
                try:
                    btn = pg.get_by_role("button", name=label, exact=True)
                    if btn.count() > 0 and btn.first.is_visible(timeout=2000):
                        btn.first.click()
                        LOG("clicked consent:", label)
                        time.sleep(2)
                        break
                except Exception:
                    pass
            time.sleep(1.5)
        LOG("reached_1ms:", reached, "final_url:", pg.url)

        if not reached:
            pg.screenshot(path=os.path.join(os.path.dirname(__file__), "login_FAIL.png"))
            LOG("did not return to 1ms.run; screenshot saved")
            b.close()
            sys.exit(1)

        time.sleep(2)
        cookies = ctx.cookies()
        # 校验: 调一次 status 接口 (Bearer = auth_token cookie value)
        import requests
        at = next((c["value"] for c in cookies if c["name"] == AUTH_COOKIE_NAME), None)
        if not at:
            LOG("WARN: auth_token cookie not found")
            b.close()
            sys.exit(1)
        s = requests.Session()
        for c in cookies:
            s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        r = s.get("https://1ms.run/api/v1/mall/checkin/status",
                  headers={"Authorization": "Bearer " + at}, timeout=15)
        ok = r.status_code == 200 and "data" in r.text
        # 存session供本地 checkin.py 复用(若需要)
        try:
            with open(SESSION, "w", encoding="utf-8") as f:
                json.dump({"cookies": cookies}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        b.close()
        if "--print-token" in sys.argv:
            print("MS_TOKEN=" + at, flush=True)
            LOG("printed MS_TOKEN")
        LOG("VERIFY OK" if ok else "VERIFY FAILED: " + r.text[:160])
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
