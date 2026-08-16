/* ========================================
   theme.js — 主题切换
   ======================================== */
(function (global) {
    'use strict';

    const Theme = {
        init() {
            // 初始化主题
            document.documentElement.setAttribute('data-theme', State.theme);
            this._syncButton();

            const btn = Utils.$('#btn-theme-toggle');
            if (btn) {
                btn.addEventListener('click', () => {
                    State.toggleTheme();
                    this._syncButton();
                    Toast.info(`已切换到${State.theme === 'dark' ? '深色' : '浅色'}主题`);
                });
            }

            // 未手动选择过主题时跟随系统深浅色
            if (window.matchMedia) {
                const media = window.matchMedia('(prefers-color-scheme: dark)');
                media.addEventListener?.('change', (e) => {
                    let saved = null;
                    try { saved = localStorage.getItem('agent-theme'); } catch (_) { }
                    if (saved) return;
                    State.theme = e.matches ? 'dark' : 'light';
                    document.documentElement.setAttribute('data-theme', State.theme);
                    this._syncButton();
                });
            }
        },

        _syncButton() {
            const btn = Utils.$('#btn-theme-toggle');
            if (btn) btn.title = State.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题';
        }
    };

    global.Theme = Theme;
})(window);
