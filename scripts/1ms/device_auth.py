#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1ms.run (毫秒镜像) 设备授权 —— 一次性获取 Bearer token (无需密码)

流程 (逆向自 1ms-helper Go 源码 app/utils/auth_utils.go):
  1. 用户去 https://1ms.run/user?menu=10 登录并生成 8 位授权码 (user_code)
  2. 本脚本生成 32 字节 secure_key (hex 64 位, 用于防止其它客户端冒领)
  3. POST /api/v1/auth/device/request 提交 {code, device_name, device_info, secure_key}
  4. 用户在网页点 [批准/授权]
  5. GET  /api/v1/auth/device/poll/{code}?secure_key=... 轮询, status=approved 时返回 token
  6. token 写入文件 (默认 /ql/data/1ms_token.txt), 供 checkin.py 日常签到使用

用法:
  python3 device_auth.py 58615642            # 直接传 8 位授权码
  MS_DEVICE_CODE=58615642 python3 device_auth.py   # 或走环境变量

青龙环境变量 (可选):
  MS_TOKEN_FILE    token 文件位置
  MS_DEVICE_NAME   设备名 (展示用, 默认 QingLong)
  MS_DEVICE_INFO   设备信息 (展示用, 默认 Linux)
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error

BASE = "https://1ms.run"


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


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def token_file_path():
    tf = os.environ.get("MS_TOKEN_FILE", "").strip()
    if tf:
        return tf
    if os.path.isdir("/ql/data"):
        return "/ql/data/1ms_token.txt"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def gen_secure_key():
    """32 字节随机密钥 -> hex 64 字符。"""
    return os.urandom(32).hex()


def request_device_auth(code, secure_key, device_name, device_info):
    url = BASE + "/api/v1/auth/device/request"
    body = json.dumps({
        "code": code,
        "device_name": device_name,
        "device_info": device_info,
        "secure_key": secure_key,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "1ms-device-auth/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "ignore") if e.fp else ""
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"code": -1, "msg": txt}
    except Exception as e:
        return -1, {"code": -1, "msg": f"{type(e).__name__}: {e}"}


def poll_device_auth(code, secure_key, max_attempts=36, interval=5):
    """轮询授权结果, 最多 max_attempts*interval 秒。"""
    for i in range(max_attempts):
        time.sleep(interval)
        url = f"{BASE}/api/v1/auth/device/poll/{code}?secure_key={secure_key}"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            log(f"  poll 网络错误 (忽略重试): {e}")
            continue
        if not isinstance(resp, dict) or resp.get("code") != 0:
            msg = resp.get("msg") if isinstance(resp, dict) else str(resp)
            if "过期" in str(msg) or "expired" in str(msg).lower():
                return None, f"授权已过期: {msg}"
            log(f"  poll 返回非0 (继续): {msg}")
            continue
        data = resp.get("data", {})
        status = data.get("status")
        if status == "approved":
            tok = data.get("token")
            if tok:
                return tok, None
            return None, "授权成功但未返回 token"
        if status == "rejected":
            return None, "授权被拒绝"
        if status == "expired":
            return None, "授权已过期"
        # pending / requested -> 继续等
        remaining = (max_attempts - i - 1) * interval
        log(f"  等待授权... status={status} (剩余约 {remaining}s)")
    return None, "授权超时 (请确认已在网页点 [批准])"


def main():
    load_env_file()
    code = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MS_DEVICE_CODE", "").strip()
    if not code:
        print("用法: python3 device_auth.py <8位授权码>  或设置环境变量 MS_DEVICE_CODE", flush=True)
        sys.exit(2)
    if len(code) != 8 or not code.isdigit():
        print("授权码必须是 8 位数字", flush=True)
        sys.exit(2)

    secure_key = gen_secure_key()
    device_name = os.environ.get("MS_DEVICE_NAME", "QingLong")
    device_info = os.environ.get("MS_DEVICE_INFO", "Linux")

    log(f"提交设备授权请求 code={code} device={device_name}({device_info}) ...")
    st, resp = request_device_auth(code, secure_key, device_name, device_info)
    if st != 200 or (isinstance(resp, dict) and resp.get("code") != 0):
        msg = resp.get("msg") if isinstance(resp, dict) else str(resp)
        log(f"❌ 设备授权请求被拒绝: HTTP={st} {msg}")
        sys.exit(1)

    log("✅ 请求已提交, 请在网页/App 的设备授权页点击 [批准/授权] ...")
    token, err = poll_device_auth(code, secure_key)
    if not token:
        log(f"❌ 授权失败: {err}")
        sys.exit(1)

    tp = token_file_path()
    try:
        with open(tp, "w", encoding="utf-8") as f:
            f.write(token)
        log(f"🎉 设备授权成功, token 已写入: {tp} (长度 {len(token)})")
        log("日常签到将自动使用该 token (无需密码/无登录验证码)。如将来 token 失效,"
            "重新生成授权码后运行本脚本或设置 MS_DEVICE_CODE 即可。")
    except Exception as e:
        log(f"❌ 写入 token 文件失败: {e}")
        log(f"   token={token}")
        sys.exit(1)


if __name__ == "__main__":
    main()
