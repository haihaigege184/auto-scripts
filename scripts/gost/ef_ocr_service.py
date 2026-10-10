#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[已迁移] EasyFrp 验证码 OCR 服务 —— 宿主机侧快照

⚠️ 2026-10-11 起本服务已**容器化部署**，本文件仅作历史快照。
   最新代码与镜像:
     仓库  https://github.com/haihaigege184/easyfrp-ocr
     镜像  ghcr.io/haihaigege184/easyfrp-ocr:latest
     运行  docker run -d --name ef-ocr --restart unless-stopped -p 13003:13003 \
             ghcr.io/haihaigege184/easyfrp-ocr:latest
   改动请改新仓库，避免两份代码漂移。

为什么当初在宿主机跑:
  青龙容器是 Alpine musl，`onnxruntime` 没有二进制 wheel，ddddocr 装不上。
  宿主机 Debian + Python 3.11 有 wheel，于是把"重活"放宿主机，容器只做 HTTP 调用。
  （容器化后这个限制被封进镜像，宿主机不再需要 venv。）

核心策略（实测结论，非常重要）:
  ddddocr 的输出可二分：
    - 输出「4 位纯数字」→ 准确率 100%（raw 24/24、bright 28/29 实测）
    - 输出其他（含字母/位数不对）→ 准确率 0%
  因此本服务对同一张图跑两条通道，只在能给出「4 位纯数字」时才置 clean=true，
  调用方看到 clean=false 就换图，不浪费提交轮次。

  通道 1 (raw)    : 原图直送 ddddocr       —— 命中率约 75%
  通道 2 (bright) : 亮度门限去除彩色干扰线后送 ddddocr —— 命中率约 87.5%
  两通道合并 ≈ 97% 的单张图命中率（平均只需抓 1.03 张图）

接口:
  POST /ocr   {"imageBase64": "<b64>"}
       -> {"ok":true, "text":"1234", "clean":true,
           "channels":{"raw":{"text":"1234","clean":true},
                       "bright":{"text":"1234","clean":true}},
           "ms":123, "bytes":6081}
  GET  /health -> {"ok":true,"engine":"ddddocr",...}

部署（容器化，见 https://github.com/haihaigege184/easyfrp-ocr）:
  docker run -d --name ef-ocr --restart unless-stopped -p 13003:13003 \
    ghcr.io/haihaigege184/easyfrp-ocr:latest
青龙容器调用:
  EF_OCR_URL=http://172.17.0.1:13003/ocr
"""
import argparse
import base64
import io
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ocr = None
_load_err = None
_load_at = 0.0

# bright 通道参数
LUM_THRESHOLD = 130
BG_TOL = 14


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


def is_clean(text):
    """「4 位纯数字」是 ddddocr 高置信的标志（实测该条件下准确率≈100%）。"""
    return bool(text) and len(text) == 4 and text.isdigit()


def sep_bright(img_bytes):
    """亮度门限分离：彩色干扰线普遍较亮，深色字符得以保留。返回 PNG bytes。"""
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    a = np.asarray(im).astype(np.float32)
    lum = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    flat = a.reshape(-1, 3).astype(np.int32)
    cols, cnt = np.unique(flat, axis=0, return_counts=True)
    bg = cols[cnt.argmax()].astype(np.float32)
    mask = (lum < LUM_THRESHOLD) & (np.abs(a - bg).sum(2) > BG_TOL)
    out = np.full(a.shape, 255, np.uint8)
    out[mask] = 0
    buf = io.BytesIO()
    Image.fromarray(out).save(buf, "PNG")
    return buf.getvalue()


def recognize(img_bytes):
    """双通道识别。

    返回 (best_text, clean, channels)：
      clean=True 表示 best_text 来自「4 位纯数字」通道，可以放心提交。
      clean=False 时调用方应换图重试，而不是浪费一次提交。
    """
    eng = get_ocr()
    if eng is None:
        return None, False, {"error": _load_err}

    ch = {}
    # --- 通道 1: 原图 ---
    try:
        t1 = (eng.classification(img_bytes) or "").strip()
    except Exception as e:
        t1 = ""
        ch["raw_err"] = f"{type(e).__name__}: {e}"
    ch["raw"] = {"text": t1, "clean": is_clean(t1)}

    # --- 通道 2: 亮度分离 ---
    t2 = ""
    try:
        t2 = (eng.classification(sep_bright(img_bytes)) or "").strip()
    except Exception as e:
        ch["bright_err"] = f"{type(e).__name__}: {e}"
    ch["bright"] = {"text": t2, "clean": is_clean(t2)}

    # 优先返回 clean 的通道；raw 优先（raw 的 clean 实测 100%）
    if ch["raw"]["clean"]:
        return t1, True, ch
    if ch["bright"]["clean"]:
        return t2, True, ch
    # 都不 clean：返回更长的那个（供调用方参考/兜底提交）
    best = t1 if len(t1) >= len(t2) else t2
    return best, False, ch


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
                "channels": ["raw", "bright"],
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
        b64 = b64.split(",", 1)[-1]
        try:
            img = base64.b64decode(b64)
        except Exception as e:
            self._reply(400, {"ok": False, "error": f"invalid base64: {e}"})
            return

        t0 = time.time()
        text, clean, channels = recognize(img)
        dt = int((time.time() - t0) * 1000)
        if text is None:
            self._reply(500, {"ok": False, "error": str(channels)})
            return
        self._reply(200, {
            "ok": True, "text": text, "clean": clean,
            "channels": channels, "ms": dt, "bytes": len(img),
        })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=13003)
    args = ap.parse_args()

    sys.stderr.write(f"[ef_ocr] starting on {args.host}:{args.port}\n")
    if get_ocr() is None:
        sys.stderr.write(f"[ef_ocr] 警告: ddddocr 不可用({_load_err})，服务将返回 500\n")
    else:
        sys.stderr.write("[ef_ocr] ddddocr 就绪 (双通道 raw+bright)\n")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
