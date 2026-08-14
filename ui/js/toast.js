/* ========================================
   toast.js — Toast 通知系统
   ======================================== */
(function (global) {
    'use strict';

    const ICONS = {
        success: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>`,
        error:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></svg>`,
        info:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>`,
        warning: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0zM12 9v4M12 17h.01"/></svg>`
    };

    const Toast = {
        container: null,

        init() {
            this.container = Utils.$('#toast-container');
            if (!this.container) {
                this.container = Utils.create('div', { class: 'toast-container', id: 'toast-container' });
                document.body.appendChild(this.container);
            }
        },

        show(message, type = 'info', duration = 3500) {
            if (!this.container) this.init();

            const toast = Utils.create('div', { class: `toast toast-${type}` }, [
                Utils.create('div', { class: 'toast-icon', html: ICONS[type] || ICONS.info }),
                Utils.create('div', { class: 'toast-content', text: message }),
                Utils.create('button', { class: 'toast-close', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>' })
            ]);

            const closeBtn = Utils.$('.toast-close', toast);
            const remove = () => {
                toast.classList.add('removing');
                setTimeout(() => toast.remove(), 300);
            };

            closeBtn.addEventListener('click', remove);
            this.container.appendChild(toast);

            if (duration > 0) {
                setTimeout(remove, duration);
            }

            return { close: remove };
        },

        success(msg, dur) { return this.show(msg, 'success', dur); },
        error(msg, dur)   { return this.show(msg, 'error', dur); },
        info(msg, dur)    { return this.show(msg, 'info', dur); },
        warning(msg, dur) { return this.show(msg, 'warning', dur); }
    };

    global.Toast = Toast;
})(window);
