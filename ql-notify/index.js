/**
 * SEA2 插件：ql-notify —— 接收青龙任务消息并【私发管理员】(+可选发群)
 *
 * 设计（2026-08-22 改版，按用户要求）：
 *   - 默认【不】推送到任何群，也不推送给所有群。
 *   - 所有青龙任务消息（1ms / gost 签到等）一律【私聊】发送给超级管理员
 *     （config.superAdmin），与框架「重启成功通知」走同一私发通道。
 *   - 私发通道：global.sea1.adapter.send(uin, text) （不传 groupId 即私聊），
 *     与 adapters/qq/index.js 的 sendRebootNotification 完全一致，零改动框架。
 *
 * 3.2.0 扩展（2026-09-07，宝宝在哪隧道告警需求）：
 *   - webhook payload 支持可选 "group": "<QQ群号>" 字段——带该字段时，
 *     消息同时发送到指定群（adapter.send('', text, {groupId})）与私发管理员。
 *   - 不带 group 字段 → 行为与旧版完全一致（只私发管理员）。
 *   - 用途：静静追踪 8012 服务端的「隧道可用/延迟过高/设备离线」告警同时推群+管理员。
 *
 * 3.2.0 扩展（2026-10-10，EasyFrp 签到验证码人工过码）：
 *   - POST /webhook/captcha  {imageBase64, task, admin?, timeoutSec?}
 *       → 把验证码图片【私发】给管理员，并登记一个待确认 challenge；
 *       → 立即返回 {ok, challengeId}（不阻塞等待）。
 *   - 管理员在 QQ 里直接回复 4 位数字（私聊即可，不必带指令）→ 插件 onMessage 拦截，
 *       按"最近一个未确认的 challenge"匹配并落库答案，回复「已收到」确认。
 *   - 青龙脚本随后 GET /webhook/captcha/result?task=<task> 轮询取答案。
 *   - 设计成"挑战-应答"而非同步阻塞：HTTP 长连接跨容器不可靠，
 *       轮询模型与青龙 task 的执行模型天然契合，且超时可控。
 *   - 安全性：只有 superAdmin / payload.admin 的消息才会被当作答案；
 *     challenge 带随机 id + 过期时间，过期即失效，不会被无关消息误配。
 *
 * 3.2.1 扩展（2026-10-10，主动发起人工过码）：
 *   - 管理员在 QQ 私聊发「过码 / 签到 / 补签 / gostc / easyfrp」→ 立即触发青龙签到任务，
 *     验证码图片随后照常推送到 QQ，等管理员回复。
 *   - 解决"定时任务推送验证码时人不在，回执过期"的问题：想签的时候随时手动打一发。
 *   - 实现走青龙自身 API（PUT /api/crons/run），token 从容器 auth.json 读
 *     （/root/ql1 已 bind mount 到容器 /ql/data）。不另建执行通道，避免两套调度打架。
 *   - ⚠️ 格式坑(实测): /api/crons/run 的 body 是【裸数组】 [659]；
 *     {"ids":[659]} 会 400("value must be an array")。
 *     而 /api/crons/disable|enable 才收 {"ids":[...]}。两者不一样，别混用。
 *   - 环境变量 EF_SIGN_TASK_ID（默认 659）、QL_API_BASE（默认 http://127.0.0.1:5701）、
 *     QL_AUTH_JSON（默认 /root/ql1/config/auth.json）
 *   - ⚠️ 命令判定必须在"4位数字=验证码答案"之前，否则命令里带的数字会被误当成答案。
 *
 * 3.1.15 扩展（2026-09-15，宝宝在哪控制台加「推送管理员 QQ」）：
 *   - webhook payload 支持可选 "admin": "<QQ号>" 字段——带该字段时本次私发目标改为它，
 *     覆盖 config.superAdmin 默认值（控制台改完即时生效，不必去改框架配置）。
 *   - 只接受纯数字（5~12 位），非法值忽略并回落 superAdmin。
 *   - 不带 admin 字段 → 行为与旧版完全一致（青龙签到等调用零影响）。
 *
 * 安全铁律：
 *   - 群发仅在 webhook 调用方【显式】携带 group 字段时触发；青龙签到等旧调用不传 group，
 *     行为不变（仍只私发管理员）。绝不自动推所有群，绝不改动其它插件/框架代码。
 *   - 仅读取 config.superAdmin（已存在于框架 config），不写配置、不改群白名单。
 *   - 验证码应答仅接受 superAdmin（或 payload 指定 admin）的私聊文本；
 *     群里任何人回复都不会被采纳。challenge 随机 id + TTL，过期自动清理。
 *
 * 工作机制：
 *   - 本插件在 onEnable 时启动一个本地 HTTP 服务（默认 0.0.0.0:13002）。
 *   - 青龙侧 checkin.py 在成功或失败时，用纯标准库 urllib
 *     POST 到 http://172.17.0.1:13002/webhook/ql ，body 为 JSON:
 *        { "title": "毫秒镜像签到", "content": "✅ 今日已签到 ...", "level": "ok|warn|error" }
 *   - 本插件收到后，把消息【私聊】发送给超级管理员（带 group 时同时发群）。
 *
 * 约定（与框架一致）：
 *   - 入口 plugins/ql-notify/index.js，导出 class QlNotifyPlugin extends BasePlugin
 *   - plugin-manager 自动扫描 plugins/ 子目录并 new QlNotifyPlugin(name, config)
 *   - onEnable(db) 钩子用来启动 HTTP 服务，onDisable(db) 用来关闭
 *
 * 环境变量 / 可配置：
 *   - QL_NOTIFY_PORT   (默认 13002)  监听端口
 *   - QL_NOTIFY_HOST   (默认 0.0.0.0) 监听地址（青龙容器经 docker0 网关 172.17.0.1 访问）
 */

'use strict';

const BasePlugin = require('../base-plugin');
const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');

const CHALLENGE_TTL_MS = 10 * 60 * 1000;   // challenge 最长存活 10 分钟
const CAPTCHA_HINT = '请直接回复这 4 位数字（私聊即可，不必加指令）';

class QlNotifyPlugin extends BasePlugin {
    constructor(name, config) {
        super(name, config);
        this.priority = 50;
        // 不拦截任何消息，只被动接收 webhook
        this.permissionLevel = 0;

        this.superAdmin = String(config.superAdmin || process.env.QL_NOTIFY_ADMIN || '');

        // 监听所有接口, 以便青龙容器 (网关 172.17.0.1) 能访问本机 webhook
        this.host = process.env.QL_NOTIFY_HOST || '0.0.0.0';
        this.port = parseInt(process.env.QL_NOTIFY_PORT || '13002', 10);

        this.configPath = null; // 本版不再写配置
        this._server = null;
        // 待确认验证码挑战: Map<challengeId, {id, task, admin, createdAt, answer}>
        this._challenges = new Map();
        this._log('init superAdmin=%s port=%s', this.superAdmin || '(none)', this.port);
    }

    _log(fmt, ...args) {
        // 3.1.15 修：原实现只替换 %s，格式串里写了 %d 时该占位符原样输出，
        // 且它不消耗 args → 后面的 %s 会整体错位（实测日志出现 group=39 这种把内容长度
        // 当成群号打印的假信息）。改为 %s 与 %d 都按顺序消费。
        const msg = typeof fmt === 'string' && args.length
            ? fmt.replace(/%[sd]/g, () => String(args.shift()))
            : fmt;
        console.log('[ql-notify] ' + msg);
    }

    /**
     * 私发管理员（与框架重启通知同一通道：global.sea1.adapter.send(uin, text) 不传 groupId=私聊）。
     * 失败仅记录，绝不抛错影响主流程。
     */
    async _sendPrivate(adminUin, text) {
        const adapter = global.sea1 && global.sea1.adapter;
        if (!adapter || typeof adapter.send !== 'function') {
            this._log('私发跳过：适配器未就绪 (global.sea1.adapter 不存在)');
            return false;
        }
        try {
            await adapter.send(String(adminUin), text);
            this._log('私发管理员 %s -> OK', adminUin);
            return true;
        } catch (e) {
            this._log('私发管理员 %s -> FAIL: %s', adminUin, (e && e.message) || e);
            return false;
        }
    }

    /**
     * 发送到群（3.2.0 扩展）：adapter.send('', text, {groupId}) —— 走群消息参数。
     * 失败仅记录，绝不抛错影响主流程。
     */
    async _sendGroup(groupId, text) {
        const adapter = global.sea1 && global.sea1.adapter;
        if (!adapter || typeof adapter.send !== 'function') {
            this._log('群发跳过：适配器未就绪');
            return false;
        }
        try {
            await adapter.send('', text, { groupId: String(groupId) });
            this._log('群发 %s -> OK', groupId);
            return true;
        } catch (e) {
            this._log('群发 %s -> FAIL: %s', groupId, (e && e.message) || e);
            return false;
        }
    }

    // 把一条消息私发管理员（payload 带 group 时同时发群 —— 3.2.0）
    //   3.1.15：payload 可带 admin 覆盖私发目标（宝宝在哪控制台里可配「推送管理员 QQ」）。
    //           ⚠️ 上一版只改了调用方、漏改本函数签名 → admin 实际不生效，本次补上。
    //   3.1.16：时间戳改为 `—— 2026/9/21 【18:25:56】`（用户指定：日期 + 方括号时间）；
    //           content 为空时不再输出空行（围栏告警现在信息全在 title 里）。
    async _broadcast(title, content, level, groupId, adminQq) {
        const emoji = level === 'error' ? '❌' : level === 'warn' ? '⚠️' : '✅';
        // 显式指定 Asia/Shanghai，不依赖进程时区（宿主机 CST、容器却是 UTC，这个坑踩过）
        const _now = new Date();
        const _ymd = _now.toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' });
        const _hms = _now.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
        const _lines = [`${emoji} ${title}`];
        if (String(content || '').trim()) _lines.push(String(content).trim());
        _lines.push(`—— ${_ymd} 【${_hms}】`);
        const text = _lines.join('\n');
        const results = [];
        // 群（可选）：调用方显式带 group 字段才发群，旧调用零影响
        if (groupId) {
            results.push({ group: groupId, ok: await this._sendGroup(groupId, text) });
        }
        // 3.1.15：payload 显式带 admin 时用它做私发目标，否则回落 config.superAdmin
        const target = String(adminQq || this.superAdmin || '').trim();
        if (!target) {
            this._log('warn: 未配置 superAdmin，私发被丢弃: %s', title);
            results.push({ admin: null, ok: false });
            return results;
        }
        results.push({ admin: target, ok: await this._sendPrivate(target, text) });
        return results;
    }

    // ==================== 验证码人工过码（EasyFrp 签到） ====================

    _gcChallenges() {
        const now = Date.now();
        for (const [id, c] of this._challenges) {
            if (now - c.createdAt > CHALLENGE_TTL_MS) this._challenges.delete(id);
        }
    }

    /** 私发图片给管理员。adapter.send(uin, base64, {type:'image'}) */
    async _sendImage(adminUin, base64Raw) {
        const adapter = global.sea1 && global.sea1.adapter;
        if (!adapter || typeof adapter.send !== 'function') {
            this._log('发图跳过：适配器未就绪');
            return false;
        }
        // 允许调用方带 data:image/jpeg;base64, 前缀，这里剥掉
        const b64 = String(base64Raw).replace(/^data:image\/\w+;base64,/, '').trim();
        if (!b64 || b64.length < 32) {
            this._log('发图失败：imageBase64 为空或过短 (len=%d)', b64.length);
            return false;
        }
        try {
            await adapter.send(String(adminUin), b64, { type: 'image' });
            this._log('验证码图片 -> 私发 %s OK (%d 字符)', adminUin, b64.length);
            return true;
        } catch (e) {
            this._log('验证码图片 -> 私发 %s FAIL: %s', adminUin, (e && e.message) || e);
            return false;
        }
    }

    _handleCaptchaPost(req, res) {
        let body = '';
        req.on('data', (c) => (body += c));
        req.on('end', async () => {
            let payload;
            try {
                payload = JSON.parse(body || '{}');
            } catch (e) {
                res.statusCode = 400;
                res.end(JSON.stringify({ ok: false, error: 'invalid json' }));
                return;
            }
            const image = String(payload.imageBase64 || '');
            const task = String(payload.task || 'easyfrp').replace(/[^\w.-]/g, '').slice(0, 40);
            let admin = String(payload.admin || '').trim();
            if (admin && !/^\d{5,12}$/.test(admin)) admin = '';
            const target = admin || String(this.superAdmin || '').trim();
            if (!target) {
                res.statusCode = 400;
                res.end(JSON.stringify({ ok: false, error: 'no admin configured' }));
                return;
            }
            if (!image) {
                res.statusCode = 400;
                res.end(JSON.stringify({ ok: false, error: 'imageBase64 required' }));
                return;
            }

            const okImg = await this._sendImage(target, image);
            // 先发图成功才登记 challenge：图没到用户手上就不该开始计时
            if (!okImg) {
                res.statusCode = 502;
                res.end(JSON.stringify({ ok: false, error: 'send image failed' }));
                return;
            }
            this._gcChallenges();
            const id = crypto.randomBytes(8).toString('hex');
            this._challenges.set(id, {
                id, task, admin: target, createdAt: Date.now(), answer: null
            });
            this._log('challenge 创建 id=%s task=%s admin=%s', id, task, target);

            // 文字提示（图片已单独发出）
            const lines = [
                '🔐 EasyFrp 签到验证码',
                CAPTCHA_HINT,
                `任务: ${task}`,
                '（10 分钟内有效）',
            ];
            await this._sendPrivate(target, lines.join('\n'));

            res.statusCode = 200;
            res.end(JSON.stringify({ ok: true, challengeId: id, admin: target, ttlSec: CHALLENGE_TTL_MS / 1000 }));
        });
    }

    /** 青龙轮询取答案 */
    _handleCaptchaResult(req, res, query) {
        this._gcChallenges();
        const id = String(query.get('id') || '').trim();
        const task = String(query.get('task') || '').trim();
        let found = null;
        if (id && this._challenges.has(id)) {
            found = this._challenges.get(id);
        } else if (task) {
            // 不带 id 时取该 task 最新的未过期 challenge（幂等重试友好）
            for (const c of this._challenges.values()) {
                if (c.task !== task || c.answer === null) continue;
                if (!found || c.createdAt > found.createdAt) found = c;
            }
        }
        if (!found) {
            res.statusCode = 200;
            res.end(JSON.stringify({ ok: true, status: 'pending', answer: null }));
            return;
        }
        res.statusCode = 200;
        res.end(JSON.stringify({
            ok: true,
            status: found.answer === null ? 'pending' : 'answered',
            answer: found.answer,
            challengeId: found.id,
        }));
    }

    // ==================== 主动发起人工过码（QQ 命令） ====================

    /**
     * 触发青龙任务立即执行。
     * 走青龙自身 API（PUT /api/crons/run），token 从容器 auth.json 读
     * （/root/ql1 已 bind mount 到容器 /ql/data，宿主机可直接读）。
     * 这样不需要在插件里另建一套执行通道，也不会有"两套调度打架"的问题。
     */
    async _runCrontask(taskId) {
        const id = parseInt(taskId, 10);
        if (!id) return { ok: false, error: 'bad taskId' };
        const authPath = process.env.QL_AUTH_JSON || '/root/ql1/config/auth.json';
        const base = process.env.QL_API_BASE || 'http://127.0.0.1:5701';
        let token = '';
        try {
            token = JSON.parse(fs.readFileSync(authPath, 'utf8')).token || '';
        } catch (e) {
            return { ok: false, error: `读取 ${authPath} 失败: ${e.message}` };
        }
        if (!token) return { ok: false, error: 'auth.json 里没有 token' };

        // node 18+ 有全局 fetch；老版本退回 http 模块
        const doPut = async (url, bodyStr) => {
            if (typeof fetch === 'function') {
                const r = await fetch(url, {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                        Authorization: 'Bearer ' + token,
                    },
                    body: bodyStr,
                });
                return { status: r.status, text: await r.text() };
            }
            return new Promise((resolve, reject) => {
                const u = new URL(url);
                const req = http.request({
                    hostname: u.hostname, port: u.port || 80, path: u.pathname,
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                        'Content-Length': Buffer.byteLength(bodyStr),
                        Authorization: 'Bearer ' + token,
                    },
                }, (r2) => {
                    let b = '';
                    r2.on('data', (c) => (b += c));
                    r2.on('end', () => resolve({ status: r2.statusCode, text: b }));
                });
                req.on('error', reject);
                req.write(bodyStr);
                req.end();
            });
        };

        try {
            // ⚠️ 格式坑(实测): /api/crons/run 要【裸数组】 [659]；
            //    {"ids":[659]} 会被判 "value must be an array" 而 400。
            //    注意与 /api/crons/disable|enable 不同 —— 那两个才收 {"ids":[...]}。
            const r = await doPut(base + '/api/crons/run', JSON.stringify([id]));
            let obj = {};
            try { obj = JSON.parse(r.text || '{}'); } catch (e) { /* 非 JSON 也算到达 */ }
            const ok = r.status === 200 && obj.code !== 401;
            this._log('触发青龙任务 %d -> HTTP %d %s', id, r.status, (obj.msg || obj.message || '').toString().slice(0, 80));
            return { ok, status: r.status, msg: obj.msg || obj.message || '' };
        } catch (e) {
            this._log('触发青龙任务 %d 失败: %s', id, (e && e.message) || e);
            return { ok: false, error: String((e && e.message) || e) };
        }
    }

    /** 私聊命令: 过码 / 签到 —— 立即触发一次签到任务(验证码会再推到 QQ) */
    async _handleCommand(context, msg, sender) {
        const t = msg.replace(/^[#！!\/]/, '').trim();
        const cmd = t.toLowerCase();
        // 允许: 过码 / 过验证码 / gostc / easyfrp / 签到 / 补签
        const isSign = /^(过码|过验证码|补签|签到|签到一下)$/.test(t) ||
            /^(gostc|easyfrp)(\s*签到)?$/.test(cmd);
        if (!isSign) return false;

        const taskId = process.env.EF_SIGN_TASK_ID || '659';
        if (typeof context.reply === 'function') {
            await context.reply('⏳ 正在触发 EasyFrp 签到，稍后会推一张验证码图片给你，请回复 4 位数字。');
        }
        const r = await this._runCrontask(taskId);
        if (r.ok) {
            this._log('管理员 %s 主动触发签到任务 %s', sender, taskId);
            if (typeof context.reply === 'function') {
                await context.reply(`✅ 已触发签到任务（#${taskId}）。验证码图片马上到，记得回复 4 位数字。`);
            }
        } else {
            if (typeof context.reply === 'function') {
                await context.reply(`❌ 触发失败：${r.error || r.msg || ('HTTP ' + r.status)}`);
            }
        }
        return false; // 已处理, 但不阻断其它插件
    }

    async onMessage(context) {
        const msg = String((context && context.msg) || '').trim();
        if (!msg) return false;
        // 只在私聊里收集答案（群里任何人都能回验证码 -> 不安全）
        if (context && context.groupId) return false;

        const sender = String((context && context.userId) || '').trim();

        // 0) 优先处理命令（过码/签到）—— 必须在"4位数字"判定之前,
        //    否则命令里带的数字会被误当成验证码答案
        if (this.superAdmin && sender === String(this.superAdmin)) {
            if (await this._handleCommand(context, msg, sender)) return false;
        }

        // 1) 4 位数字 = 验证码回复
        const m = msg.match(/^\s*(\d{4})\s*$/);
        if (!m) return false;

        this._gcChallenges();
        // 只接受该 challenge 指定管理员（即 superAdmin / payload.admin）的回复
        const candidates = [...this._challenges.values()]
            .filter((c) => c.answer === null && c.admin === sender)
            .sort((a, b) => b.createdAt - a.createdAt);
        if (!candidates.length) return false;

        const c = candidates[0];
        c.answer = m[1];
        this._log('challenge %s (task=%s) 收到管理员 %s 的答案 %s',
            c.id, c.task, sender, c.answer);
        try {
            if (typeof context.reply === 'function') {
                await context.reply(`✅ 已收到验证码 ${c.answer}，青龙将继续执行签到。`);
            }
        } catch (e) { /* 回复失败不影响答案落库 */ }
        // 返回 false：答案已记录，但不必阻断后续插件（保持与旧版一致的最小拦截）
        return false;
    }

    _handleWebhook(req, res) {
        let body = '';
        req.on('data', (c) => (body += c));
        req.on('end', async () => {
            res.setHeader('Content-Type', 'application/json; charset=utf-8');
            let payload;
            try {
                payload = JSON.parse(body || '{}');
            } catch (e) {
                res.statusCode = 400;
                res.end(JSON.stringify({ ok: false, error: 'invalid json' }));
                return;
            }
            const title = String(payload.title || '青龙任务通知');
            const content = String(payload.content || '');
            const level = String(payload.level || 'ok');
            const group = payload.group ? String(payload.group).trim() : '';   // 3.2.0：可选群号
            // 3.1.15：可选 admin（管理员 QQ）——只接受纯数字，非法一律忽略并回落 superAdmin，
            //         避免把任意字符串当 uin 发出去。
            let admin = payload.admin ? String(payload.admin).trim() : '';
            if (admin && !/^\d{5,12}$/.test(admin)) {
                this._log('warn: 忽略非法 admin 字段: %s', admin);
                admin = '';
            }
            this._log('webhook recv title=%s level=%s len=%d group=%s admin=%s',
                title, level, content.length, group || '-', admin || '(default)');
            try {
                const r = await this._broadcast(title, content, level, group, admin);
                res.statusCode = 200;
                res.end(JSON.stringify({ ok: true, sent: r, target: group ? ('group:' + group + '+private:' + this.superAdmin) : ('private:' + this.superAdmin) }));
            } catch (e) {
                res.statusCode = 500;
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
        });
    }

    async onEnable(/* db */) {
        if (this._server) return;
        this._server = http.createServer((req, res) => {
            if (req.method === 'POST' && req.url === '/webhook/ql') {
                this._handleWebhook(req, res);
                return;
            }
            if (req.method === 'POST' && req.url === '/webhook/captcha') {
                this._handleCaptchaPost(req, res);
                return;
            }
            if (req.method === 'GET' && req.url.startsWith('/webhook/captcha/result')) {
                const u = new URL(req.url, 'http://127.0.0.1');
                this._handleCaptchaResult(req, res, u.searchParams);
                return;
            }
            if (req.method === 'GET' && req.url === '/webhook/captcha/pending') {
                // 诊断用：当前未过期 challenge 概况
                this._gcChallenges();
                res.setHeader('Content-Type', 'application/json; charset=utf-8');
                res.statusCode = 200;
                res.end(JSON.stringify({
                    ok: true,
                    count: this._challenges.size,
                    items: [...this._challenges.values()].map((c) => ({
                        id: c.id, task: c.task, admin: c.admin,
                        answered: c.answer !== null,
                        ageSec: Math.round((Date.now() - c.createdAt) / 1000),
                    })),
                }));
                return;
            }
            if (req.method === 'GET' && (req.url === '/' || req.url === '/health')) {
                res.setHeader('Content-Type', 'application/json; charset=utf-8');
                res.statusCode = 200;
                res.end(JSON.stringify({
                    ok: true, plugin: 'ql-notify',
                    mode: 'private-admin-only',
                    admin: this.superAdmin,
                    captcha: {
                        enabled: true,
                        pending: this._challenges ? this._challenges.size : 0,
                        ttlSec: CHALLENGE_TTL_MS / 1000,
                    },
                }));
                return;
            }
            res.statusCode = 404;
            res.end(JSON.stringify({ ok: false, error: 'not found' }));
        });
        this._server.on('error', (e) => this._log('server error: %s', e.message));
        this._server.listen(this.port, this.host, () => {
            this._log('HTTP server listening on %s:%s (mode=private-admin-only)', this.host, this.port);
            this._log('target admin: %s', this.superAdmin || '(none)');
        });
    }

    async onDisable(/* db */) {
        if (this._server) {
            try { this._server.close(); } catch (e) {}
            this._server = null;
            this._log('HTTP server closed');
        }
    }
}

module.exports = QlNotifyPlugin;
