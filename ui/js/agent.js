/* ========================================
   agent.js — 真实后端耦合版
   计划 → 分步（思考+执行默认折叠）→ 展开回复
   ======================================== */
(function (global) {
    'use strict';

    const PLAN_BY_TOOL = {
        get_task_overview: 0,
        list_case_materials: 0,
        get_material_status: 0,
        refresh_task_materials: 0,
        confirm_task_plan: 1,
        run_task_collision: 2,
        run_task_timeline: 3,
        write_ai_clues: 4,
        read_artifact: 4,
        read_material_chunk: 4
    };

    const Agent = {
        apiUrl: '/chat',

        init(options) {
            if (options?.apiUrl) this.apiUrl = options.apiUrl;
        },

        async process(userInput) {
            const chatMessages = Utils.$('#chat-messages');
            const welcome = Utils.$('#welcome-screen');
            if (welcome) welcome.remove();

            const userMsg = Message.renderUser(userInput);
            chatMessages.appendChild(userMsg);

            this._showStatus('分析中…', 5);
            State.setAgentState('thinking');

            await this._streamFromBackend(userInput);

            this._hideStatus();
            State.setAgentState('done');
            setTimeout(() => State.setAgentState('idle'), 2000);
        },

        async _streamFromBackend(userInput) {
            const chatMessages = Utils.$('#chat-messages');
            const taskId = global.Workbench?.task?.id || State.currentTaskId;
            if (!taskId) {
                console.error('No active task');
                return;
            }

            let assistantWrap = null;
            let assistantContent = null;
            let currentPlan = null;
            let stepIndex = 0;
            let currentStep = null;
            let currentToolCard = null;
            let awaitingNewStep = true;

            const ensureStep = () => {
                if (currentStep && !awaitingNewStep) return currentStep;
                if (currentStep) Thinking.finishStep(currentStep);
                stepIndex += 1;
                currentStep = Thinking.createStep({
                    index: stepIndex,
                    title: `第 ${stepIndex} 步 · 分析中`,
                    expanded: true
                });
                chatMessages.appendChild(currentStep);
                awaitingNewStep = false;
                chatMessages.scrollTop = chatMessages.scrollHeight;
                return currentStep;
            };

            const parseToolParams = (raw) => {
                if (raw == null) return {};
                if (typeof raw === 'object') return raw;
                try { return JSON.parse(raw); } catch { return { raw: String(raw) }; }
            };

            try {
                const response = await fetch(`/chat/${taskId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ messages: [{ role: 'user', content: userInput }] })
                });

                if (!response.ok) throw new Error(`请求失败（${response.status}）`);

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop();

                    for (const line of lines) {
                        if (!line.startsWith('data: ')) continue;
                        const jsonStr = line.slice(6).trim();
                        if (!jsonStr) continue;

                        let event;
                        try {
                            event = JSON.parse(jsonStr);
                        } catch {
                            continue;
                        }

                        switch (event.type) {
                            case 'plan': {
                                const planData = event.plan;
                                currentPlan = Plan.create({
                                    title: planData.title || '分析计划',
                                    steps: (planData.steps || []).map((s, i) => ({
                                        title: s.title,
                                        description: s.description,
                                        status: i === 0 ? 'running' : 'pending'
                                    }))
                                });
                                chatMessages.appendChild(currentPlan);
                                this._showStatus('按计划推进…', 15);
                                this._updateProgress(20);
                                break;
                            }
                            case 'thinking': {
                                ensureStep();
                                Thinking.appendStepThinking(currentStep, event.content || '');
                                this._showStatus('梳理分析思路…', 25);
                                this._updateProgress(30);
                                break;
                            }
                            case 'tool_call': {
                                ensureStep();
                                const tool = event.tool || {};
                                const planIndex = PLAN_BY_TOOL[tool.name];
                                if (currentPlan && planIndex != null && window.Plan) {
                                    Plan.setRunning(currentPlan, planIndex);
                                }
                                const label = ToolCall.displayName(tool.name);
                                const titleEl = Utils.$('.thinking-title', currentStep);
                                if (titleEl) titleEl.textContent = `第 ${stepIndex} 步 · ${label}`;
                                currentToolCard = ToolCall.create({
                                    type: tool.type === 'search' ? 'search' : (tool.type === 'file' ? 'file' : 'db'),
                                    name: tool.name || '',
                                    label,
                                    description: '点击展开查看执行说明',
                                    params: parseToolParams(tool.params),
                                    status: 'running',
                                    expanded: false
                                });
                                Thinking.addToolToStep(currentStep, currentToolCard);
                                this._showStatus(`${label}…`, 45);
                                this._updateProgress(50);
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                break;
                            }
                            case 'tool_result': {
                                const tool = event.tool || {};
                                if (currentToolCard) {
                                    const ok = tool.status !== 'error';
                                    ToolCall.updateStatus(
                                        currentToolCard,
                                        ok ? 'success' : 'error',
                                        tool.result || ''
                                    );
                                    currentToolCard = null;
                                }
                                // 工具结束后，下一轮思考开启新步骤
                                awaitingNewStep = true;
                                this._updateProgress(65);
                                break;
                            }
                            case 'text_delta': {
                                if (currentStep) {
                                    Thinking.finishStep(currentStep, `第 ${stepIndex} 步 · 已完成`);
                                    currentStep = null;
                                    awaitingNewStep = true;
                                }
                                if (!assistantWrap) {
                                    const result = Message.renderAssistantContainer();
                                    assistantWrap = result.wrap;
                                    assistantContent = result.content;
                                    chatMessages.appendChild(assistantWrap);
                                }
                                Message.appendDelta(assistantContent, event.text || '');
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                this._showStatus('整理核验说明…', 80);
                                this._updateProgress(85);
                                break;
                            }
                            case 'error': {
                                if (currentStep) {
                                    Thinking.finishStep(currentStep, `第 ${stepIndex} 步 · 已中断`);
                                    currentStep = null;
                                }
                                const { wrap, content } = Message.renderAssistantContainer();
                                assistantWrap = wrap;
                                assistantContent = content;
                                chatMessages.appendChild(wrap);
                                content.innerHTML = `<div class="message-error">${event.message || '请求未能完成，请稍后重试'}</div>`;
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                break;
                            }
                            case 'done': {
                                if (currentStep) {
                                    Thinking.finishStep(currentStep, `第 ${stepIndex} 步 · 已完成`);
                                }
                                if (currentPlan) {
                                    const steps = currentPlan.querySelectorAll('.plan-step');
                                    steps.forEach((step, i) => {
                                        if (!step.classList.contains('completed')) {
                                            Plan.updateStep(currentPlan, i, 'completed');
                                        }
                                    });
                                }
                                if (global.Workbench && typeof global.Workbench.refreshTask === 'function') {
                                    global.Workbench.refreshTask().catch(() => {});
                                } else if (global.Workbench && global.Workbench.task) {
                                    fetch(`/api/tasks/${taskId}`)
                                        .then(r => r.json())
                                        .then(task => {
                                            if (task && !task.error_code && global.Workbench) {
                                                global.Workbench.task = task;
                                                if (global.Workbench._renderDirectory) {
                                                    global.Workbench._renderDirectory();
                                                }
                                            }
                                        })
                                        .catch(() => {});
                                }
                                this._updateProgress(100);
                                break;
                            }
                            default:
                                break;
                        }
                    }
                }
            } catch (err) {
                console.error('Agent stream error:', err);
                const raw = (err && err.message) ? String(err.message) : '';
                const network = /failed to fetch|networkerror|network error|load failed/i.test(raw);
                const text = network
                    ? '连接已中断。对话记录已自动修复的话，请再发送一次即可继续。'
                    : `请求未能完成：${raw || '请稍后重试'}`;
                const { wrap, content } = Message.renderAssistantContainer();
                chatMessages.appendChild(wrap);
                content.innerHTML = `<div class="message-error">${text}</div>`;
            }
        },

        _showStatus(text, progress) {
            const bar = Utils.$('#status-bar');
            const textEl = Utils.$('#status-bar-text');
            const fill = Utils.$('#progress-fill');
            if (bar) bar.style.display = 'flex';
            if (textEl) textEl.textContent = text;
            if (fill && progress !== undefined) fill.style.width = progress + '%';
        },

        _hideStatus() {
            const bar = Utils.$('#status-bar');
            if (bar) {
                setTimeout(() => {
                    bar.style.display = 'none';
                    const fill = Utils.$('#progress-fill');
                    if (fill) fill.style.width = '0%';
                }, 500);
            }
        },

        _updateProgress(pct) {
            const fill = Utils.$('#progress-fill');
            if (fill) fill.style.width = Math.min(100, pct) + '%';
            const tokenEl = Utils.$('#token-count');
            if (tokenEl) tokenEl.textContent = `进度 ${Math.min(100, Math.floor(pct))}%`;
        }
    };

    global.Agent = Agent;
})(window);
