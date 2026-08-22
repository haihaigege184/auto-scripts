#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 取 token 脚本 —— 用无头浏览器登录, 取出 auth_token 写文件

依赖:
  - selenium (pip install selenium)
  - 系统 chromium 浏览器 + chromedriver
        Debian/Ubuntu 系:  apt-get install -y chromium chromium-driver
        (qinglong 容器多为 Alpine, 无法装 playwright; 推荐在本脚本跑在宿主机 cron)
  - 1ms.run 登录走 Logto OIDC + 腾讯防水墙验证码, 纯标准库无法绕过, 必须浏览器。

设计:
  - 账号密码从环境变量 MS_PHONE / MS_PASSWORD 读取 (青龙/宿主机环境变量配置)
  - 登录成功后取 auth_token cookie, 写入 token 文件 (供 checkin.py 读取, 实现自动刷新)
  - token 文件默认:
        若 /ql/data 目录存在 -> /ql/data/1ms_token.txt   (青龙持久化目录)
        否则 -> 与本脚本同目录的 .token                  (本机/调试用)
    可用环境变量 MS_TOKEN_FILE 覆盖路径。
  - 兼容 --print-token: 把 MS_TOKEN=xxx 打到 stdout

青龙部署 (推荐宿主机 cron 方案, 因容器 Alpine 无法跑浏览器):
  - 宿主机 (Debian) 装好 chromium + chromedriver + selenium
  - 环境变量: MS_PHONE, MS_PASSWORD, MS_TOKEN_FILE=/root/ql1/1ms_token.txt
    (该路径 bind 挂进容器即 /ql/data/1ms_token.txt, checkin.py 原样读取)
  - 宿主机 cron: 55 8 * * *  (需早于签到 checkin.py 的 0 9 * * *)
  - 命令: MS_PHONE=... MS_PASSWORD=... MS_TOKEN_FILE=/root/ql1/1ms_token.txt python3 /root/ql1/scripts/haihaigege184_auto-scripts_main/scripts/1ms/login.py

流程:
  1ms.run/login -> 点"统一认证登录" -> OIDC 页填手机号 -> 继续
  -> 改用密码登录 -> 填密码 -> 登录 -> (腾讯验证码 headless 自动过)
  -> 授权确认页点"授权" -> 跳回 1ms.run -> 导出 auth_token cookie
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
        "安装:  pip3 install selenium  (Debian 还需 apt-get install -y chromium chromium-driver)\n"
        "安装后重新运行本任务即可自动登录。\n\n"
        "临时兜底: 在环境变量设 MS_TOKEN=<从浏览器开发者工具拿到的 token>,\n"
        "          checkin.py 会优先读 token 文件, 读不到再回退 MS_TOKEN 环境变量。\n"
    )
    sys.exit(2)


TARGET = "https://1ms.run/login?redirect=/user/domain"
AUTH_COOKIE_NAME = "auth_token"
STATUS_API = "https://1ms.run/api/v1/mall/checkin/status"


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
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] in ("--phone", "-u"):
            phone = sys.argv[i + 1]; i += 2
        elif sys.argv[i] in ("--password", "-p"):
            password = sys.argv[i + 1]; i += 2
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


def find_and_click_text(driver, text, timeout=6, contains=False, exact=False):
    if exact:
        xp = f"//*[normalize-space()='{text}']"
    elif contains:
        xp = f"//*[contains(normalize-space(), '{text}')]"
    else:
        xp = f"//*[normalize-space()='{text}']"
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xp)))
        el.click()
        return True
    except Exception:
        return False


def find_and_click_button(driver, text, timeout=6):
    xp = f"//button[normalize-space()='{text}']"
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xp)))
        el.click()
        return True
    except Exception:
        return False


def main():
    phone, password = get_args()
    if not phone or not password:
        log("ERROR: 需要 MS_PHONE / MS_PASSWORD 环境变量 (或 --phone/--password 参数)。")
        sys.exit(1)

    chrome_bin = resolve_binary("MS_CHROMIUM_BIN",
                                "/usr/bin/chromium", "/usr/bin/chromium-browser",
                                "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable")
    driver_bin = resolve_binary("MS_CHROMEDRIVER_BIN",
                                 "/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver")
    if not chrome_bin or not driver_bin:
        log("ERROR: 找不到 chromium/chromedriver, 请安装并设 MS_CHROMIUM_BIN/MS_CHROMEDRIVER_BIN")
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
        time.sleep(3)

        # 1) 点"统一认证登录"
        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".auth-entry__btn"))).click()
        log("clicked 统一认证登录")
        time.sleep(5)

        # 2) 填手机号 -> 继续
        ph = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "v-0-phone")))
        ph.clear(); ph.send_keys(phone)
        log("filled phone")
        time.sleep(0.5)
        if not find_and_click_button(driver, "继续", timeout=8):
            find_and_click_text(driver, "继续", timeout=4)
        log("clicked 继续")
        time.sleep(4)

        # 3) 改用密码登录 -> 填密码 -> 登录
        if find_and_click_text(driver, "改用密码登录", timeout=8):
            log("clicked 改用密码登录")
            time.sleep(2)
        pw = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "v-0-password")))
        pw.clear(); pw.send_keys(password)
        log("filled password")
        time.sleep(0.5)
        if not find_and_click_button(driver, "登录", timeout=8):
            find_and_click_text(driver, "登录", timeout=4)
        log("clicked 登录, waiting for redirect...")

        # 4) 等待跳转回 1ms.run, 期间点授权确认
        t0 = time.time()
        reached = False
        while time.time() - t0 < 60:
            url = driver.current_url
            if "1ms.run" in url and "login.wang" not in url and "/login" not in url:
                reached = True
                break
            for label in ["授权", "允许", "Authorize", "Allow", "同意并继续"]:
                if find_and_click_text(driver, label, exact=True, timeout=2):
                    log("clicked consent:", label)
                    time.sleep(2)
                    break
            time.sleep(2)
        log("reached_1ms:", reached, "final_url:", driver.current_url)

        if not reached:
            driver.save_screenshot(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_FAIL.png"))
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
                headers={"Authorization": "Bearer " + at, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode())
                ok = r.status == 200 and "data" in j
        except Exception as e:
            log("verify err:", e)

        if not ok:
            log("VERIFY FAILED")
            sys.exit(1)

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
