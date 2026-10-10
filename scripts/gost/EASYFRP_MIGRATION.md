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

## OCR 模块评估（2026-10-10 21:40 修正结论）

### 方案 B 实测：宿主机 ddddocr —— **不达标，已停用**

容器装不上 onnxruntime（Alpine musl 无 wheel），改在宿主机（Debian 12 + py3.11）装：
`ddddocr 1.6.1` + `onnxruntime 1.31.0` 成功，单次推理约 105ms。

72 张已标注样本上准确率仅 **29%**（21/72），比自研 cv2 模块还差。

### 关键发现：**评估基准本身不可靠**

放大 6 倍逐张核对后确认，**双方都有错，但错法不同**：

| 样本 | 我标注 | ddddocr | 真相（6倍放大确认） |
|---|---|---|---|
| cap05 | 2314 ❌ | 2668 | **2668**（ddddocr 对） |
| cap06 | 2668 ❌ | 8810 | **8810**（ddddocr 对） |
| cap09 | 0638 ❌ | 6593 | **6593**（ddddocr 对） |
| cap73 | 3513 | `3+513` | **3513**（我 对，ddddocr 把干扰线当字符） |
| cap38 | 3526 | `1=04` | **3526**（我 对） |
| cap30 | 3580 | `200` | **3580**（我 对，ddddocr 丢字符） |

- **早期标注（cap00-11 那批）确有错** —— 当时批量目视，在密集干扰线下把细笔画字符（`1`/`7`/`4`）看错。
- **ddddocr 的失败模式更系统化**：输出长度 ≠ 4（`231`/`200`/`144`）、含非数字字符（`o638`/`3+513`/`1=04`），
  这一点本身就说明它在这批高密度干扰线上失效了。

### 结论：两条自动化路线都不成立

| 方案 | 训练集 | 真实难度 | 结论 |
|---|---|---|---|
| 自研 cv2 模板匹配 | 100% | **0%** | 过拟合（且训练标注部分有错） |
| 宿主机 ddddocr | — | **29%** | 干扰线密度超出其训练分布 |

**根本障碍**：EasyFrp 的干扰线（多条粗彩线直接压在字符上）密度远超公开验证码数据集，
现有 CV/OCR 方案都在这个分布外。

**因此维持人工过码方案**（已上线并验证可靠，见上文）。自动化需要自建数据集 + 专门训练，
而 `ef_samples/` 现有标注质量不足以支撑 —— 若要继续 ML 路线，**必须先重标样本**。

### 保留资产
- `ef_ocr_service.py` / `ef_ocr_deploy.sh`：宿主机 OCR 服务（已验证可跑通，
  装 ddddocr + onnxruntime 无障碍；如将来拿到高质量标注集可立即复用）
- `ef_samples/` 72 张图（**标注需重新校验**）
- `ef_captcha_ocr.py`：掩膜提取 + DP 切分骨架可用（切分整齐率 20/20），
  差的是"从残缺字模恢复字符"

## 走过的弯路（避免重复踩坑）

| 尝试 | 结果 | 教训 |
|---|---|---|
| 连通域过滤切分 | 8/12 张切错 | 字符被线切断/粘连时，连通域几何特征不可靠 |
| 竖直投影直接 DP | 退化成均分 | 前缀和使切分点边际收益恒为 0，必须加宽度惩罚 + 低墨奖励 |
| 滑窗模板匹配 + 全局指派 | 训练集 0/12 | 掩膜尺度/极性与模板不匹配，且指派逻辑掩盖了切分问题 |
| 结构特征加权（NCC+密度+连通域） | 0/20 | 手工权重把纯 NCC 信号拉偏，不如纯形状匹配 |
| 形态学细线抑制 | 字符被一起抹掉 | "实心块判定"失效；细线穿过字符时与字符同连通域 |
| 服务端 oracle 换图重试 | 25 轮 0 通过 | 前提是单次准确率够高，0% 时重试无意义 |
| 宿主机 ddddocr | 29% | 装得上（Debian 有 wheel）但准确率不达标，已停用 |
| 11 变体投票 | 28%（无提升） | ddddocr 是**稳定地错**而非随机抖动，投票无用 |
| 用 ddddocr 反向"修正"标注 | 71% 不一致 | 不可行：它本身错误率更高，混用会污染标注 |

**根本教训**：这类验证码的干扰线与字符**在像素层面交织**，传统 CV 路线（分割→分类）
天花板低。要么用**能整体看图的大模型**（远超手搓 CRNN 的投入），要么用**人工**（当前方案）。

⚠️ **另一条教训**：批量目视标注在密集干扰线下不可靠 —— 我早期那批标注错了约 30%，
导致"训练集 100%"成为自欺欺人的假象。**任何 OCR 评估都必须先验证标注质量**。


## 部署要点（踩过的坑）

- 容器是 Alpine musl：`onnxruntime` / `opencv-python-headless` 都无 wheel
- `/usr/bin/python3`（py3.12）pip 坏了（pyexpat 符号冲突）；**`/usr/local/bin/python3`（py3.11）才可用**；cv2 只在 py3.12 那个里
- 传文件到容器：`docker cp` 不认 Windows 路径 → **SFTP 传到宿主机 `/tmp`**（纯 ASCII 文件名）→ `docker cp` 进容器
- 中文文件名 + `echo base64 >` 会写失败；SFTP 最稳
- 大 payload（>~500KB）会断连，别用 base64 分块
- 青龙 `/api/crons/disable|enable` 的 body 必须是**数字数组** `{"ids":[659]}`，字符串数组 400

## 后续可选

- `EF_CAPTCHA_MODE` 默认 `auto`，每天仍会先试 2 轮 OCR（实测线上准确率 0%，纯属浪费约 5 秒）。
  **建议改成 `manual`**，直接推图给人工。
- 若还想走自动化，**前置条件是先重标 `ef_samples/`**（现有标注约 30% 有误），
  再考虑多模态大模型读图（视觉模型对"线压在字上"的鲁棒性远好于专用 OCR，
  成本也远低于自训 CRNN）。ddddocr 不要再试。
- 老平台任务 `644 GOSTC签到.py` 保持启用作为保底，等新平台稳定几天后再停。
