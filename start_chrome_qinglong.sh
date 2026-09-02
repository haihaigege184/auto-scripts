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
  # Alpine(musl): numpy/cv2/paramiko 用系统包(py3-*)装进【任务实际用的系统 python3】
  #   opencv 的 manylinux wheel 在 musl 上装不上, 只能 apk
  # playwright 只能 pip, 且逐个装(任一失败不影响其它); 多镜像兜底(Tsinghua 对 playwright 偶发返回空)
  apk add --no-cache py3-numpy py3-opencv py3-paramiko py3-pip
  for PKG in numpy paramiko playwright; do
    python3 -m pip install --break-system-packages --no-cache-dir \
      -i https://mirrors.aliyun.com/pypi/simple \
      --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      --extra-index-url https://pypi.org/simple \
      "$PKG" 2>&1 | tail -2 \
      || echo "⚠️ $PKG 安装失败(若青龙依赖管理已有可忽略)"
  done
fi
python3 -c "import numpy, cv2, paramiko, playwright; print('系统 python3 依赖检查: numpy/cv2/paramiko/playwright OK')" 2>&1 || true

# 兜底桥接: 若青龙把依赖装在别的 python(如 /ql/py3), 把系统 site-packages 桥接过去, 让它能 import cv2
for PY in $(find /ql -maxdepth 4 -name python3 -path "*/bin/*" 2>/dev/null); do
  if $PY -c "import playwright" >/dev/null 2>&1; then
    SP=$($PY -c "import site;print(site.getsitepackages()[0])" 2>/dev/null)
    SYS_SP=$(python3 -c "import site;print(site.getsitepackages()[0])" 2>/dev/null)
    if [ -n "$SP" ] && [ -n "$SYS_SP" ] && [ "$SP" != "$SYS_SP" ]; then
      echo "$SYS_SP" > "$SP/zz_qinglong_sys.pth"
      echo "已桥接: $PY 现可从系统 site-packages 导入 cv2"
    fi
  fi
done

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
# setsid+nohup+disown: 脚本结束后 chromium 仍常驻(否则任务结束会被一起杀掉)
LAUNCH_ARGS="--remote-debugging-port=9222 --no-sandbox --disable-dev-shm-usage --user-data-dir=/ql/data/chrome_prof --no-first-run --no-default-browser-check --disable-extensions"
if [ -z "$DISPLAY" ] && command -v xvfb-run >/dev/null 2>&1; then
  setsid nohup xvfb-run -a "$CHROME_BIN" $LAUNCH_ARGS >/ql/data/chrome.log 2>&1 &
else
  setsid nohup "$CHROME_BIN" $LAUNCH_ARGS >/ql/data/chrome.log 2>&1 &
fi
disown 2>/dev/null || true

echo "[3/3] 给签到任务加环境变量后生效:"
echo "      CHROME_CDP_URL=http://127.0.0.1:9222"
# 等待 CDP 真正起来再返回, 避免任务结束早于浏览器就绪
for i in $(seq 1 30); do
  VER=$(curl -s http://127.0.0.1:9222/json/version 2>/dev/null | head -c 200)
  if [ -n "$VER" ]; then echo "CDP 已就绪: $VER"; break; fi
  sleep 0.5
done
[ -z "$VER" ] && echo "⚠️ CDP 未就绪, 查看 /ql/data/chrome.log"
