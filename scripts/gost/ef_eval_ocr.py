#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyFrp 验证码 OCR 准确率评估工具（宿主机运行）

★ 核心方法：用服务端当裁判，不需要人工标注
  登录接口先校验验证码、再校验密码，且 captchaKey 可重复提交。
  因此"提交后未返回验证码错误" == "OCR 答案正确"。

  为什么必须这样测：早前拿 72 张人工标注样本测 ddddocr，得出"仅 29%、不可用"，
  但那批标注本身约 30% 有误 —— 用坏基准评估会把正确识别判成错的。
  换成服务端 oracle 后，真实准确率是 80%（raw）/ 87.5%（bright）。

用法（宿主机）:
  /opt/ocrvenv/bin/python ef_eval_ocr.py                # 默认 20 组
  /opt/ocrvenv/bin/python ef_eval_ocr.py 50             # 50 组
  /opt/ocrvenv/bin/python ef_eval_ocr.py 20 --url http://127.0.0.1:13003/ocr

凭证来源（按序回退）:
  环境变量 EF_ACCOUNT / EF_PASSWORD 或 GOST_ACCOUNT / GOST_PASSWORD
  -> /ql/data/config/env.sh -> 容器内同名文件（经 docker exec 不可用，故仅前两者）
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API = "https://easyfrp.sian.one/api"
DEFAULT_OCR = "http://127.0.0.1:13003/ocr"
ENV_CANDIDATES = [
    "/ql/data/config/env.sh",
    "/root/ql1/config/env.sh",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.sh"),
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def load_env_file():
    for path in ENV_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("export ") or "=" not in line:
                        continue
                    k, v = line[7:].split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


def creds():
    for ak, pk in (("EF_ACCOUNT", "EF_PASSWORD"), ("GOST_ACCOUNT", "GOST_PASSWORD")):
        a, p = os.environ.get(ak, "").strip(), os.environ.get(pk, "").strip()
        if a and p:
            return a, p
    return "", ""


def post(url, obj, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "Accept": "application/json, text/plain, */*",
                 "Origin": "https://easyfrp.sian.one",
                 "Referer": "https://easyfrp.sian.one/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return {"_http": e.code}
    except Exception as e:
        return {"_err": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=20, help="采样组数")
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--url", default=DEFAULT_OCR, help="OCR 服务地址")
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    load_env_file()
    acc, pwd = creds()
    if not acc or not pwd:
        print("!! 未找到账号凭证（EF_ACCOUNT/EF_PASSWORD 或 GOST_ACCOUNT/GOST_PASSWORD）")
        return 1
    print(f"账号 {acc} | OCR {args.url} | 采样 {args.n} 组\n", flush=True)

    # 先探活
    try:
        h = json.loads(urllib.request.urlopen(
            args.url.rsplit("/", 1)[0] + "/health", timeout=20).read().decode())
        print(f"OCR 服务健康: {h}\n", flush=True)
    except Exception as e:
        print(f"!! OCR 服务不可达: {type(e).__name__}: {e}\n", flush=True)

    grabs = submitted = hit = clean_cnt = 0
    for i in range(args.n):
        o = post(args.api + "/v1/auth/captcha", {})
        d = o.get("data") or {}
        b64, key = d.get("bs64") or "", d.get("key")
        if not b64:
            print(f"[{i}] 抓图失败 {o}", flush=True)
            continue
        grabs += 1
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]

        r = post(args.url, {"imageBase64": b64})
        if not r.get("ok"):
            print(f"[{i}] OCR 异常 {r}", flush=True)
            continue
        txt, clean = r["text"], r["clean"]
        ch = r.get("channels") or {}
        raw_t = (ch.get("raw") or {}).get("text")
        br_t = (ch.get("bright") or {}).get("text")

        if not clean:
            print(f"[{i:>3}] clean=0 raw={raw_t!r:9} bright={br_t!r:9} -> 换图", flush=True)
            time.sleep(args.sleep)
            continue

        clean_cnt += 1
        submitted += 1
        lo = post(args.api + "/v1/auth/login", {
            "account": acc, "password": pwd,
            "captchaKey": key, "captchaValue": txt})
        ok = lo.get("code") == 0
        hit += 1 if ok else 0
        print(f"[{i:>3}] clean=1 采用={txt!r:9} ok={int(ok)} {r.get('ms')}ms", flush=True)
        time.sleep(args.sleep)

    print("\n========== 结果 ==========", flush=True)
    print(f"抓图 {grabs} 张, clean(=可放心提交) {clean_cnt} 张 = {clean_cnt/max(grabs,1):.1%}")
    print(f"提交 {submitted} 次, 命中 {hit} 次 = {hit/max(submitted,1):.1%}")
    print(f"=> 每次成功平均需抓图 {grabs/max(hit,1):.2f} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
