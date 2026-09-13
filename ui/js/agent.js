/* ========================================
   agent.js — 真实后端耦合版
   计划 → 分步（思考+执行默认折叠）→ 展开回复 → 页面跳转
   实体复核 / 线索核验停等：定时检测，全部确认后自动续跑
   ======================================== */
(function (global) {
    'use strict';

    const PLAN_BY_TOOL = {
        get_task_overview: 0,
        list_case_materials: 0,
        get_material_status: 0,
        refresh_task_materials: 0,
        confirm_task_plan: 0,
        run_task_collision: 1,
        put_task_entity_candidate: 1,
        compare_material_images: 1,
        run_task_timeline: 3,
        list_association_hints: 4,
        write_ai_clues: 4,
        put_task_clue: 4,
        list_task_clues: 4,
        read_artifact: 4,
        read_material_chunk: 4,
        write_report: 5,
        read_report: 5
    };
    const PLAN_ENTITY_REVIEW_INDEX = 2;
    const PLAN_CLUE_REVIEW_INDEX = 5;

    const JUMP_BY_TOOL = {
        run_task_collision: { view: 'entities', label: '去实体复核' },
        put_task_entity_candidate: { view: 'entities', label: '去实体复核' },
        compare_material_images: { view: 'entities', label: '去实体复核' },
        put_task_clue: { view: 'leads', label: '去线索中心' },
        write_ai_clues: { view: 'leads', label: '去线索中心' },
        delete_task_clue: { view: 'leads', label: '去线索中心' },
        list_task_clues: { view: 'leads', label: '去线索中心' },
        run_task_timeline: { view: 'timeline', label: '去角色时间线' },
        write_report: { view: 'reports', label: '去报告与审计' },
        read_report: { view: 'reports', label: '去报告与审计' },
        refresh_task_materials: { view: 'materials', label: '去材料中心' },
        list_case_materials: { view: 'materials', label: '去材料中心' },
        delete_task_material: { view: 'materials', label: '去材料中心' }
    };

    const TEXT_JUMPS = [
        { re: /实体复核|对象待核|视为同一|保留独立/, view: 'entities', label: '去实体复核' },
        { re: /线索中心|疑似关联线索|待核线索/, view: 'leads', label: '去线索中心' },
        { re: /时间线/, view: 'timeline', label: '去角色时间线' },
        { re: /图谱|关系图/, view: 'graph', label: '去链条图谱' },
        { re: /核验单|撰写报告|报告与审计/, view: 'reports', label: '去报告与审计' },
        { re: /材料中心|卷宗材料/, view: 'materials', label: '去材料中心' }
    ];

    const LOOKUP_TOOLS = {
        get_task_overview: 1,
        read_artifact: 1,
        list_association_hints: 1,
        read_material_chunk: 1,
        list_task_clues: 1,
        read_report: 1
    };

    const CONTINUE_AFTER_ENTITY = [
        '【系统续跑】实体复核已全部确认完毕。请继续下一步分析：',
        '若事件时间线尚未整理则先整理时间线，再形成可回原文核验的疑似关联线索。',
        '线索写完后必须停步，请用户到中间工作区线索中心逐条核验；全部处置完成前不要撰写报告。',
        '不要重复跨案标识比对。用办案口吻说明结果在哪一页查看。',
        '禁止定罪、并案或量刑结论。'
    ].join('');

    const CONTINUE_AFTER_CLUES = [
        '【系统续跑】关联线索已全部核验完毕。请继续撰写《跨案关联线索核验单》。',
        '先读取报告素材，再提交正文。不要重复跨案标识比对，也不要重写已处置的线索。',
        '用办案口吻说明核验单在哪一页查看。禁止定罪、并案或量刑结论。'
    ].join('');

    const Agent = {
        apiUrl: '/chat',
        _busy: false,
        _wait: null,

        init(options) {
            if (options?.apiUrl) this.apiUrl = options.apiUrl;
        },

        collectJumps(toolNames, replyText, extra) {
            const jumps = [];
            const seen = new Set();
            const add = (item) => {
                if (!item || !item.view || seen.has(item.view)) return;
                seen.add(item.view);
                jumps.push({ view: item.view, label: item.label });
            };
            (toolNames || []).forEach((name) => {
                if (LOOKUP_TOOLS[name]) return;
                add(JUMP_BY_TOOL[name]);
            });
            if (extra && extra.entityReview) add({ view: 'entities', label: '去实体复核' });
            if (extra && extra.clueReview) add({ view: 'leads', label: '去线索中心' });
            const text = String(replyText || '');
            TEXT_JUMPS.forEach((item) => {
                if (item.re.test(text)) add(item);
            });
            return jumps;
        },

        async process(userInput, options) {
            const silent = !!(options && options.silent);
            if (!silent) this.stopReviewWait({ silent: true });

            const chatMessages = Utils.$('#chat-messages');
            const welcome = Utils.$('#welcome-screen');
            if (welcome) welcome.remove();

            if (silent) {
                chatMessages.appendChild(Utils.create('div', {
                    class: 'agent-continue-note',
                    text: (options && options.note) || '核验已完成，助手继续分析…'
                }));
            } else {
                chatMessages.appendChild(Message.renderUser(userInput));
            }

            this._busy = true;
            this._showStatus('分析中…', 5);
            State.setAgentState('thinking');

            try {
                await this._streamFromBackend(userInput);
            } finally {
                this._busy = false;
                this._hideStatus();
                State.setAgentState('done');
                setTimeout(() => {
                    if (!this._busy) State.setAgentState('idle');
                }, 2000);
            }
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
            let lastToolName = '';
            let sameStep = null;
            let sameCard = null;
            let sameCount = 0;
            const turnTools = [];
            let turnText = '';
            let entityReviewGate = false;
            let clueReviewGate = false;

            const closeStreak = () => {
                if (sameCard && (sameCard._current || 1) > 1 && !(sameCard._total > 1)) {
                    ToolCall.setCount(sameCard, sameCard._current, sameCard._current, 'success');
                }
            };

            const finishCurrent = (interrupted) => {
                closeStreak();
                if (!currentStep) return;
                const label = lastToolName ? ToolCall.displayName(lastToolName) : '已完成';
                Thinking.finishStep(
                    currentStep,
                    interrupted ? `第 ${stepIndex} 步 · 已中断` : `第 ${stepIndex} 步 · ${label}`
                );
                currentStep = null;
                awaitingNewStep = true;
                lastToolName = '';
                sameStep = null;
                sameCard = null;
                sameCount = 0;
            };

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

            const parseToolPayload = (raw) => {
                if (raw == null || raw === '') return null;
                if (typeof raw === 'object') return raw;
                try { return JSON.parse(raw); } catch { return null; }
            };

            const maybeStartWait = () => {
                const startClue = () => this._readClueUndisposed(taskId).then((state) => {
                    if (state.waiting) this.startReviewWait(taskId, 'CLUE_REVIEW');
                });
                if (entityReviewGate) {
                    this._readEntityUnconfirmed(taskId).then((state) => {
                        if (state.waiting) this.startReviewWait(taskId, 'ENTITY_REVIEW');
                        else if (clueReviewGate) return startClue();
                    }).catch(() => {});
                    return;
                }
                if (clueReviewGate) startClue().catch(() => {});
            };

            const finishReplyChrome = () => {
                if (assistantWrap) {
                    const jumps = this.collectJumps(turnTools, turnText, {
                        entityReview: entityReviewGate,
                        clueReview: clueReviewGate
                    });
                    Message.attachJumps(assistantWrap, jumps);
                }
                maybeStartWait();
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
                                    title: planData.title || '执行计划',
                                    steps: (planData.steps || []).map((s, i) => ({
                                        title: s.title,
                                        description: s.description,
                                        status: s.status || (i === 0 ? 'running' : 'pending')
                                    }))
                                });
                                chatMessages.appendChild(currentPlan);
                                this._showStatus('按计划推进…', 15);
                                this._updateProgress(20);
                                break;
                            }
                            case 'thinking': {
                                if (!currentStep) ensureStep();
                                else Thinking.setExpanded(currentStep, true);
                                Thinking.appendStepThinking(currentStep, event.content || '');
                                this._showStatus('梳理分析思路…', 25);
                                this._updateProgress(30);
                                break;
                            }
                            case 'tool_call': {
                                const tool = event.tool || {};
                                const name = tool.name || '';
                                if (name) turnTools.push(name);
                                const planIndex = PLAN_BY_TOOL[name];
                                if (currentPlan && planIndex != null && window.Plan) {
                                    Plan.setRunning(currentPlan, planIndex);
                                }
                                const label = ToolCall.displayName(name);
                                const batchIndex = Number(tool.batch_index || 0);
                                const batchTotal = Number(tool.batch_total || 0);
                                if (name && lastToolName === name && sameCard && sameStep) {
                                    currentStep = sameStep;
                                    awaitingNewStep = false;
                                    sameCount = batchIndex > 0 ? batchIndex : sameCount + 1;
                                    const total = batchTotal > 1 ? batchTotal : (sameCard._total || 0);
                                    ToolCall.setCount(sameCard, sameCount, total, 'running');
                                    currentToolCard = sameCard;
                                    Thinking.setExpanded(sameStep, true);
                                    const meta = Utils.$('.analysis-step-meta', sameStep);
                                    if (meta) {
                                        meta.textContent = total > 1
                                            ? `${sameCount}/${total}`
                                            : (sameCount > 1 ? String(sameCount) : '进行中');
                                    }
                                    this._showStatus(
                                        total > 1 ? `${label} ${sameCount}/${total}` : `${label}…`,
                                        45
                                    );
                                } else {
                                    closeStreak();
                                    if (currentStep && lastToolName) {
                                        Thinking.finishStep(
                                            currentStep,
                                            `第 ${stepIndex} 步 · ${ToolCall.displayName(lastToolName)}`
                                        );
                                        awaitingNewStep = true;
                                    }
                                    ensureStep();
                                    lastToolName = name;
                                    sameCount = batchIndex > 0 ? batchIndex : 1;
                                    const total = batchTotal > 1 ? batchTotal : 0;
                                    const titleEl = Utils.$('.thinking-title', currentStep);
                                    if (titleEl) titleEl.textContent = `第 ${stepIndex} 步 · ${label}`;
                                    Thinking.ensureHint(currentStep, ToolCall.hintFor(name));
                                    currentToolCard = ToolCall.create({
                                        name,
                                        label,
                                        current: sameCount,
                                        total,
                                        status: 'running'
                                    });
                                    Thinking.addToolToStep(currentStep, currentToolCard);
                                    sameStep = currentStep;
                                    sameCard = currentToolCard;
                                    this._showStatus(
                                        total > 1 ? `${label} ${sameCount}/${total}` : `${label}…`,
                                        45
                                    );
                                }
                                this._updateProgress(50);
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                break;
                            }
                            case 'tool_result': {
                                const tool = event.tool || {};
                                const payload = parseToolPayload(tool.result);
                                if (payload && (
                                    payload.analysis_gate === 'ENTITY_REVIEW'
                                    || payload.blocked_by_gate === 'ENTITY_REVIEW'
                                    || (payload.pending_entity_reviews != null && Number(payload.pending_entity_reviews) > 0)
                                )) {
                                    entityReviewGate = true;
                                    if (currentPlan && window.Plan) {
                                        Plan.setRunning(currentPlan, PLAN_ENTITY_REVIEW_INDEX);
                                    }
                                }
                                if (payload && (
                                    payload.analysis_gate === 'CLUE_REVIEW'
                                    || payload.blocked_by_gate === 'CLUE_REVIEW'
                                    || (payload.pending_clue_reviews != null && Number(payload.pending_clue_reviews) > 0)
                                )) {
                                    clueReviewGate = true;
                                    if (currentPlan && window.Plan) {
                                        Plan.setRunning(currentPlan, PLAN_CLUE_REVIEW_INDEX);
                                    }
                                }
                                if (sameCard) {
                                    const ok = tool.status !== 'error';
                                    ToolCall.setCount(
                                        sameCard,
                                        sameCount,
                                        sameCard._total || 0,
                                        ok ? 'success' : 'error'
                                    );
                                }
                                this._updateProgress(65);
                                break;
                            }
                            case 'text_delta': {
                                finishCurrent();
                                if (!assistantWrap) {
                                    const result = Message.renderAssistantContainer();
                                    assistantWrap = result.wrap;
                                    assistantContent = result.content;
                                    chatMessages.appendChild(assistantWrap);
                                }
                                turnText += event.text || '';
                                Message.appendDelta(assistantContent, event.text || '');
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                this._showStatus('整理核验说明…', 80);
                                this._updateProgress(85);
                                break;
                            }
                            case 'error': {
                                finishCurrent(true);
                                const { wrap, content } = Message.renderAssistantContainer();
                                assistantWrap = wrap;
                                assistantContent = content;
                                chatMessages.appendChild(wrap);
                                content.innerHTML = `<div class="message-error">${event.message || '请求未能完成，请稍后重试'}</div>`;
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                                break;
                            }
                            case 'done': {
                                finishCurrent();
                                if (currentPlan) {
                                    const steps = currentPlan.querySelectorAll('.plan-step');
                                    if (entityReviewGate && window.Plan) {
                                        steps.forEach((step, i) => {
                                            if (i < PLAN_ENTITY_REVIEW_INDEX) {
                                                Plan.updateStep(currentPlan, i, 'completed');
                                            }
                                        });
                                        Plan.updateStep(currentPlan, PLAN_ENTITY_REVIEW_INDEX, 'running');
                                    } else if (clueReviewGate && window.Plan) {
                                        steps.forEach((step, i) => {
                                            if (i < PLAN_CLUE_REVIEW_INDEX) {
                                                Plan.updateStep(currentPlan, i, 'completed');
                                            }
                                        });
                                        Plan.updateStep(currentPlan, PLAN_CLUE_REVIEW_INDEX, 'running');
                                    } else {
                                        steps.forEach((step, i) => {
                                            if (!step.classList.contains('completed')) {
                                                Plan.updateStep(currentPlan, i, 'completed');
                                            }
                                        });
                                    }
                                }
                                if (!entityReviewGate && /实体复核|视为同一|保留独立/.test(turnText)) {
                                    entityReviewGate = true;
                                }
                                if (!clueReviewGate && /线索中心|确认关联|待核线索/.test(turnText)) {
                                    clueReviewGate = true;
                                }
                                finishReplyChrome();
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
                if (assistantWrap && !Utils.$('.agent-jump-row', assistantWrap)) {
                    const jumps = this.collectJumps(turnTools, turnText, {
                        entityReview: entityReviewGate,
                        clueReview: clueReviewGate
                    });
                    Message.attachJumps(assistantWrap, jumps);
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

        _waitKey(taskId) {
            return `lz-agent-wait:${taskId}`;
        },

        _readWaitFlag(taskId) {
            try {
                const raw = sessionStorage.getItem(this._waitKey(taskId));
                if (!raw) return null;
                const parsed = JSON.parse(raw);
                if (parsed && parsed.kind) return parsed;
                return { kind: 'ENTITY_REVIEW' };
            } catch (_) {
                return null;
            }
        },

        _hasWaitFlag(taskId) {
            return !!this._readWaitFlag(taskId);
        },

        _setWaitFlag(taskId, on, kind) {
            try {
                if (on) {
                    sessionStorage.setItem(
                        this._waitKey(taskId),
                        JSON.stringify({ kind: kind || 'ENTITY_REVIEW' })
                    );
                } else {
                    sessionStorage.removeItem(this._waitKey(taskId));
                }
            } catch (_) { /* ignore */ }
        },

        _waitCopy(kind) {
            if (kind === 'CLUE_REVIEW') {
                return {
                    idle: '正在等待您完成线索核验。全部处置后将自动继续撰写核验单。',
                    remain: (n) => `正在等待您完成线索核验（尚余 ${n} 条）。全部处置后将自动继续。`,
                    done: '线索已全部核验，即将继续撰写核验单…',
                    note: '线索已全部核验，助手继续撰写核验单…'
                };
            }
            return {
                idle: '正在等待您完成实体复核。全部确认后将自动继续。',
                remain: (n) => `正在等待您完成实体复核（尚余 ${n} 条）。全部确认后将自动继续。`,
                done: '实体复核已全部确认，即将继续分析…',
                note: '实体复核已全部确认，助手继续分析…'
            };
        },

        startReviewWait(taskId, kind) {
            const waitKind = kind || 'ENTITY_REVIEW';
            if (!taskId) return;
            if (this._wait && this._wait.taskId === taskId && this._wait.timer && this._wait.kind === waitKind) {
                this._setWaitFlag(taskId, true, waitKind);
                return;
            }
            this.stopReviewWait({ silent: true });
            this._setWaitFlag(taskId, true, waitKind);
            const copy = this._waitCopy(waitKind);
            const chatMessages = Utils.$('#chat-messages');
            const el = Utils.create('div', { class: 'agent-wait-line' });
            const text = Utils.create('span', { class: 'agent-wait-text', text: copy.idle });
            const cancel = Utils.create('button', { type: 'button', class: 'agent-wait-cancel', text: '取消自动继续' });
            cancel.addEventListener('click', () => this.stopReviewWait());
            el.appendChild(text);
            el.appendChild(cancel);
            if (chatMessages) {
                chatMessages.appendChild(el);
                chatMessages.scrollTop = chatMessages.scrollHeight;
            }
            this._wait = { taskId, kind: waitKind, el, text, timer: null, continuing: false };
            this._pollReviewWait();
            this._wait.timer = setInterval(() => this._pollReviewWait(), 4000);
        },

        stopReviewWait(options) {
            const wait = this._wait;
            if (wait && wait.timer) {
                clearInterval(wait.timer);
                wait.timer = null;
            }
            if (wait && wait.el && wait.el.parentNode) wait.el.remove();
            if (wait && wait.taskId && !(options && options.keepFlag)) {
                this._setWaitFlag(wait.taskId, false);
            }
            this._wait = null;
            if (wait && !(options && options.silent) && window.Toast) {
                Toast.info('已取消自动继续，确认完成后可再让助手往下做');
            }
        },

        notifyReviewState(info) {
            const kind = (info && info.kind) || 'ENTITY_REVIEW';
            if (!this._wait || this._wait.kind !== kind) return;
            const pending = Number((info && info.pending) != null ? info.pending : -1);
            if (pending >= 0) this._updateWaitLine(pending);
            if (pending === 0) this._continueAfterReview();
        },

        async syncReviewWait(task, historyRows) {
            const taskId = task && task.id;
            if (!taskId) return;
            this.stopReviewWait({ silent: true, keepFlag: true });
            const flag = this._readWaitFlag(taskId);
            const entityContinued = this._historyHasContinue(historyRows, 'ENTITY_REVIEW');
            const clueContinued = this._historyHasContinue(historyRows, 'CLUE_REVIEW');
            const entityState = await this._readEntityUnconfirmed(taskId);
            const clueState = await this._readClueUndisposed(taskId);
            const asksEntity = this._historyAsksEntityReview(historyRows);
            const asksClue = this._historyAsksClueReview(historyRows);

            if (flag && flag.kind === 'ENTITY_REVIEW') {
                if (entityContinued) {
                    this._setWaitFlag(taskId, false);
                } else if (entityState.waiting) {
                    this.startReviewWait(taskId, 'ENTITY_REVIEW');
                    this._updateWaitLine(entityState.pending);
                    return;
                } else {
                    this._wait = { taskId, kind: 'ENTITY_REVIEW', el: null, text: null, timer: null, continuing: false };
                    this._continueAfterReview();
                    return;
                }
            } else if (flag && flag.kind === 'CLUE_REVIEW') {
                if (clueContinued) {
                    this._setWaitFlag(taskId, false);
                    return;
                }
                if (clueState.waiting) {
                    this.startReviewWait(taskId, 'CLUE_REVIEW');
                    this._updateWaitLine(clueState.pending);
                    return;
                }
                this._wait = { taskId, kind: 'CLUE_REVIEW', el: null, text: null, timer: null, continuing: false };
                this._continueAfterReview();
                return;
            }

            if (entityState.waiting && asksEntity && !entityContinued) {
                this.startReviewWait(taskId, 'ENTITY_REVIEW');
                this._updateWaitLine(entityState.pending);
                return;
            }
            if (clueState.waiting && asksClue && !clueContinued) {
                this.startReviewWait(taskId, 'CLUE_REVIEW');
                this._updateWaitLine(clueState.pending);
            }
        },

        _historyAsksEntityReview(rows) {
            if (!Array.isArray(rows)) return false;
            for (let i = rows.length - 1; i >= 0; i -= 1) {
                const row = rows[i];
                if (!row || row.role !== 'assistant') continue;
                if (Array.isArray(row.tool_calls) && row.tool_calls.length) continue;
                const text = String(row.content || '');
                if (!text.trim()) continue;
                return /实体复核|视为同一|保留独立|对象待核/.test(text);
            }
            return false;
        },

        _historyAsksClueReview(rows) {
            if (!Array.isArray(rows)) return false;
            for (let i = rows.length - 1; i >= 0; i -= 1) {
                const row = rows[i];
                if (!row || row.role !== 'assistant') continue;
                if (Array.isArray(row.tool_calls) && row.tool_calls.length) continue;
                const text = String(row.content || '');
                if (!text.trim()) continue;
                return /线索中心|待核线索|确认关联|排除或待补/.test(text);
            }
            return false;
        },

        _historyHasContinue(rows, kind) {
            if (!Array.isArray(rows)) return false;
            const prefix = kind === 'CLUE_REVIEW'
                ? '【系统续跑】关联线索已全部核验'
                : '【系统续跑】实体复核已全部确认';
            for (let i = rows.length - 1; i >= 0; i -= 1) {
                const row = rows[i];
                if (!row) continue;
                if (row.role === 'user' && String(row.content || '').startsWith(prefix)) {
                    return rows.slice(i + 1).some((item) => item.role === 'assistant' && String(item.content || '').trim());
                }
            }
            return false;
        },

        async _readEntityUnconfirmed(taskId) {
            const id = taskId || (global.Workbench && global.Workbench.task && global.Workbench.task.id);
            if (!id) return { waiting: false, pending: 0 };
            if (global.Workbench && typeof global.Workbench._entityUnconfirmedCount === 'function'
                && global.Workbench.task && global.Workbench.task.id === id) {
                const cached = global.Workbench._entityUnconfirmedCount();
                if (cached != null && cached >= 0) {
                    return { waiting: cached > 0, pending: cached };
                }
            }
            try {
                const resp = await fetch(`/api/tasks/${id}`);
                const task = await resp.json();
                if (!task || task.error_code) return { waiting: false, pending: 0 };
                if (global.Workbench && global.Workbench.task && global.Workbench.task.id === id) {
                    global.Workbench.task = task;
                }
                const art = (task.artifacts || []).find((item) =>
                    item.type === 'ENTITY_CANDIDATE_SET' && item.status !== 'INVALID' && item.status !== 'STALE'
                );
                if (!art) return { waiting: false, pending: 0 };
                if (art.status === 'PENDING_REVIEW') {
                    return { waiting: true, pending: -1 };
                }
                const detail = await fetch(`/api/tasks/${id}/artifacts/${art.id}`);
                const data = await detail.json();
                const pending = this._countUnconfirmed(data && data.payload);
                return { waiting: pending > 0, pending };
            } catch (_) {
                return { waiting: false, pending: 0 };
            }
        },

        async _readClueUndisposed(taskId) {
            const id = taskId || (global.Workbench && global.Workbench.task && global.Workbench.task.id);
            if (!id) return { waiting: false, pending: 0 };
            try {
                const resp = await fetch(`/api/tasks/${id}`);
                const task = await resp.json();
                if (!task || task.error_code) return { waiting: false, pending: 0 };
                const art = (task.artifacts || []).find((item) =>
                    item.type === 'CLUE_SET' && item.status !== 'INVALID' && item.status !== 'STALE'
                );
                if (!art) return { waiting: false, pending: 0 };
                const detail = await fetch(`/api/tasks/${id}/artifacts/${art.id}`);
                const data = await detail.json();
                const payload = data && data.payload;
                const summary = (payload && payload.summary) || {};
                const total = Number(summary.total || ((payload && payload.items) || []).length || 0);
                if (!Object.prototype.hasOwnProperty.call(summary, 'pending')) {
                    return { waiting: total > 0, pending: total > 0 ? -1 : 0 };
                }
                const pending = Number(summary.pending || 0);
                return { waiting: total > 0 && pending > 0, pending };
            } catch (_) {
                return { waiting: false, pending: 0 };
            }
        },

        _countUnconfirmed(payload) {
            const candidates = (payload && payload.candidates) || [];
            if (candidates.length) {
                return candidates.filter((item) => {
                    const decision = item.decision;
                    return !decision || decision === 'PENDING' || decision === 'DEFER';
                }).length;
            }
            return Number((payload && payload.summary && payload.summary.pending) || 0);
        },

        _updateWaitLine(pending) {
            if (!this._wait || !this._wait.text) return;
            const copy = this._waitCopy(this._wait.kind);
            if (pending == null || pending < 0) {
                this._wait.text.textContent = copy.idle;
                return;
            }
            this._wait.text.textContent = pending > 0 ? copy.remain(pending) : copy.done;
        },

        async _pollReviewWait() {
            if (!this._wait || this._busy) return;
            const kind = this._wait.kind;
            const state = kind === 'CLUE_REVIEW'
                ? await this._readClueUndisposed(this._wait.taskId)
                : await this._readEntityUnconfirmed(this._wait.taskId);
            this._updateWaitLine(state.pending);
            if (!state.waiting) this._continueAfterReview();
        },

        async _continueAfterReview() {
            if (!this._wait || this._wait.continuing || this._busy) return;
            const taskId = this._wait.taskId;
            const kind = this._wait.kind || 'ENTITY_REVIEW';
            const copy = this._waitCopy(kind);
            this._wait.continuing = true;
            this.stopReviewWait({ silent: true });
            this._setWaitFlag(taskId, false);
            const prompt = kind === 'CLUE_REVIEW' ? CONTINUE_AFTER_CLUES : CONTINUE_AFTER_ENTITY;
            try {
                await this.process(prompt, {
                    silent: true,
                    note: copy.note
                });
            } catch (err) {
                console.error('Agent auto-continue error:', err);
                if (window.Toast) Toast.error('自动继续未能完成，请在对话框里让助手继续');
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
