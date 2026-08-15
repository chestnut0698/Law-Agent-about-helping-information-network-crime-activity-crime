/* ========================================
   utils.js — 通用工具函数
   ======================================== */
(function (global) {
    'use strict';

    const Utils = {
        /** 防抖 */
        debounce(fn, delay) {
            let timer;
            return function (...args) {
                clearTimeout(timer);
                timer = setTimeout(() => fn.apply(this, args), delay);
            };
        },

        /** 节流 */
        throttle(fn, interval) {
            let last = 0;
            return function (...args) {
                const now = Date.now();
                if (now - last >= interval) {
                    last = now;
                    fn.apply(this, args);
                }
            };
        },

        /** 生成唯一 ID */
        uid(prefix = 'id') {
            return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
        },

        /** 转义 HTML */
        escapeHtml(str) {
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        },

        /** 格式化时间 */
        formatTime(date) {
            const d = typeof date === 'string' ? new Date(date) : date;
            const h = d.getHours().toString().padStart(2, '0');
            const m = d.getMinutes().toString().padStart(2, '0');
            return `${h}:${m}`;
        },

        /** 相对时间 */
        timeAgo(date) {
            const d = typeof date === 'string' ? new Date(date) : date;
            const diff = (Date.now() - d.getTime()) / 1000;
            if (diff < 60) return '刚刚';
            if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
            if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
            return `${Math.floor(diff / 86400)}天前`;
        },

        /** 复制文本到剪贴板 */
        async copyText(text) {
            try {
                await navigator.clipboard.writeText(text);
                return true;
            } catch {
                // fallback
                const ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                return true;
            }
        },

        /** 延迟 */
        sleep(ms) {
            return new Promise(resolve => setTimeout(resolve, ms));
        },

        /** 带重试的异步函数 */
        async retry(fn, times = 3, delay = 1000) {
            for (let i = 0; i < times; i++) {
                try {
                    return await fn();
                } catch (e) {
                    if (i === times - 1) throw e;
                    await this.sleep(delay * (i + 1));
                }
            }
        },

        /** 解析 JSON 安全 */
        safeJsonParse(str, fallback = null) {
            try { return JSON.parse(str); } catch { return fallback; }
        },

        /** 截断文本 */
        truncate(str, len = 50) {
            if (str.length <= len) return str;
            return str.slice(0, len) + '...';
        },

        /** 检测是否为移动端 */
        isMobile() {
            return window.innerWidth <= 768;
        },

        /** 获取元素 */
        $(selector, root = document) {
            return root.querySelector(selector);
        },

        /** 获取所有元素 */
        $$(selector, root = document) {
            return Array.from(root.querySelectorAll(selector));
        },

        /** 创建元素 */
        create(tag, attrs = {}, children = []) {
            const el = document.createElement(tag);
            Object.entries(attrs).forEach(([k, v]) => {
                if (k === 'class') el.className = v;
                else if (k === 'html') el.innerHTML = v;
                else if (k === 'text') el.textContent = v;
                else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
                else el.setAttribute(k, v);
            });
            children.forEach(child => {
                if (typeof child === 'string') {
                    el.appendChild(document.createTextNode(child));
                } else {
                    el.appendChild(child);
                }
            });
            return el;
        }
    };

    global.Utils = Utils;
})(window);
