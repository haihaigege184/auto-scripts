#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 每日签到 —— 青龙版（无浏览器依赖）

读 token 顺序:
  1. 文件 (默认 /ql/data/1ms_token.txt, 或同目录 .token, 可用 MS_TOKEN_FILE 覆盖)
     —— 该文件由 login.py (青龙定时任务) 自动刷新
  2. 环境变量 MS_TOKEN  (手动填的兜底)
接口鉴权: Authorization: Bearer <auth_token>

青龙环境变量:
  MS_PHONE / MS_PASSWORD  —— 给 login.py 用 (取/刷新 token)
  (可选) MS_TOKEN_FILE    —— 指定 token 文件位置 (默认见上)
  (可选) MS_TOKEN         —— 手动兜底 token

定时: 0 9 * * *  (每天 09:00, 需晚于 login.py 的刷新时间)
"""
import os
import requests

BASE = "https://1ms.run"
STATUS_API = "/api/v1/mall/checkin/status"
CHECKIN_API = "/api/v1/mall/checkin"


def token_file_path():
    tf = os.environ.get("MS_TOKEN_FILE", "").strip()
    if tf:
        return tf
    if os.path.isdir("/ql/data"):
        return "/ql/data/1ms_token.txt"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def get_token():
    tp = token_file_path()
    if os.path.exists(tp):
        try:
            v = open(tp, "r", encoding="utf-8").read().strip()
            if v:
                return v, "file:" + tp
        except Exception:
            pass
    v = os.environ.get("MS_TOKEN", "").strip()
    if v:
        return v, "env:MS_TOKEN"
    return None, None


try:
    from notify import send
except Exception:
    try:
        from sendNotify import send
    except Exception:
        def send(title, content):
            print(f"[{title}]\n{content}")


def main():
    token, src = get_token()
    if not token:
        send("毫秒镜像签到",
             "❌ 未找到 token: 请先确保 login.py 定时任务正常运行 (早于本任务), "
             "或在青龙环境变量里设置 MS_TOKEN 兜底。")
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Referer": BASE + "/user/checkin",
        "Origin": BASE,
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
    }

    # 1) 查状态 (同时校验登录态)
    try:
        sr = requests.get(BASE + STATUS_API, headers=headers, timeout=20)
    except Exception as e:
        send("毫秒镜像签到", f"❌ 请求失败: {e}")
        return

    if sr.status_code in (401, 403):
        send("毫秒镜像签到",
             "❌ 登录态失效 (401/403)。请确认 login.py 定时任务正常执行 (需早于本任务), "
             "或手动重跑 login.py 刷新 token。")
        return

    try:
        sdata = sr.json().get("data", {})
    except Exception:
        send("毫秒镜像签到", f"⚠️ 状态接口返回异常: {sr.text[:200]}")
        return

    today = sdata.get("today")
    if sdata.get("today_checked"):
        msg = (f"✅ {today} 今日已签到\n"
               f"连续 {sdata.get('continuous_days')} 天 / 累计 {sdata.get('total_days')} 天")
        send("毫秒镜像签到", msg)
        return

    # 2) 签到
    try:
        cr = requests.post(BASE + CHECKIN_API, headers=headers, json={}, timeout=20)
    except Exception as e:
        send("毫秒镜像签到", f"❌ 签到请求失败: {e}")
        return

    if cr.status_code in (401, 403):
        send("毫秒镜像签到", "❌ 登录态失效 (401/403)。请确认 login.py 定时任务正常执行。")
        return

    try:
        cdata = cr.json()
    except Exception:
        send("毫秒镜像签到", f"⚠️ 签到返回异常: {cr.text[:200]}")
        return

    if cdata.get("code") == 0:
        rd = cdata.get("data", {})
        msg = (f"🎉 {today} 签到成功\n"
               f"连续 {rd.get('continuous_days')} 天 / 累计 {rd.get('total_days')} 天")
        send("毫秒镜像签到", msg)
    else:
        send("毫秒镜像签到", f"⚠️ 签到返回异常: {cdata}")


if __name__ == "__main__":
    main()
