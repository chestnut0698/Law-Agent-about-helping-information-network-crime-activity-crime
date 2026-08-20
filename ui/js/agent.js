/* ========================================
   agent.js — 真实后端耦合版
   从后端 SSE 流接收事件，驱动 UI
   工作台有任务时走任务级 ReAct，否则走会话 /chat
   ======================================== */
(function (global) {
    'use strict';

    const Agent = {
        /** 后端 API 地址，可在 main.js 中配置 */
        apiUrl: '/chat',

        init(options) {
            if (options?.apiUrl) this.apiUrl = options.apiUrl;
        },

        _endpoint() {
            const taskId = global.Workbench && Workbench.task && Workbench.task.id;
            if (taskId) return `/api/tasks/${taskId}/agent/chat`;
            const convId = State.currentConversationId;
            return `${this.apiUrl}/${convId}`;
        },

        /**
         * 主入口：处理用户输入
         */
        async process(userInput) {
            const chatMessages = Utils.$('#chat-messages');
            const welcome = Utils.$('#welcome-screen');
            if (welcome) welcome.remove();

            const userMsg = Message.renderUser(userInput);
            chatMessages.appendChild(userMsg);

            this._showStatus('思考中...', 5);
            State.setAgentState('thinking');

            await this._streamFromBackend(userInput);

            this._hideStatus();
            State.setAgentState('done');
            setTimeout(() => State.setAgentState('idle'), 2000);

            if (global.Workbench && Workbench.task && typeof Workbench.openTask === 'function') {
                try {
                    await Workbench.openTask(Workbench.task.id, Workbench.activeTabId || null);
                } catch (_) { /* ignore */ }
            }
        },

        async _streamFromBackend(userInput) {
            const chatMessages = Utils.$('#chat-messages');
            let currentPlan = null;

            try {
                const response = await fetch(this._endpoint(), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ messages: [{ role: 'user', content: userInput }] })
                });

                if (!response.ok) throw new Error(`HTTP ${response.status}`);

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                let currentThinking = null;
                let currentToolCard = null;
                let assistantWrap = null;
                let assistantContent = null;

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
                                    title: planData.title,
                                    steps: planData.steps.map((s, i) => ({
                                        title: s.title,
                                        description: s.description,
                                        status: i === 0 ? 'running' : 'pending'
                                    }))
                                });
                                chatMessages.appendChild(currentPlan);
                                this._updateProgress(30);
                                break;
                            }
                            case 'thinking': {
                                if (!currentThinking) {
                                    currentThinking = Thinking.create({
                                        title: '思考中...',
                                        steps: [{ text: event.content, status: 'active' }],
                                        defaultExpanded: true
                                    });
                                    chatMessages.appendChild(currentThinking);
                                }
                                Thinking.appendText(currentThinking, event.content || '');
                                this._updateProgress(20);
                                break;
                            }

                            case 'tool_call': {
                                if (currentThinking) {
                                    Thinking.setDone(currentThinking, '思考完成');
                                    currentThinking = null;
                                }
                                const tool = event.tool || {};
                                currentToolCard = ToolCall.create({
                                    type: tool.type === 'search' ? 'search' : 'code',
                                    name: tool.name || '未知工具',
                                    description: '',
                                    params: tool.params || {},
                                    status: 'running',
                                    expanded: true
                                });
                                chatMessages.appendChild(currentToolCard);
                                this._updateProgress(40);
                                break;
                            }

                            case 'tool_result': {
                                const tool = event.tool || {};
                                if (currentToolCard) {
                                    const isSuccess = tool.status === 'success';
                                    ToolCall.updateStatus(
                                        currentToolCard,
                                        isSuccess ? 'success' : 'error',
                                        tool.result || ''
                                    );
                                    currentToolCard = null;
                                }
                                this._maybeArtifactFromToolResult(tool.result);
                                this._updateProgress(60);
                                break;
                            }

                            case 'artifact': {
                                this._postArtifact(event.artifact_id, event.title, event.summary);
                                break;
                            }

                            case 'text_delta': {
                                if (currentThinking) {
                                    Thinking.setDone(currentThinking, '思考完成');
                                    currentThinking = null;
                                }
                                if (!assistantWrap) {
                                    const result = Message.renderAssistantContainer();
                                    assistantWrap = result.wrap;
                                    assistantContent = result.content;
                                    chatMessages.appendChild(assistantWrap);
                                }
                                Message.appendDelta(assistantContent, event.text || '');
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                break;
                            }

                            case 'done': {
                                if (assistantContent && typeof Prism !== 'undefined') {
                                    Prism.highlightAllUnder(assistantWrap);
                                }
                                if (currentPlan) {
                                    const steps = currentPlan.querySelectorAll('.plan-step');
                                    let foundRunning = false;
                                    steps.forEach((step, i) => {
                                        if (step.classList.contains('running') && !foundRunning) {
                                            Plan.updateStep(currentPlan, i, 'completed');
                                            foundRunning = true;
                                            if (i + 1 < steps.length) {
                                                Plan.updateStep(currentPlan, i + 1, 'running');
                                            }
                                        }
                                    });
                                }
                                assistantWrap = null;
                                assistantContent = null;
                                this._updateProgress(90);
                                break;
                            }

                            default:
                                break;
                        }
                    }
                }
            } catch (err) {
                console.error('Agent stream error:', err);
                const { wrap, content } = Message.renderAssistantContainer();
                chatMessages.appendChild(wrap);
                content.innerHTML = `<div class="message-error">⚠️ 请求失败：${err.message}</div>`;
            }
        },

        _maybeArtifactFromToolResult(raw) {
            if (!raw || typeof raw !== 'string') return;
            try {
                const data = JSON.parse(raw);
                if (data && data.artifact_id && data.ok !== false) {
                    this._postArtifact(
                        data.artifact_id,
                        data.title || data.artifact_type,
                        data.message || ''
                    );
                }
            } catch (_) { /* ignore */ }
        },

        _postArtifact(artifactId, title, summary) {
            if (!artifactId) return;
            if (global.Workbench && typeof Workbench._postArtifactCard === 'function') {
                Workbench._postArtifactCard(
                    artifactId,
                    title || '产物',
                    summary || '点击打开中间预览'
                );
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
            if (tokenEl) tokenEl.textContent = `${Math.floor(pct * 35)} tokens`;
        }
    };

    global.Agent = Agent;
})(window);
