#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 每日签到 —— 青龙版（纯标准库 urllib，无第三方依赖）

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
import json
import urllib.request
import urllib.error

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


def api_call(method, path, token, data=None):
    """返回 (status_code, obj_or_text)。成功返回解析后的 dict；HTTPError 返回 (code, 文本)。"""
    url = BASE + path
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Referer": BASE + "/user/checkin",
        "Origin": BASE,
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "ignore") if e.fp else ""
        return e.code, txt
    except Exception as e:  # 网络错误等
        return -1, str(e)


def main():
    token, src = get_token()
    if not token:
        send("毫秒镜像签到",
             "❌ 未找到 token: 请先确保 login.py 定时任务正常运行 (早于本任务), "
             "或在青龙环境变量里设置 MS_TOKEN 兜底。")
        return

    st, resp = api_call("GET", STATUS_API, token)
    if isinstance(resp, str):  # 出错文本
        if st in (401, 403):
            send("毫秒镜像签到", "❌ 登录态失效 (401/403)。请确认 login.py 定时任务正常执行 (需早于本任务)。")
        else:
            send("毫秒镜像签到", f"⚠️ 状态接口返回异常 (HTTP {st}): {resp[:200]}")
        return

    sdata = resp.get("data", {})
    today = sdata.get("today")
    if sdata.get("today_checked"):
        msg = (f"✅ {today} 今日已签到\n"
               f"连续 {sdata.get('continuous_days')} 天 / 累计 {sdata.get('total_days')} 天")
        send("毫秒镜像签到", msg)
        return

    cst, cresp = api_call("POST", CHECKIN_API, token, {})
    if isinstance(cresp, str):
        if cst in (401, 403):
            send("毫秒镜像签到", "❌ 登录态失效 (401/403)。请确认 login.py 定时任务正常执行。")
        else:
            send("毫秒镜像签到", f"⚠️ 签到返回异常 (HTTP {cst}): {cresp[:200]}")
        return

    if cst == 200 and cresp.get("code") == 0:
        rd = cresp.get("data", {})
        msg = (f"🎉 {today} 签到成功\n"
               f"连续 {rd.get('continuous_days')} 天 / 累计 {rd.get('total_days')} 天")
        send("毫秒镜像签到", msg)
    else:
        send("毫秒镜像签到", f"⚠️ 签到返回异常: {cresp}")


if __name__ == "__main__":
    main()
