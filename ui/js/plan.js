/* ========================================
   plan.js — 执行计划视图渲染
   ======================================== */
(function (global) {
    'use strict';

    const Plan = {
        /**
         * 创建执行计划视图
         * @param {Object} data {
         *   title: string,
         *   steps: [{ title, description, status }]
         * }
         */
        create(data) {
            const container = Utils.create('div', { class: 'plan-container' });

            const header = Utils.create('div', { class: 'plan-header' }, [
                Utils.create('svg', { class: 'plan-header-icon', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>' }),
                Utils.create('span', { class: 'plan-title', text: data.title || '执行计划' }),
                Utils.create('span', { class: 'plan-count', text: `${data.steps.filter(s => s.status === 'completed').length}/${data.steps.length}` })
            ]);

            const stepsEl = Utils.create('div', { class: 'plan-steps' });

            data.steps.forEach((step, i) => {
                const stepClass = `plan-step ${step.status || 'pending'}`;
                const indicatorClass = step.status === 'running' ? 'step-indicator running' :
                                      step.status === 'completed' ? 'step-indicator completed' :
                                      'step-indicator pending';

                const indicatorContent = step.status === 'completed'
                    ? Utils.create('svg', { class: 'check-svg', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '3', html: '<path d="M20 6L9 17l-5-5"/>' })
                    : Utils.create('span', { text: String(i + 1) });

                stepsEl.appendChild(Utils.create('div', { class: stepClass }, [
                    Utils.create('div', { class: indicatorClass }, [indicatorContent]),
                    Utils.create('div', { class: 'step-content' }, [
                        Utils.create('div', { class: 'step-title', text: step.title }),
                        step.description ? Utils.create('div', { class: 'step-desc', text: step.description }) : null
                    ].filter(Boolean))
                ]));
            });

            container.appendChild(header);
            container.appendChild(stepsEl);
            return container;
        },

        /**
         * 更新某一步的状态
         */
        updateStep(planEl, index, status) {
            const steps = Utils.$$('.plan-step', planEl);
            if (!steps[index]) return;

            const step = steps[index];
            step.classList.remove('pending', 'running', 'completed');
            step.classList.add(status);

            const indicator = Utils.$('.step-indicator', step);
            if (indicator) {
                indicator.className = `step-indicator ${status}`;
                if (status === 'completed') {
                    indicator.innerHTML = '<svg class="check-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg>';
                } else if (status === 'running') {
                    indicator.textContent = String(index + 1);
                }
            }

            // 更新计数
            const container = Utils.$('.plan-container', planEl.parentElement) || planEl.parentElement;
            const total = Utils.$$('.plan-step', container).length;
            const completed = Utils.$$('.plan-step.completed', container).length;
            const count = Utils.$('.plan-count', container);
            if (count) count.textContent = `${completed}/${total}`;
        },

        /**
         * 设置当前运行步骤
         */
        setRunning(planEl, index) {
            // 先把之前的 running 改为 completed
            const prevRunning = Utils.$('.plan-step.running', planEl);
            if (prevRunning) {
                const idx = Utils.$$('.plan-step', planEl).indexOf(prevRunning);
                if (idx >= 0 && idx < index) {
                    this.updateStep(planEl, idx, 'completed');
                }
            }
            this.updateStep(planEl, index, 'running');
        }
    };

    global.Plan = Plan;
})(window);
