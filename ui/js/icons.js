/* ========================================
   icons.js — lucide 风格内联 SVG（无 npm）
   ======================================== */
(function (global) {
    'use strict';

    const PATHS = {
        folderKanban: '<path d="M4 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5z"/><path d="M8 9h2v6H8zM14 9h2v3h-2z"/>',
        fileStack: '<path d="M16 6v2a2 2 0 0 0 2 2h2"/><path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10l-6-6z"/><path d="M14 2v6h6"/><path d="M8 12h8M8 16h5"/>',
        users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
        link2: '<path d="M9 17H7A5 5 0 0 1 7 7h2"/><path d="M15 7h2a5 5 0 1 1 0 10h-2"/><path d="M8 12h8"/>',
        waypoints: '<circle cx="12" cy="4.5" r="2.5"/><path d="m10.2 6.3-3.7 3.7"/><circle cx="4.5" cy="12" r="2.5"/><path d="M7 12h10"/><circle cx="19.5" cy="12" r="2.5"/><path d="m13.8 17.7 3.7-3.7"/><circle cx="12" cy="19.5" r="2.5"/>',
        gitBranch: '<circle cx="6" cy="6" r="3"/><path d="M6 9v12"/><circle cx="18" cy="6" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
        fileCheck2: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m9 15 2 2 4-4"/>',
        play: '<polygon points="6 3 20 12 6 21 6 3"/>',
        plus: '<path d="M5 12h14M12 5v14"/>',
        search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
        upload: '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
        filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
        calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
        info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
        shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
        shieldCheck: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
        externalLink: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
        usersRound: '<path d="M18 21a8 8 0 0 0-12 0"/><circle cx="12" cy="8" r="5"/>',
        download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
        check: '<path d="M20 6 9 17l-5-5"/>',
        x: '<path d="M18 6 6 18M6 6l12 12"/>',
        banknote: '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2"/><path d="M6 12h.01M18 12h.01"/>',
        phone: '<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/>',
        cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
        refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
        settings: '<circle cx="12" cy="12" r="3"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>',
        helpCircle: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01"/>',
        lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
        fileText: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h8M8 9h2"/>',
        arrowUpRight: '<path d="M7 17 17 7"/><path d="M7 7h10v10"/>',
        chevronRight: '<path d="m9 18 6-6-6-6"/>',
        paperclip: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>'
    };

    const VIEW_ICONS = {
        tasks: 'folderKanban',
        materials: 'fileStack',
        entities: 'users',
        leads: 'link2',
        timeline: 'waypoints',
        graph: 'gitBranch',
        reports: 'fileCheck2'
    };

    const Icons = {
        svg(name, cls) {
            const paths = PATHS[name] || PATHS.info;
            return `<svg class="${cls || 'wb-ico'}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
        },

        el(name, cls) {
            const span = document.createElement('span');
            span.className = cls || 'wb-ico-wrap';
            span.innerHTML = this.svg(name);
            return span;
        },

        /** 带图标的按钮内容 */
        labeled(iconName, text) {
            return this.svg(iconName, 'wb-ico') + `<span>${text}</span>`;
        },

        forView(view) {
            return this.svg(VIEW_ICONS[view] || 'info', 'wb-nav-ico');
        },

        forEntityType(type) {
            const t = String(type || '').toUpperCase();
            if (t.includes('PHONE') || t.includes('手机')) return 'phone';
            if (t.includes('ACCOUNT') || t.includes('银行') || t.includes('卡')) return 'banknote';
            if (t.includes('DEVICE') || t.includes('IMEI') || t.includes('设备')) return 'cpu';
            return 'users';
        }
    };

    global.Icons = Icons;
})(window);
