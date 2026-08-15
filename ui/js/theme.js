/* ========================================
   theme.js — 主题切换
   ======================================== */
(function (global) {
    'use strict';

    const Theme = {
        init() {
            // 初始化主题
            document.documentElement.setAttribute('data-theme', State.theme);

            const btn = Utils.$('#btn-theme-toggle');
            if (btn) {
                btn.addEventListener('click', () => {
                    State.toggleTheme();
                    Toast.info(`已切换到${State.theme === 'dark' ? '深色' : '浅色'}主题`);
                });
            }

            // 监听系统主题变化
            if (window.matchMedia) {
                const media = window.matchMedia('(prefers-color-scheme: dark)');
                media.addEventListener?.('change', (e) => {
                    if (!localStorage.getItem('agent-theme')) {
                        State.theme = e.matches ? 'dark' : 'light';
                        document.documentElement.setAttribute('data-theme', State.theme);
                    }
                });
            }
        }
    };

    global.Theme = Theme;
})(window);
