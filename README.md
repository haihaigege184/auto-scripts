# 自动脚本仓库 (auto-scripts)

青龙订阅源。集合各类每日自动化脚本，后续新脚本都放进本仓库，青龙通过订阅链接直接拉取。

> 仓库地址（订阅用）：`https://github.com/haihaigege184/auto-scripts.git`

---

## 目录结构

```
auto-scripts/
├── README.md
├── .gitignore
└── scripts/
    └── 1ms/
        ├── checkin.py   # 毫秒镜像(1ms.run)每日签到 —— 青龙定时任务 (无浏览器依赖)
        └── login.py     # 取/刷新 token —— 青龙定时任务 (需 selenium + 系统 chromium)
```

约定：每个站点一个子目录（`scripts/<站点>/`），脚本读取环境变量、纯 `requests`、用青龙 `notify` 推送。

---

## 毫秒镜像签到（1ms.run）

领每日签到积分 / 流量。签到 3/7/15/30 天分别奖励 1G/3G/10G/30G 流量。

### 架构（自动化，无需手动粘 token）

```
青龙环境变量 MS_PHONE / MS_PASSWORD
        │
        ▼
  login.py  (定时 08:55)  ──登录──▶  写 token 到 /ql/data/1ms_token.txt
        │                                      │
        │                                      ▼
  checkin.py (定时 09:00) ◀──读 token──  1ms.run 每日签到
```

- `login.py` 用账号密码自动登录，把鉴权 token 写入**持久文件**（`/ql/data/1ms_token.txt`，不受仓库重新拉取影响）。
- `checkin.py` 直接读该文件，无需浏览器、无需手动维护 token。
- token 过期后，`login.py` 次日自动重新登录刷新，整条链路自愈。

### 一、青龙订阅拉取

**方式 A：图形界面**
青龙面板 → 定时任务 → 右上角「添加」→ 选择「拉取」：
- 仓库链接：`https://github.com/haihaigege184/auto-scripts.git`
- 分支：`main`
- 定时规则：先随便填（如 `0 9 * * *`），订阅后单独改 `login.py` 任务的时间
- 脚本路径（子目录）：`scripts/1ms`
- 文件后缀：`py`
- 排除规则：留空（让 `checkin.py` 和 `login.py` 都注册成任务）

**方式 B：命令行（ql repo）**
```
ql repo https://github.com/haihaigege184/auto-scripts.git "main" "scripts/1ms" "checkin|login" ""
```
参数含义：`仓库地址 分支 子目录 任务正则(匹配 checkin|login) 排除正则(留空)`。

拉取后，青龙会自动把 `scripts/1ms/checkin.py` 和 `scripts/1ms/login.py` 都注册成定时任务。

### 二、配置环境变量（账号密码）

青龙 → 环境变量 → 新建两条（全局，对所有任务生效）：
- 名称：`MS_PHONE`      值：你的手机号（如 `15927534728`）
- 名称：`MS_PASSWORD`   值：你的密码

> 仓库里**不含任何密码**。`login.py` 的账号密码完全来自青龙环境变量。

（可选）如想把 token 文件放到别处，设 `MS_TOKEN_FILE=/path/to/token.txt`；不填则默认
`/ql/data/1ms_token.txt`（青龙）或同目录 `.token`（本机）。

### 三、调整两个任务的定时

订阅默认两个任务都是同一时间。请改成：
- `login.py` 任务 → `55 8 * * *`（每天 08:55，先刷新 token）
- `checkin.py` 任务 → `0 9 * * *`（每天 09:00，再签到）

青龙「定时任务」列表里点对应任务 → 编辑 → 改「定时规则」即可。

### 四、青龙容器内安装 selenium + 系统 chromium（**必需，一次性**）

`login.py` 必须靠无头浏览器登录：**1ms.run 登录含腾讯验证码，纯标准库无法绕过**（已实测：不带验证码报「请完成验证码验证」，带假 ticket 报「验证失败」）。所以 `login.py` 一定要在青龙里装好 selenium + 系统 chromium，否则每次 token 过期（约 2 天）后签到就会失效。

> 为什么用 selenium 而不是 playwright：qinglong 镜像（whyour/qinglong）是 **Alpine/musl**，而 playwright 只发 manylinux 轮子，在 Alpine 上 `pip install playwright` 会报 `from versions: none`，装不上。selenium 是纯 Python 包，Alpine/Debian 都能装，再配合系统原生 chromium 即可。

```bash
# 1) 进容器 (在宿主机执行; 若你用 NAS/面板自带终端, 直接开终端即可)
docker exec -it qinglong bash

# 2) 容器内安装 (务必装在"运行定时任务的那个 python"里)
#    Alpine 系 (whyour/qinglong 等):
pip3 install selenium -i https://pypi.org/simple
apk add --no-cache chromium chromium-chromedriver
#    Debian/Ubuntu 系:
# pip3 install selenium
# apt-get install -y chromium chromium-driver

# 3) 验证安装成功 (无报错即 OK)
python3 -c "from selenium import webdriver; print('selenium OK')"
which chromium chromedriver
```

常见坑：
- **装完仍 ImportError**：确认 `pip3` 与运行任务的 python 是同一个。青龙定时任务用的是容器内 python3，所以要在 `docker exec` 进容器后装，别在宿主机装。
- **找不到 chromium / chromedriver**：脚本会自动探测 `/usr/bin/chromium`、`/usr/bin/chromedriver` 等路径；若装在别处，用环境变量 `MS_CHROMIUM_BIN` / `MS_CHROMEDRIVER_BIN` 指定。
- **磁盘/网络**：`apk add chromium` 会下载约 100MB+，确保容器有网络与空间。

装好后 `login.py` 即可定时运行，token 自动续期。若暂时没装，`login.py` 只会在运行时报 ImportError（脚本已内置友好提示），`checkin.py` 仍可用 `MS_TOKEN` 环境变量兜底运行（见第五节）。

### 五、手动兜底（可选）

若你不想在青龙跑 `login.py`，仍可手动取 token 填进 `MS_TOKEN` 环境变量：
```bash
# 在本机 (需 selenium + chromium)
MS_PHONE=你的手机号 MS_PASSWORD=你的密码 python scripts/1ms/login.py --print-token
# 输出 MS_TOKEN=eyJhbGciOi...  → 复制等号后整段填进青龙环境变量 MS_TOKEN
```
`checkin.py` 在找不到 token 文件时会回退读 `MS_TOKEN` 环境变量。

### 六、token 过期怎么办

正常情况下不用管：`login.py` 每天刷新，token 自动续期。
只有当 `login.py` 连续失败（如密码改了、接口变动）时，`checkin.py` 会推送「登录态失效」提醒，
届时修正 `MS_PHONE`/`MS_PASSWORD` 或重跑 `login.py` 即可。

---

## 新增脚本约定（供后续扩展）

1. 读取配置一律用环境变量（如 `MS_PHONE`/`MS_PASSWORD`），**不要硬编码密码/密钥**。
2. 推送用青龙标准方式：
   ```python
   try: from notify import send
   except Exception:
       try: from sendNotify import send
       except Exception:
           def send(t, c): print(f"[{t}]\n{c}")
   ```
3. 定时任务脚本（如 `checkin.py`）纯 `requests`，无浏览器依赖。
4. 需要登录取 token 的，单独放 `login.py`/`fetch_xxx.py`，用 selenium + 系统 chromium，并**把 token 写文件**供签到脚本读取。
5. 放到 `scripts/<站点>/` 下，青龙订阅时按子目录拉取。

---

## 本地开发 / 调试

本机直接跑（跳过青龙）：
```bash
# 取 token 并写入同目录 .token
MS_PHONE=159xxxx MS_PASSWORD=xxx python scripts/1ms/login.py

# 用 .token 签到 (已签则跳过)
python scripts/1ms/checkin.py

# 手动指定 token 文件位置
MS_TOKEN_FILE=/tmp/t.txt MS_PHONE=... MS_PASSWORD=... python scripts/1ms/login.py
MS_TOKEN_FILE=/tmp/t.txt python scripts/1ms/checkin.py
```
