#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 取 token 脚本 —— 可在青龙里定时运行
(需青龙容器内安装 playwright + chromium: pip install playwright && playwright install chromium)

设计:
  - 从环境变量 MS_PHONE / MS_PASSWORD 读取账号密码 (青龙环境变量配置, 无需手动填)
  - 登录后取出 auth_token, 写入 token 文件 (供 checkin.py 读取, 实现自动刷新)
  - token 文件默认路径:
        若 /ql/data 目录存在 -> /ql/data/1ms_token.txt   (青龙持久化目录, 仓库重新拉取不会清掉)
        否则 -> 与本脚本同目录的 .token                  (本机/调试用)
    可用环境变量 MS_TOKEN_FILE 覆盖路径。
  - 兼容 --print-token: 把 MS_TOKEN=xxx 打到 stdout (便于本地/手动取用)

青龙部署:
  - 环境变量: MS_PHONE, MS_PASSWORD  (必填)
  - 定时任务: 例如 55 8 * * *  (每天 08:55, 需早于签到 checkin.py)
  - 命令: task /ql/data/scripts/auto-scripts/scripts/1ms/login.py

流程 (Logto OIDC, 密码登录无图形验证码):
  填手机号 -> 继续 -> 改用密码登录 -> 填密码 -> 登录
  -> OIDC 授权确认页点"授权" -> 跳回 1ms.run -> 导出 auth_token cookie
"""
import os
import sys
import json
import time
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.stderr.write(
        "\n[login] 错误: 未安装 playwright, 无法用无头浏览器登录 (1ms.run 登录含腾讯验证码,\n"
        "        纯标准库无法绕过, 必须借助浏览器完成验证)。\n"
        "请在青龙容器内一次性安装 (docker exec -it qinglong bash 后执行):\n"
        "    pip3 install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "    playwright install chromium\n"
        "    playwright install-deps chromium   # 若启动报缺少系统库 (需 root)\n"
        "安装后重新运行本任务即可自动登录。\n\n"
        "临时兜底: 在青龙环境变量设 MS_TOKEN=<从浏览器开发者工具/本机 login.py --print-token 拿到的 token>,\n"
        "          checkin.py 会优先读 token 文件, 读不到再回退 MS_TOKEN 环境变量。\n"
    )
    sys.exit(2)

TARGET = "https://1ms.run/user/domain"
AUTH_COOKIE_NAME = "auth_token"
BASE = "https://1ms.run"
STATUS_API = BASE + "/api/v1/mall/checkin/status"


def token_file_path():
    tf = os.environ.get("MS_TOKEN_FILE", "").strip()
    if tf:
        return tf
    if os.path.isdir("/ql/data"):
        return "/ql/data/1ms_token.txt"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def log(*a):
    print("[login]", *a, flush=True)


def get_args():
    phone = os.environ.get("MS_PHONE", "").strip()
    password = os.environ.get("MS_PASSWORD", "").strip()
    args = sys.argv  # 仅用于 --print-token 检测, 不在这里解析账号
    i = 1
    while i < len(args):
        if args[i] in ("--phone", "-u"):
            phone = args[i + 1]; i += 2
        elif args[i] in ("--password", "-p"):
            password = args[i + 1]; i += 2
        else:
            i += 1
    return phone, password


def main():
    phone, password = get_args()
    if not phone or not password:
        log("ERROR: 需要 MS_PHONE / MS_PASSWORD 环境变量 (或 --phone/--password 参数)。")
        sys.exit(1)

    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = b.new_context()
        pg = ctx.new_page()
        pg.goto(TARGET, wait_until="networkidle", timeout=20000)
        try:
            pg.get_by_text("统一认证登录", exact=False).first.click(timeout=6000)
            log("clicked 统一认证登录")
        except Exception as e:
            log("login btn (maybe already redirected):", e)
        time.sleep(2.5)

        pg.get_by_placeholder("请输入手机号", exact=False).first.fill(phone)
        time.sleep(0.4)
        pg.get_by_text("继续", exact=True).first.click(timeout=6000)
        log("clicked 继续")
        time.sleep(3)

        try:
            pg.get_by_text("改用密码登录", exact=False).first.click(timeout=6000)
            log("clicked 改用密码登录")
        except Exception as e:
            log("switch to password (maybe already there):", e)
        time.sleep(1.5)

        pg.get_by_placeholder("请输入密码", exact=False).first.fill(password)
        time.sleep(0.4)
        pg.get_by_text("登录", exact=True).first.click(timeout=6000)
        log("clicked 登录, waiting for redirect...")

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
                    log("clicked 授权 consent")
                    time.sleep(2)
                    continue
            except Exception:
                pass
            for label in ["允许", "Authorize", "Allow", "同意并继续"]:
                try:
                    btn = pg.get_by_role("button", name=label, exact=True)
                    if btn.count() > 0 and btn.first.is_visible(timeout=2000):
                        btn.first.click()
                        log("clicked consent:", label)
                        time.sleep(2)
                        break
                except Exception:
                    pass
            time.sleep(1.5)
        log("reached_1ms:", reached, "final_url:", pg.url)

        if not reached:
            pg.screenshot(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_FAIL.png"))
            log("did not return to 1ms.run; screenshot saved")
            b.close()
            sys.exit(1)

        time.sleep(2)
        cookies = ctx.cookies()
        at = next((c["value"] for c in cookies if c["name"] == AUTH_COOKIE_NAME), None)
        if not at:
            log("WARN: auth_token cookie not found")
            b.close()
            sys.exit(1)

        # 校验登录态 (标准库 urllib, 免额外依赖)
        ok = False
        try:
            req = urllib.request.Request(
                STATUS_API,
                headers={"Authorization": "Bearer " + at, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode())
                ok = r.status == 200 and "data" in j
        except Exception as e:
            log("verify err:", e)

        b.close()
        if not ok:
            log("VERIFY FAILED")
            sys.exit(1)

        # 写 token 文件 (供 checkin.py 自动读取)
        tp = token_file_path()
        with open(tp, "w", encoding="utf-8") as f:
            f.write(at)
        log("TOKEN SAVED ->", tp)

        if "--print-token" in sys.argv:
            print("MS_TOKEN=" + at, flush=True)
            log("printed MS_TOKEN")
        log("VERIFY OK")
        sys.exit(0)


if __name__ == "__main__":
    main()
