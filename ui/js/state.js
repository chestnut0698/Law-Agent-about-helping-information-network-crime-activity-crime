/* ========================================
   state.js — 全局状态管理
   ======================================== */
(function (global) {
    'use strict';

    const _memStore = {};
    const safeStorage = {
        getItem(key) {
            try { return localStorage.getItem(key); } catch (_) { return _memStore[key] || null; }
        },
        setItem(key, val) {
            try { localStorage.setItem(key, val); } catch (_) { _memStore[key] = val; }
        }
    };

    function normalizeThemeMode(raw) {
        if (raw === 'system' || raw === 'dark' || raw === 'light') return raw;
        // 兼容旧布尔/二值
        if (raw === 'true' || raw === true) return 'dark';
        if (raw === 'false' || raw === false) return 'light';
        return 'system';
    }

    function resolveTheme(mode) {
        if (mode === 'dark' || mode === 'light') return mode;
        const prefersDark = window.matchMedia
            && window.matchMedia('(prefers-color-scheme: dark)').matches;
        return prefersDark ? 'dark' : 'light';
    }

    const savedMode = normalizeThemeMode(safeStorage.getItem('agent-theme'));

    const State = {
        currentTaskId: null,
        currentModel: 'deepseek-v4-flash',
        modelNames: {
            'deepseek-v4-flash': 'DeepSeek V4 Flash'
        },
        agentState: 'idle',
        isStreaming: false,
        abortController: null,

        themeMode: savedMode,
        theme: resolveTheme(savedMode),
        sidebarCollapsed: false,

        setAgentState(state) {
            this.agentState = state;
            Events.emit('agent:state-change', state);
        },

        setModel(model) {
            this.currentModel = model;
            Events.emit('model:change', model);
        },

        setThemeMode(mode) {
            const next = normalizeThemeMode(mode);
            this.themeMode = next;
            safeStorage.setItem('agent-theme', next);
            this.applyThemeMode(next);
            // 只传可序列化字符串字段，避免监听方误把对象写成 data-theme
            Events.emit('theme:change', { mode: next, theme: this.theme });
        },

        applyThemeMode(mode) {
            const m = normalizeThemeMode(mode == null ? this.themeMode : mode);
            this.themeMode = m;
            this.theme = resolveTheme(m);
            const root = document.documentElement;
            // 实际深/浅只能是 dark|light；偏好写在 data-theme-pref，避免与按钮 [data-theme-mode] 撞选择器
            const resolved = (this.theme === 'dark' || this.theme === 'light') ? this.theme : 'light';
            this.theme = resolved;
            root.setAttribute('data-theme', resolved);
            root.setAttribute('data-theme-pref', m);
            // 清理历史错误属性 / 误写（如 [object Object]、把 mode 写进 data-theme）
            root.removeAttribute('data-theme-mode');
            if (root.getAttribute('data-theme') !== 'dark' && root.getAttribute('data-theme') !== 'light') {
                root.setAttribute('data-theme', resolved);
            }
        },

        /** @deprecated 保留兼容；改为循环三档 */
        toggleTheme() {
            const order = ['system', 'dark', 'light'];
            const i = order.indexOf(this.themeMode);
            this.setThemeMode(order[(i + 1) % order.length]);
        },

        toggleSidebar() {
            this.sidebarCollapsed = !this.sidebarCollapsed;
            Events.emit('sidebar:toggle', this.sidebarCollapsed);
        }
    };

    const Events = {
        listeners: {},
        on(event, fn) {
            (this.listeners[event] = this.listeners[event] || []).push(fn);
        },
        off(event, fn) {
            this.listeners[event] = (this.listeners[event] || []).filter(f => f !== fn);
        },
        emit(event, data) {
            (this.listeners[event] || []).forEach(fn => {
                try { fn(data); } catch (e) { console.error(e); }
            });
        }
    };

    State.Events = Events;
    State.applyThemeMode(State.themeMode);

    global.State = State;
    global.Events = Events;
})(window);
