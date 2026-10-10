# EasyFrp 签到迁移 —— 工程状态与结论

## ✅ 最终状态：**已跑通**（2026-10-10 19:02 端到端验证）

| 环节 | 结果 |
|---|---|
| 登录（新平台） | ✅ 人工过码 `1966` → 登录成功，token 已缓存 |
| 每日签到 | ✅ 人工过码 `4352` → 签到成功，+5 积分（signScore 0→5） |
| token 缓存 | ✅ 384 字符，`/ql/data/ef_token.txt`，**7 天有效** |
| 缓存命中 | ✅ 后续运行跳过登录验证码，每日只需过一次码（签到） |
| 已签到识别 | ✅ 靠 signScore 变化判断，返回「今日已签到」不误报 |
| 青龙任务 | ✅ `659 EasyFrp签到-新平台`，每日 `25 8* * *` 已启用 |

## 人工过码闭环（QQ 收发）—— 已上线

用户只需在 QQ 私聊里**直接回复 4 位数字**，全程无需守在电脑前。

### 实现（SEA2 `ql-notify` 插件 3.2.0）

```
青龙脚本                SEA2 插件 (13002)              QQ
  │ POST /webhook/captcha        │                        │
  │ {imageBase64, task} ───────> │ adapter.send(admin,    │
  │                              │ b64, {type:'image'}) ─>│ 图片送达
  │<─ {challengeId} ─────────────│                        │
  │                              │   用户回复 4 位数字 ───>│
  │                              │<─ onMessage 匹配       │
  │ GET /webhook/captcha/result  │                        │
  │─────────────────────────────> │ (轮询取答案)           │
```

- **协议**：`POST /webhook/captcha` → `{ok, challengeId}`；`GET /webhook/captcha/result?id=` → `{status, answer}`
- **安全**：仅私聊 + 仅 challenge 指定管理员（superAdmin）+ 4位数字正则 + 随机 id + 10 分钟 TTL
- **诊断**：`GET /webhook/captcha/pending` 查看未过期 challenge 概况
- 关键 API：`adapter.send(uin, base64, {type:'image'})` 发图片；`onMessage` 里 `context.userId` / `context.groupId` / `context.reply`
- SEA2 由 pm2 管理，改插件后 `pm2 restart sea2-bot`；重启后需等 NapCat 重新连入（约 1-2 分钟）才能发图

### 验证码模式（`EF_CAPTCHA_MODE`）

| 值 | 行为 |
|---|---|
| `auto`（默认） | 先 OCR 试 `EF_OCR_TRY` 轮（默认 2），失败转人工（最多 `EF_HUMAN_TRY` 轮，默认 3） |
| `manual` | 直接人工过码 |
| `ocr` | 只用 OCR |

其他参数：`EF_CAPTCHA_WAIT`（等待秒数，默认 300）、`EF_HUMAN_TRY`、`EF_OCR_TRY`、`EF_ADMIN`、`EF_CAPTCHA_WEBHOOK`（默认 `http://172.17.0.1:13002`）

## 接口对照

| 项 | 老平台 GOSTC | 新平台 EasyFrp |
|---|---|---|
| 域名 | `gost.sian.one` | `easyfrp.sian.one` |
| API 前缀 | 无 | **`/api`** |
| 登录 | `POST /v1/auth/login` | `POST /v1/auth/login` + **图形验证码** |
| 用户信息 | `POST /v1/auth/userInfo` | `POST /v1/auth/userinfo` |
| 签到 | `POST /v1/auth/checkin` | `POST /v1/auth/sign` **body 需 captchaKey/captchaValue** |
| 签到日历 | — | `POST /v1/normal/system/user/sign/summary`（实测返回 EOF，暂不可用） |
| 鉴权头 | `Token: <jwt>` | `Token: <jwt>`（一致） |
| 返回码 | `code: 0` 成功 | `code: 0` 成功 / `1` 业务错（如"验证码错误"） |

## 验证码实测特性

- 图片：`POST /api/v1/auth/captcha` → `data.key` + `data.bs64`（120x40 JPEG，4 位纯数字）
- `captchaKey` **一次性且可重复提交**（同一 key 连续试 4 个值都返回"验证码错误"，key 未失效）
- 登录校验顺序：**先验验证码，再验密码**（错误密码也返回"验证码错误"）
- ⚠️ **签到接口无法区分"验证码错"和"今日已签"** —— 两者都返回"验证码错误"，
  所以脚本改为比对签到前后的 `signScore` 来判定是否已签（实测可靠）
- 干扰线：多条粗彩线直接压在字符上，密度很高

## OCR 模块（保留为备用自动化手段）

| 文件 | 作用 |
|---|---|
| `ef_captcha_ocr.py` | 纯 cv2 验证码 OCR（暗度分割 + DP 切分 + 模板匹配），零 ML 依赖 |
| `build_ef_templates.py` | 模板库训练脚本（72 张人工标注样本 → `templates_ef.npz`） |
| `templates_ef.npz` | 字模模板库 |
| `ef_samples/` | 72 张标注样本（后续 ML 方案可直接复用） |

**实测准确率**：训练集 100% / **线上真实难度 0%** —— 干扰线与字符在像素层面交织，
传统 CV 路线天花板低。详见下方「走过的弯路」。

### 保留价值

掩膜提取 + DP 切分骨架是可用的（切分整齐率 20/20），差的是"从残缺字模恢复字符"
—— 这正是 ML（CNN/CRNN）的用武之地。72 张标注样本已入库，可直接作训练集。

### 走过的弯路（避免重复踩坑）

| 尝试 | 结果 | 教训 |
|---|---|---|
| 连通域过滤切分 | 8/12 张切错 | 字符被线切断/粘连时，连通域几何特征不可靠 |
| 竖直投影直接 DP | 退化成均分 | 前缀和使切分点边际收益恒为 0，必须加宽度惩罚 + 低墨奖励 |
| 滑窗模板匹配 + 全局指派 | 训练集 0/12 | 掩膜尺度/极性与模板不匹配，且指派逻辑掩盖了切分问题 |
| 结构特征加权（NCC+密度+连通域） | 0/20 | 手工权重把纯 NCC 信号拉偏，不如纯形状匹配 |
| 形态学细线抑制 | 字符被一起抹掉 | "实心块判定"失效；细线穿过字符时与字符同连通域 |
| 服务端 oracle 换图重试 | 25 轮 0 通过 | 前提是单次准确率够高，0% 时重试无意义 |
| ddddocr | 容器内不可用 | 依赖 `onnxruntime`，Alpine musl 无 wheel |

**根本教训**：这类验证码的干扰线与字符**在像素层面交织**，传统 CV 路线（分割→分类）
天花板低。要么用**能整体看图的模型**，要么用**人工**（当前方案）。

## 部署要点（踩过的坑）

- 容器是 Alpine musl：`onnxruntime` / `opencv-python-headless` 都无 wheel
- `/usr/bin/python3`（py3.12）pip 坏了（pyexpat 符号冲突）；**`/usr/local/bin/python3`（py3.11）才可用**；cv2 只在 py3.12 那个里
- 传文件到容器：`docker cp` 不认 Windows 路径 → **SFTP 传到宿主机 `/tmp`**（纯 ASCII 文件名）→ `docker cp` 进容器
- 中文文件名 + `echo base64 >` 会写失败；SFTP 最稳
- 大 payload（>~500KB）会断连，别用 base64 分块
- 青龙 `/api/crons/disable|enable` 的 body 必须是**数字数组** `{"ids":[659]}`，字符串数组 400

## 后续可选

- `EF_CAPTCHA_MODE` 默认 `auto`，每天仍会先试 2 轮 OCR（浪费约 5 秒）。
  若确认 OCR 无用，可设 `manual` 省掉。
- 彻底无人值守需走 ML：72 张标注已在 `ef_samples/`，建议扩到 300+ 训 CRNN；
  **注意 onnxruntime 在容器内装不上，需在宿主机跑**。
- 老平台任务 `644 GOSTC签到.py` 保持启用作为保底，等新平台稳定几天后再停。
