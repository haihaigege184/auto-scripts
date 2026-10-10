#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EasyFrp (easyfrp.sian.one) 每日签到 —— GOSTC 改版后的新平台

迁移背景 (2026-10-10):
  GOSTC 改版, 签到迁到 EasyFrp 新平台, 老域名 gost.sian.one 即将弃用。
  新旧平台接口结构几乎同构, 唯一差异 = 登录与签到都需图形验证码(4位数字)。

接口 (从官方前端 bundle 反查得到):
  POST /api/v1/auth/login              {account, password, captchaKey, captchaValue}
  POST /api/v1/auth/captcha                        -> {key, bs64}
  POST /api/v1/auth/userinfo                      -> 用户信息/积分
  POST /api/v1/auth/sign              {captchaKey, captchaValue} -> {score}
  POST /api/v1/normal/system/user/sign/summary    -> 签到日历/积分汇总
  鉴权头: Token: <jwt>   (注意不是 Authorization: Bearer)

验证码处理 (ef_captcha_ocr.py):
  纯 cv2 OCR, 无 ML 依赖。裸准确率约 55%, 配合"低分换图重试"可显著提升成功率。
  captchaKey 一次性但可重复提交, 因此重试成本极低。

环境变量:
  EF_ACCOUNT   —— 账号 (必填, 默认回落到 GOST_ACCOUNT)
  EF_PASSWORD  —— 密码 (必填, 默认回落到 GOST_PASSWORD)
  EF_BASE      —— 域名, 默认 https://easyfrp.sian.one
  EF_TOKEN_FILE—— JWT 缓存路径, 默认 /ql/data/ef_token.txt (登录一次可用 7 天)
  EF_MAX_TRY   —— 验证码最大重试轮数, 默认 25
  EF_MANUAL_CAPTCHA —— 设为 1 时不做 OCR, 直接读 EF_CAPTCHA_VALUE 提交(人工兜底)

定时 (青龙容器内): 30 8 * * *
"""

import base64
import datetime
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import ef_captcha_ocr as ocr
except Exception:      # cv2 缺失时不阻塞登录/签到主体逻辑
    ocr = None

DEFAULT_BASE = "https://easyfrp.sian.one"
API_PREFIX = "/api"          # 前端 axios baseURL 就是 /api, 漏掉会拿到 SPA 的 index.html
LOGIN_API = "/v1/auth/login"
CAPTCHA_API = "/v1/auth/captcha"
USERINFO_API = "/v1/auth/userinfo"
SIGN_API = "/v1/auth/sign"
SUMMARY_API = "/v1/normal/system/user/sign/summary"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
]

ENV_FILE_CANDIDATES = [
    "/ql/data/config/env.sh",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh"),
]

# OCR 分数低于此值时, 连"最可能"的猜测都不可信, 直接换图 (不浪费提交)。
# 刻意定得比 ocr 模块的 MIN_CHAR_SCORE 低 —— 因为服务端是最终裁判,
# 猜错只是浪费一轮(captchaKey 可重复提交), 而漏掉可猜的图会白白损失成功率。
# 实测 OCR 裸准确率约 55%, 靠换图重试可把成功率推到接近 100%。
MIN_SUBMIT_SCORE = float(os.environ.get("EF_MIN_SUBMIT_SCORE", "0.45") or 0.45)


# ============================ 基础设施 ============================
def load_env_file():
    """解析 env.sh 的 `export KEY=VALUE` 注入 os.environ (青龙 task 不自动注入 Envs 表)。"""
    for path in ENV_FILE_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("export ") or "=" not in line:
                        continue
                    k, v = line[7:].split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:
            print(f"[env] 读取 {path} 失败 (已忽略): {e}", flush=True)


def log_path():
    lp = os.environ.get("EF_LOG_FILE", "").strip()
    if lp:
        return lp
    if os.path.isdir("/ql/data"):
        return "/ql/data/ef_checkin.log"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ef_checkin.log")


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def base_url():
    return (os.environ.get("EF_BASE", "").strip() or DEFAULT_BASE).rstrip("/")


def api_call(path, data=None, token=None, timeout=20):
    """返回 (code, obj, http_status)。code 为业务 code, None 表示网络/解析失败。"""
    url = base_url() + API_PREFIX + path
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": base_url() + "/",
        "Origin": base_url(),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "DNT": "1",
    }
    if token:
        headers["Token"] = token
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


# ============================ token 缓存 ============================
def token_file():
    p = os.environ.get("EF_TOKEN_FILE", "").strip()
    if p:
        return p
    if os.path.isdir("/ql/data"):
        return "/ql/data/ef_token.txt"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ef_token")


def save_token(tok):
    try:
        p = token_file()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(tok)
        os.chmod(p, 0o600)
    except Exception as e:
        log(f"[token] 写缓存失败(忽略): {e}")


def load_cached_token():
    p = token_file()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            t = f.read().strip()
        return t or None
    except Exception:
        return None


def clear_token():
    try:
        p = token_file()
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


# ============================ 验证码 ============================
def fetch_captcha():
    """-> (key, bs64) ; 失败返回 (None, None)。"""
    code, obj, status = api_call(CAPTCHA_API, {})
    if code == 0 and isinstance(obj, dict):
        d = obj.get("data") or {}
        return d.get("key"), d.get("bs64")
    log(f"  [captcha] 获取失败 HTTP={status} resp={str(obj)[:120]}")
    return None, None


def solve_captcha(bs64, verbose=True):
    """识别验证码。返回 (value, info)。

    容错策略: OCR 给出"最可能"的值就提交, 由**服务端**判定对错。
    实测 captchaKey 一次性但可重复提交, 且错误响应固定为"验证码错误",
    因此即使识别错也只是浪费一轮 —— 换张新图重试即可, 不会锁定账号。
    只有在 OCR 模块不可用/极端低分时才放弃该轮。
    """
    manual = os.environ.get("EF_CAPTCHA_VALUE", "").strip()
    if manual:
        return manual, {"mode": "manual"}
    if ocr is None:
        return None, {"mode": "no_ocr"}
    try:
        tpl = ocr.load_templates()
        r = ocr.ocr_bs64(bs64, templates=tpl)
        if verbose:
            log(f"  [captcha] OCR={r['text'] or '----'} score={r['score']} "
                f"margin={r['min_margin']} ok={r['ok']} {r['debug']}")
        # score 过低说明连候选都不可靠, 换图; 仅仅 margin 小则照提交让服务端判
        if r["score"] < MIN_SUBMIT_SCORE:
            return None, r
        return r["text"], r
    except Exception as e:
        log(f"  [captcha] OCR 异常: {e}")
        return None, {"mode": "ocr_error", "err": str(e)}


def save_captcha_png(bs64, path):
    """把验证码存成图片, 供人工查看 (EF_MANUAL_CAPTCHA=1 时的兜底)。"""
    try:
        with open(path, "wb") as f:
            f.write(base64.b64decode(bs64))
        return path
    except Exception:
        return None


# ============================ 登录 ============================
def login_with_captcha(account, password, max_try=None):
    """反复取验证码 -> OCR -> 登录。成功返回 (token, None)。"""
    max_try = int(os.environ.get("EF_MAX_TRY", "25") or 12)
    best_seen = None
    for i in range(1, max_try + 1):
        key, bs64 = fetch_captcha()
        if not key or not bs64:
            time.sleep(1.5)
            continue
        value, info = solve_captcha(bs64)
        if best_seen is None and info.get("text"):
            best_seen = info["text"]
        if not value:
            time.sleep(random.uniform(0.6, 1.4))
            continue
        code, obj, status = api_call(LOGIN_API, {
            "account": account, "password": password,
            "captchaKey": key, "captchaValue": value,
        })
        if code == 0 and isinstance(obj, dict):
            tok = (obj.get("data") or {}).get("token")
            if tok:
                log(f"  [login] 第 {i} 轮成功 (验证码={value})")
                return tok, None
        msg = obj.get("msg", "") if isinstance(obj, dict) else str(obj)
        log(f"  [login] 第 {i} 轮失败: HTTP={status} msg={msg} (验证码={value})")
        if "密码" in str(msg) or "账号" in str(msg):
            return None, f"账号或密码错误: {msg}"
        time.sleep(random.uniform(0.8, 1.8))
    return None, f"验证码重试 {max_try} 轮仍未登录成功 (最后识别={best_seen})"


def ensure_token(account, password):
    """优先用缓存 token(7天有效), 否则重新登录。返回 (token, err)。"""
    cached = load_cached_token()
    if cached:
        code, obj, _ = api_call(USERINFO_API, {}, token=cached)
        if code == 0:
            log("  [token] 命中缓存且有效")
            return cached, None
        log("  [token] 缓存失效, 重新登录")
        clear_token()
    tok, err = login_with_captcha(account, password)
    if tok:
        save_token(tok)
    return tok, err


# ============================ 签到 ============================
def get_summary(token):
    """签到汇总(积分/连续天数等)。返回 dict 或 {}。"""
    code, obj, _ = api_call(SUMMARY_API, {}, token=token)
    if code == 0 and isinstance(obj, dict):
        d = obj.get("data")
        return d if isinstance(d, dict) else {"raw": d}
    return {}


def get_userinfo(token):
    code, obj, _ = api_call(USERINFO_API, {}, token=token)
    if code == 0 and isinstance(obj, dict):
        return obj.get("data") or {}
    return {}


def do_sign(token, account, password):
    """执行签到(带验证码重试)。返回 (status, detail)。

    status: SUCCESS / ALREADY / MANUAL
    """
    max_try = int(os.environ.get("EF_MAX_TRY", "25") or 12)
    best = None
    for i in range(1, max_try + 1):
        key, bs64 = fetch_captcha()
        if not key or not bs64:
            time.sleep(1.5)
            continue
        value, info = solve_captcha(bs64)
        if best is None and info.get("text"):
            best = info["text"]
        if not value:
            time.sleep(random.uniform(0.6, 1.4))
            continue
        code, obj, status = api_call(SIGN_API, {"captchaKey": key, "captchaValue": value}, token=token)
        if code == 0 and isinstance(obj, dict):
            d = obj.get("data") or {}
            score = d.get("score", d.get("Score", "?"))
            log(f"  [sign] 第 {i} 轮签到成功, 获得 {score} 积分")
            return "SUCCESS", f"签到成功, 获得 {score} 积分"
        msg = obj.get("msg", "") if isinstance(obj, dict) else str(obj)
        log(f"  [sign] 第 {i} 轮: HTTP={status} msg={msg} (验证码={value})")
        low = str(msg).lower()
        if "已签" in str(msg):
            return "ALREADY", "今日已签到"
        time.sleep(random.uniform(0.8, 1.8))
    return "MANUAL", f"验证码重试 {max_try} 轮未完成签到 (最后识别={best})"


# ============================ 推送 ============================
def push(title, content, level="ok"):
    """青龙 notify/sendNotify -> 自建 webhook -> 仅本地日志。"""
    pushed = False
    for mod in ("notify", "sendNotify"):
        try:
            m = __import__(mod)
            m.send(title, content)
            pushed = True
            log(f"[推送] {mod}.send ok")
        except Exception:
            pass
    wh = os.environ.get("MS_WEBHOOK_URL", "").strip()
    if wh:
        try:
            data = json.dumps({"title": title, "content": content, "level": level}).encode()
            req = urllib.request.Request(wh, data=data,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                log(f"[推送] webhook HTTP {r.status}")
            pushed = True
        except Exception as e:
            log(f"[推送] webhook 失败(忽略): {e}")
    if not pushed:
        log("[推送] 无可用推送渠道, 仅本地日志")


# ============================ 主流程 ============================
def main():
    load_env_file()
    log("=" * 46)
    log("EasyFrp 每日签到开始 (新平台)")
    account = (os.environ.get("EF_ACCOUNT", "").strip()
               or os.environ.get("GOST_ACCOUNT", "").strip())
    password = (os.environ.get("EF_PASSWORD", "").strip()
                or os.environ.get("GOST_PASSWORD", "").strip())
    if not account or not password:
        log("❌ 未配置 EF_ACCOUNT/EF_PASSWORD (或 GOST_ACCOUNT/GOST_PASSWORD)")
        push("❌ EasyFrp 签到失败", "未配置账号或密码", "error")
        return 1

    if os.environ.get("EF_MANUAL_CAPTCHA", "").strip() == "1":
        log("⚠️ EF_MANUAL_CAPTCHA=1: 跳过 OCR, 直接用 EF_CAPTCHA_VALUE 登录")
    if ocr is None:
        log("⚠️ 未导入 cv2/OCR 模块, 验证码将无法自动识别 (仅支持 EF_CAPTCHA_VALUE 人工模式)")

    # 1) 登录
    token, err = ensure_token(account, password)
    if err or not token:
        log("❌ 登录失败: " + str(err))
        if "验证码" in str(err):
            log("   验证码自动识别未通过。可选: 补样本提升 OCR, 或设 EF_CAPTCHA_VALUE 人工登录一次缓存 token。")
        push("❌ EasyFrp 登录失败", str(err), "error")
        return 1
    log("✅ 登录成功")

    # 2) 签到前信息
    info = get_userinfo(token)
    if info:
        log(f"签到前: {json.dumps(info, ensure_ascii=False)[:160]}")

    # 3) 签到
    status, detail = do_sign(token, account, password)
    time.sleep(1.0)

    # 4) 签到后信息
    summary = get_summary(token)
    info2 = get_userinfo(token)
    if summary:
        log(f"签到汇总: {json.dumps(summary, ensure_ascii=False)[:220]}")
    if info2:
        log(f"签到后: {json.dumps(info2, ensure_ascii=False)[:160]}")

    if status == "SUCCESS":
        title, level = "✅ EasyFrp 签到成功", "ok"
    elif status == "ALREADY":
        title, level = "ℹ️ EasyFrp 今日已签到", "info"
    else:
        title, level = "⚠️ EasyFrp 签到未完成", "error"

    content = f"账号 {account}\n{detail}"
    if summary:
        content += f"\n{json.dumps(summary, ensure_ascii=False)[:200]}"
    log("📤 " + title)
    push(title, content, level)
    log("EasyFrp 每日签到结束")
    return 0 if status in ("SUCCESS", "ALREADY") else 1


if __name__ == "__main__":
    sys.exit(main())