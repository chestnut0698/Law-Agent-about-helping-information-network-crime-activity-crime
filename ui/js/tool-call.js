/* ========================================
   tool-call.js — 分析执行卡片（办案用语）
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

    const TOOL_LABELS = {
        get_task_overview: '查看任务与材料情况',
        confirm_task_plan: '确认分析计划',
        refresh_task_materials: '刷新材料接入情况',
        run_task_collision: '跨案标识比对',
        run_task_timeline: '整理事件时间线',
        write_ai_clues: '写入疑似关联线索',
        read_artifact: '查阅分析成果',
        read_material_chunk: '回原文查阅材料片段',
        list_case_materials: '列出案件材料',
        search_lawlibrary: '检索法规库',
        search_policy: '检索规范性文件'
    };

    const ToolCall = {
        displayName(name) {
            if (!name) return '分析动作';
            return TOOL_LABELS[name] || '查阅与分析';
        },

        summarizeResult(raw) {
            if (raw == null || raw === '') return '已完成';
            let data = raw;
            if (typeof raw === 'string') {
                try { data = JSON.parse(raw); } catch { return this._shortText(raw); }
            }
            if (typeof data !== 'object' || !data) return this._shortText(String(raw));
            if (data.message) return String(data.message);
            if (data.ok === false && data.message) return String(data.message);
            if (data.artifact_type === 'ENTITY_CANDIDATE_SET' || data.title && String(data.title).includes('实体')) {
                return data.message || '已生成跨案对象待核清单';
            }
            if (data.artifact_type === 'ROLE_TIMELINE') {
                return data.message || `事件时间线已整理${data.event_count != null ? `（${data.event_count} 条）` : ''}`;
            }
            if (data.artifact_type === 'CLUE_SET' || data.clue_count != null) {
                return data.message || `已写入疑似关联线索${data.clue_count != null ? `（${data.clue_count} 条）` : ''}`;
            }
            if (data.artifact_type === 'MATERIAL_BATCH') {
                return data.message || '材料批次已更新';
            }
            if (data.title) return String(data.title);
            return '本步已完成，请在中间工作区核验';
        },

        _shortText(text) {
            const t = String(text).replace(/\s+/g, ' ').trim();
            return t.length > 120 ? `${t.slice(0, 120)}…` : t;
        },

        create(data) {
            const card = Utils.create('div', { class: 'tool-call-card' });
            const expanded = data.expanded === true;
            if (expanded) card.classList.add('expanded');
            else card.classList.add('compact');

            const displayName = data.label || this.displayName(data.name);
            const type = data.type === 'search' ? 'search' : (data.type === 'file' ? 'file' : 'db');

            const header = Utils.create('div', { class: 'tool-call-header' }, [
                Utils.create('div', { class: `tool-icon ${type}`, html: TOOL_ICONS[type] || TOOL_ICONS.db }),
                Utils.create('div', { class: 'tool-info' }, [
                    Utils.create('div', { class: 'tool-name', text: displayName }),
                    Utils.create('div', { class: 'tool-desc', text: data.description || '点击展开查看执行说明' })
                ]),
                Utils.create('div', { class: 'tool-status' }, [
                    Utils.create('div', { class: `tool-status-dot ${data.status || 'pending'}` }),
                    Utils.create('span', { class: 'tool-status-text', text: this._statusText(data.status) })
                ]),
                Utils.create('svg', { class: 'tool-chevron', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M6 9l6 6 6-6"/>' })
            ]);

            const body = Utils.create('div', { class: 'tool-call-body' });
            if (data.params !== undefined && data.params !== null && Object.keys(data.params || {}).length) {
                body.appendChild(Utils.create('div', { class: 'tool-section' }, [
                    Utils.create('div', { class: 'tool-section-label', text: '执行说明' }),
                    Utils.create('div', { class: 'tool-params', text: this._paramsHint(data.name, data.params) })
                ]));
            }

            if (data.result !== undefined) {
                const resultClass = data.status === 'error' ? 'tool-result error' : 'tool-result success';
                body.appendChild(Utils.create('div', { class: 'tool-section' }, [
                    Utils.create('div', { class: 'tool-section-label', text: '结果摘要' }),
                    Utils.create('div', { class: resultClass, text: this.summarizeResult(data.result) })
                ]));
                body.appendChild(this._rawDetail(data.result));
            }

            card.appendChild(header);
            card.appendChild(body);
            header.addEventListener('click', () => {
                card.classList.toggle('expanded');
                card.classList.toggle('compact');
            });
            return card;
        },

        _paramsHint(name, params) {
            if (!params || typeof params !== 'object') return '按当前任务范围执行';
            const bits = [];
            if (params.case_id) bits.push('已指定案件');
            if (params.artifact_id) bits.push('查阅既有分析成果');
            if (params.document_version_id || params.chunk_id) bits.push('回原文定位材料片段');
            if (params.clues) bits.push(`拟写入 ${Array.isArray(params.clues) ? params.clues.length : ''} 条线索`.trim());
            return bits.length ? bits.join(' · ') : '按当前监督任务范围执行';
        },

        updateStatus(card, status, result) {
            const dot = Utils.$('.tool-status-dot', card);
            const text = Utils.$('.tool-status-text', card);
            if (dot) dot.className = `tool-status-dot ${status}`;
            if (text) text.textContent = this._statusText(status);
            if (result !== undefined) {
                let body = Utils.$('.tool-call-body', card);
                if (!body) {
                    body = Utils.create('div', { class: 'tool-call-body' });
                    card.appendChild(body);
                }
                Utils.$$('.tool-result', body).forEach(el => {
                    const section = el.closest('.tool-section');
                    if (section) section.remove();
                    else el.remove();
                });
                Utils.$$('.tool-raw-detail', body).forEach(el => el.remove());
                const resultClass = status === 'error' ? 'tool-result error' : 'tool-result success';
                body.appendChild(Utils.create('div', { class: 'tool-section' }, [
                    Utils.create('div', { class: 'tool-section-label', text: '结果摘要' }),
                    Utils.create('div', { class: resultClass, text: this.summarizeResult(result) })
                ]));
                body.appendChild(this._rawDetail(result));
                card.classList.add('compact');
                card.classList.remove('expanded');
            }
        },

        _rawDetail(result) {
            let text = '';
            if (typeof result === 'string') text = result;
            else {
                try { text = JSON.stringify(result, null, 2); } catch { text = String(result); }
            }
            const details = Utils.create('details', { class: 'tool-raw-detail' });
            details.appendChild(Utils.create('summary', { text: '技术明细（可选）' }));
            details.appendChild(Utils.create('pre', { class: 'tool-raw-pre', text }));
            return details;
        },

        _statusText(status) {
            const map = {
                pending: '等待中',
                running: '进行中…',
                success: '已完成',
                error: '未完成',
                approval: '待确认'
            };
            return map[status] || status;
        }
    };

    global.ToolCall = ToolCall;
})(window);
