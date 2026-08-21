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
        ├── checkin.py   # 毫秒镜像(1ms.run)每日签到 —— 青龙定时任务
        └── login.py     # 取 token 工具 —— 本机运行，非青龙任务
```

约定：每个站点一个子目录（`scripts/<站点>/`），脚本读取环境变量、纯 `requests`、用青龙 `notify` 推送。

---

## 毫秒镜像签到（1ms.run）

领每日签到积分 / 流量。签到 3/7/15/30 天分别奖励 1G/3G/10G/30G 流量。

### 一、青龙订阅拉取

**方式 A：图形界面**
青龙面板 → 定时任务 → 右上角「添加」→ 选择「拉取」：
- 仓库链接：`https://github.com/haihaigege184/auto-scripts.git`
- 分支：`main`
- 定时规则：`0 9 * * *`（每天 09:00）
- 脚本路径（子目录）：`scripts/1ms`
- 文件后缀：`py`
- 排除规则：`login.py`（仅本机取 token 用，不注册为定时任务）

**方式 B：命令行（ql repo）**
```
ql repo https://github.com/haihaigege184/auto-scripts.git "main" "scripts/1ms" "checkin" "login"
```
参数含义：`仓库地址 分支 子目录 任务正则(匹配checkin) 排除正则(排除login)`。

拉取后，青龙会自动把 `scripts/1ms/checkin.py` 注册成定时任务（按上面的定时规则运行）。

### 二、Token 变量配置（MS_TOKEN）

`checkin.py` 通过环境变量 `MS_TOKEN` 鉴权（接口要求 `Authorization: Bearer <auth_token>`）。

**获取 MS_TOKEN（在本机，需 Python + playwright + chromium）：**
```bash
set MS_PHONE=你的手机号
set MS_PASSWORD=你的密码
python scripts/1ms/login.py --print-token
# 输出： MS_TOKEN=eyJhbGciOi...   复制等号后面整段
```
青龙 → 环境变量 → 新建：
- 名称：`MS_TOKEN`
- 值：上面复制的 `eyJhbGciOi...`

> 仓库里**不含任何密码**，`login.py` 的账号密码从环境变量读取。

### 三、token 过期怎么办

`MS_TOKEN` 会过期（具体时长不定，可能数周）。脚本检测到 401 会自动**推送提醒**。
届时在本机重新运行：
```bash
python scripts/1ms/login.py --print-token
```
把新值粘进青龙的 `MS_TOKEN` 环境变量即可。

### 四、新增脚本约定（供后续扩展）

1. 读取配置一律用环境变量（如 `MS_TOKEN`），**不要硬编码密码/密钥**。
2. 推送用青龙标准方式：
   ```python
   try: from notify import send
   except Exception:
       try: from sendNotify import send
       except Exception:
           def send(t, c): print(f"[{t}]\n{c}")
   ```
3. 纯 `requests`，无浏览器依赖（青龙侧不需要 playwright）。
4. 放到 `scripts/<站点>/` 下，青龙订阅时按子目录拉取。
5. 仅本机运行的工具（如取 token）单独放，并在拉取时加入「排除规则」避免被注册成定时任务。

---

## 本地开发 / 调试

本机直接跑签到（跳过青龙）：
```bash
MS_TOKEN=xxxx python scripts/1ms/checkin.py
MS_TOKEN=xxxx python scripts/1ms/checkin.py   # 查状态模式内建（已签则跳过）
```

本机取 token：
```bash
python scripts/1ms/login.py --print-token
```
