#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 每日签到 —— 青龙版（无浏览器依赖）

依赖: requests  (青龙内置)
环境变量:
  MS_TOKEN   —— 1ms.run 登录后的 auth_token cookie 值
               接口鉴权: Authorization: Bearer <MS_TOKEN>
  (可选) MS_UID —— 预留, 暂未使用

青龙定时建议: 0 9 * * *   (每天 09:00)

鉴权说明:
  1ms.run 的 /api/v1/mall/checkin* 接口要求
      Authorization: Bearer <auth_token cookie 的值>
  不是普通会话 cookie。token 取自 cookie 名 auth_token。

获取 MS_TOKEN: 在本机运行 scripts/1ms/login.py (需 playwright+chromium)
  set MS_PHONE=你的手机号
  set MS_PASSWORD=你的密码
  python scripts/1ms/login.py --print-token
  输出 MS_TOKEN=xxxx  → 复制到青龙环境变量。

token 过期 (脚本检测 401) 会推送提醒, 重新运行 login.py 取新值替换即可。

推送: 优先用青龙自带 notify / sendNotify; 都没有则只打印。
"""
import os
import requests

BASE = "https://1ms.run"
STATUS_API = "/api/v1/mall/checkin/status"
CHECKIN_API = "/api/v1/mall/checkin"

try:
    from notify import send
except Exception:
    try:
        from sendNotify import send
    except Exception:
        def send(title, content):
            print(f"[{title}]\n{content}")


def main():
    token = (os.environ.get("MS_TOKEN") or "").strip()
    if not token:
        send("毫秒镜像签到", "❌ 未配置 MS_TOKEN 环境变量, 请在青龙环境变量中添加。")
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
        send("毫秒镜像签到", "❌ 登录态失效 (401/403), 请重新获取 MS_TOKEN 并更新青龙环境变量。\n获取方法: 在本机运行 scripts/1ms/login.py --print-token, 复制输出的 MS_TOKEN 值。")
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
        send("毫秒镜像签到", "❌ 登录态失效 (401/403), 请重新获取 MS_TOKEN 并更新青龙环境变量。")
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
