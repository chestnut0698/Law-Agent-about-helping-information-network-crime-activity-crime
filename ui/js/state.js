/* ========================================
   state.js — 全局状态管理
   ======================================== */
(function (global) {
    'use strict';

    // ---------- 安全 storage 包装 ----------
    // 解决隐私模式 / iframe / file:// 下 localStorage 抛异常
    // 导致整个 IIFE 崩溃、State/Events 未定义、Input 无法初始化的连锁问题
    const _memStore = {};
    const safeStorage = {
        getItem(key) {
            try { return localStorage.getItem(key); } catch (_) { return _memStore[key] || null; }
        },
        setItem(key, val) {
            try { localStorage.setItem(key, val); } catch (_) { _memStore[key] = val; }
        }
    };

    const State = {
        // 会话状态
        currentConversationId: 1,
        conversations: [
        ],

        // 模型
        currentModel: 'deepseek-v4-flash',
        modelNames: {
            'deepseek-v4-flash': 'DeepSeek V4 Flash'
        },

        // Agent 状态
        agentState: 'idle', // idle | thinking | working | done
        isStreaming: false,
        abortController: null,

        // UI 状态
        theme: safeStorage.getItem('agent-theme') || 'dark',
        sidebarCollapsed: false,

        // 消息列表（当前会话）
        get messages() {
            const conv = this.conversations.find(c => c.id === this.currentConversationId);
            return conv ? conv.messages : [];
        },

        // 工具方法
        addMessage(role, content, extra) {
            const msg = {
                id: Date.now() + Math.random(),
                role,
                content,
                timestamp: new Date().toISOString(),
                ...extra
            };
            this.messages.push(msg);
            return msg;
        },

        updateMessage(id, updates) {
            const msg = this.messages.find(m => m.id === id);
            if (msg) Object.assign(msg, updates);
        },

        setAgentState(state) {
            this.agentState = state;
            Events.emit('agent:state-change', state);
        },

        setModel(model) {
            this.currentModel = model;
            Events.emit('model:change', model);
        },

        toggleTheme() {
            this.theme = this.theme === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', this.theme);
            safeStorage.setItem('agent-theme', this.theme);
            Events.emit('theme:change', this.theme);
        },

        toggleSidebar() {
            this.sidebarCollapsed = !this.sidebarCollapsed;
            Events.emit('sidebar:toggle', this.sidebarCollapsed);
        }
    };

    // 简易事件总线
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

    // 初始化主题
    document.documentElement.setAttribute('data-theme', State.theme);

    global.State = State;
    global.Events = Events;
})(window);