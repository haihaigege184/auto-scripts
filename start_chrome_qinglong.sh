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
# pip 参数: aliyun 主镜像 + Tsinghua 兜底; 不配 pypi.org(容器里不通, 会死等超时, 拖成 10 分钟)
PIPI="-i https://mirrors.aliyun.com/pypi/simple --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple --retries 1 --timeout 20"
if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y python3-numpy python3-opencv python3-paramiko 2>/dev/null || true
  python3 -m pip install --break-system-packages playwright 2>/dev/null \
    || pip3 install playwright 2>/dev/null \
    || echo "⚠️ playwright 安装失败, 请在青龙依赖管理补 playwright"
elif command -v apk >/dev/null 2>&1; then
  # Alpine(musl): 先 apk 拿 numpy/paramiko/opencv; 再逐个 pip 补(一个失败不影响其它)
  apk add --no-cache py3-numpy py3-opencv py3-paramiko py3-pip
  for PKG in numpy paramiko playwright opencv-python-headless; do
    python3 -m pip install --break-system-packages --no-cache-dir $PIPI "$PKG" \
      >/tmp/pip_$PKG.log 2>&1 || echo "⚠️ $PKG pip 安装失败(可忽略)"
    tail -1 /tmp/pip_$PKG.log 2>/dev/null || true
  done
fi

# cv2 兜底: apk 的 py3-opencv 装到了【另一个 python 前缀】(如 /usr/lib/python3.x),
# 用 .pth 把那个 site-packages 桥接进任务实际用的 python(opencv 的 manylinux wheel 在 musl 装不上, 不能只靠 pip)
if ! python3 -c "import cv2" >/dev/null 2>&1; then
  CV2_DIR=$(find /usr /lib /opt /ql -maxdepth 7 -type d -name cv2 -path "*site-packages*" 2>/dev/null | head -1)
  if [ -n "$CV2_DIR" ]; then
    SP=$(python3 -c "import site;print(site.getsitepackages()[0])" 2>/dev/null)
    SYS_SP=$(dirname "$CV2_DIR")
    if [ -n "$SP" ] && [ "$SP" != "$SYS_SP" ]; then
      echo "$SYS_SP" > "$SP/zz_cv2_bridge.pth" 2>/dev/null || true
      echo "已桥接 cv2: $SYS_SP -> $SP"
    fi
  fi
fi
python3 -c "import numpy, cv2, paramiko, playwright; print('依赖检查 OK: numpy/cv2/paramiko/playwright')" 2>&1 \
  || echo "⚠️ 仍有依赖缺失(见上一行), 签到任务 import 会失败"

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

# 无桌面环境: 自己拉起 Xvfb
# 注意: Alpine 的 xvfb 包只有 Xvfb 二进制, 往往没有 xvfb-run 脚本, 不能依赖 command -v xvfb-run
if [ -z "$DISPLAY" ] && command -v Xvfb >/dev/null 2>&1; then
  pkill -f "Xvfb :99" 2>/dev/null || true
  setsid nohup Xvfb :99 -screen 0 1280x900x24 >/dev/null 2>&1 &
  sleep 2
  export DISPLAY=:99
  echo "已启动 Xvfb :99"
fi

# setsid+nohup+disown: 脚本结束后 chromium 仍常驻(否则任务结束会被一起杀掉)
LAUNCH_ARGS="--remote-debugging-port=9222 --no-sandbox --disable-dev-shm-usage --user-data-dir=/ql/data/chrome_prof --no-first-run --no-default-browser-check --disable-extensions"
setsid nohup "$CHROME_BIN" $LAUNCH_ARGS >/ql/data/chrome.log 2>&1 &
disown 2>/dev/null || true

echo "[3/3] 给签到任务加环境变量后生效:"
echo "      CHROME_CDP_URL=http://127.0.0.1:9222"
# 等待 CDP 真正起来再返回 (curl 带 --max-time, 避免连接挂起)
VER=""
for i in $(seq 1 40); do
  VER=$(curl -s --max-time 3 http://127.0.0.1:9222/json/version 2>/dev/null | head -c 200)
  if [ -n "$VER" ]; then break; fi
  sleep 0.5
done
if [ -n "$VER" ]; then
  echo "CDP 已就绪: $VER"
else
  echo "⚠️ CDP 未就绪, chrome.log 末尾:"
  tail -10 /ql/data/chrome.log 2>/dev/null || true
fi
