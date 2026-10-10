#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyFrp 验证码 OCR 服务（宿主机侧，方案 B）

为什么在宿主机跑:
  青龙容器是 Alpine musl，`onnxruntime` / `opencv-python-headless` 都没有二进制 wheel，
  ddddocr 装不上。宿主机是 Debian 12 + Python 3.11，onnxruntime 有 wheel，可正常安装。
  于是把"重活"放宿主机，青龙容器只做 HTTP 调用 —— 与 1ms 项目 crackTCaptcha 同一思路。

接口:
  POST /ocr   {"imageBase64": "<b64>"}  -> {"ok": true, "text": "1234", "conf": 0.98}
  GET  /health                          -> {"ok": true, "engine": "ddddocr", ...}

部署（宿主机 10.0.0.11）:
  python3 -m venv /opt/ocrvenv
  /opt/ocrvenv/bin/pip install ddddocr
  nohup /opt/ocrvenv/bin/python /opt/ef_ocr_service.py --port 13003 > /var/log/ef_ocr.log 2>&1 &

青龙容器调用（容器经 docker0 网关 172.17.0.1 访问宿主机）:
  EF_OCR_URL=http://172.17.0.1:13003/ocr

说明:
  - ddddocr 首次推理会加载 ONNX 模型（约 1-2 秒），之后单次约 100-300ms
  - 只识别 4 位数字验证码；置信度低时如实返回 conf，交由调用方决定是否转人工
  - 纯标准库 http.server，无 web 框架依赖
"""
import argparse
import base64
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 懒加载 ddddocr：避免 import 失败时整个服务起不来
_ocr = None
_load_err = None
_load_at = 0.0


def get_ocr():
    """懒加载 ddddocr 引擎（全局单例）。"""
    global _ocr, _load_err, _load_at
    if _ocr is not None or _load_err is not None:
        return _ocr
    try:
        import ddddocr
        _ocr = ddddocr.DdddOcr(show_ad=False)
        _load_at = time.time()
    except Exception as e:
        _load_err = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[ef_ocr] ddddocr 加载失败: {_load_err}\n")
    return _ocr


def recognize(img_bytes):
    """识别验证码图片字节 -> (text, conf)。conf 为启发式置信度(字符集纯数字)。"""
    eng = get_ocr()
    if eng is None:
        return None, _load_err
    try:
        text = eng.classification(img_bytes)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    text = (text or "").strip()
    # 启发式置信度: 恰好 4 位纯数字才算完全可信
    conf = 1.0 if (len(text) == 4 and text.isdigit()) else 0.3
    return text, conf


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("[ef_ocr] " + (fmt % args) + "\n")

    def do_GET(self):
        if self.path in ("/health", "/"):
            eng = get_ocr()
            self._reply(200, {
                "ok": eng is not None,
                "engine": "ddddocr",
                "loaded": eng is not None,
                "error": _load_err,
                "loadSec": round(time.time() - _load_at, 1) if _load_at else None,
            })
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/ocr":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 4 * 1024 * 1024:
            self._reply(400, {"ok": False, "error": "bad content-length"})
            return
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            self._reply(400, {"ok": False, "error": f"invalid json: {e}"})
            return
        b64 = str(payload.get("imageBase64") or "")
        if not b64:
            self._reply(400, {"ok": False, "error": "imageBase64 required"})
            return
        b64 = b64.split(",", 1)[-1]          # 容忍 data:image/jpeg;base64, 前缀
        try:
            img = base64.b64decode(b64)
        except Exception as e:
            self._reply(400, {"ok": False, "error": f"invalid base64: {e}"})
            return

        t0 = time.time()
        text, conf = recognize(img)
        dt = int((time.time() - t0) * 1000)
        if text is None:
            self._reply(500, {"ok": False, "error": conf})
            return
        self._reply(200, {
            "ok": True, "text": text, "conf": conf,
            "ms": dt, "bytes": len(img),
        })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=13003)
    args = ap.parse_args()

    sys.stderr.write(f"[ef_ocr] starting on {args.host}:{args.port}\n")
    # 预热：提前加载模型，避免首个请求等 1-2 秒
    eng = get_ocr()
    if eng is None:
        sys.stderr.write(f"[ef_ocr] 警告: ddddocr 不可用({_load_err})，服务将返回 500\n")
    else:
        sys.stderr.write(f"[ef_ocr] ddddocr 就绪\n")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()