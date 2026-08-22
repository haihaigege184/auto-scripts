/**
 * SEA2 插件：ql-notify —— 接收青龙任务消息并转发到「白名单 + 管理员群」
 *
 * 设计（2026-08-22 改版，按用户要求）：
 *   - 默认【不】推送给所有 notify_groups，只推送给「管理员群」(QL_NOTIFY_ADMIN_GROUPS)。
 *   - 普通群需在群内发送命令「开启青龙推送」把自己加入推送白名单，
 *     发送「关闭青龙推送」把自己从白名单移除。
 *   - 白名单持久化到 config.json 的 ql_notify_whitelist（数组，群号字符串）。
 *   - 命令仅限管理员/开发者（permissionLevel 2）执行。
 *
 * 工作机制：
 *   - 本插件在 onEnable 时启动一个本地 HTTP 服务（默认 0.0.0.0:13002）。
 *   - 青龙侧 checkin.py / login.py 在成功或失败时，用纯标准库 urllib
 *     POST 到 http://172.17.0.1:13002/webhook/ql ，body 为 JSON:
 *        { "title": "毫秒镜像签到", "content": "✅ 今日已签到 ...", "level": "ok|warn|error" }
 *   - 本插件收到后，把消息推送到：管理员群 ∪ 白名单群（去重）。
 *
 * 约定（与框架一致）：
 *   - 入口 plugins/ql-notify/index.js，导出 class QlNotifyPlugin extends BasePlugin
 *   - plugin-manager 自动扫描 plugins/ 子目录并 new QlNotifyPlugin(name, config)
 *   - onEnable(db) 钩子用来启动 HTTP 服务，onDisable(db) 用来关闭
 *   - onMessage(context) 处理群命令；context 含 msg/userId/groupId/reply/userPermission
 *
 * 环境变量 / 可配置：
 *   - QL_NOTIFY_PORT   (默认 13002)  监听端口
 *   - QL_NOTIFY_HOST   (默认 0.0.0.0) 监听地址（青龙容器经 docker0 网关 172.17.0.1 访问）
 *   - QL_NOTIFY_ADMIN_GROUPS (默认空) 逗号分隔的群号，作为默认推送目标（管理员群）
 *       若未设置，则退化为 config.notify_groups 的第一个群作为唯一直推管理员群。
 *   - 白名单群持久化于 config.json 的 ql_notify_whitelist。
 */

'use strict';

const BasePlugin = require('../base-plugin');
const http = require('node:http');
const fs = require('fs');
const path = require('path');

class QlNotifyPlugin extends BasePlugin {
    constructor(name, config) {
        super(name, config);
        this.priority = 50;
        // 命令需管理员权限；onMessage 里再二次校验，此处设 0 让分发能进入本插件
        this.permissionLevel = 0;

        this.napcatHost = config.napcat_host || '127.0.0.1';
        this.napcatPort = config.napcat_port || 4000;
        this.napcatToken = config.napcat_token || '';
        this.superAdmin = String(config.superAdmin || '');
        this.developer = String(config.developer || '');

        // 监听所有接口, 以便青龙容器 (网关 172.17.0.1) 能访问本机 webhook
        this.host = process.env.QL_NOTIFY_HOST || '0.0.0.0';
        this.port = parseInt(process.env.QL_NOTIFY_PORT || '13002', 10);

        // 管理员群：环境变量优先，否则取 notify_groups 第一个作为兜底管理员群
        const envAdmin = (process.env.QL_NOTIFY_ADMIN_GROUPS || '').split(',').map(s => s.trim()).filter(Boolean);
        const cfgAdmin = (config.ql_notify_admin_groups || []).map(x => String(x));
        this.adminGroups = (envAdmin.length ? envAdmin : (cfgAdmin.length ? cfgAdmin : [])).map(String);
        if (this.adminGroups.length === 0 && Array.isArray(config.notify_groups) && config.notify_groups.length) {
            this.adminGroups = [String(config.notify_groups[0])];
        }

        // 白名单群（持久化于 config.json ql_notify_whitelist）
        this.whitelist = (config.ql_notify_whitelist || []).map(x => String(x));

        this.configPath = path.join(__dirname, '../../config.json');
        this._server = null;
        this._log('init adminGroups=%s whitelist=%s napcat=%s:%s',
            this.adminGroups.join(','), this.whitelist.join(','), this.napcatHost, this.napcatPort);
    }

    _log(fmt, ...args) {
        const msg = typeof fmt === 'string' && args.length
            ? fmt.replace(/%s/g, () => String(args.shift()))
            : fmt;
        console.log('[ql-notify] ' + msg);
    }

    // NapCat OneBot: 发送群消息
    _sendGroupMsg(groupId, text) {
        return new Promise((resolve) => {
            const postData = JSON.stringify({ group_id: parseInt(groupId, 10), message: text });
            const headers = { 'Content-Type': 'application/json' };
            if (this.napcatToken) headers['Authorization'] = 'Bearer ' + this.napcatToken;
            const req = http.request({
                hostname: this.napcatHost,
                port: this.napcatPort,
                path: '/send_group_msg',
                method: 'POST',
                headers,
                timeout: 8000,
            }, (res) => {
                let data = '';
                res.on('data', (c) => (data += c));
                res.on('end', () => {
                    try {
                        const j = JSON.parse(data);
                        resolve(j && j.status === 'ok' && j.data && j.data.message_id);
                    } catch (e) { resolve(null); }
                });
            });
            req.on('error', () => resolve(null));
            req.setTimeout(8000, () => { try { req.destroy(); } catch (e) {} resolve(null); });
            req.end(postData);
        });
    }

    // 计算实际推送目标群（管理员群 ∪ 白名单，去重）
    _targetGroups() {
        const set = new Set();
        this.adminGroups.forEach(g => set.add(String(g)));
        this.whitelist.forEach(g => set.add(String(g)));
        return Array.from(set);
    }

    // 把一条消息广播到目标群
    async _broadcast(title, content, level) {
        const emoji = level === 'error' ? '❌' : level === 'warn' ? '⚠️' : '✅';
        const text = `${emoji} ${title}\n${content}\n—— ${new Date().toLocaleString('zh-CN', { hour12: false })}`;
        const targets = this._targetGroups();
        const results = [];
        for (const g of targets) {
            const mid = await this._sendGroupMsg(g, text);
            results.push({ group: g, ok: !!mid });
            this._log('send group %s -> %s', g, mid ? 'OK' : 'FAIL');
        }
        if (targets.length === 0) {
            this._log('warn: 无推送目标（管理员群为空且白名单为空），消息被丢弃: %s', title);
        }
        return results;
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
            this._log('webhook recv title=%s level=%s len=%d', title, level, content.length);
            try {
                const r = await this._broadcast(title, content, level);
                res.statusCode = 200;
                res.end(JSON.stringify({ ok: true, sent: r, targets: this._targetGroups() }));
            } catch (e) {
                res.statusCode = 500;
                res.end(JSON.stringify({ ok: false, error: String(e) }));
            }
        });
    }

    // ===== 群命令：白名单管理 =====
    _isAdmin(context) {
        const uid = String(context.userId || '');
        if (uid && (uid === this.superAdmin || uid === this.developer)) return true;
        // 兼容框架权限系统（若有）
        try {
            const perm = global.sea1 && global.sea1.permission;
            if (perm && typeof perm.hasLevelSync === 'function') {
                return perm.hasLevelSync(uid, 2);
            }
        } catch (e) { /* 走 config 兜底 */ }
        return false;
    }

    async _cmdEnable(context) {
        const gid = String(context.groupId);
        if (this.whitelist.includes(gid)) {
            await context.reply('[!] 本群已在青龙推送白名单');
        } else {
            this.whitelist.push(gid);
            await this._saveWhitelist();
            await context.reply('[√] 已开启青龙推送，本群加入白名单');
        }
        return true;
    }

    async _cmdDisable(context) {
        const gid = String(context.groupId);
        const idx = this.whitelist.indexOf(gid);
        if (idx === -1) {
            await context.reply('[!] 本群不在青龙推送白名单');
        } else {
            this.whitelist.splice(idx, 1);
            await this._saveWhitelist();
            await context.reply('[√] 已关闭青龙推送，本群移出白名单');
        }
        return true;
    }

    async _saveWhitelist() {
        try {
            const raw = fs.readFileSync(this.configPath, 'utf-8');
            const cfg = JSON.parse(raw);
            cfg.ql_notify_whitelist = this.whitelist;
            fs.writeFileSync(this.configPath, JSON.stringify(cfg, null, 2));
            this._log('whitelist saved: %s', this.whitelist.join(','));
        } catch (e) {
            this._log('save whitelist failed: %s', e.message);
        }
    }

    async onMessage(context) {
        const msg = (context.msg || '').trim();
        if (!context.groupId) return false;

        if (msg === '开启青龙推送' || msg === '关闭青龙推送') {
            // 仅管理员/开发者可操作
            if (!this._isAdmin(context)) {
                await context.reply('[×] 权限不足：仅管理员可管理青龙推送白名单').catch(() => {});
                return true;
            }
            if (msg === '开启青龙推送') return await this._cmdEnable(context);
            return await this._cmdDisable(context);
        }
        return false;
    }

    async onEnable(/* db */) {
        if (this._server) return;
        this._server = http.createServer((req, res) => {
            if (req.method === 'POST' && req.url === '/webhook/ql') {
                this._handleWebhook(req, res);
                return;
            }
            if (req.method === 'GET' && (req.url === '/' || req.url === '/health')) {
                res.setHeader('Content-Type', 'application/json; charset=utf-8');
                res.statusCode = 200;
                res.end(JSON.stringify({
                    ok: true, plugin: 'ql-notify',
                    adminGroups: this.adminGroups,
                    whitelist: this.whitelist,
                    targets: this._targetGroups(),
                }));
                return;
            }
            res.statusCode = 404;
            res.end(JSON.stringify({ ok: false, error: 'not found' }));
        });
        this._server.on('error', (e) => this._log('server error: %s', e.message));
        this._server.listen(this.port, this.host, () => {
            this._log('HTTP server listening on %s:%s', this.host, this.port);
            this._log('default admin groups: %s', this.adminGroups.join(',') || '(none)');
            this._log('whitelist groups: %s', this.whitelist.join(',') || '(none)');
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
