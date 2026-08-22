#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 取 token 脚本 —— 可在青龙里定时运行

依赖:
  - selenium (纯 python 包, pip install selenium 即可, Alpine/Debian 均可)
  - 系统 chromium 浏览器 + chromedriver
        Alpine 系 (如 whyour/qinglong):  apk add --no-cache chromium chromium-chromedriver
        Debian/Ubuntu 系:               apt-get install -y chromium chromium-driver
  (注: 早期版本用 playwright, 但 qinglong 镜像多为 Alpine/musl, playwright 的 manylinux
   轮子无法安装, 故改用 selenium + 系统原生 chromium, 兼容性更好)

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
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    sys.stderr.write(
        "\n[login] 错误: 未安装 selenium, 无法用无头浏览器登录 (1ms.run 登录含腾讯验证码,\n"
        "        纯标准库无法绕过, 必须借助浏览器完成验证)。\n"
        "请在青龙容器内一次性安装 (docker exec -it qinglong bash 后执行):\n"
        "  Alpine 系 (whyour/qinglong 等):\n"
        "    pip3 install selenium -i https://pypi.org/simple\n"
        "    apk add --no-cache chromium chromium-chromedriver\n"
        "  Debian/Ubuntu 系:\n"
        "    pip3 install selenium\n"
        "    apt-get install -y chromium chromium-driver\n"
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
    args = sys.argv
    i = 1
    while i < len(args):
        if args[i] in ("--phone", "-u"):
            phone = args[i + 1]; i += 2
        elif args[i] in ("--password", "-p"):
            password = args[i + 1]; i += 2
        else:
            i += 1
    return phone, password


def resolve_binary(env_name, *cands):
    p = os.environ.get(env_name, "").strip()
    if p and os.path.exists(p):
        return p
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def click_text(driver, text, timeout=6, exact=True):
    xp = (f"//*[normalize-space()='{text}']" if exact
          else f"//*[contains(normalize-space(), '{text}')]")
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xp))
        )
        el.click()
        return True
    except Exception:
        return False


def click_button_contains(driver, text, timeout=6):
    xp = f"//button[contains(normalize-space(), '{text}')]"
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xp))
        )
        el.click()
        return True
    except Exception:
        return False


def fill_placeholder(driver, ph, value, timeout=8):
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, f"//input[@placeholder='{ph}']"))
        )
        el.clear()
        el.send_keys(value)
        return True
    except Exception as e:
        log("fill fail:", ph, e)
        return False


def main():
    phone, password = get_args()
    if not phone or not password:
        log("ERROR: 需要 MS_PHONE / MS_PASSWORD 环境变量 (或 --phone/--password 参数)。")
        sys.exit(1)

    chrome_bin = resolve_binary(
        "MS_CHROMIUM_BIN",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    )
    driver_bin = resolve_binary(
        "MS_CHROMEDRIVER_BIN",
        "/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver",
        "/usr/bin/chromedriver.exe",
    )
    if not chrome_bin:
        log("ERROR: 找不到 chromium, 请安装并在 MS_CHROMIUM_BIN 指定, 或放到 /usr/bin/chromium")
        sys.exit(2)
    if not driver_bin:
        log("ERROR: 找不到 chromedriver, 请安装 chromium-chromedriver 并在 MS_CHROMEDRIVER_BIN 指定")
        sys.exit(2)
    log("using chromium:", chrome_bin, "| chromedriver:", driver_bin)

    opts = Options()
    opts.binary_location = chrome_bin
    for a in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--headless=new"):
        opts.add_argument(a)
    svc = Service(driver_bin)
    driver = webdriver.Chrome(service=svc, options=opts)
    try:
        driver.get(TARGET)
        time.sleep(2.5)

        if click_text(driver, "统一认证登录", timeout=8):
            log("clicked 统一认证登录")
        time.sleep(2.5)

        if not fill_placeholder(driver, "请输入手机号", phone):
            log("WARN: 手机号输入框未找到, 可能已跳转到登录页")
        time.sleep(0.4)
        if click_text(driver, "继续", exact=True, timeout=6):
            log("clicked 继续")
        time.sleep(3)

        if click_text(driver, "改用密码登录", timeout=8):
            log("clicked 改用密码登录")
        time.sleep(1.5)

        if not fill_placeholder(driver, "请输入密码", password):
            log("WARN: 密码输入框未找到")
        time.sleep(0.4)
        if click_button_contains(driver, "登录", timeout=6):
            log("clicked 登录, waiting for redirect...")
        else:
            click_text(driver, "登录", exact=True, timeout=4)

        t0 = time.time()
        reached = False
        while time.time() - t0 < 30:
            url = driver.current_url
            if "1ms.run" in url and "login.wang" not in url:
                reached = True
                break
            for label in ["授权", "允许", "Authorize", "Allow", "同意并继续"]:
                if click_text(driver, label, exact=True, timeout=2):
                    log("clicked consent:", label)
                    time.sleep(2)
                    break
            time.sleep(1.5)
        log("reached_1ms:", reached, "final_url:", driver.current_url)

        if not reached:
            driver.save_screenshot(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_FAIL.png")
            )
            log("did not return to 1ms.run; screenshot saved")
            sys.exit(1)

        time.sleep(2)
        cookies = driver.get_cookies()
        at = next((c["value"] for c in cookies if c["name"] == AUTH_COOKIE_NAME), None)
        if not at:
            log("WARN: auth_token cookie not found")
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
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
