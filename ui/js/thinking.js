/* ========================================
   thinking.js — 分析步骤 / 思考过程（默认折叠）
   ======================================== */
(function (global) {
    'use strict';

    const Thinking = {
        /**
         * 兼容旧接口：单块思考面板
         */
        create(data) {
            const expanded = data.defaultExpanded === true;
            const block = Utils.create('div', {
                class: expanded ? 'thinking-block expanded' : 'thinking-block compact'
            });

            const header = Utils.create('div', { class: 'thinking-header' }, [
                Utils.create('div', { class: 'thinking-icon', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>' }),
                Utils.create('div', { class: 'thinking-title', text: data.title || '分析中…' }),
                Utils.create('div', { class: 'thinking-meta', text: `${data.steps ? data.steps.length : 0} 项` }),
                Utils.create('svg', { class: 'thinking-chevron', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M6 9l6 6 6-6"/>' })
            ]);

            const body = Utils.create('div', { class: 'thinking-body' });
            const content = Utils.create('div', { class: 'thinking-content' });
            (data.steps || []).forEach((step) => {
                const cls = step.status === 'active' ? 'thinking-step active' :
                            step.status === 'done'   ? 'thinking-step done' : 'thinking-step';
                content.appendChild(Utils.create('div', { class: cls, text: step.text }));
            });
            body.appendChild(content);
            block.appendChild(header);
            block.appendChild(body);
            header.addEventListener('click', () => {
                block.classList.toggle('expanded');
                block.classList.toggle('compact');
            });
            return block;
        },

        /**
         * 分步卡片：思考 + 执行同卡；进行中默认展开，完成后折叠
         */
        createStep(data) {
            const index = data.index || 1;
            const title = data.title || `第 ${index} 步`;
            const expanded = data.expanded !== false;
            const block = Utils.create('div', {
                class: expanded ? 'analysis-step expanded' : 'analysis-step compact',
                'data-step-index': String(index)
            });
            const header = Utils.create('div', { class: 'analysis-step-header' }, [
                Utils.create('div', { class: 'thinking-icon', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>' }),
                Utils.create('div', { class: 'thinking-title', text: title }),
                Utils.create('div', { class: 'thinking-meta analysis-step-meta', text: expanded ? '进行中' : '已完成' }),
                Utils.create('svg', { class: 'thinking-chevron', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M6 9l6 6 6-6"/>' })
            ]);
            const body = Utils.create('div', { class: 'analysis-step-body' });
            const thinkLabel = Utils.create('div', { class: 'analysis-step-label', text: '分析思路' });
            const thinkContent = Utils.create('div', { class: 'thinking-content analysis-step-think' });
            const toolsLabel = Utils.create('div', { class: 'analysis-step-label', text: '执行动作' });
            const toolsHost = Utils.create('div', { class: 'analysis-step-tools' });
            body.appendChild(thinkLabel);
            body.appendChild(thinkContent);
            body.appendChild(toolsLabel);
            body.appendChild(toolsHost);
            block.appendChild(header);
            block.appendChild(body);
            header.addEventListener('click', () => {
                block.classList.toggle('expanded');
                block.classList.toggle('compact');
            });
            return block;
        },

        setExpanded(stepEl, expanded) {
            if (!stepEl) return;
            if (expanded) {
                stepEl.classList.add('expanded');
                stepEl.classList.remove('compact');
            } else {
                stepEl.classList.remove('expanded');
                stepEl.classList.add('compact');
            }
        },

        appendStepThinking(stepEl, text) {
            if (!stepEl || !text) return;
            const host = Utils.$('.analysis-step-think', stepEl);
            if (!host) return;
            let last = host.lastElementChild;
            if (!last || !last.classList.contains('thinking-step')) {
                last = Utils.create('div', { class: 'thinking-step active', text: '' });
                host.appendChild(last);
            }
            last.textContent += text;
            this.setExpanded(stepEl, true);
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
                steps[steps.length - 1].textContent += text;
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
                content.appendChild(Utils.create('div', { class: cls, text }));
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
