/* ========================================
   tool-call.js — 工具调用卡片渲染
   ======================================== */
(function (global) {
    'use strict';

    const TOOL_ICONS = {
        search:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>',
        code:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg>',
        file:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg>',
        calc:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="2" width="16" height="20" rx="2"/><path d="M8 6h8M8 10h8M8 14h4M8 18h4"/></svg>',
        db:       '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
        email:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><path d="M22 6l-10 7L2 6"/></svg>',
        terminal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 17l6-6-6-6M12 19h8"/></svg>',
        weather:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v2M4.93 4.93l1.41 1.41M2 12h2M4.93 19.07l1.41-1.41M12 20v2M19.07 19.07l-1.41-1.41M22 12h-2M19.07 4.93l-1.41 1.41"/><circle cx="12" cy="12" r="5"/></svg>'
    };

    const ToolCall = {
        /**
         * 创建工具调用卡片
         * @param {Object} data {
         *   type: 'search'|'code'|'file'|...,
         *   name: string,
         *   description: string,
         *   params: object|string,
         *   status: 'pending'|'running'|'success'|'error'|'approval',
         *   result: string,
         *   expanded: boolean
         * }
         */
        create(data) {
            const card = Utils.create('div', { class: 'tool-call-card' });
            if (data.expanded) card.classList.add('expanded');
            // 头部
            const header = Utils.create('div', { class: 'tool-call-header' }, [
                Utils.create('div', { class: `tool-icon ${data.type}`, html: TOOL_ICONS[data.type] }),
                Utils.create('div', { class: 'tool-info' }, [
                    Utils.create('div', { class: 'tool-name', text: data.name || '工具调用' }),
                    Utils.create('div', { class: 'tool-desc', text: data.description || '' })
                ]),
                Utils.create('div', { class: 'tool-status' }, [
                    Utils.create('div', { class: `tool-status-dot ${data.status || 'pending'}` }),
                    Utils.create('span', { class: 'tool-status-text', text: this._statusText(data.status) })
                ]),
                Utils.create('svg', { class: 'tool-chevron', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M6 9l6 6 6-6"/>' })
            ]);

            // 主体
            const body = Utils.create('div', { class: 'tool-call-body' });

            // 参数区
            if (data.params !== undefined) {
                const paramsStr = typeof data.params === 'string' ? data.params : JSON.stringify(data.params, null, 2);
                body.appendChild(Utils.create('div', { class: 'tool-section' }, [
                    Utils.create('div', { class: 'tool-section-label', text: '输入参数' }),
                    Utils.create('div', { class: 'tool-params', text: paramsStr })
                ]));
            }

            // 结果区
            if (data.result !== undefined) {
                const resultClass = data.status === 'error' ? 'tool-result error' : 'tool-result success';
                body.appendChild(Utils.create('div', { class: 'tool-section' }, [
                    Utils.create('div', { class: 'tool-section-label', text: '返回结果' }),
                    Utils.create('div', { class: resultClass, text: data.result })
                ]));
            }

            // 审批操作
            if (data.status === 'approval') {
                body.appendChild(Utils.create('div', { class: 'tool-approval-actions' }, [
                    Utils.create('button', { class: 'btn-approve', text: '✓ 批准执行' }),
                    Utils.create('button', { class: 'btn-reject', text: '✗ 拒绝' })
                ]));
            }

            card.appendChild(header);
            card.appendChild(body);

            // 点击展开/折叠
            header.addEventListener('click', () => {
                card.classList.toggle('expanded');
            });

            // 审批按钮事件
            const approveBtn = Utils.$('.btn-approve', card);
            const rejectBtn = Utils.$('.btn-reject', card);
            if (approveBtn) {
                approveBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    Events.emit('tool:approved', { card, toolData: data });
                });
            }
            if (rejectBtn) {
                rejectBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    Events.emit('tool:rejected', { card, toolData: data });
                });
            }

            return card;
        },

        /** 更新状态 */
        updateStatus(card, status, result) {
            const dot = Utils.$('.tool-status-dot', card);
            const text = Utils.$('.tool-status-text', card);
            if (dot) {
                dot.className = `tool-status-dot ${status}`;
            }
            if (text) {
                text.textContent = this._statusText(status);
            }
            if (result !== undefined) {
                let body = Utils.$('.tool-call-body', card);
                if (!body) {
                    body = Utils.create('div', { class: 'tool-call-body' });
                    card.appendChild(body);
                }
                const resultClass = status === 'error' ? 'tool-result error' : 'tool-result success';
                // 移除旧结果
                Utils.$$('.tool-result', body).forEach(el => el.remove());
                body.appendChild(Utils.create('div', { class: 'tool-section' }, [
                    Utils.create('div', { class: 'tool-section-label', text: '返回结果' }),
                    Utils.create('div', { class: resultClass, text: result })
                ]));
                card.classList.add('compact');
                card.classList.remove('expanded')
            }
        },

        _statusText(status) {
            const map = {
                pending: '等待中',
                running: '执行中...',
                success: '已完成',
                error: '失败',
                approval: '等待审批'
            };
            return map[status] || status;
        }
    };

    global.ToolCall = ToolCall;
})(window);
