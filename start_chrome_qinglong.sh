#!/usr/bin/env bash
# 在【青龙容器内部】预备真实浏览器环境 (只需跑一次, 或作为青龙"依赖"任务执行)
# 用途: 让 checkin_slide.py 能用"真实 chromium + CDP"过天御, 全程不依赖宿主机.
set -e

echo "[1/3] 安装 xvfb + chromium ..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get install -y xvfb chromium \
    || apt-get install -y xvfb chromium-browser
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache xvfb chromium
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y xorg-x11-server-Xvfb chromium
else
  echo "未知包管理器, 请手动安装 xvfb + chromium"; exit 1
fi

echo "[1.5/3] 安装 Python 依赖 (numpy / cv2 / paramiko / playwright) ..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y python3-numpy python3-opencv python3-paramiko 2>/dev/null || true
  python3 -m pip install --break-system-packages playwright 2>/dev/null \
    || pip3 install playwright 2>/dev/null \
    || echo "⚠️ playwright 安装失败, 请在青龙依赖管理补 playwright"
elif command -v apk >/dev/null 2>&1; then
  # Alpine(musl) 用系统包: opencv-python-headless 的 manylinux wheel 在 musl 上装不上
  apk add --no-cache py3-numpy py3-opencv py3-paramiko py3-pip
  python3 -m pip install --break-system-packages --no-cache-dir playwright 2>/dev/null \
    || pip3 install --break-system-packages --no-cache-dir playwright 2>/dev/null \
    || echo "⚠️ playwright 安装失败, 请在青龙依赖管理补 playwright"
fi
python3 -c "import numpy, cv2, paramiko, playwright; print('依赖检查: numpy/cv2/paramiko/playwright OK')" 2>&1 || true

echo "[2/3] 启动常驻 chromium (CDP :9222) ..."
CHROME_BIN=$(command -v chromium || command -v chromium-browser \
             || command -v google-chrome || command -v google-chrome-stable)
if [ -z "$CHROME_BIN" ]; then
  echo "找不到 chromium 可执行文件"; exit 1
fi
# 清理旧实例, 避免重复占用 9222
pkill -f "remote-debugging-port=9222" 2>/dev/null || true
sleep 1
mkdir -p /ql/data/chrome_prof

# 无桌面环境用 xvfb 包一层 (headed 真实浏览器); 有 DISPLAY 直接 headed
if [ -z "$DISPLAY" ] && command -v xvfb-run >/dev/null 2>&1; then
  xvfb-run -a "$CHROME_BIN" --remote-debugging-port=9222 --no-sandbox \
           --disable-dev-shm-usage --user-data-dir=/ql/data/chrome_prof \
           --no-first-run --no-default-browser-check --disable-extensions &
else
  "$CHROME_BIN" --remote-debugging-port=9222 --no-sandbox \
           --disable-dev-shm-usage --user-data-dir=/ql/data/chrome_prof \
           --no-first-run --no-default-browser-check --disable-extensions &
fi

echo "[3/3] 给签到任务加环境变量后生效:"
echo "      CHROME_CDP_URL=http://127.0.0.1:9222"
echo "就绪. 验证: curl -s http://127.0.0.1:9222/json/version | head -c 200"
