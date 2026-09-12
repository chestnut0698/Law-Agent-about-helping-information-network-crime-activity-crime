/* ========================================
   thinking.js — 分析步骤 / 思考过程（> 折叠，点开右转朝下）
   只展示法律人能看懂的中文，不展示英文思维链与技术细节
   ======================================== */
(function (global) {
    'use strict';

    const FALLBACK = '正在梳理本步核验思路。';

    const Thinking = {
        _caret() {
            return Utils.create('span', { class: 'thinking-caret', 'aria-hidden': 'true' });
        },

        _bindToggle(header, block) {
            header.addEventListener('click', () => {
                const open = block.classList.contains('expanded');
                this.setExpanded(block, !open);
            });
        },

        _latinCount(text) {
            return (String(text).match(/[A-Za-z]/g) || []).length;
        },

        _cjkCount(text) {
            return (String(text).match(/[\u4e00-\u9fff]/g) || []).length;
        },

        isMostlyEnglish(text) {
            const s = String(text || '').trim();
            if (!s) return false;
            const latin = this._latinCount(s);
            const cjk = this._cjkCount(s);
            if (cjk >= 8 && cjk >= latin) return false;
            if (cjk === 0 && latin >= 8) return true;
            if (latin >= 8 && latin > cjk * 2) return true;
            return false;
        },

        isTechnical(text) {
            const s = String(text || '').trim();
            if (!s) return true;
            if (/^[{[\s]*["']?[a-z_][a-z0-9_]*["']?\s*[:=]/i.test(s)) return true;
            return /\bhttps?:\/\/|```|\bfunction\s|\btool_call\b|\barguments\b|\berror_code\b|\btraceback\b|\bquote_hash\b|\bchunk_id\b|\bartifact_id\b|\btask_id\b|\bcandidate_id\b|\banalysis_gate\b|\bTrue\b|\bFalse\b|\bNone\b|\bnull\b|\bundefined\b/i.test(s);
        },

        /**
         * 只留下法律人能看懂的中文；英文、JSON、代码与工程字段一律丢掉。
         */
        legalize(text) {
            if (!text) return '';
            let t = String(text);
            t = t.replace(/```[\s\S]*?```/g, '\n');
            t = t.replace(/\{[^{}]*\}/g, '\n');
            t = t.replace(/\[[^[\]]*\]/g, '\n');
            t = t.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '');
            const names = (global.ToolCall && ToolCall.TOOL_LABELS) || {};
            Object.keys(names).sort((a, b) => b.length - a.length).forEach((name) => {
                t = t.replace(new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g'), names[name]);
            });
            const pairs = [
                [/analysis_gate/gi, ''],
                [/ENTITY_REVIEW/g, '实体复核'],
                [/ENTITY_CANDIDATE_SET/g, '跨案对象待核清单'],
                [/ROLE_TIMELINE/g, '事件时间线'],
                [/CLUE_SET|CLUE_ITEM/g, '疑似关联线索'],
                [/REPORT_DRAFT|REPORT_EXPORT/g, '核验单'],
                [/MATERIAL_BATCH/g, '材料批次'],
                [/KEEP_SEPARATE/g, '保留独立'],
                [/PENDING_REVIEW/g, '待人工核验'],
                [/\bMERGE\b/g, '视为同一'],
                [/\bPENDING\b/g, '待确认'],
                [/\bDEFER\b/g, '暂缓']
            ];
            pairs.forEach(([re, label]) => {
                t = t.replace(re, label);
            });
            t = t.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n');

            const kept = [];
            t.split(/\n+/).forEach((line) => {
                const s = line.replace(/\s+/g, ' ').trim();
                if (!s) return;
                if (this.isTechnical(s) || this.isMostlyEnglish(s)) return;
                if (this._cjkCount(s) < 4) return;
                kept.push(s);
            });
            return kept.join('\n');
        },

        create(data) {
            const expanded = data.defaultExpanded === true;
            const block = Utils.create('div', {
                class: expanded ? 'thinking-block expanded' : 'thinking-block compact'
            });

            const header = Utils.create('div', { class: 'thinking-header' }, [
                this._caret(),
                Utils.create('div', { class: 'thinking-title', text: data.title || '分析思路' }),
                Utils.create('div', { class: 'thinking-meta', text: expanded ? '进行中' : '已完成' })
            ]);

            const body = Utils.create('div', { class: 'thinking-body' });
            const content = Utils.create('div', { class: 'thinking-content' });
            (data.steps || []).forEach((step) => {
                const cls = step.status === 'active' ? 'thinking-step active' :
                            step.status === 'done'   ? 'thinking-step done' : 'thinking-step';
                const text = this.legalize(step.text || '');
                if (!text) return;
                content.appendChild(Utils.create('div', { class: cls, text }));
            });
            if (!content.childElementCount) {
                content.appendChild(Utils.create('div', { class: 'thinking-step done', text: '已按办案步骤梳理核验思路。' }));
            }
            body.appendChild(content);
            block.appendChild(header);
            block.appendChild(body);
            this._bindToggle(header, block);
            return block;
        },

        createStep(data) {
            const index = data.index || 1;
            const title = data.title || `第 ${index} 步`;
            const expanded = data.expanded !== false;
            const block = Utils.create('div', {
                class: expanded ? 'analysis-step expanded' : 'analysis-step compact',
                'data-step-index': String(index)
            });
            const header = Utils.create('div', { class: 'analysis-step-header' }, [
                this._caret(),
                Utils.create('div', { class: 'thinking-title', text: title }),
                Utils.create('div', { class: 'thinking-meta analysis-step-meta', text: expanded ? '进行中' : '已完成' })
            ]);
            const body = Utils.create('div', { class: 'analysis-step-body' });
            const thinkContent = Utils.create('div', { class: 'thinking-content analysis-step-think' });
            const toolsHost = Utils.create('div', { class: 'analysis-step-tools' });
            thinkContent.appendChild(Utils.create('div', {
                class: 'thinking-step active',
                text: FALLBACK
            }));
            body.appendChild(thinkContent);
            body.appendChild(toolsHost);
            block.appendChild(header);
            block.appendChild(body);
            this._bindToggle(header, block);
            return block;
        },

        setExpanded(stepEl, expanded) {
            if (!stepEl) return;
            if (expanded) {
                stepEl.classList.add('expanded');
                stepEl.classList.remove('compact');
                this._scrollBottom(Utils.$('.analysis-step-think', stepEl));
            } else {
                stepEl.classList.remove('expanded');
                stepEl.classList.add('compact');
            }
        },

        _scrollBottom(el) {
            if (!el || el.scrollHeight <= el.clientHeight) return;
            const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
            if (nearBottom) el.scrollTop = el.scrollHeight;
        },

        _thinkNode(stepEl) {
            const host = stepEl && Utils.$('.analysis-step-think', stepEl);
            if (!host) return null;
            let last = host.lastElementChild;
            if (!last || !last.classList.contains('thinking-step')) {
                last = Utils.create('div', { class: 'thinking-step active', text: '' });
                host.appendChild(last);
            }
            return last;
        },

        appendStepThinking(stepEl, text) {
            if (!stepEl) return;
            if (text) stepEl._rawThink = (stepEl._rawThink || '') + text;
            const cleaned = this.legalize(stepEl._rawThink || '');
            const node = this._thinkNode(stepEl);
            if (!node) return;
            if (cleaned) {
                node.textContent = cleaned;
                node.setAttribute('data-from-model', '1');
            } else if (node.getAttribute('data-from-model') !== '1') {
                node.textContent = stepEl._hint || FALLBACK;
            }
            this.setExpanded(stepEl, true);
            this._scrollBottom(Utils.$('.analysis-step-think', stepEl));
        },

        ensureHint(stepEl, hint) {
            if (!stepEl || !hint) return;
            stepEl._hint = hint;
            const node = this._thinkNode(stepEl);
            if (node && node.getAttribute('data-from-model') !== '1') {
                node.textContent = hint;
            }
        },

        addToolToStep(stepEl, toolCard) {
            if (!stepEl || !toolCard) return;
            const host = Utils.$('.analysis-step-tools', stepEl);
            if (host) host.appendChild(toolCard);
            this.setExpanded(stepEl, true);
        },

        finishStep(stepEl, summary) {
            if (!stepEl) return;
            const title = Utils.$('.thinking-title', stepEl);
            const meta = Utils.$('.analysis-step-meta', stepEl);
            if (title && summary) title.textContent = summary;
            if (meta) meta.textContent = '已完成';
            this.setExpanded(stepEl, false);
            Utils.$$('.thinking-step.active', stepEl).forEach(s => {
                s.classList.remove('active');
                s.classList.add('done');
            });
        },

        appendText(thinkingEl, text) {
            if (thinkingEl && thinkingEl.classList.contains('analysis-step')) {
                this.appendStepThinking(thinkingEl, text);
                return;
            }
            const steps = Utils.$$('.thinking-step', thinkingEl);
            if (steps.length > 0) {
                const cleaned = this.legalize(steps[steps.length - 1].textContent + text);
                if (cleaned) steps[steps.length - 1].textContent = cleaned;
            }
        },

        updateStep(thinkingEl, stepIndex, status) {
            const steps = Utils.$$('.thinking-step', thinkingEl);
            if (steps[stepIndex]) {
                steps[stepIndex].classList.remove('active', 'done');
                steps[stepIndex].classList.add(status);
            }
        },

        addStep(thinkingEl, text, status = '') {
            const content = Utils.$('.thinking-content', thinkingEl);
            if (content) {
                const cls = status ? `thinking-step ${status}` : 'thinking-step';
                const cleaned = this.legalize(text) || (this.isMostlyEnglish(text) ? '' : text);
                if (cleaned) content.appendChild(Utils.create('div', { class: cls, text: cleaned }));
            }
        },

        setDone(thinkingEl, summary) {
            if (thinkingEl && thinkingEl.classList.contains('analysis-step')) {
                this.finishStep(thinkingEl, summary);
                return;
            }
            const title = Utils.$('.thinking-title', thinkingEl);
            if (title) title.textContent = summary || '本步已完成';
            const meta = Utils.$('.thinking-meta', thinkingEl);
            if (meta) meta.textContent = '完成';
            thinkingEl.classList.remove('expanded');
            thinkingEl.classList.add('compact');
            Utils.$$('.thinking-step.active', thinkingEl).forEach(s => {
                s.classList.remove('active');
                s.classList.add('done');
            });
        }
    };

    global.Thinking = Thinking;
})(window);
