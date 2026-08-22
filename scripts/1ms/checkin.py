#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
毫秒镜像 (1ms.run) 每日签到 —— 青龙版（纯标准库 urllib，无第三方依赖）

读 token 顺序:
  1. 文件 (默认 /ql/data/1ms_token.txt, 或同目录 .token, 可用 MS_TOKEN_FILE 覆盖)
     —— 该文件由 login.py (宿主机 cron 定时任务) 自动刷新
  2. 环境变量 MS_TOKEN  (手动填的兜底)
接口鉴权: Authorization: Bearer <auth_token>

青龙环境变量:
  MS_PHONE / MS_PASSWORD  —— 给 login.py 用 (取/刷新 token, 跑在宿主机 cron)
  (可选) MS_TOKEN_FILE    —— 指定 token 文件位置 (默认见上)
  (可选) MS_TOKEN         —— 手动兜底 token

定时 (青龙容器内): 20 7 * * *  (每天 07:20, 需晚于宿主机 login 的 08:55 前一天刷新)

日志策略: 无论是否配置推送渠道, 全程用 print 输出到 stdout (青龙日志可见),
          并额外写一份本地日志文件 (MS_LOG_FILE 或默认 /ql/data/1ms_checkin.log),
          绝不静默, 手动运行也能看到完整过程。
"""
import os
import sys
import json
import datetime
import urllib.request
import urllib.error

BASE = "https://1ms.run"
STATUS_API = "/api/v1/mall/checkin/status"
CHECKIN_API = "/api/v1/mall/checkin"


def log_path():
    lp = os.environ.get("MS_LOG_FILE", "").strip()
    if lp:
        return lp
    if os.path.isdir("/ql/data"):
        return "/ql/data/1ms_checkin.log"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkin.log")


def log(msg):
    """打屏 (青龙日志可见) + 写本地日志文件。"""
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
        except Exception as e:
            log(f"读取 token 文件失败 {tp}: {e}")
    v = os.environ.get("MS_TOKEN", "").strip()
    if v:
        return v, "env:MS_TOKEN"
    return None, None


def api_call(method, path, token, data=None):
    """返回 (status_code, obj_or_text)。"""
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


def push(title, content, level="ok"):
    """推送顺序: 青龙原生 notify/sendNotify  ->  自建 webhook (SEA2 机器人转发群)  ->  兜底只 log。"""
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
    # 自建 webhook: 把消息推到 SEA2 机器人 -> 通知群 (纯 urllib, 无依赖)
    wh = os.environ.get("MS_WEBHOOK_URL", "").strip()
    if wh:
        try:
            import urllib.parse
            data = json.dumps({
                "title": title,
                "content": content,
                "level": level,
            }).encode("utf-8")
            req = urllib.request.Request(
                wh,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                log(f"[推送] webhook 已发送 (HTTP {r.status})")
                pushed = True
        except Exception as e:
            log(f"[推送] webhook 发送失败 (已忽略): {e}")
    if not pushed:
        log("[推送] 未配置任何推送渠道 (notify/sendNotify/webhook 均不可用), 仅本地日志。")


def main():
    log("=== 毫秒镜像签到 开始 ===")
    token, src = get_token()
    if not token:
        log("❌ 未找到 token: 来源=无")
        log("   请确认宿主机 cron 的 login.py 已正常执行 (早于本任务), ")
        log("   或在青龙环境变量设置 MS_TOKEN 兜底。")
        push("毫秒镜像签到", "❌ 未找到 token", level="error")
        return
    log(f"token 来源: {src} (长度 {len(token)})")

    st, resp = api_call("GET", STATUS_API, token)
    log(f"GET {STATUS_API} -> HTTP {st}")
    if isinstance(resp, str):  # 出错文本
        log(f"   响应文本: {resp[:300]}")
        if st in (401, 403):
            log("❌ 登录态失效 (401/403)。请确认 login.py 定时任务正常执行。")
            push("毫秒镜像签到", "❌ 登录态失效 (401/403)", level="error")
        else:
            log(f"⚠️ 状态接口返回异常 (HTTP {st})")
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

    log("尚未签到, 发起 POST 签到...")
    cst, cresp = api_call("POST", CHECKIN_API, token, {})
    log(f"POST {CHECKIN_API} -> HTTP {cst}")
    if isinstance(cresp, str):
        log(f"   响应文本: {cresp[:300]}")
        if cst in (401, 403):
            log("❌ 登录态失效 (401/403)。")
            push("毫秒镜像签到", "❌ 登录态失效 (401/403)", level="error")
        else:
            log(f"⚠️ 签到返回异常 (HTTP {cst})")
            push("毫秒镜像签到", f"⚠️ 签到异常 (HTTP {cst})", level="warn")
        return

    if cst == 200 and cresp.get("code") == 0:
        rd = cresp.get("data", {})
        msg = (f"🎉 {today} 签到成功\n"
               f"连续 {rd.get('continuous_days')} 天 / 累计 {rd.get('total_days')} 天")
        log(msg.replace("\n", " | "))
        push("毫秒镜像签到", msg, level="ok")
    else:
        log(f"⚠️ 签到返回非预期: code={cresp.get('code')} msg={cresp.get('message')} raw={cresp}")
        push("毫秒镜像签到", f"⚠️ 签到异常: {cresp}", level="warn")
    log("=== 毫秒镜像签到 结束 ===")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
