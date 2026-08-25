#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 每日签到 —— 青龙版（纯标准库 urllib，无第三方依赖）

优化目标: 最大程度模拟真实用户浏览器操作, 降低风控触发概率。
  - 使用真实浏览器 User-Agent + 完整请求头 (Accept / Accept-Language / sec-fetch-* / DNT)
  - 使用 cookiejar 维护会话 cookie (先 GET 签到页暖场, 再请求 API, 像真人先开页再点)
  - 登录 -> 查状态 -> 签到 之间加入随机人为延迟 (2~6s), 避免瞬间连发
  - 优先用账号密码自动登录拿新 token (不依赖可能过期的静态 token 文件)
  - 签到前先探测 /api/v1/captcha/params 的 enabled:
       enabled=false -> 直接带空 captchaTicket 签到 (真人豁免路径)
       enabled=true  -> 不再硬撞 (硬撞会持续触发风控), 改为提醒手动签到

读 token 顺序:
  1. 环境变量 MS_TOKEN (手动兜底)
  2. 文件 (默认 /ql/data/1ms_token.txt, 或同目录 .token, 可用 MS_TOKEN_FILE 覆盖)
  3. 设备授权 (若设 MS_DEVICE_CODE, 提交授权码+网页批准后拿 token, 最稳不触发登录风控)
账号密码 (用于自动登录刷新 token, 兜底):
  MS_PHONE / MS_PASSWORD

青龙环境变量:
  MS_PHONE / MS_PASSWORD  —— 自动登录用 (兜底, 可能触发验证码风控)
  (可选) MS_DEVICE_CODE   —— 设备授权 8 位授权码; 设了且 token 失效时自动走设备授权
  (可选) MS_DEVICE_NAME   —— 设备授权设备名 (展示用, 默认 QingLong)
  (可选) MS_DEVICE_INFO   —— 设备授权设备信息 (展示用, 默认 Linux)
  (可选) MS_TOKEN_FILE    —— token 文件位置
  (可选) MS_TOKEN         —— 手动兜底 token
  (可选) MS_WEBHOOK_URL   —— 推送到 SEA2 机器人 (私发管理员)
  (可选) MS_LOG_FILE      —— 本地日志路径

设备授权 (推荐, 免密码免登录验证码):
  用户去 https://1ms.run/user?menu=10 生成 8 位授权码, 设 MS_DEVICE_CODE=该码,
  下次签到会自动提交并在网页批准后写入 token 文件; 或直接跑 device_auth.py <授权码>。
  授权成功后即可清除 MS_DEVICE_CODE, 日常签到复用该 token。

定时 (青龙容器内): 20 7 * * *
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

BASE = "https://1ms.run"

# 真实浏览器请求头模板 (Chrome 149 Win10)
BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": BASE + "/user/checkin",
    "Origin": BASE,
    "sec-ch-ua": '"Chromium";v="149", "Not)A;Brand";v="24", "Google Chrome";v="149"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "DNT": "1",
}


def load_env_file():
    """青龙 task 不注入 Envs 也不 source env.sh, 手动解析 /ql/data/config/env.sh。"""
    candidates = [
        "/ql/data/config/env.sh",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "env.sh"),
    ]
    for p in candidates:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if not line.startswith("export "):
                        continue
                    body = line[len("export "):].strip()
                    if "=" not in body:
                        continue
                    k, v = body.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


def log_path():
    lp = os.environ.get("MS_LOG_FILE", "").strip()
    if lp:
        return lp
    if os.path.isdir("/ql/data"):
        return "/ql/data/1ms_checkin.log"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkin.log")


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


def token_file_path():
    tf = os.environ.get("MS_TOKEN_FILE", "").strip()
    if tf:
        return tf
    if os.path.isdir("/ql/data"):
        return "/ql/data/1ms_token.txt"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def save_token(tok):
    """把自动登录拿到的新 token 写回文件, 供下次复用。"""
    try:
        tp = token_file_path()
        with open(tp, "w", encoding="utf-8") as f:
            f.write(tok)
        log(f"token 已刷新写入: {tp}")
    except Exception as e:
        log(f"写入 token 文件失败 (忽略): {e}")


def get_static_token():
    tp = token_file_path()
    if os.path.exists(tp):
        try:
            v = open(tp, "r", encoding="utf-8").read().strip()
            if v:
                return v
        except Exception:
            pass
    return os.environ.get("MS_TOKEN", "").strip()


def make_opener():
    """带 cookiejar 的 opener, 维护会话 cookie。"""
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)), cj


def api_call(opener, method, path, token=None, data=None, timeout=20):
    """返回 (status_code, obj_or_text)。"""
    url = BASE + path
    headers = dict(BASE_HEADERS)
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "ignore") if e.fp else ""
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def warmup_page(opener):
    """先 GET 签到页 HTML, 让服务端种下会话 cookie (模拟真人先开页面)。"""
    try:
        req = urllib.request.Request(BASE + "/user/checkin", headers=dict(BASE_HEADERS), method="GET")
        with opener.open(req, timeout=20) as r:
            return r.status
    except Exception as e:
        log(f"  暖场 GET 签到页失败 (忽略): {e}")
        return -1


def auto_login(opener):
    """用账号密码自动登录, 返回 (token, err)。"""
    phone = os.environ.get("MS_PHONE", "").strip()
    pwd = os.environ.get("MS_PASSWORD", "").strip()
    if not phone or not pwd:
        return None, "未配置 MS_PHONE / MS_PASSWORD, 无法自动登录"
    st, resp = api_call(opener, "POST", "/api/v1/auth/login",
                        data={"account": phone, "password": pwd})
    if isinstance(resp, dict) and resp.get("code") == 0:
        tok = (resp.get("data") or {}).get("token")
        if tok:
            return tok, None
    msg = resp.get("msg", "") if isinstance(resp, dict) else str(resp)
    return None, f"自动登录失败 HTTP={st} msg={msg}"


def device_auth(opener, code):
    """设备授权一次性拿 token: 提交授权码 -> 轮询直到批准。返回 (token, err)。"""
    secure_key = os.urandom(32).hex()
    device_name = os.environ.get("MS_DEVICE_NAME", "QingLong")
    device_info = os.environ.get("MS_DEVICE_INFO", "Linux")
    st, resp = api_call(opener, "POST", "/api/v1/auth/device/request",
                        data={"code": code, "device_name": device_name,
                              "device_info": device_info, "secure_key": secure_key})
    if not (isinstance(resp, dict) and resp.get("code") == 0):
        msg = resp.get("msg") if isinstance(resp, dict) else str(resp)
        return None, f"设备授权请求失败 HTTP={st} msg={msg}"
    log("  设备授权请求已提交, 等待网页批准...")
    for i in range(36):
        time.sleep(5)
        pst, presp = api_call(
            opener, "GET", f"/api/v1/auth/device/poll/{code}?secure_key={secure_key}")
        if isinstance(presp, dict) and presp.get("code") == 0:
            d = presp.get("data", {})
            s = d.get("status")
            if s == "approved" and d.get("token"):
                return d["token"], None
            if s == "rejected":
                return None, "授权被拒绝"
            if s == "expired":
                return None, "授权已过期"
        else:
            msg = presp.get("msg") if isinstance(presp, dict) else str(presp)
            if "过期" in str(msg) or "expired" in str(msg).lower():
                return None, "授权已过期"
    return None, "授权超时 (请确认已在网页点 [批准])"


def get_valid_token(opener):
    """优先用静态 token, 失效则: 设备授权(若设 MS_DEVICE_CODE) -> 自动登录刷新。
    返回 (token, src, err)。"""
    tok = get_static_token()
    if tok:
        # 探测静态 token 是否还有效
        st, resp = api_call(opener, "GET", "/api/v1/mall/checkin/status", token=tok)
        if isinstance(resp, dict) and resp.get("code") not in (401, 403):
            return tok, "static", None
        log("  静态 token 失效, 尝试刷新...")
    # 设备授权 (一次性, 需用户在网页批准; 获批后写入 token 文件供日后复用)
    dcode = os.environ.get("MS_DEVICE_CODE", "").strip()
    if dcode:
        log("  检测到 MS_DEVICE_CODE, 尝试设备授权获取 token ...")
        dtok, derr = device_auth(opener, dcode)
        if dtok:
            save_token(dtok)
            return dtok, "device-auth", None
        log(f"  设备授权失败: {derr} (回退自动登录)")
    tok, err = auto_login(opener)
    if tok:
        save_token(tok)
        return tok, "auto-login", None
    return None, None, err


def push(title, content, level="ok"):
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
    log("=== 毫秒镜像签到 开始 (真人模拟模式) ===")

    opener, _ = make_opener()

    # 1) 暖场: 先打开签到页, 种会话 cookie
    warmup_page(opener)
    human_delay(1.5, 3.5)

    # 2) 获取有效 token
    token, src, err = get_valid_token(opener)
    if not token:
        log("❌ 未找到有效 token: " + str(err))
        push("毫秒镜像签到", "❌ 未找到有效 token (请配置 MS_PHONE/MS_PASSWORD 自动登录)", level="error")
        return
    log(f"token 来源: {src} (长度 {len(token)})")

    # 3) 查签到状态 (像真人进页面先看今天签没签)
    human_delay(2.0, 5.0)
    st, resp = api_call(opener, "GET", "/api/v1/mall/checkin/status", token=token)
    log(f"GET status -> HTTP {st}")
    if isinstance(resp, str):
        log(f"   响应文本: {resp[:300]}")
        if st in (401, 403):
            log("❌ 登录态失效。请确认 MS_PHONE/MS_PASSWORD 正确。")
            push("毫秒镜像签到", "❌ 登录态失效 (401/403)", level="error")
        else:
            log(f"⚠️ 状态接口异常 (HTTP {st})")
            push("毫秒镜像签到", f"⚠️ 状态接口异常 (HTTP {st})", level="warn")
        return

    sdata = resp.get("data", {})
    today = sdata.get("today")
    log(f"   今日={today} 今日已签={sdata.get('today_checked')} "
        f"连续={sdata.get('continuous_days')} 累计={sdata.get('total_days')}")
    if sdata.get("today_checked"):
        msg = (f"✅ {today} 今日已签到\n"
               f"连续 {sdata.get('continuous_days')} 天 / 累计 {sdata.get('total_days')} 天")
        log(msg.replace("\n", " | "))
        push("毫秒镜像签到", msg, level="ok")
        return

    # 4) 探测验证码开关 (关键: 不硬撞风控)
    human_delay(1.0, 3.0)
    cst, cresp = api_call(opener, "POST", "/api/v1/captcha/params", token=token,
                          data={"scene": "checkin"})
    captcha_enabled = False
    if isinstance(cresp, dict) and cresp.get("code") == 0:
        captcha_enabled = bool((cresp.get("data") or {}).get("enabled"))
    log(f"验证码探测: enabled={captcha_enabled}")

    if captcha_enabled:
        # 服务端强制验证码 -> 不再硬撞 (硬撞会持续触发风控), 提醒手动
        log("⚠️ 服务端当前开启验证码风控, 脚本无法自动过验证码 (腾讯云真人验证)。")
        log("   已按最低风控策略跳过自动签到, 请在网页/App 手动完成今日签到。")
        push("毫秒镜像签到",
             f"⚠️ {today} 今日需手动签到\n服务端开启验证码风控, 脚本已自动跳过以免触发更严限制。\n请到 1ms.run 网页或 App 手动点一下签到。",
             level="warn")
        return

    # 5) 未开启验证码 -> 模拟真人点击签到 (带随机延迟)
    log("尚未签到且无需验证码, 发起 POST 签到...")
    human_delay(2.0, 5.0)
    cst, cresp = api_call(opener, "POST", "/api/v1/mall/checkin", token=token,
                          data={"captchaTicket": ""})
    log(f"POST checkin -> HTTP {cst}")
    if isinstance(cresp, str):
        log(f"   响应文本: {cresp[:300]}")
        if "验证码" in cresp or cst == 400:
            log("⚠️ 服务端仍要求验证码, 脚本跳过 (避免硬撞风控)。")
            push("毫秒镜像签到", f"⚠️ {today} 服务端要求验证码\n脚本已自动跳过, 请手动签到。", level="warn")
        elif cst in (401, 403):
            log("❌ 登录态失效 (401/403)。")
            push("毫秒镜像签到", "❌ 登录态失效 (401/403)", level="error")
        else:
            log(f"⚠️ 签到返回异常 (HTTP {cst})")
            push("毫秒镜像签到", f"⚠️ 签到异常 (HTTP {cst})", level="warn")
        return

    if cst == 200 and isinstance(cresp, dict) and cresp.get("code") == 0:
        rd = cresp.get("data", {})
        msg = (f"🎉 {today} 签到成功\n"
               f"连续 {rd.get('continuous_days')} 天 / 累计 {rd.get('total_days')} 天")
        log(msg.replace("\n", " | "))
        push("毫秒镜像签到", msg, level="ok")
    else:
        code = cresp.get("code") if isinstance(cresp, dict) else "?"
        msg = cresp.get("msg") if isinstance(cresp, dict) else str(cresp)
        log(f"⚠️ 签到返回非预期: code={code} msg={msg}")
        # 若服务端突然要求验证码, 不刷屏报错, 温和提示
        if "验证码" in str(msg):
            push("毫秒镜像签到", f"⚠️ {today} 服务端要求验证码\n脚本已自动跳过, 请手动签到。", level="warn")
        else:
            push("毫秒镜像签到", f"⚠️ 签到异常: code={code} msg={msg}", level="warn")
    log("=== 毫秒镜像签到 结束 ===")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
