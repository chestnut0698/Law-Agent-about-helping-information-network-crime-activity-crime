/* ========================================
   theme.js — 主题三档：跟随系统 / 深色 / 浅色
   ======================================== */
(function (global) {
    'use strict';

    const LABELS = {
        system: '跟随系统',
        dark: '深色',
        light: '浅色'
    };

    const Theme = {
        _media: null,
        _onSystemChange: null,
        _bound: false,

        init() {
            // 只按已保存的 mode 应用实际深/浅，不二次写入错误值
            State.applyThemeMode(State.themeMode || 'system');
            this._bindControls();
            this._syncControls();
            this._bindSystemListener();
        },

        setMode(mode) {
            const next = mode === 'dark' || mode === 'light' ? mode : 'system';
            State.setThemeMode(next);
            this._bindSystemListener();
            this._syncControls();
            Toast.info(`主题：${LABELS[next]}`);
        },

        /** 仅选择分段控件内的按钮，绝不绑定 <html data-theme-*> */
        _segButtons() {
            return Utils.$$('#wb-theme-seg [data-theme-mode]');
        },

        _bindControls() {
            if (this._bound) return;
            this._bound = true;
            const seg = Utils.$('#wb-theme-seg');
            if (seg) {
                // 阻止冒泡，避免被外壳其它 click 逻辑误伤
                seg.addEventListener('click', (e) => e.stopPropagation());
            }
            this._segButtons().forEach((btn) => {
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const mode = btn.getAttribute('data-theme-mode');
                    if (mode === 'system' || mode === 'dark' || mode === 'light') {
                        this.setMode(mode);
                    }
                });
            });
        },

        _syncControls() {
            const mode = State.themeMode || 'system';
            this._segButtons().forEach((btn) => {
                btn.classList.toggle('active', btn.getAttribute('data-theme-mode') === mode);
            });
            const label = Utils.$('#wb-theme-label');
            if (label) label.textContent = LABELS[mode] || '跟随系统';
        },

        _bindSystemListener() {
            if (!window.matchMedia) return;
            if (!this._media) {
                this._media = window.matchMedia('(prefers-color-scheme: dark)');
            }
            if (this._onSystemChange) {
                if (this._media.removeEventListener) {
                    this._media.removeEventListener('change', this._onSystemChange);
                } else if (this._media.removeListener) {
                    this._media.removeListener(this._onSystemChange);
                }
                this._onSystemChange = null;
            }
            if (State.themeMode !== 'system') return;
            this._onSystemChange = () => {
                if (State.themeMode !== 'system') return;
                // 仅刷新实际深/浅，保持 mode=system
                State.applyThemeMode('system');
                this._syncControls();
            };
            if (this._media.addEventListener) {
                this._media.addEventListener('change', this._onSystemChange);
            } else if (this._media.addListener) {
                this._media.addListener(this._onSystemChange);
            }
        }
    };

    global.Theme = Theme;
})(window);
