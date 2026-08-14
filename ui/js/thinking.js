/* ========================================
   thinking.js — 思考过程面板渲染
   ======================================== */
(function (global) {
    'use strict';

    const Thinking = {
        /**
         * 创建思考过程面板
         * @param {Object} data { title, steps: [{text, status}], defaultExpanded }
         */
        create(data) {
            const block = Utils.create('div', { class: 'thinking-block expanded' });
            if (data.defaultExpanded) block.classList.remove('compact');

            // 头部
            const header = Utils.create('div', { class: 'thinking-header' }, [
                Utils.create('div', { class: 'thinking-icon', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>' }),
                Utils.create('div', { class: 'thinking-title', text: data.title || '思考中...' }),
                Utils.create('div', { class: 'thinking-meta', text: `${data.steps ? data.steps.length : 0} 步` }),
                Utils.create('svg', { class: 'thinking-chevron', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M6 9l6 6 6-6"/>' })
            ]);

            // 内容
            const body = Utils.create('div', { class: 'thinking-body' });
            const content = Utils.create('div', { class: 'thinking-content' });

            (data.steps || []).forEach((step, i) => {
                const cls = step.status === 'active' ? 'thinking-step active' :
                            step.status === 'done'   ? 'thinking-step done' : 'thinking-step';
                content.appendChild(Utils.create('div', { class: cls, text: step.text }));


            });

            body.appendChild(content);
            block.appendChild(header);
            block.appendChild(body);

            // 点击切换
            header.addEventListener('click', () => {
                block.classList.toggle('expanded');
                block.classList.toggle('compact');
            });

            return block;
        },

        appendText(thinkingEl, text) {
            const steps = Utils.$$('.thinking-step', thinkingEl);
            if (steps.length > 0) {
                const lastStep = steps[steps.length - 1];
                lastStep.textContent += text;
            }
        },
        /**
         * 更新思考步骤状态
         */
        updateStep(thinkingEl, stepIndex, status) {
            const steps = Utils.$$('.thinking-step', thinkingEl);
            if (steps[stepIndex]) {
                steps[stepIndex].classList.remove('active', 'done');
                steps[stepIndex].classList.add(status);
            }
        },

        /**
         * 添加一步思考
         */
        addStep(thinkingEl, text, status = '') {
            const content = Utils.$('.thinking-content', thinkingEl);
            if (content) {
                const cls = status ? `thinking-step ${status}` : 'thinking-step';
                content.appendChild(Utils.create('div', { class: cls, text }));
            }
        },

        /**
         * 设置为完成
         */
        setDone(thinkingEl, summary) {
            const title = Utils.$('.thinking-title', thinkingEl);
            if (title) title.textContent = summary || '思考完成';
            const meta = Utils.$('.thinking-meta', thinkingEl);
            if (meta) meta.textContent = '完成';


            // 确保切换到 compact 状态（折叠）
            thinkingEl.classList.remove('expanded');
            thinkingEl.classList.add('compact');
            // 所有步骤标为 done
            Utils.$$('.thinking-step.active', thinkingEl).forEach(s => {
                s.classList.remove('active');
                s.classList.add('done');
            });
        }
    };

    global.Thinking = Thinking;
})(window);
