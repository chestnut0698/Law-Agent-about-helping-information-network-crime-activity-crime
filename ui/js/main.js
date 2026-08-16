/* ========================================
   main.js — 主入口，初始化所有模块
   ======================================== */
(function () {
    'use strict';

    // 等待 DOM 就绪
    function ready(fn) {
        if (document.readyState !== 'loading') {
            fn();
        } else {
            document.addEventListener('DOMContentLoaded', fn);
        }
    }

    ready(() => {
        // 初始化各模块（注意顺序：State → Toast → Sidebar → Input → Theme → Workbench）
        Toast.init();
        Sidebar.init();
        FileUpload.init();
        Input.init();
        Theme.init();
        Workbench.init();

        // 监听 Agent 状态变化，更新顶部状态指示器
        Events.on('agent:state-change', (state) => {
            const dot = Utils.$('.status-dot');
            const text = Utils.$('.status-text');
            if (dot) {
                dot.className = 'status-dot';
                if (state === 'idle') {
                    dot.classList.add('status-idle');
                    if (text) text.textContent = '空闲';
                } else if (state === 'thinking') {
                    dot.classList.add('status-thinking');
                    if (text) text.textContent = '思考中';
                } else if (state === 'working') {
                    dot.classList.add('status-working');
                    if (text) text.textContent = '工作中';
                } else if (state === 'done') {
                    dot.classList.add('status-done');
                    if (text) text.textContent = '完成';
                }
            }
        });

        // 监听模型切换
        Events.on('model:change', (model) => {
            const currentModel = Utils.$('#current-model');
            if (currentModel) currentModel.textContent = State.modelNames[model] || model;
        });

        // 监听主题切换
        Events.on('theme:change', (theme) => {
            document.documentElement.setAttribute('data-theme', theme);
        });

        // 监听工具审批事件
        Events.on('tool:approved', ({ card }) => {
            Toast.success('已批准工具执行');
            ToolCall.updateStatus(card, 'running');
        });

        Events.on('tool:rejected', ({ card }) => {
            Toast.warning('已拒绝工具执行');
            ToolCall.updateStatus(card, 'error', '用户拒绝了该操作');
        });

        // 全局错误捕获
        window.addEventListener('error', (e) => {
            console.error('[Agent UI Error]', e.error);
            Toast.error('发生错误：' + (e.message || '未知错误'));
        });

        // 阻止表单默认提交
        document.addEventListener('submit', (e) => e.preventDefault());

        console.log('%c链证智析 工作台就绪', 'color: #85CC16; font-size: 14px; font-weight: bold;');
        console.log('%c模型: ' + State.modelNames[State.currentModel], 'color: #666;');
    });
})();
