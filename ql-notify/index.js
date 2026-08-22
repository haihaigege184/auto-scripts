/**
 * SEA2 插件：ql-notify —— 接收青龙任务消息并转发到通知群
 *
 * 工作机制：
 *   - 本插件在 onEnable 时启动一个本地 HTTP 服务（默认 127.0.0.1:13002）。
 *   - 青龙侧 checkin.py / login.py 在成功或失败时，用纯标准库 urllib
 *     POST 到 http://127.0.0.1:13002/webhook/ql ，body 为 JSON:
 *        { "title": "毫秒镜像签到", "content": "✅ 今日已签到 ...", "level": "ok|warn|error" }
 *   - 本插件收到后，遍历 config.notify_groups，逐个调用 NapCat OneBot
 *     /send_group_msg 把消息推到群里。
 *
 * 约定（与框架一致）：
 *   - 入口 plugins/ql-notify/index.js，导出 class QlNotifyPlugin extends BasePlugin
 *   - plugin-manager 自动扫描 plugins/ 子目录并 new QlNotifyPlugin(name, config)
 *   - onEnable(db) 钩子用来启动 HTTP 服务，onDisable(db) 用来关闭
 *
 * 环境变量 / 可配置：
 *   - QL_NOTIFY_PORT  (默认 13002)  监听端口
 *   - QL_NOTIFY_HOST  (默认 127.0.0.1) 监听地址（仅本机，青龙同机）
 *   - 群列表取自 config.notify_groups（与框架通知群一致）
 */

'use strict';

const BasePlugin = require('../base-plugin');
const http = require('node:http');

class QlNotifyPlugin extends BasePlugin {
    constructor(name, config) {
        super(name, config);
        this.priority = 50;
        this.permissionLevel = 0;

        this.napcatHost = config.napcat_host || '127.0.0.1';
        this.napcatPort = config.napcat_port || 4000;
        this.napcatToken = config.napcat_token || '';
        this.notifyGroups = (config.notify_groups || []).map((x) => String(x));

        this.host = process.env.QL_NOTIFY_HOST || '127.0.0.1';
        this.port = parseInt(process.env.QL_NOTIFY_PORT || '13002', 10);

        this._server = null;
        this._log('init groups=%s napcat=%s:%s', this.notifyGroups.join(','), this.napcatHost, this.napcatPort);
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

    // 把一条消息广播到所有通知群
    async _broadcast(title, content, level) {
        const emoji = level === 'error' ? '❌' : level === 'warn' ? '⚠️' : '✅';
        const text = `${emoji} ${title}\n${content}\n—— ${new Date().toLocaleString('zh-CN', { hour12: false })}`;
        const results = [];
        for (const g of this.notifyGroups) {
            const mid = await this._sendGroupMsg(g, text);
            results.push({ group: g, ok: !!mid });
            this._log('send group %s -> %s', g, mid ? 'OK' : 'FAIL');
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
                res.end(JSON.stringify({ ok: true, sent: r }));
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
            if (req.method === 'GET' && (req.url === '/' || req.url === '/health')) {
                res.setHeader('Content-Type', 'application/json; charset=utf-8');
                res.statusCode = 200;
                res.end(JSON.stringify({ ok: true, plugin: 'ql-notify', groups: this.notifyGroups }));
                return;
            }
            res.statusCode = 404;
            res.end(JSON.stringify({ ok: false, error: 'not found' }));
        });
        this._server.on('error', (e) => this._log('server error: %s', e.message));
        this._server.listen(this.port, this.host, () => {
            this._log('HTTP server listening on %s:%s  (groups: %s)',
                this.host, this.port, this.notifyGroups.join(','));
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
