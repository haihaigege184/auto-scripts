#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOSTC (gost.sian.one) 每日签到 —— 青龙版（纯标准库 urllib，无第三方依赖）

流程:
  1. GET 首页 HTML 暖场 (种会话 cookie, 模拟真人先开站)
  2. 账号密码登录 POST /api/v1/auth/login  -> 拿 token (JWT, 有效期 7 天)
  3. 查用户信息 POST /api/v1/auth/userInfo (Token header) -> 签到前余额 / 连续签到天数
  4. 签到       POST /api/v1/auth/checkin  (Token header)
  5. 再查用户信息 -> 签到后余额 / 连续签到天数, 计算本次获得

优化目标: 模拟真实用户浏览器操作, 降低风控触发概率。
  - 完整浏览器请求头 (Accept / Accept-Language / sec-fetch-* / DNT) + 随机 UA
  - cookiejar 维护会话 cookie
  - 登录 -> 查信息 -> 签到 之间加入随机人为延迟 (2~6s), 避免瞬间连发暴露脚本特征
  - 签到前先 GET 首页暖场

鉴权: 请求头 Token: <jwt>   (注意: 不是 Authorization: Bearer)

青龙环境变量:
  GOST_ACCOUNT    —— 账号 (必填)
  GOST_PASSWORD   —— 密码 (必填)
  (可选) GOST_BASE —— 域名, 默认 https://gost.sian.one
  (可选) MS_WEBHOOK_URL —— 推送到 SEA2 机器人 (复用 1ms 同一 webhook, 默认只推管理员群)
  (可选) GOST_LOG_FILE  —— 本地日志路径 (默认 /ql/data/gost_checkin.log)

定时 (青龙容器内): 30 8 * * *
"""

import os
import sys
import json
import time
import random
import datetime
import urllib.request
import urllib.error
import http.cookiejar

DEFAULT_BASE = "https://gost.sian.one"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
]

# 青龙容器内的环境变量文件 (手动维护, 青龙 task 不会自动注入 Envs 表变量)
ENV_FILE_CANDIDATES = [
    "/ql/data/config/env.sh",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh"),
]


def load_env_file():
    """解析 env.sh 里的 `export KEY=VALUE` 行注入 os.environ (青龙 task 不自动注入, 需自行加载)。"""
    for path in ENV_FILE_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("export "):
                        continue
                    kv = line[len("export "):].strip()
                    if "=" not in kv:
                        continue
                    k, v = kv.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:
            print(f"[env] 读取 {path} 失败 (已忽略): {e}", flush=True)

LOGIN_API = "/api/v1/auth/login"
USERINFO_API = "/api/v1/auth/userInfo"
CHECKIN_API = "/api/v1/auth/checkin"


def log_path():
    lp = os.environ.get("GOST_LOG_FILE", "").strip()
    if lp:
        return lp
    if os.path.isdir("/ql/data"):
        return "/ql/data/gost_checkin.log"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gost_checkin.log")


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def human_delay(lo=2.0, hi=6.0):
    """模拟真人操作间隔的随机停顿。"""
    d = random.uniform(lo, hi)
    time.sleep(d)
    return d


def base_url():
    b = os.environ.get("GOST_BASE", "").strip()
    if b:
        return b.rstrip("/")
    return DEFAULT_BASE


def make_opener():
    """带 cookiejar + 随机 UA 的 opener。"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    ua = random.choice(USER_AGENTS)
    return opener, cj, ua


def api_call(opener, ua, method, path, token=None, data=None, timeout=15):
    """返回 (code, obj_or_text, http_status)。code 为业务 code (None 表示网络/解析失败)。"""
    url = base_url() + path
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Referer": base_url() + "/",
        "Origin": base_url(),
        "sec-ch-ua": '"Chromium";v="149", "Not)A;Brand";v="24", "Google Chrome";v="149"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "DNT": "1",
    }
    if token:
        headers["Token"] = token
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
            try:
                obj = json.loads(raw)
            except Exception:
                return None, raw, r.status
            return obj.get("code"), obj, r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            obj = json.loads(raw)
            return obj.get("code"), obj, e.code
        except Exception:
            return None, raw, e.code
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", 0


def warmup_home(opener, ua):
    """先 GET 首页 HTML 暖场, 种会话 cookie。"""
    try:
        headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", "DNT": "1"}
        req = urllib.request.Request(base_url() + "/", headers=headers, method="GET")
        with opener.open(req, timeout=15) as r:
            return r.status
    except Exception as e:
        log(f"  暖场 GET 首页失败 (忽略): {e}")
        return -1


def login(opener, ua, account, password):
    code, obj, status = api_call(opener, ua, "POST", LOGIN_API,
                                 data={"account": account, "password": password})
    if code != 0 or not isinstance(obj, dict):
        msg = obj.get("msg", "") if isinstance(obj, dict) else str(obj)
        return None, f"登录失败 HTTP={status} code={code} msg={msg}"
    token = (obj.get("data") or {}).get("token")
    if not token:
        return None, f"登录返回无 token: {obj}"
    return token, None


def get_userinfo(opener, ua, token):
    code, obj, status = api_call(opener, ua, "POST", USERINFO_API, token=token, data={})
    if code != 0 or not isinstance(obj, dict):
        msg = obj.get("msg", "") if isinstance(obj, dict) else str(obj)
        return None, f"查询用户信息失败 HTTP={status} code={code} msg={msg}"
    return obj.get("data") or {}, None


def do_checkin(opener, ua, token):
    code, obj, status = api_call(opener, ua, "POST", CHECKIN_API, token=token, data={})
    if code == 0:
        return "ok", None
    if code == 1:
        msg = obj.get("msg", "已签到") if isinstance(obj, dict) else "已签到"
        return "already", msg
    msg = obj.get("msg", "") if isinstance(obj, dict) else str(obj)
    return "fail", f"签到失败 HTTP={status} code={code} msg={msg}"


def push(title, content, level="ok"):
    """推送顺序: 青龙原生 notify/sendNotify -> 自建 webhook (SEA2 机器人转发群) -> 兜底只 log。"""
    pushed = False
    try:
        from notify import send as _send
        try:
            _send(title, content)
            pushed = True
        except Exception as e:
            log(f"[推送] notify.send 失败 (已忽略): {e}")
    except Exception:
        pass
    try:
        from sendNotify import send as _send2
        try:
            _send2(title, content)
            pushed = True
        except Exception as e:
            log(f"[推送] sendNotify.send 失败 (已忽略): {e}")
    except Exception:
        pass
    wh = os.environ.get("MS_WEBHOOK_URL", "").strip()
    if wh:
        try:
            data = json.dumps({
                "title": title,
                "content": content,
                "level": level,
            }).encode("utf-8")
            req = urllib.request.Request(
                wh, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                log(f"[推送] webhook 已发送 (HTTP {r.status})")
                pushed = True
        except Exception as e:
            log(f"[推送] webhook 发送失败 (已忽略): {e}")
    if not pushed:
        log("[推送] 未配置任何推送渠道 (notify/sendNotify/webhook 均不可用), 仅本地日志。")


def main():
    load_env_file()
    log("=" * 40)
    log("GOSTC 每日签到开始 (真人模拟模式)")
    account = os.environ.get("GOST_ACCOUNT", "").strip()
    password = os.environ.get("GOST_PASSWORD", "").strip()
    if not account or not password:
        log("❌ 未配置 GOST_ACCOUNT / GOST_PASSWORD 环境变量, 退出")
        push("❌ GOSTC 签到失败", "未配置 GOST_ACCOUNT / GOST_PASSWORD 环境变量", "error")
        return 1

    opener, _, ua = make_opener()
    log(f"UA: {ua[:40]}...")

    # 1) 暖场: 先打开首页, 种会话 cookie
    warmup_home(opener, ua)
    human_delay(1.5, 3.5)

    # 2) 登录
    token, err = login(opener, ua, account, password)
    if err:
        log("❌ " + err)
        push("❌ GOSTC 签到失败", err, "error")
        return 1
    log("✅ 登录成功")
    human_delay(2.0, 5.0)

    # 3) 查签到前信息
    info_before, err = get_userinfo(opener, ua, token)
    if err:
        log("⚠️ " + err)
        info_before = {}
    else:
        amt = info_before.get("amount", "?")
        cka = info_before.get("checkinAmount", "?")
        log(f"签到前: 余额={amt} 连续签到天数={cka}")
    human_delay(2.0, 5.0)

    # 4) 签到
    result, err = do_checkin(opener, ua, token)
    if result == "fail":
        log("❌ " + err)
        push("❌ GOSTC 签到失败", err, "error")
        return 1
    elif result == "already":
        log("ℹ️ 今日已签到 (跳过)")
    else:
        log("✅ 签到成功")

    # 5) 查签到后信息
    human_delay(1.5, 3.5)
    info_after, err = get_userinfo(opener, ua, token)
    if err:
        log("⚠️ " + err)
        gained = "?"
        cka_after = "?"
        amt_after = "?"
    else:
        amt_after = info_after.get("amount", "?")
        cka_after = info_after.get("checkinAmount", "?")
        try:
            gained = str(int(float(amt_after)) - int(float(info_before.get("amount", amt_after))))
        except Exception:
            gained = "?"
        log(f"签到后: 余额={amt_after} 连续签到天数={cka_after} 本次获得={gained}")

    # 推送摘要
    if result == "already":
        title = "ℹ️ GOSTC 今日已签到"
        content = f"账号 {account}\n连续签到 {cka_after} 天\n当前余额 {amt_after}"
        level = "info"
    else:
        title = "✅ GOSTC 签到成功"
        content = (f"账号 {account}\n本次获得 {gained}\n连续签到 {cka_after} 天\n当前余额 {amt_after}")
        level = "ok"

    log("📤 推送结果摘要: " + title)
    push(title, content, level)
    log("GOSTC 每日签到结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
