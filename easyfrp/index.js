/**
 * SEA2 插件：easyfrp —— EasyFrp 签到验证码「人工过码」通道
 *
 * 职责边界（单一）：
 *   只做 EasyFrp 签到相关的事——验证码挑战-应答 + 主动触发签到任务。
 *   凡是「青龙任务结果通知 / 群广播」一律不归本插件，走 ql-notify。
 *
 * 缘起（2026-10-11 拆分）：
 *   这些功能原先寄生在 ql-notify 里（该插件建站之初只做通知转发），
 *   2026-10-10 陆续加了验证码挑战-应答与 QQ 命令触发，导致"青龙通知"插件
 *   同时干着过码的活，名实不符、职责混淆。故按用户要求拆出本插件，
 *   ql-notify 回归纯通知。拆分为纯搬迁，行为逐行保持不变。
 *
 * 对外接口（默认监听 0.0.0.0:13004，青龙容器经 docker0 网关 172.17.0.1 访问）：
 *   POST /webhook/captcha           {imageBase64, task, admin?, timeoutSec?}
 *       → 把验证码图片【私发】给管理员，并登记一个待确认 challenge；
 *       → 立即返回 {ok, challengeId}（不阻塞等待）。
 *   GET  /webhook/captcha/result?id=<challengeId> | ?task=<task>
 *       → {ok, status: pending|answered, answer, challengeId}  青龙轮询取答案。
 *   GET  /webhook/captcha/pending   → 当前未过期 challenge 概况（诊断用）
 *   GET  /health 或 /               → {ok, plugin:'easyfrp', admin, pending, ttlSec}
 *
 * QQ 命令（私聊，仅 superAdmin 有效）：
 *   「过码 / 过验证码 / 补签 / 签到 / 签到一下 / gostc / easyfrp」
 *   → 立即触发青龙签到任务（验证码图片随后照常推到 QQ，等管理员回复）。
 *   解决"定时任务推送验证码时人不在、回执过期"的问题：想签时随时手动打一发。
 *
 * 答复方式：
 *   管理员在 QQ 私聊直接回复 4 位数字（不必带指令）→ onMessage 按
 *   "最近一个未确认的 challenge" 匹配并落库答案，回复「已收到」确认。
 *   设计成"挑战-应答 + 轮询"而非同步阻塞：HTTP 长连接跨容器不可靠，
 *   轮询模型与青龙 task 的执行模型天然契合，且超时可控。
 *
 * 安全铁律：
 *   - 应答仅接受 superAdmin（或 payload 指定 admin）的【私聊】文本；
 *     群里任何人回复都不会被采纳。
 *   - challenge 带随机 id + 10 分钟 TTL，过期自动清理，不会被无关消息误配。
 *   - ⚠️ 命令判定必须在"4 位数字=验证码答案"之前，否则命令里带的数字会被误当成答案。
 *   - 群发/通知能力本插件一概不提供（那是 ql-notify 的活）。
 *
 * 触发青龙任务的方式：
 *   走青龙自身 API（PUT /api/crons/run），token 从容器 auth.json 读
 *   （/root/ql1 已 bind mount 到容器 /ql/data，宿主机可直接读）。
 *   不另建执行通道，避免两套调度打架。
 *   ⚠️ 格式坑(实测)：/api/crons/run 的 body 是【裸数组】[659]；
 *      {"ids":[659]} 会 400("value must be an array")。
 *      而 /api/crons/disable|enable 才收 {"ids":[...]}。两者不一样，别混用。
 *
 * 约定（与框架一致）：
 *   - 入口 plugins/easyfrp/index.js，导出 class EasyFrpPlugin extends BasePlugin
 *   - plugin-manager 自动扫描 plugins/ 子目录并 new EasyFrpPlugin(name, config)
 *   - onEnable(db) 钩子用来启动 HTTP 服务，onDisable(db) 用来关闭
 *
 * 环境变量 / 可配置：
 *   - EASYFRP_PORT     (默认 13004)  监听端口
 *   - EASYFRP_HOST     (默认 0.0.0.0) 监听地址（青龙容器经 docker0 网关 172.17.0.1 访问）
 *   - EASYFRP_ADMIN    (可选) 管理员 QQ，未设则取 config.superAdmin
 *   - EF_SIGN_TASK_ID  (默认 659) 要触发的青龙签到任务 ID
 *   - QL_API_BASE      (默认 http://127.0.0.1:5701)
 *   - QL_AUTH_JSON     (默认 /root/ql1/config/auth.json)
 *
 * 健康检查：
 *   curl -s http://127.0.0.1:13004/health
 */

'use strict';

const BasePlugin = require('../base-plugin');
const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');

const CHALLENGE_TTL_MS = 10 * 60 * 1000;   // challenge 最长存活 10 分钟
const CAPTCHA_HINT = '请直接回复这 4 位数字（私聊即可，不必加指令）';

class EasyFrpPlugin extends BasePlugin {
    constructor(name, config) {
        super(name, config);
        this.priority = 50;
        // 本插件不拦截/阻断任何消息：onMessage 只做识别，恒返回 false
        this.permissionLevel = 0;

        this.superAdmin = String(config.superAdmin || process.env.EASYFRP_ADMIN || '');

        // 监听所有接口, 以便青龙容器 (网关 172.17.0.1) 能访问本机 webhook
        this.host = process.env.EASYFRP_HOST || '0.0.0.0';
        this.port = parseInt(process.env.EASYFRP_PORT || '13004', 10);

        this._server = null;
        // 待确认验证码挑战: Map<challengeId, {id, task, admin, createdAt, answer}>
        this._challenges = new Map();
        this._log('init superAdmin=%s port=%s signTask=%s',
            this.superAdmin || '(none)', this.port, process.env.EF_SIGN_TASK_ID || '659');
    }

    _log(fmt, ...args) {
        // %s 与 %d 都按顺序消费 args（否则写了 %d 会原样输出并导致后续 %s 错位）
        const msg = typeof fmt === 'string' && args.length
            ? fmt.replace(/%[sd]/g, () => String(args.shift()))
            : fmt;
        console.log('[easyfrp] ' + msg);
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

    // ==================== 验证码人工过码 ====================

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
            res.setHeader('Content-Type', 'application/json; charset=utf-8');
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
            // 不带 id 时取该 task 最新的已答复 challenge（幂等重试友好）
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
            // ⚠️ /api/crons/run 要【裸数组】 [659]（见文件头说明）
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
        // 返回 false：答案已记录，但不必阻断后续插件（与拆分前行为一致）
        return false;
    }

    async onEnable(/* db */) {
        if (this._server) return;
        this._server = http.createServer((req, res) => {
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
                    ok: true, plugin: 'easyfrp',
                    admin: this.superAdmin,
                    signTaskId: process.env.EF_SIGN_TASK_ID || '659',
                    pending: this._challenges ? this._challenges.size : 0,
                    ttlSec: CHALLENGE_TTL_MS / 1000,
                }));
                return;
            }
            res.statusCode = 404;
            res.end(JSON.stringify({ ok: false, error: 'not found' }));
        });
        this._server.on('error', (e) => this._log('server error: %s', e.message));
        this._server.listen(this.port, this.host, () => {
            this._log('HTTP server listening on %s:%s (captcha 人工过码通道)', this.host, this.port);
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

module.exports = EasyFrpPlugin;
