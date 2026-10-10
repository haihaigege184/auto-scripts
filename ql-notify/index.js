/**
 * SEA2 插件：ql-notify —— 接收青龙任务消息并【私发管理员】(+可选发群)
 *
 * 职责边界（单一）：青龙任务结果通知 / 告警转发。只做"送信"。
 *   验证码过码（挑战-应答、QQ 命令触发签到）已于 2026-10-11 拆分到
 *   独立插件 plugins/easyfrp/（端口 13004），本插件不再承载任何过码逻辑。
 *
 * 设计（2026-08-22 改版，按用户要求）：
 *   - 默认【不】推送到任何群，也不推送给所有群。
 *   - 所有青龙任务消息（1ms / gost 签到等）一律【私聊】发送给超级管理员
 *     （config.superAdmin），与框架「重启成功通知」走同一私发通道。
 *   - 私发通道：global.sea1.adapter.send(uin, text) （不传 groupId 即私聊），
 *     与 adapters/qq/index.js 的 sendRebootNotification 完全一致，零改动框架。
 *
 * 3.1.15 扩展（2026-09-15，宝宝在哪控制台加「推送管理员 QQ」）：
 *   - webhook payload 支持可选 "admin": "<QQ号>" 字段——带该字段时本次私发目标改为它，
 *     覆盖 config.superAdmin 默认值（控制台改完即时生效，不必去改框架配置）。
 *   - 只接受纯数字（5~12 位），非法值忽略并回落 superAdmin。
 *   - 不带 admin 字段 → 行为与旧版完全一致（青龙签到等调用零影响）。
 *
 * 3.1.16（2026-09-21，用户指定）：时间戳格式改为 `—— 2026/9/21 【18:25:56】`；
 *   content 为空时不再输出空行（围栏告警信息现在全在 title 里）。
 *
 * 3.2.0 扩展（2026-09-07，宝宝在哪隧道告警需求）：
 *   - webhook payload 支持可选 "group": "<QQ群号>" 字段——带该字段时，
 *     消息同时发送到指定群（adapter.send('', text, {groupId})）与私发管理员。
 *   - 用途：静静追踪 8012 服务端的「隧道可用/延迟过高/设备离线」告警同时推群+管理员。
 *
 * 3.3.0 重构（2026-10-11，按用户要求拆分职责）：
 *   - 【移除】验证码挑战-应答（POST /webhook/captcha、GET .../result、.../pending）
 *   - 【移除】QQ 私聊命令触发签到（过码/签到/补签/gostc/easyfrp）与 onMessage 钩子
 *   - 【移除】/health 里的 captcha 字段
 *   - 以上能力逐行原样迁至 plugins/easyfrp/index.js（监听 13004，行为不变）。
 *     拆分原因：本插件名与定位是"青龙通知"，却承担着过码的活，名实不符、
 *     职责混淆；且过码需要 onMessage 拦截，与"纯被动接收 webhook"的定位冲突。
 *   - 本插件对外接口收敛为：POST /webhook/ql、GET /health。通知行为零变化。
 *
 * 安全铁律：
 *   - 群发仅在 webhook 调用方【显式】携带 group 字段时触发；青龙签到等旧调用不传 group，
 *     行为不变（仍只私发管理员）。绝不自动推所有群，绝不改动其它插件/框架代码。
 *   - 仅读取 config.superAdmin（已存在于框架 config），不写配置、不改群白名单。
 *
 * 工作机制：
 *   - 本插件在 onEnable 时启动一个本地 HTTP 服务（默认 0.0.0.0:13002）。
 *   - 青龙侧脚本在成功或失败时，用纯标准库 urllib
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
 *   - QL_NOTIFY_ADMIN  (可选) 管理员 QQ，未设则取 config.superAdmin
 *
 * 配套：验证码人工过码 → plugins/easyfrp/（13004），详见其文件头。
 */

'use strict';

const BasePlugin = require('../base-plugin');
const http = require('node:http');

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

        this._server = null;
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
            if (req.method === 'GET' && (req.url === '/' || req.url === '/health')) {
                res.setHeader('Content-Type', 'application/json; charset=utf-8');
                res.statusCode = 200;
                res.end(JSON.stringify({
                    ok: true, plugin: 'ql-notify',
                    mode: 'private-admin-only',
                    admin: this.superAdmin,
                    note: '仅通知; 验证码过码见 plugins/easyfrp (13004)',
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
