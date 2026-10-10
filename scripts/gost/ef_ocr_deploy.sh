#!/bin/bash
# 部署宿主机 OCR 服务（方案 B）—— EasyFrp 验证码 ddddocr 识别
#
# 为什么在宿主机跑：青龙容器是 Alpine musl，onnxruntime / opencv-python-headless
# 都没有二进制 wheel，ddddocr 装不上。宿主机是 Debian 12 + py3.11，onnxruntime 有 wheel。
#
# 用法（宿主机 root 执行）:
#   bash /opt/ef_ocr_deploy.sh          # 安装依赖 + 启动服务
#   bash /opt/ef_ocr_deploy.sh status   # 查看状态
#   bash /opt/ef_ocr_deploy.sh restart  # 重启
set -euo pipefail

PORT="${EF_OCR_PORT:-13003}"
VENV="/opt/ocrvenv"
SVC="/opt/ef_ocr_service.py"
LOG="/var/log/ef_ocr.log"
MIRROR="https://mirrors.aliyun.com/pypi/simple/"

case "${1:-install}" in
  status)
    curl -s "http://127.0.0.1:${PORT}/health" || echo "(服务未响应)"
    echo
    pgrep -af "ef_ocr_service.py" || echo "(进程未运行)"
    ;;
  restart)
    pkill -f "ef_ocr_service.py" 2>/dev/null || true
    sleep 2
    nohup "${VENV}/bin/python" "${SVC}" --port "${PORT}" >> "${LOG}" 2>&1 &
    sleep 6
    curl -s "http://127.0.0.1:${PORT}/health" || echo "(启动后未响应)"
    echo
    ;;
  install|*)
    # 1) venv
    if [ ! -x "${VENV}/bin/python" ]; then
      echo "[1/4] 创建 venv ${VENV} ..."
      python3 -m venv "${VENV}"
    else
      echo "[1/4] venv 已存在"
    fi

    # 2) 升级 pip (Debian 12 的 pip 23.0.1 偏老, 装 onnxruntime 可能解析失败)
    echo "[2/4] 升级 pip ..."
    "${VENV}/bin/pip" install -q -i "${MIRROR}" --trusted-host mirrors.aliyun.com \
      --upgrade pip

    # 3) 装 ddddocr (含 onnxruntime, 约 80MB, 慢是正常的)
    echo "[3/4] 安装 ddddocr (onnxruntime 较大, 请耐心等待) ..."
    "${VENV}/bin/pip" install -i "${MIRROR}" --trusted-host mirrors.aliyun.com ddddocr

    # 4) 启动
    echo "[4/4] 启动服务 :${PORT} ..."
    pkill -f "ef_ocr_service.py" 2>/dev/null || true
    sleep 2
    nohup "${VENV}/bin/python" "${SVC}" --port "${PORT}" >> "${LOG}" 2>&1 &
    sleep 8
    curl -s "http://127.0.0.1:${PORT}/health" || {
      echo "[FAIL] 服务未正常响应, 查看日志: tail -30 ${LOG}"
      exit 1
    }
    echo
    echo "[OK] OCR 服务已就绪"
    echo "  青龙容器侧配置: EF_OCR_URL=http://172.17.0.1:${PORT}/ocr"
    ;;
esac
