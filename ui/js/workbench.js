/* ========================================
   workbench.js — 监督分析任务工作台
   负责：任务列表、范围设置、计划确认、任务目录、产物标签页、材料进度。
   产物同一性：目录节点与智能体消息中的链接都通过 artifact_id 打开同一对象。
   ======================================== */
(function (global) {
    'use strict';

    const STAGE_ORDER = ['UPLOADED', 'PARSING', 'PARSED'];
    const STAGE_TEXT = {
        UPLOADED: '排队中',
        PARSING: '解析中',
        PARSED: '可用于分析',
        NEEDS_OCR_REVIEW: '扫描件需人工看清',
        OCR_FAILED: '文字识别失败',
        DUPLICATE_PENDING: '疑似重复，待处理',
        FAILED: '处理失败',
        DELETED: '已删除'
    };
    const ATTENTION = ['NEEDS_OCR_REVIEW', 'OCR_FAILED', 'FAILED', 'DUPLICATE_PENDING'];

    const Workbench = {
        task: null,
        tabs: [],
        activeTabId: null,
        pollTimer: null,
        currentView: 'tasks',
        selectedEntityId: null,
        selectedLeadId: null,
        selectedGraphNodeId: null,
        materialsQuery: '',
        materialsCaseFilter: 'all',
        leadsQuery: '',
        leadsStatusFilter: 'all',
        artifactCache: {},

        async init() {
            this._injectNavIcons();
            this._bindShell();
            this._bindScopeForm();
            await this.loadTasks();
            if (window.Events) {
                Events.on('agent:state-change', (state) => {
                    if (state === 'done' || state === 'idle') {
                        this._softRefreshTask();
                    }
                });
            }
        },

        _injectNavIcons() {
            if (!window.Icons) return;
            Utils.$$('#wb-nav-list .wb-nav-item[data-view]').forEach((btn) => {
                if (btn.querySelector('.wb-nav-ico')) return;
                const view = btn.getAttribute('data-view');
                const wrap = document.createElement('span');
                wrap.className = 'wb-ico-wrap';
                wrap.innerHTML = Icons.forView(view);
                btn.insertBefore(wrap, btn.firstChild);
            });
            const footMap = [
                ['#wb-new-task', 'plus'],
                ['#wb-settings', 'settings'],
                ['#wb-help', 'helpCircle']
            ];
            footMap.forEach(([sel, icon]) => {
                const btn = Utils.$(sel);
                if (!btn || btn.querySelector('.wb-nav-ico')) return;
                const wrap = document.createElement('span');
                wrap.className = 'wb-ico-wrap';
                wrap.innerHTML = Icons.svg(icon, 'wb-nav-ico');
                btn.insertBefore(wrap, btn.firstChild);
            });
            // 新建页静态按钮
            [
                ['#wb-add-case', 'plus', '添加案件'],
                ['#wb-gen-plan', 'play', '生成分析任务']
            ].forEach(([sel, icon, label]) => {
                const btn = Utils.$(sel);
                if (!btn || btn.querySelector('.wb-ico')) return;
                btn.innerHTML = Icons.labeled(icon, label);
            });
            const citeClose = Utils.$('#wb-cite-close');
            if (citeClose && window.Icons && !citeClose.querySelector('.wb-ico')) {
                citeClose.innerHTML = Icons.svg('x', 'wb-ico');
            }
        },

        _viewLabel(view) {
            return ({
                tasks: '分析任务',
                materials: '材料中心',
                entities: '实体复核',
                leads: '线索中心',
                timeline: '角色时间线',
                graph: '链条图谱',
                reports: '报告与审计'
            })[view] || '分析任务';
        },

        _statusTag(text, tone) {
            const t = tone || 'neutral';
            return Utils.create('span', { class: `wb-status-tag ${t}`, text: text || '' });
        },

        _iconBtn(className, iconName, text, attrs) {
            const btn = Utils.create('button', Object.assign({
                class: className,
                type: 'button'
            }, attrs || {}));
            if (window.Icons && iconName) {
                btn.innerHTML = Icons.labeled(iconName, text);
            } else {
                btn.textContent = text;
            }
            return btn;
        },

        _crumb(currentLabel) {
            return Utils.create('div', { class: 'wb-crumb' }, [
                Utils.create('span', { text: '分析任务' }),
                Utils.create('span', { class: 'wb-crumb-sep', text: '›' }),
                Utils.create('span', { class: 'wb-crumb-current', text: currentLabel || this._viewLabel(this.currentView) })
            ]);
        },

        // ---------- 任务列表 / 导航 ----------

        async loadTasks() {
            try {
                const resp = await fetch('/api/tasks?limit=30');
                const data = await resp.json();
                this.tasks = data.tasks || [];
                this._renderNav();
                if (this.tasks.length && !this.task) {
                    await this.openTask(this.tasks[0].id);
                } else if (!this.tasks.length) {
                    this.showStart();
                }
            } catch (e) {
                console.warn('加载任务失败', e);
                this.showStart();
            }
        },

        _renderRail() {
            this._renderNav();
        },

        _renderNav() {
            this._renderSwitcher();
            this._updateNavActive();
            this._updateNavBadges();
        },

        _renderSwitcher() {
            const nameEl = Utils.$('#wb-switcher-name');
            const metaEl = Utils.$('#wb-switcher-meta');
            const menu = Utils.$('#wb-task-menu');
            if (nameEl) {
                nameEl.textContent = this.task
                    ? (this.task.title || '未命名任务')
                    : '未选择分析任务';
            }
            if (metaEl) {
                metaEl.textContent = this.task
                    ? `${(this.task.cases || []).length} 起案件 · ${this.task.status || ''}`
                    : '请新建或选择任务';
            }
            if (!menu) return;
            menu.innerHTML = '';
            (this.tasks || []).forEach((task) => {
                const item = Utils.create('button', {
                    type: 'button',
                    class: `wb-nav-switcher-item${this.task && this.task.id === task.id ? ' active' : ''}`,
                    text: task.title || '未命名任务'
                });
                item.addEventListener('click', async () => {
                    menu.hidden = true;
                    await this.openTask(task.id);
                });
                const del = Utils.create('span', {
                    class: 'wb-task-del',
                    text: ' ×',
                    title: '删除任务'
                });
                del.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    if (!confirm(`确定删除任务「${task.title}」？`)) return;
                    try {
                        const resp = await fetch(`/api/tasks/${task.id}`, { method: 'DELETE' });
                        const data = await resp.json();
                        if (data.error_code) {
                            Toast.error(data.message || '删除失败');
                            return;
                        }
                        Toast.success('任务已删除');
                        if (this.draftTaskId === task.id) this.draftTaskId = null;
                        if (this.task && this.task.id === task.id) {
                            this.task = null;
                            State.currentTaskId = null;
                            this.showStart();
                        }
                        await this.loadTasks();
                    } catch (err) {
                        Toast.error('删除失败：' + err.message);
                    }
                });
                item.appendChild(del);
                menu.appendChild(item);
            });
            if (!(this.tasks || []).length) {
                menu.appendChild(Utils.create('div', {
                    class: 'wb-rail-empty',
                    text: '暂无任务',
                    style: 'padding:10px'
                }));
            }
        },

        _updateNavActive() {
            Utils.$$('.wb-nav-item[data-view]').forEach((btn) => {
                btn.classList.toggle('active', btn.getAttribute('data-view') === this.currentView);
            });
        },

        _updateNavBadges() {
            const counts = this._navCounts();
            Utils.$$('[data-badge]').forEach((el) => {
                const key = el.getAttribute('data-badge');
                const n = counts[key] || 0;
                el.hidden = !n;
                el.textContent = String(n);
            });
        },

        _navCounts() {
            const arts = (this.task && this.task.artifacts) || [];
            const dir = (this.task && this.task.directory) || [];
            let entities = 0;
            let leads = 0;
            let materials = 0;
            dir.forEach((g) => {
                if (g.key === 'entities') {
                    entities = (g.items || []).filter((i) =>
                        i.status === 'PENDING_REVIEW' || i.status === 'DRAFT'
                    ).length;
                }
                if (g.key === 'clues') {
                    leads = (g.items || []).filter((i) =>
                        i.status === 'PENDING_REVIEW' || i.status === 'DRAFT'
                    ).length;
                }
            });
            const batch = arts.find((a) => a.type === 'MATERIAL_BATCH');
            if (batch && this.artifactCache[batch.id]) {
                const payload = this.artifactCache[batch.id].payload || {};
                materials = (payload.totals && payload.totals.attention) || 0;
            }
            return { entities, leads, materials };
        },

        // ---------- 状态 A：范围设置 ----------

        showStart() {
            Utils.$('#wb-start').hidden = false;
            Utils.$('#wb-start').classList.remove('is-leaving');
            Utils.$('#wb-workspace').hidden = true;
            Utils.$('#wb-scope').hidden = false;
            this.task = null;
            this._renderNav();
            if (!Utils.$$('.wb-case-row').length) {
                this._addCaseRow('案件 A');
                this._addCaseRow('案件 B');
            }
            this._checkScope();
        },

        _bindShell() {
            const switcher = Utils.$('#wb-task-switcher');
            const menu = Utils.$('#wb-task-menu');
            if (switcher && menu) {
                switcher.addEventListener('click', () => {
                    menu.hidden = !menu.hidden;
                });
                document.addEventListener('click', (e) => {
                    if (!menu.hidden && !switcher.contains(e.target) && !menu.contains(e.target)) {
                        menu.hidden = true;
                    }
                });
            }

            Utils.$$('.wb-nav-item[data-view]').forEach((btn) => {
                btn.addEventListener('click', () => {
                    if (!this.task) {
                        Toast.info('请先新建或选择分析任务');
                        return;
                    }
                    this.setView(btn.getAttribute('data-view'));
                });
            });

            const newTask = Utils.$('#wb-new-task');
            if (newTask) newTask.addEventListener('click', () => {
                this.task = null;
                this.draftTaskId = null;
                const purpose = Utils.$('#wb-purpose');
                const title = Utils.$('#wb-title');
                const until = Utils.$('#wb-until');
                const list = Utils.$('#wb-case-list');
                if (purpose) purpose.value = '';
                if (title) title.value = '';
                if (until) until.value = '';
                if (list) list.innerHTML = '';
                this._renderNav();
                this.showStart();
            });

            const citeClose = Utils.$('#wb-cite-close');
            if (citeClose) citeClose.addEventListener('click', () => this._closeCiteDrawer());

            this._bindSplitters();

            ['#wb-settings', '#wb-help'].forEach((sel) => {
                const btn = Utils.$(sel);
                if (btn) btn.addEventListener('click', () => Toast.info('该入口将在后续阶段接入'));
            });
        },

        _bindSplitters() {
            const workspace = Utils.$('#wb-workspace');
            if (!workspace || workspace._splittersBound) return;
            workspace._splittersBound = true;

            const AGENT_MIN = 260;
            const AGENT_MAX = 560;
            const CENTER_MIN = 280;

            const readPx = (name, fallback) => {
                const raw = getComputedStyle(workspace).getPropertyValue(name).trim();
                const n = parseFloat(raw);
                return Number.isFinite(n) ? n : fallback;
            };

            const el = Utils.$('#wb-split-agent');
            if (!el) return;
            el.addEventListener('pointerdown', (e) => {
                if (e.button !== 0) return;
                if (workspace.classList.contains('agent-collapsed')) return;
                e.preventDefault();
                const startX = e.clientX;
                const startAgent = readPx('--wb-agent-w', 360);
                const total = workspace.getBoundingClientRect().width;
                const splitW = readPx('--wb-split-w', 5);

                workspace.classList.add('is-resizing');
                el.classList.add('is-active');
                el.setPointerCapture(e.pointerId);

                const onMove = (ev) => {
                    const dx = ev.clientX - startX;
                    let next = Math.round(startAgent - dx);
                    next = Math.max(AGENT_MIN, Math.min(AGENT_MAX, next));
                    const maxForCenter = total - splitW - CENTER_MIN;
                    next = Math.min(next, Math.max(AGENT_MIN, maxForCenter));
                    workspace.style.setProperty('--wb-agent-w', `${next}px`);
                };

                const onUp = (ev) => {
                    el.releasePointerCapture(ev.pointerId);
                    el.removeEventListener('pointermove', onMove);
                    el.removeEventListener('pointerup', onUp);
                    el.removeEventListener('pointercancel', onUp);
                    workspace.classList.remove('is-resizing');
                    el.classList.remove('is-active');
                };

                el.addEventListener('pointermove', onMove);
                el.addEventListener('pointerup', onUp);
                el.addEventListener('pointercancel', onUp);
            });
        },

        _bindScopeForm() {
            const addCase = Utils.$('#wb-add-case');
            if (addCase) addCase.addEventListener('click', () => this._addCaseRow(''));

            ['#wb-purpose', '#wb-until', '#wb-title'].forEach(sel => {
                const el = Utils.$(sel);
                if (el) el.addEventListener('input', () => this._checkScope());
            });

            const gen = Utils.$('#wb-gen-plan');
            if (gen) gen.addEventListener('click', () => this._createTask());
        },

        _addCaseRow(value) {
            const list = Utils.$('#wb-case-list');
            if (!list) return;
            const input = Utils.create('input', { type: 'text', placeholder: '案件名称或案号' });
            input.value = value || '';
            input.addEventListener('input', () => this._checkScope());

            const fileInput = Utils.create('input', {
                type: 'file',
                multiple: 'multiple',
                accept: '.pdf,.docx,.txt,.png,.jpg,.jpeg'
            });
            fileInput.style.display = 'none';
            const fileBtn = this._iconBtn('wb-btn wb-btn-ghost', 'paperclip', '挂材料');
            const fileMeta = Utils.create('span', { class: 'wb-file-meta', text: '未选文件' });
            fileBtn.addEventListener('click', (e) => {
                e.preventDefault();
                fileInput.click();
            });
            fileInput.addEventListener('change', () => {
                row._files = Array.from(fileInput.files || []);
                fileMeta.textContent = row._files.length
                    ? row._files.map(f => f.name).join('、')
                    : '未选文件';
            });

            const del = Utils.create('button', { class: 'wb-case-del', text: '×', title: '移除' });
            const row = Utils.create('div', { class: 'wb-case-row' }, [
                input, fileBtn, fileMeta, del, fileInput
            ]);
            row._files = [];
            del.addEventListener('click', () => {
                row.remove();
                this._checkScope();
            });
            list.appendChild(row);
            this._checkScope();
        },

        _scopeValues() {
            const cases = Utils.$$('.wb-case-row input[type="text"]')
                .map(i => i.value.trim())
                .filter(Boolean)
                .map(name => ({ name }));  // 返回对象数组
            return {
                title: (Utils.$('#wb-title') || {}).value || '',
                purpose: (Utils.$('#wb-purpose') || {}).value || '',
                authorized_until: (Utils.$('#wb-until') || {}).value || '',  // 字段名改为 authorized_until
                cases
            };
        },

        _checkScope() {
            const v = this._scopeValues();
            let filled = 0;
            if (v.purpose.trim()) filled++;
            if (v.authorized_until) filled++;
            if (v.cases.length >= 2) filled++;

            const pill = Utils.$('#wb-scope-check');
            if (pill) {
                pill.textContent = `必填项 ${filled}/3`;
                pill.className = `wb-pill${filled === 3 ? ' ok' : ''}`;
            }
            const btn = Utils.$('#wb-gen-plan');
            if (btn) btn.disabled = filled !== 3;
            return filled === 3;
        },

        // ---------- 状态 B：计划确认 ----------

        async _createTask() {
            if (!this._checkScope()) return;
            const payload = this._scopeValues();
            const button = Utils.$('#wb-gen-plan');
            if (button) {
                button.disabled = true;
                button.textContent = '正在创建任务…';
            }
            try {
                const editing = this.draftTaskId && this.tasks.some(t => t.id === this.draftTaskId);
                const url = editing ? `/api/tasks/${this.draftTaskId}/scope` : '/api/tasks';
                const resp = await fetch(url, {
                    method: editing ? 'PATCH' : 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '任务创建失败');
                    return;
                }
                this.draftTaskId = data.task.id;
                this.task = data.task;

                this.tasks.unshift(data.task);
                this._renderNav();

                await this._uploadStartFiles(data.task);
                await this._transitionToDraftWorkspace(
                    data.task.id,
                    data.scope_artifact_id || this._scopeArtifactId(data.task)
                );
            } catch (e) {
                Toast.error('任务创建失败：' + e.message);
            } finally {
                if (button) {
                    button.textContent = '生成分析任务';
                    this._checkScope();
                }
            }
        },

        _scopeArtifactId(task) {
            return ((task.artifacts || []).find(a => a.type === 'TASK_SCOPE') || {}).id;
        },

        async _transitionToDraftWorkspace(taskId, scopeArtifactId) {
            const start = Utils.$('#wb-start');
            start.classList.add('is-leaving');
            await Utils.sleep(360);
            const workspace = Utils.$('#wb-workspace');
            workspace.classList.add('is-entering');
            // 新建成功后默认进入「分析任务」页（不再打开目录产物）
            await this.openTask(taskId);
            requestAnimationFrame(() => requestAnimationFrame(() => {
                workspace.classList.remove('is-entering');
            }));
        },

        async _uploadStartFiles(task) {
            const rows = Utils.$$('.wb-case-row').filter(row => {
                const name = (row.querySelector('input[type="text"]') || {}).value || '';
                return name.trim();
            });
            for (let i = 0; i < rows.length; i++) {
                const files = rows[i]._files || [];
                const caseId = ((task.cases || [])[i] || {}).case_id;
                if (!files.length || !caseId) continue;
                await this._uploadMaterials(caseId, files, null);
            }
        },

        async _confirmPlan() {
            if (!this.draftTaskId) return;
            try {
                const resp = await fetch(`/api/tasks/${this.draftTaskId}/plan/confirm`, { method: 'POST' });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '计划确认失败');
                    return null;
                }
                await this.loadTasksAndOpen(this.draftTaskId, data.batch_artifact_id);
                return data;
            } catch (e) {
                Toast.error('计划确认失败：' + e.message);
                return null;
            }
        },

        async loadTasksAndOpen(taskId, artifactId) {
            const resp = await fetch('/api/tasks?limit=8');
            this.tasks = (await resp.json()).tasks || [];
            await this.openTask(taskId, artifactId);
            this._renderNav();
        },

        // ---------- 状态 C：工作台 ----------

        async openTask(taskId, artifactId) {
            const resp = await fetch(`/api/tasks/${taskId}`);
            const task = await resp.json();
            if (task.error_code) {
                Toast.error(task.message || '任务打开失败');
                return;
            }
            this.task = task;
            this.tabs = [];
            this.activeTabId = null;
            this.artifactCache = {};
            this.currentView = 'tasks';

            await this._bindConversation(task);
            Utils.$('#wb-start').hidden = true;
            Utils.$('#wb-workspace').hidden = false;
            this._renderNav();
            this._renderContext();
            await this.setView('tasks');

            if (task.status === 'SCOPE_DRAFT') {
                this.draftTaskId = task.id;
                const planResp = await fetch(`/api/tasks/${task.id}/plan`);
                this._renderAgentPlan(await planResp.json());
            } else {
                this.draftTaskId = task.id;
                this._maybeOfferRerun();
            }

            if (artifactId) {
                await this.openArtifact(artifactId);
            }
        },

        _maybeOfferRerun() {
            if (!this.task || this.task.status === 'SCOPE_DRAFT') return;
            const hasCandidates = (this.task.artifacts || []).some(a => a.type === 'ENTITY_CANDIDATE_SET');
            const hasMaterials = (this.task.artifacts || []).some(a => a.type === 'MATERIAL_DOC' || a.type === 'MATERIAL_BATCH');
            if (!hasMaterials || hasCandidates) return;
            const messages = Utils.$('#chat-messages');
            if (!messages || messages.querySelector('[data-wb-rerun]')) return;
            const run = this._iconBtn('wb-btn wb-btn-primary', 'play', '开始跨案分析');
            run.addEventListener('click', () => this._executeAnalysis(run));
            const card = Utils.create('div', { class: 'wb-agent-plan-card', 'data-wb-rerun': '1' }, [
                Utils.create('div', { class: 'wb-agent-plan-kicker', text: '材料已接入' }),
                Utils.create('div', { class: 'wb-agent-plan-title', text: '尚未完成跨案分析' }),
                Utils.create('div', {
                    class: 'wb-agent-plan-desc',
                    text: '点击下方按钮，由助手按步骤分析；完成后请到左侧「实体复核 / 线索中心」核验原文。'
                }),
                Utils.create('div', { class: 'wb-agent-plan-actions' }, [run])
            ]);
            messages.appendChild(card);
            messages.scrollTop = messages.scrollHeight;
        },

        /** 每个任务绑定自己的智能体会话：还原计划式折叠步骤 + 最终回复 */
        async _bindConversation(task) {
            const messages = Utils.$('#chat-messages');
            if (messages) messages.innerHTML = '';

            try {
                const response = await fetch(`/chat/${task.id}/messages`);
                const data = await response.json();
                this._renderChatHistory(messages, data.messages || []);
            } catch (_) {
                // 新任务还没有聊天消息时保持空白
            }
        },

        _renderChatHistory(container, rows) {
            if (!container || !rows.length) return;
            let stepIndex = 0;
            let i = 0;
            while (i < rows.length) {
                const msg = rows[i];
                const role = msg.role;
                if (role === 'system') {
                    i += 1;
                    continue;
                }
                if (role === 'user') {
                    container.appendChild(Message.renderUser(msg.content || ''));
                    i += 1;
                    continue;
                }
                if (role === 'assistant' && Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
                    stepIndex += 1;
                    const firstName = (msg.tool_calls[0].function || {}).name
                        || msg.tool_calls[0].name
                        || '';
                    const label = window.ToolCall
                        ? ToolCall.displayName(firstName)
                        : '分析动作';
                    const step = Thinking.createStep({
                        index: stepIndex,
                        title: `第 ${stepIndex} 步 · ${label}`,
                        expanded: false
                    });
                    const thinkText = (msg.reasoning_content || '').trim() || (msg.content || '').trim();
                    if (thinkText) Thinking.appendStepThinking(step, thinkText);

                    const results = {};
                    let j = i + 1;
                    while (j < rows.length && rows[j].role === 'tool') {
                        if (rows[j].tool_call_id) {
                            results[rows[j].tool_call_id] = rows[j].content || '';
                        }
                        j += 1;
                    }

                    msg.tool_calls.forEach((tc) => {
                        const fn = tc.function || {};
                        const name = fn.name || tc.name || '';
                        let params = {};
                        const rawArgs = fn.arguments != null ? fn.arguments : tc.arguments;
                        if (typeof rawArgs === 'string') {
                            try { params = JSON.parse(rawArgs); } catch { params = { raw: rawArgs }; }
                        } else if (rawArgs && typeof rawArgs === 'object') {
                            params = rawArgs;
                        }
                        const result = results[tc.id] || '';
                        const ok = !String(result).includes('工具调用出错');
                        const card = ToolCall.create({
                            type: 'db',
                            name,
                            label: ToolCall.displayName(name),
                            description: ToolCall.summarizeResult(result),
                            params,
                            result,
                            status: ok ? 'success' : 'error',
                            expanded: false
                        });
                        Thinking.addToolToStep(step, card);
                    });
                    Thinking.finishStep(step, `第 ${stepIndex} 步 · 已完成`);
                    container.appendChild(step);
                    i = j;
                    continue;
                }
                if (role === 'assistant' && (msg.content || '').trim()) {
                    if ((msg.reasoning_content || '').trim() && window.Thinking) {
                        const block = Thinking.create({
                            title: '分析思路',
                            defaultExpanded: false,
                            steps: [{ text: msg.reasoning_content, status: 'done' }]
                        });
                        container.appendChild(block);
                    }
                    const { wrap, content } = Message.renderAssistantContainer();
                    content.innerHTML = Markdown.parse(msg.content);
                    container.appendChild(wrap);
                    i += 1;
                    continue;
                }
                // 孤立 tool 行跳过
                i += 1;
            }
            container.scrollTop = container.scrollHeight;
        },

        _renderAgentPlan(plan) {
            const messages = Utils.$('#chat-messages');
            if (!messages || !plan || plan.error_code) return;

            const steps = Utils.create('div', { class: 'wb-agent-plan-steps' });
            plan.steps.forEach((step, index) => {
                steps.appendChild(Utils.create('div', { class: 'wb-agent-plan-step' }, [
                    Utils.create('span', { class: 'wb-step-no', text: String(index + 1) }),
                    Utils.create('span', { class: 'wb-step-name', text: step.label }),
                    Utils.create('span', {
                        class: `wb-pill${step.mode === 'review' ? ' warn' : ''}`,
                        text: step.mode === 'review' ? '人工' : '自动'
                    })
                ]));
            });

            const edit = this._iconBtn('wb-btn wb-btn-ghost', 'x', '返回修改');
            edit.addEventListener('click', () => this._returnToScope());
            const confirm = this._iconBtn('wb-btn wb-btn-primary', 'play', '开始跨案分析');
            confirm.addEventListener('click', () => this._executeAnalysis(confirm));

            const card = Utils.create('div', { class: 'wb-agent-plan-card' }, [
                Utils.create('div', { class: 'wb-agent-plan-kicker', text: '分析计划已生成' }),
                Utils.create('div', { class: 'wb-agent-plan-title', text: plan.title }),
                Utils.create('div', {
                    class: 'wb-agent-plan-desc',
                    text: `${plan.cases.length} 起案件 · 授权至 ${plan.authorized_until}。无需补充材料或提示词，可直接执行。`
                }),
                steps,
                Utils.create('div', { class: 'wb-agent-plan-actions' }, [edit, confirm])
            ]);
            messages.appendChild(card);
            messages.scrollTop = messages.scrollHeight;
        },

        _returnToScope() {
            if (!this.task) return;
            const purpose = Utils.$('#wb-purpose');
            const title = Utils.$('#wb-title');
            const until = Utils.$('#wb-until');
            if (purpose) purpose.value = this.task.purpose || '';
            if (title) title.value = this.task.title || '';
            if (until) until.value = this.task.authorized_until || '';
            const caseList = Utils.$('#wb-case-list');
            if (caseList) caseList.innerHTML = '';
            (this.task.cases || []).forEach(item => this._addCaseRow(item.display_name));
            this.showStart();
        },

        _defaultArtifactId() {
            const batch = (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH');
            const scope = (this.task.artifacts || []).find(a => a.type === 'TASK_SCOPE');
            return (batch || scope || {}).id;
        },

        _renderContext() {
            const agentCtx = Utils.$('#wb-agent-context');
            if (agentCtx && this.task) {
                agentCtx.textContent = `当前绑定：${this.task.title}`;
            }
            this._renderNav();
        },

        _renderDirectory() {
            this._updateNavBadges();
        },

        /** 目录点击与智能体链接共用入口：按产物类型切换中间业务页 */
        async openArtifact(artifactId) {
            if (!this.task) return;
            const data = await this._fetchArtifact(artifactId);
            if (!data) return;
            const type = data.artifact.type;
            const viewMap = {
                TASK_SCOPE: 'tasks',
                MATERIAL_BATCH: 'materials',
                MATERIAL_DOC: 'materials',
                ENTITY_CANDIDATE_SET: 'entities',
                CLUE_SET: 'leads',
                CLUE_ITEM: 'leads',
                ROLE_TIMELINE: 'timeline',
                LINK_GRAPH: 'graph',
                SOURCE_VERIFY: 'reports',
                REPORT_DRAFT: 'reports',
                REPORT_EXPORT: 'reports'
            };
            const view = viewMap[type] || 'tasks';
            if (type === 'ENTITY_CANDIDATE_SET') {
                const first = ((data.payload || {}).candidates || [])[0];
                this.selectedEntityId = first && first.candidate_id;
            }
            if (type === 'CLUE_ITEM') this.selectedLeadId = artifactId;
            await this.setView(view);
        },

        async _fetchArtifact(artifactId) {
            try {
                const resp = await fetch(`/api/tasks/${this.task.id}/artifacts/${artifactId}`);
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '分析成果打开失败');
                    return null;
                }
                this.artifactCache[artifactId] = data;
                const existing = this.tabs.find(t => t.id === artifactId);
                if (existing) existing.data = data;
                else this.tabs.push({ id: artifactId, title: data.artifact.title, data });
                this.activeTabId = artifactId;
                return data;
            } catch (e) {
                Toast.error('加载失败：' + e.message);
                return null;
            }
        },

        _renderTabs() {},

        _renderPanel() {
            this._renderCurrentView();
        },

        _renderScopeArtifact(panel, payload) {
            const summary = Utils.create('div', { class: 'wb-summary' });
            [
                ['监督目的', payload.purpose],
                ['授权有效期', payload.authorized_until],
                ['案件范围', (payload.cases || []).map(c => c.display_name).join('、')],
                ['补充说明', payload.note || '—']
            ].forEach(([k, v]) => {
                summary.appendChild(Utils.create('div', {}, [
                    Utils.create('div', { class: 'wb-summary-k', text: k }),
                    Utils.create('div', { class: 'wb-summary-v', text: v || '—' })
                ]));
            });
            panel.appendChild(summary);
        },

        _renderMaterialBatch(panel, payload) {
            const totals = payload.totals || {};
            if (totals.attention) {
                panel.appendChild(Utils.create('div', { class: 'wb-callout warn' }, [
                    Utils.create('span', { text: `${totals.attention} 份材料需要人工处理（识别质量或重复），不影响其他材料继续解析。` })
                ]));
            } else if (totals.documents) {
                panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                    Utils.create('span', { text: `共 ${totals.documents} 份材料，其中 ${totals.ready} 份已可用于分析。` })
                ]));
            }

            const actions = Utils.create('div', { class: 'wb-entity-actions', style: 'margin-bottom:12px' });
            const runBtn = this._iconBtn('wb-btn wb-btn-primary', 'play', '开始跨案分析');
            runBtn.addEventListener('click', () => this._executeAnalysis(runBtn));
            const eventBtn = this._iconBtn('wb-btn wb-btn-ghost', 'waypoints', '只整理事件时间线');
            eventBtn.addEventListener('click', () => this._runTimeline(eventBtn));
            actions.appendChild(runBtn);
            actions.appendChild(eventBtn);
            panel.appendChild(actions);
            panel.appendChild(Utils.create('div', {
                class: 'wb-file-meta',
                text: '完整跨案分析由右侧助手按步骤推进。点「开始跨案分析」下达指令；也可在对话框直接说明需求。',
                style: 'margin-bottom:12px'
            }));

            (payload.groups || []).forEach(group => {
                const materials = (group.materials || []).filter(m => m.status !== 'DELETED');
                panel.appendChild(Utils.create('div', {
                    class: 'wb-group-label',
                    text: `${group.case_name} · ${materials.length} 份材料`
                }));

                if (!materials.length) {
                    panel.appendChild(Utils.create('div', { class: 'wb-dir-empty', text: '尚未上传材料' }));
                }
                materials.forEach(row => panel.appendChild(this._materialRow(row)));
            });

            panel.appendChild(this._uploadBox(payload));
            this._schedulePoll(payload);
        },

        _renderEntityCandidates(panel, payload, status, artifact, version) {
            const summary = payload.summary || {};
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', {
                    text: payload.boundary || '标识重合仅为待核验提示，不代表已认定同一人、同一账户或共同犯罪。请在下方每条对象卡片中作出判断。'
                })
            ]));

            const actions = Utils.create('div', { class: 'wb-entity-actions', style: 'margin-bottom:12px' });
            const runBtn = this._iconBtn('wb-btn wb-btn-primary', 'play', '开始跨案分析');
            runBtn.addEventListener('click', () => this._executeAnalysis(runBtn));
            const collideBtn = this._iconBtn('wb-btn wb-btn-ghost', 'users', '只做标识比对');
            collideBtn.addEventListener('click', () => this._runCollision(collideBtn));
            const clueBtn = this._iconBtn('wb-btn wb-btn-ghost', 'link2', '只生成疑似关联线索');
            clueBtn.addEventListener('click', () => this._generateClues(clueBtn));
            actions.appendChild(runBtn);
            actions.appendChild(collideBtn);
            actions.appendChild(clueBtn);
            panel.appendChild(actions);

            const metrics = Utils.create('div', { class: 'wb-entity-metrics' }, [
                this._metric('待核对象', summary.total || 0),
                this._metric('待判断', summary.pending || 0),
                this._metric('材料中出现', summary.mention_count || (payload.mentions || []).length)
            ]);
            panel.appendChild(metrics);

            const mentions = payload.mentions || [];
            if (mentions.length) {
                panel.appendChild(Utils.create('div', { class: 'wb-group-label', text: `材料中出现的标识 · ${mentions.length}` }));
                mentions.slice(0, 40).forEach(mention => {
                    const rec = (mention.records || [])[0] || {};
                    const kindLabel = {
                        tail_only: '仅尾号·不参与跨案比对',
                        luhn_failed: '卡号校验未通过·不参与跨案比对'
                    }[mention.mask_kind] || (mention.masked ? '已掩码·不参与跨案比对' : '可参与比对');
                    const typeLabel = {
                        ACCOUNT: '账户/卡号',
                        PHONE: '手机号',
                        DEVICE: '设备',
                        NAME: '姓名',
                        ID_CARD: '证件'
                    }[mention.object_type] || (mention.object_type || '标识');
                    const row = Utils.create('div', { class: 'wb-entity-record' }, [
                        Utils.create('div', { class: 'case', text: `${typeLabel} · ${kindLabel}` }),
                        Utils.create('div', { class: 'value', text: mention.display_name || '脱敏标识' }),
                        Utils.create('div', { class: 'source', text: [rec.case_name || rec.case_id, rec.filename].filter(Boolean).join(' · ') })
                    ]);
                    if (rec.chunk_id && rec.quote_hash) {
                        row.style.cursor = 'pointer';
                        row.addEventListener('click', () => this._openCitation(rec));
                    }
                    panel.appendChild(row);
                });
            }

            const candidates = payload.candidates || [];
            if (!candidates.length) {
                const mentionCount = summary.mention_count || mentions.length;
                const luhnFailed = mentions.some(m => m.mask_kind === 'luhn_failed');
                const reason = !mentionCount
                    ? '尚未在材料中发现可用于跨案比对的完整手机号、银行卡号或设备号。请确认材料已解析可用后，再执行「只做标识比对」。出现跨案同一标识后，将在本页每条对象下提供判断按钮。'
                    : luhnFailed
                        ? '已发现卡号写法，但校验未通过，或仅为尾号/掩码号。须完整卡号同时出现在两起及以上案件，才会生成待判断对象。出现后，判断按钮在本页每条对象卡片底部。'
                        : '材料中已有标识记载，但没有「完整强标识同时出现在两起及以上案件」。尾号、掩码号、校验未通过的卡号不会生成待判断对象。一旦出现跨案同一标识，请在本页对应卡片底部作出判断。';
                panel.appendChild(Utils.create('div', { class: 'wb-empty', text: reason }));
                return;
            }
            panel.appendChild(Utils.create('div', {
                class: 'wb-group-label',
                text: '请对下列跨案对象作出判断（同一对象 / 不是同一对象等）'
            }));
            candidates.forEach((candidate, index) => {
                panel.appendChild(this._entityCandidateCard(candidate, index, status, version));
            });
        },

        _metric(label, value) {
            return Utils.create('div', { class: 'wb-entity-metric' }, [
                Utils.create('div', { class: 'v', text: String(value) }),
                Utils.create('div', { class: 'k', text: label })
            ]);
        },

        _entityCandidateCard(candidate, index, artifactStatus, version) {
            const records = Utils.create('div', { class: 'wb-entity-records' });
            (candidate.records || []).forEach(record => {
                const source = record.source || {};
                const recEl = Utils.create('div', { class: 'wb-entity-record' }, [
                    Utils.create('div', { class: 'case', text: record.case_name || record.case_id || '案件' }),
                    Utils.create('div', {
                        class: 'value',
                        text: record.value || '脱敏标识'
                    }),
                    Utils.create('div', {
                        class: 'source',
                        text: [source.document_name, source.page_no ? `第 ${source.page_no} 页` : '']
                            .filter(Boolean).join(' · ') || '待补原文定位'
                    })
                ]);
                if (source.chunk_id && source.quote_hash) {
                    recEl.style.cursor = 'pointer';
                    recEl.addEventListener('click', () => this._openCitation(source));
                }
                records.appendChild(recEl);
            });

            const basis = (candidate.match_basis || []).length
                ? candidate.match_basis.join('；')
                : '未提供匹配依据';
            const differences = (candidate.differences || []).length
                ? candidate.differences.join('；')
                : '未发现已知差异';
            const decisionLabel = {
                PENDING: '待判断',
                MERGE: '视为同一对象',
                KEEP_SEPARATE: '不是同一对象',
                CORRECT: '已更正',
                DEFER: '暂缓判断'
            }[candidate.decision] || candidate.decision;

            const body = Utils.create('div', { class: 'wb-entity-card-body' }, [
                records,
                Utils.create('div', { class: 'wb-entity-evidence' }, [
                    Utils.create('div', {}, [
                        Utils.create('span', { class: 'label', text: '一致依据' }),
                        Utils.create('span', { text: basis })
                    ]),
                    Utils.create('div', {}, [
                        Utils.create('span', { class: 'label', text: '差异提示' }),
                        Utils.create('span', { text: differences })
                    ])
                ])
            ]);

            const reviewArea = Utils.create('div', { class: 'wb-entity-review' });
            if (candidate.decision !== 'PENDING') {
                reviewArea.appendChild(Utils.create('div', {
                    class: 'wb-callout',
                    text: `${decisionLabel}：${candidate.reason || '已记录'}`
                }));
            } else if (!['STALE', 'INVALID'].includes(artifactStatus)) {
                const actions = [
                    ['MERGE', '视为同一对象（待继续核查）', 'check'],
                    ['KEEP_SEPARATE', '不是同一对象', 'x'],
                    ['CORRECT', '更正标识信息', 'fileCheck2'],
                    ['DEFER', '暂缓判断', 'info']
                ];
                const buttons = Utils.create('div', { class: 'wb-entity-actions' });
                actions.forEach(([decision, label, icon]) => {
                    const button = this._iconBtn(
                        `wb-btn${decision === 'MERGE' ? ' wb-btn-primary' : ' wb-btn-ghost'}`,
                        icon,
                        label
                    );
                    button.addEventListener('click', () => {
                        this._showEntityDecisionForm(reviewArea, candidate, decision, label, version);
                    });
                    buttons.appendChild(button);
                });
                reviewArea.appendChild(buttons);
            }

            const typeHuman = {
                ACCOUNT: '账户/卡号',
                PHONE: '手机号',
                DEVICE: '设备',
                NAME: '姓名',
                ID_CARD: '证件',
                OTHER: '其他'
            }[candidate.entity_type] || (candidate.entity_type || '对象');
            return Utils.create('section', { class: 'wb-entity-card' }, [
                Utils.create('div', { class: 'wb-entity-card-head' }, [
                    Utils.create('div', {}, [
                        Utils.create('div', {
                            class: 'wb-entity-title',
                            text: candidate.display_name || `待核对象 ${index + 1}`
                        }),
                        Utils.create('div', {
                            class: 'wb-file-meta',
                            text: `${typeHuman} · ${candidate.confidence_label || '待核验'}`
                        })
                    ]),
                    Utils.create('span', {
                        class: `wb-pill${candidate.decision === 'PENDING' ? ' warn' : ' ok'}`,
                        text: decisionLabel
                    })
                ]),
                body,
                reviewArea
            ]);
        },

        _showEntityDecisionForm(container, candidate, decision, label, version) {
            container.innerHTML = '';
            const reason = Utils.create('textarea', {
                class: 'wb-decision-reason',
                rows: '2',
                placeholder: `填写“${label}”的理由（必填）`
            });
            const cancel = this._iconBtn('wb-btn wb-btn-ghost', 'x', '取消');
            cancel.addEventListener('click', () => this._renderPanel());
            const submit = this._iconBtn('wb-btn wb-btn-primary', 'check', '确认提交');
            submit.addEventListener('click', () => {
                this._submitEntityDecision(candidate.candidate_id, decision, reason.value, submit, version);
            });
            container.appendChild(reason);
            container.appendChild(Utils.create('div', { class: 'wb-entity-actions' }, [cancel, submit]));
            reason.focus();
        },

        async _submitEntityDecision(candidateId, decision, reason, button, version) {
            if (!(reason || '').trim()) {
                Toast.warning('请填写判断理由');
                return;
            }
            button.disabled = true;
            try {
                const resp = await fetch(
                    `/api/tasks/${this.task.id}/entity-candidates/${candidateId}/decision`,
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            decision,
                            reason: reason.trim(),
                            expected_version: version
                        })
                    }
                );
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '判断未能保存');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success('判断已记录');
            } catch (e) {
                Toast.error('判断未能保存：' + e.message);
            } finally {
                button.disabled = false;
            }
        },

        async _runCollision(button) {
            button.disabled = true;
            try {
                const resp = await fetch(`/api/tasks/${this.task.id}/collision/run`, { method: 'POST' });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '标识比对未能完成');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success(`标识比对完成，待核对象 ${data.candidate_count || 0} 条`);
            } catch (e) {
                Toast.error('标识比对未能完成：' + e.message);
            } finally {
                button.disabled = false;
            }
        },

        async _generateClues(button) {
            button.disabled = true;
            try {
                const resp = await fetch(`/api/tasks/${this.task.id}/clues/generate`, { method: 'POST' });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '线索生成未能完成');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success(`疑似关联线索已生成 ${ (data.created || []).length } 条`);
            } catch (e) {
                Toast.error('线索生成未能完成：' + e.message);
            } finally {
                button.disabled = false;
            }
        },

        async _runTimeline(button) {
            button.disabled = true;
            try {
                const resp = await fetch(`/api/tasks/${this.task.id}/timeline/run`, { method: 'POST' });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '事件整理未能完成');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success(`事件时间线已整理 ${data.event_count || 0} 条`);
            } catch (e) {
                Toast.error('事件整理未能完成：' + e.message);
            } finally {
                button.disabled = false;
            }
        },

        async _openCitation(source) {
            const versionId = source.document_version_id;
            const chunkId = source.chunk_id;
            if (!versionId || !chunkId) {
                Toast.warning('缺少原文定位');
                return;
            }
            const params = new URLSearchParams();
            if (source.quote_hash) params.set('quote_hash', source.quote_hash);
            if (source.quote) params.set('quote', source.quote);
            const pane = Utils.$('#wb-cite-drawer');
            if (pane) {
                this._openCiteDrawerShell();
                const body = Utils.$('#wb-cite-drawer-body');
                if (body) {
                    body.innerHTML = '';
                    body.appendChild(Utils.create('div', { class: 'wb-cite-loading', text: '正在核对原文…' }));
                }
            }
            try {
                const resp = await fetch(
                    `/api/materials/versions/${versionId}/chunks/${chunkId}?${params.toString()}`
                );
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '引用失效');
                    this._renderCitePane({
                        error: true,
                        title: '引用失效',
                        text: data.message || '原文已变更或哈希不匹配，禁止展示旧内容',
                        meta: source.filename || source.document_name || ''
                    }, source);
                    return;
                }
                this._renderCitePane({
                    error: false,
                    title: '原文回链',
                    text: data.text || source.quote || '无文本',
                    meta: [source.filename || source.document_name, source.page_start || source.page_no ? `第 ${source.page_start || source.page_no} 页` : '']
                        .filter(Boolean).join(' · ')
                }, source);
            } catch (e) {
                Toast.error('回链失败：' + e.message);
                this._renderCitePane({
                    error: true,
                    title: '回链失败',
                    text: e.message,
                    meta: ''
                }, source);
            }
        },

        _renderCitePane(view, source) {
            const drawer = Utils.$('#wb-cite-drawer');
            const bodyEl = Utils.$('#wb-cite-drawer-body');
            const title = Utils.$('#wb-cite-title');
            const kicker = Utils.$('#wb-cite-kicker');
            if (!bodyEl) return;
            this._openCiteDrawerShell();
            if (kicker) kicker.textContent = view.error ? '核验未通过' : '原文对照';
            if (title) title.textContent = view.title || '原文';
            bodyEl.innerHTML = '';
            if (view.meta) {
                bodyEl.appendChild(Utils.create('div', { class: 'wb-file-meta', text: view.meta }));
            }
            if (view.error) {
                bodyEl.appendChild(Utils.create('div', { class: 'wb-callout warn' }, [
                    Utils.create('span', { text: '该引用不可用于确认或导出，请重新核验。' })
                ]));
            }
            const body = Utils.create('div', { class: `wb-cite-body${view.error ? ' is-error' : ''}` });
            const quote = (source && source.quote) || '';
            const text = view.text || '';
            if (quote && text.includes(quote) && !view.error) {
                const idx = text.indexOf(quote);
                body.appendChild(document.createTextNode(text.slice(0, idx)));
                body.appendChild(Utils.create('mark', { class: 'wb-cite-mark', text: quote }));
                body.appendChild(document.createTextNode(text.slice(idx + quote.length)));
            } else {
                body.textContent = text;
            }
            bodyEl.appendChild(body);
        },

        _closeCiteDrawer() {
            const drawer = Utils.$('#wb-cite-drawer');
            const center = Utils.$('#wb-center');
            if (drawer) drawer.hidden = true;
            if (center) center.classList.remove('cite-open');
        },

        _openCiteDrawerShell() {
            const drawer = Utils.$('#wb-cite-drawer');
            const center = Utils.$('#wb-center');
            if (drawer) drawer.hidden = false;
            if (center) center.classList.add('cite-open');
        },

        _renderClueSet(panel, payload) {
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', { text: payload.boundary || '线索停留在待核验层级，请打开单条后在卡片内处置。' })
            ]));
            const summary = payload.summary || {};
            panel.appendChild(Utils.create('div', { class: 'wb-entity-metrics' }, [
                this._metric('线索', summary.total || 0),
                this._metric('新生成', summary.created || 0),
                this._metric('本轮跳过', summary.skipped || 0)
            ]));
            const items = payload.items || [];
            if (!items.length) {
                const skipped = payload.skipped || [];
                const skipText = skipped.length
                    ? `本轮跳过 ${skipped.length} 条（${skipped.slice(0, 3).map(s => s.reason).join('、')}）。`
                    : '';
                panel.appendChild(Utils.create('div', {
                    class: 'wb-empty',
                    text: `尚无跨案疑似关联线索。${skipText}通常需要：完整卡号/手机号/设备号同时出现在两起及以上案件，或同一账户出现在多案转账、同一手机号出现在多案联络。仅有尾号、掩码号或卡号校验未通过时，结果为 0 属正常。打开单条线索后，可在卡片底部继续核查、排除或暂缓。`
                }));
                return;
            }
            items.forEach(item => {
                const open = this._iconBtn('wb-btn wb-btn-ghost', 'externalLink', '打开并处置');
                if (item.artifact_id) {
                    open.addEventListener('click', () => this.openArtifact(item.artifact_id));
                }
                panel.appendChild(Utils.create('section', { class: 'wb-entity-card' }, [
                    Utils.create('div', { class: 'wb-entity-card-head' }, [
                        Utils.create('div', { class: 'wb-entity-title', text: item.title || '疑似关联线索' }),
                        Utils.create('span', { class: 'wb-pill ok', text: '待打开' })
                    ]),
                    Utils.create('div', { class: 'wb-entity-card-body' }, [
                        Utils.create('div', {
                            class: 'wb-file-meta',
                            text: `涉及 ${item.case_count || '—'} 起案件 · 可回原文定位 ${item.chunk_count || 0} 处`
                        }),
                        open
                    ])
                ]));
            });
        },

        _renderClueItem(panel, payload, status, artifact, version) {
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', {
                    text: payload.boundary || '本条仅为疑似关联线索，请在核对原文后作出处置；不构成法律结论。'
                })
            ]));
            panel.appendChild(Utils.create('div', { class: 'wb-summary-v', text: payload.title || '' }));
            panel.appendChild(Utils.create('div', { class: 'wb-file-meta', text: payload.summary || '' }));
            panel.appendChild(Utils.create('div', { class: 'wb-group-label', text: '涉及案件' }));
            (payload.cases || []).forEach(c => {
                panel.appendChild(Utils.create('div', { class: 'wb-file-meta', text: c.case_name || c.case_id }));
            });
            panel.appendChild(Utils.create('div', { class: 'wb-group-label', text: '证据摘录（点击回原文）' }));
            (payload.evidence || []).forEach(ev => {
                const row = Utils.create('div', { class: 'wb-entity-record' }, [
                    Utils.create('div', { class: 'case', text: ev.case_name || ev.case_id || '' }),
                    Utils.create('div', { class: 'value', text: ev.quote || '脱敏片段' }),
                    Utils.create('div', {
                        class: 'source',
                        text: [ev.filename, ev.page_start ? `第 ${ev.page_start} 页` : ''].filter(Boolean).join(' · ')
                    })
                ]);
                row.style.cursor = 'pointer';
                row.addEventListener('click', () => this._openCitation(ev));
                panel.appendChild(row);
            });
            if (payload.uncertainty) {
                panel.appendChild(Utils.create('div', { class: 'wb-callout warn', text: payload.uncertainty }));
            }

            const dispositionLabel = {
                CONTINUE: '继续核查',
                NEED_MATERIAL: '需补材料',
                EXCLUDE: '排除',
                DEFER: '暂缓'
            };
            const review = Utils.create('div', { class: 'wb-entity-review' });
            panel.appendChild(Utils.create('div', { class: 'wb-group-label', text: '内联处置' }));
            if (payload.disposition && dispositionLabel[payload.disposition]) {
                review.appendChild(Utils.create('div', {
                    class: 'wb-callout',
                    text: `${dispositionLabel[payload.disposition]}：${payload.disposition_reason || '已记录'}`
                }));
            } else if (!['STALE', 'INVALID'].includes(status)) {
                const actions = [
                    ['CONTINUE', '继续核查', 'check'],
                    ['NEED_MATERIAL', '需补材料', 'fileStack'],
                    ['EXCLUDE', '排除', 'x'],
                    ['DEFER', '暂缓', 'info']
                ];
                const buttons = Utils.create('div', { class: 'wb-entity-actions' });
                actions.forEach(([code, label, icon]) => {
                    const button = this._iconBtn(
                        `wb-btn${code === 'CONTINUE' ? ' wb-btn-primary' : ' wb-btn-ghost'}`,
                        icon,
                        label
                    );
                    button.addEventListener('click', () => {
                        this._showClueDispositionForm(
                            review,
                            artifact.id,
                            code,
                            label,
                            version
                        );
                    });
                    buttons.appendChild(button);
                });
                review.appendChild(buttons);
            }
            panel.appendChild(review);
        },

        _showClueDispositionForm(container, artifactId, disposition, label, version) {
            container.innerHTML = '';
            const reason = Utils.create('textarea', {
                class: 'wb-decision-reason',
                rows: '2',
                placeholder: `填写“${label}”的理由（必填）`
            });
            const submit = this._iconBtn('wb-btn wb-btn-primary', 'check', '确认处置');
            const cancel = this._iconBtn('wb-btn wb-btn-ghost', 'x', '取消');
            cancel.addEventListener('click', () => this.openArtifact(artifactId));
            submit.addEventListener('click', () => {
                this._disposeClue(artifactId, disposition, reason.value, version, submit);
            });
            container.appendChild(reason);
            container.appendChild(Utils.create('div', { class: 'wb-entity-actions' }, [submit, cancel]));
        },

        async _disposeClue(artifactId, disposition, reason, version, button) {
            if (!(reason || '').trim()) {
                Toast.warning('请填写处置理由');
                return;
            }
            button.disabled = true;
            try {
                const resp = await fetch(
                    `/api/tasks/${this.task.id}/clues/${artifactId}/disposition`,
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            disposition,
                            reason: reason.trim(),
                            expected_version: version
                        })
                    }
                );
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '处置未能保存');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success('线索处置已记录');
            } catch (e) {
                Toast.error('处置未能保存：' + e.message);
            } finally {
                button.disabled = false;
            }
        },

        _renderRoleTimeline(panel, payload) {
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', { text: payload.boundary || '事件仅作为后续规则事实层，不直接给出关系结论。' })
            ]));
            const summary = payload.summary || {};
            const typeText = Object.entries(summary.types || {})
                .map(([key, value]) => `${key === 'TRANSFER' ? '转账' : key === 'CONTACT' ? '联络' : key} ${value}`)
                .join(' · ');
            panel.appendChild(Utils.create('div', { class: 'wb-entity-metrics' }, [
                this._metric('事件', summary.total || 0),
                this._metric('有时间', summary.dated || 0),
                this._metric('时间不明', summary.undated || 0)
            ]));
            if (typeText) {
                panel.appendChild(Utils.create('div', {
                    class: 'wb-file-meta',
                    text: `${typeText} · 已扫描材料片段 ${summary.scanned_chunks || 0}`
                }));
            }
            const items = payload.items || [];
            if (!items.length) {
                panel.appendChild(Utils.create('div', {
                    class: 'wb-empty',
                    text: '尚未抽到可定位的转账或联络事件。材料里只有标识、没有行为描述时，时间线为空是正常结果。'
                }));
                return;
            }

            const dated = items.filter(item => item.time_text && item.time_precision !== 'UNKNOWN');
            const undated = items.filter(item => !(item.time_text && item.time_precision !== 'UNKNOWN'));
            const chart = Utils.create('div', { class: 'wb-timeline-chart' });
            chart.appendChild(Utils.create('div', { class: 'wb-timeline-axis' }));

            const renderNode = (item, unknown) => {
                const source = item.source || {};
                const node = Utils.create('article', {
                    class: `wb-timeline-node${item.event_type === 'TRANSFER' ? ' is-transfer' : ' is-contact'}${unknown ? ' is-unknown' : ''}`
                }, [
                    Utils.create('div', { class: 'wb-timeline-dot' }),
                    Utils.create('div', { class: 'wb-timeline-card' }, [
                        Utils.create('div', { class: 'wb-timeline-card-head' }, [
                            Utils.create('div', { class: 'wb-timeline-time', text: item.time_text || '时间不明' }),
                            Utils.create('span', {
                                class: 'wb-pill ok',
                                text: item.event_type === 'TRANSFER' ? '转账' : '联络'
                            })
                        ]),
                        Utils.create('div', { class: 'wb-timeline-case', text: item.case_name || item.case_id || '案件' }),
                        Utils.create('div', { class: 'wb-timeline-summary', text: item.summary_text || '—' }),
                        Utils.create('div', {
                            class: 'wb-timeline-meta',
                            text: [
                                item.amount_text || '',
                                (item.parties || []).join('；'),
                                source.filename || '',
                                source.page_start ? `第 ${source.page_start} 页` : ''
                            ].filter(Boolean).join(' · ') || '点击回链原文'
                        })
                    ])
                ]);
                if (source.chunk_id && source.document_version_id) {
                    node.style.cursor = 'pointer';
                    node.addEventListener('click', () => this._openCitation(source));
                }
                return node;
            };

            if (dated.length) {
                chart.appendChild(Utils.create('div', { class: 'wb-group-label', text: '按时间排列' }));
                dated.forEach(item => chart.appendChild(renderNode(item, false)));
            }
            if (undated.length) {
                chart.appendChild(Utils.create('div', { class: 'wb-group-label', text: '时间不明（单独分组，不补造时间）' }));
                undated.forEach(item => chart.appendChild(renderNode(item, true)));
            }
            panel.appendChild(chart);
        },

        async _renderMaterialDoc(panel, artifact, payload) {
            // 从 artifact 或 payload 中获取 document_id
            const documentId = artifact.ref_key || (payload && payload.document_id);
            if (!documentId) {
                panel.appendChild(Utils.create('div', { class: 'wb-empty', text: '缺少材料标识，无法加载内容' }));
                return;
            }

            // 显示加载状态
            const loading = Utils.create('div', { class: 'wb-empty', text: '正在加载脱敏内容…' });
            panel.appendChild(loading);

            try {
                const resp = await fetch(`/api/materials/${documentId}/preview`);
                const data = await resp.json();

                panel.removeChild(loading);

                if (!data.ok) {
                    panel.appendChild(Utils.create('div', { class: 'wb-callout warn', text: data.error || '获取内容失败' }));
                    return;
                }

                // 显示文件名和元信息
                const head = Utils.create('div', { class: 'wb-panel-head' }, [
                    Utils.create('div', { class: 'wb-panel-sub', text: `脱敏全文 · ${data.chunk_count} 处片段` })
                ]);
                panel.appendChild(head);

                // 脱敏文本主体：字体与排版保持原样，颜色走主题变量
                const pre = Utils.create('pre', { class: 'wb-doc-preview' });
                pre.textContent = data.text;
                panel.appendChild(pre);

                // 底部提示
                panel.appendChild(Utils.create('div', {
                    class: 'wb-file-meta',
                    text: '以上内容已脱敏，与智能体看到的一致。',
                    style: 'margin-top: 8px; text-align: right;'
                }));

            } catch (err) {
                panel.removeChild(loading);
                panel.appendChild(Utils.create('div', { class: 'wb-callout warn', text: '网络错误：' + err.message }));
            }
        },

        _materialRow(row) {
            const docId = row.document_id;

            const status = row.status || 'UPLOADED';
            const attention = ATTENTION.includes(status);
            const stageIndex = STAGE_ORDER.indexOf(status);
            const percent = attention ? 100 : Math.round(((stageIndex + 1) / STAGE_ORDER.length) * 100);
            const ext = (row.filename || '').split('.').pop().toUpperCase().slice(0, 4) || 'FILE';

            const detail = [];
            if (row.page_count) detail.push(`${row.page_count} 页`);
            if ((row.low_confidence_pages || []).length) {
                detail.push(`低置信 ${row.low_confidence_pages.length} 页`);
            }
            if (row.version_count > 1) detail.push(`v${row.version_count}`);

            const fill = Utils.create('div', {
                class: `wb-progress-fill${attention ? ' warn' : ''}`
            });
            fill.style.width = percent + '%';

            const del = Utils.create('button', {
                type: 'button',
                class: 'wb-file-del',
                title: '删除材料',
                text: '×'
            });
            del.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this._deleteMaterial(docId);
            });

            const rowEl = Utils.create('div', { class: 'wb-file-row' }, [
                Utils.create('div', { class: 'wb-file-type', text: ext }),
                Utils.create('div', {}, [
                    Utils.create('div', { class: 'wb-file-name', text: row.filename || '未命名' }),
                    Utils.create('div', { class: 'wb-file-meta', text: detail.join(' · ') || this._sizeText(row.size) })
                ]),
                Utils.create('div', {}, [
                    Utils.create('div', { class: 'wb-progress' }, [fill]),
                    Utils.create('div', {
                        class: 'wb-file-stage',
                        text: attention ? '需人工处理' : `阶段 ${Math.max(stageIndex + 1, 1)} / ${STAGE_ORDER.length}`
                    })
                ]),
                Utils.create('div', { class: 'wb-file-status', text: STAGE_TEXT[status] || status }),
                del
            ]);

            rowEl.style.cursor = 'pointer';
            rowEl.addEventListener('click', (e) => {
                if (e.target.closest('.wb-file-del')) return;
                const docArtifact = (this.task.artifacts || []).find(
                    a => a.type === 'MATERIAL_DOC' && a.ref_key === docId
                );
                if (docArtifact) {
                    this.openArtifact(docArtifact.id);
                } else {
                    Toast.warning('该材料尚未生成独立分析成果视图');
                }
            });

            return rowEl
        },

        async _deleteMaterial(documentId) {
            if (!documentId || !this.task) return;
            try {
                const resp = await fetch(
                    `/api/tasks/${this.task.id}/materials/${documentId}`,
                    { method: 'DELETE' }
                );
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '删除失败');
                    return;
                }
                Toast.success('材料已删除');
                const gone = new Set(
                    (this.task.artifacts || [])
                        .filter(a => a.type === 'MATERIAL_DOC' && a.ref_key === documentId)
                        .map(a => a.id)
                );
                this.tabs = this.tabs.filter(t => !gone.has(t.id));
                if (gone.has(this.activeTabId)) this.activeTabId = null;
                if (data.task) {
                    this.task = data.task;
                } else {
                    const taskResp = await fetch(`/api/tasks/${this.task.id}`);
                    this.task = await taskResp.json();
                }
                this._renderDirectory();
                const batchId = data.batch_artifact_id
                    || (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH')?.id;
                if (batchId) await this.openArtifact(batchId);
                else this._renderPanel();
            } catch (e) {
                Toast.error('删除失败：' + e.message);
            }
        },

        _sizeText(size) {
            if (!size) return '';
            if (size < 1024) return size + ' B';
            if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
            return (size / 1024 / 1024).toFixed(1) + ' MB';
        },

        _uploadBox() {
            const select = Utils.create('select', { id: 'wb-upload-case' });
            (this.task.cases || []).forEach(c => {
                const opt = Utils.create('option', { value: c.case_id, text: c.display_name });
                select.appendChild(opt);
            });

            const input = Utils.create('input', {
                type: 'file',
                multiple: 'multiple',
                accept: '.pdf,.docx,.txt,.png,.jpg,.jpeg'
            });
            input.style.fontSize = '12px';

            const btn = this._iconBtn('wb-btn wb-btn-primary', 'upload', '上传到该案件');
            btn.addEventListener('click', () => this._uploadMaterials(select.value, input.files, btn));

            const refresh = this._iconBtn('wb-btn wb-btn-ghost', 'refresh', '刷新进度');
            refresh.addEventListener('click', () => this._refreshBatch());

            const box = Utils.create('div', { class: 'wb-upload-box' }, [
                Utils.create('span', { class: 'wb-file-meta', text: '材料必须归属案件：' }),
                select, input, btn, refresh
            ]);

            // 拖拽上传支持
            let dragCounter = 0;
            box.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.stopPropagation();
                box.classList.add('wb-upload-dragover');
            });
            box.addEventListener('dragleave', (e) => {
                e.preventDefault();
                e.stopPropagation();
                dragCounter--;
                if (dragCounter <= 0) {
                    dragCounter = 0;
                    box.classList.remove('wb-upload-dragover');
                }
            });
            box.addEventListener('drop', (e) => {
                e.preventDefault();
                e.stopPropagation();
                box.classList.remove('wb-upload-dragover');
                dragCounter = 0;
                const files = e.dataTransfer.files;
                if (files && files.length > 0) {
                    // 拖拽文件自动上传到当前选中的案件
                    this._uploadMaterials(select.value, files, null);
                }
            });

            return box;
        },

        async _executeAnalysis(button) {
            if (!this.draftTaskId && this.task) this.draftTaskId = this.task.id;
            if (!this.draftTaskId && !(this.task && this.task.id)) return;
            if (button) {
                button.disabled = true;
                button.textContent = '执行中…';
            }
            const prompt = [
                '请对本监督分析任务执行完整跨案分析：',
                '先查看任务范围与材料是否可用于分析；若计划仍为草稿则先确认；',
                '再开展跨案标识比对，整理转账与联络事件时间线，形成可回原文的疑似关联线索。',
                '每完成一步根据材料情况决定是否继续；完成后请提示到中间工作区打开相应分析成果核验原文。',
                '禁止输出定罪、并案、主从犯或量刑结论；回复勿使用函数名、接口或工程术语。'
            ].join('');
            try {
                if (!window.Agent) throw new Error('智能体未就绪');
                await Agent.process(prompt);
                Toast.success('本轮跨案分析已结束');
            } catch (e) {
                Toast.error(e.message || '分析未能完成');
            } finally {
                if (button) {
                    button.disabled = false;
                    button.textContent = '开始跨案分析';
                }
            }
        },

        async _waitMaterialsReady(maxRounds = 8) {
            for (let i = 0; i < maxRounds; i++) {
                const resp = await fetch(`/api/tasks/${this.task.id}/materials`);
                const data = await resp.json();
                const batch = (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH');
                if (batch) await this.openArtifact(batch.id);
                const overview = batch
                    ? ((this.tabs.find(t => t.id === batch.id) || {}).data || {}).payload
                    : null;
                const groups = (overview && overview.groups) || [];
                const materials = groups.flatMap(g => g.materials || []);
                if (!materials.length) return;
                const pending = materials.some(m => ['UPLOADED', 'PARSING'].includes(m.status));
                if (!pending) return;
                await Utils.sleep(1500);
            }
        },

        _mountRunProgress(stepLabels) {
            const messages = Utils.$('#chat-messages');
            if (!messages || !window.Thinking) return { thinking: null, fills: [] };
            const thinking = Thinking.create({
                title: '开始跨案分析',
                defaultExpanded: false,
                steps: stepLabels.map(text => ({ text, status: '' }))
            });
            const fills = [];
            Utils.$$('.thinking-step', thinking).forEach(step => {
                const fill = Utils.create('div', { class: 'wb-step-bar-fill' });
                step.appendChild(Utils.create('div', { class: 'wb-step-bar' }, [fill]));
                fills.push(fill);
            });
            messages.appendChild(thinking);
            messages.scrollTop = messages.scrollHeight;
            return { thinking, fills };
        },

        _setRunStep(run, index, status, percent) {
            if (!run || !run.thinking || !window.Thinking) return;
            if (status) Thinking.updateStep(run.thinking, index, status);
            const fill = (run.fills || [])[index];
            if (fill) {
                fill.style.width = `${percent || 0}%`;
                fill.classList.toggle('active', status === 'active');
            }
        },

        _openLatest(type) {
            const item = (this.task.artifacts || []).find(a => a.type === type);
            if (item) this.openArtifact(item.id);
        },

        async _postJson(url) {
            const resp = await fetch(url, { method: 'POST' });
            return resp.json();
        },

        async _uploadMaterials(caseId, files, btn) {
            if (!files || !files.length) {
                Toast.warning('请先选择材料文件');
                return;
            }
            const form = new FormData();
            form.append('case_id', caseId);
            Array.from(files).forEach(f => form.append('files', f));

            const taskId = (this.task && this.task.id) || this.draftTaskId;
            if (!taskId) {
                Toast.error('任务尚未创建，无法上传材料');
                return false;
            }
            if (btn) {
                btn.disabled = true;
                btn.textContent = '上传中…';
            }
            try {
                const resp = await fetch(`/api/tasks/${taskId}/materials`, {
                    method: 'POST',
                    body: form
                });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '上传失败');
                    return false;
                }
                this.task = data.task;
                this._renderDirectory();
                const batch = (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH');
                if (batch && !Utils.$('#wb-workspace').hidden) {
                    await this.openArtifact(batch.id);
                    this._postArtifactCard(batch.id, '材料接入与质量', `已接收 ${files.length} 份材料，可在此查看逐份处理进度。`);
                }
                Toast.success('材料已接入，正在处理');
                this._maybeOfferRerun();
                return true;
            } catch (e) {
                Toast.error('上传失败：' + e.message);
                return false;
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = '上传到该案件';
                }
            }
        },

        async _refreshBatch() {
            if (!this.task) return;
            const resp = await fetch(`/api/tasks/${this.task.id}/materials`);
            const data = await resp.json();
            if (data.error_code) return;
            const taskResp = await fetch(`/api/tasks/${this.task.id}`);
            const task = await taskResp.json();
            if (!task.error_code) {
                this.task = task;
                this._updateNavBadges();
            }
            const batchId = data.artifact_id
                || (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH')?.id;
            if (batchId) {
                delete this.artifactCache[batchId];
                await this._fetchArtifact(batchId);
            }
            if (this.currentView === 'materials') {
                await this._renderCurrentView();
            }
        },

        _schedulePoll(payload) {
            clearTimeout(this.pollTimer);
            const pending = (payload.groups || []).some(g =>
                (g.materials || []).filter(m => m.status !== 'DELETED')
                    .some(m => ['UPLOADED', 'PARSING'].includes(m.status))
            );
            if (!pending) return;
            this.pollTimer = setTimeout(() => this._refreshBatch(), 4000);
        },

        _renderGeneric(panel, payload) {
            panel.appendChild(Utils.create('pre', {
                class: 'wb-file-meta',
                text: JSON.stringify(payload, null, 2)
            }));
        },

        /** 在智能体消息流中投放分析成果卡片，点击后打开对应业务页 */
        _postArtifactCard(artifactId, title, desc) {
            const messages = Utils.$('#chat-messages');
            if (!messages) return;
            const btn = this._iconBtn('wb-btn wb-btn-ghost', 'fileCheck2', '打开分析成果');
            btn.className = 'wb-artifact-open';
            btn.addEventListener('click', () => this.openArtifact(artifactId));
            const card = Utils.create('div', { class: 'wb-artifact-card' }, [
                Utils.create('div', { class: 't', text: title }),
                Utils.create('div', { class: 'd', text: desc }),
                btn
            ]);
            messages.appendChild(card);
            messages.scrollTop = messages.scrollHeight;
        },

        async _softRefreshTask() {
            if (!this.task) return;
            try {
                const resp = await fetch(`/api/tasks/${this.task.id}`);
                const task = await resp.json();
                if (!task.error_code) {
                    this.task = task;
                    this._renderNav();
                    if (Utils.$('#wb-workspace') && !Utils.$('#wb-workspace').hidden) {
                        await this._renderCurrentView();
                    }
                }
            } catch (_) { /* ignore */ }
        },

        async setView(view) {
            this.currentView = view || 'tasks';
            // 切换视图时重申主题，防止错误 data-theme 残留导致“突然变浅色”
            if (window.State && State.applyThemeMode) {
                State.applyThemeMode(State.themeMode || 'system');
            }
            this._updateNavActive();
            await this._renderCurrentView();
        },

        async _renderCurrentView() {
            const root = Utils.$('#wb-view');
            if (!root || !this.task) return;
            root.innerHTML = '';
            const map = {
                tasks: () => this._viewTasks(root),
                materials: () => this._viewMaterials(root),
                entities: () => this._viewEntities(root),
                leads: () => this._viewLeads(root),
                timeline: () => this._viewTimeline(root),
                graph: () => this._viewGraph(root),
                reports: () => this._viewReports(root)
            };
            const fn = map[this.currentView] || map.tasks;
            await fn();
            this._updateNavBadges();
        },

        _pageHead(title, desc, trailing, badgeText) {
            const wrap = Utils.create('div', {});
            wrap.appendChild(this._crumb(title));
            const head = Utils.create('div', { class: 'wb-page-head' });
            const titleEl = Utils.create('div', { class: 'wb-page-title' });
            if (window.Icons) {
                const iconHtml = Icons.forView(this.currentView).replace('wb-nav-ico', 'wb-ico wb-page-title-ico');
                titleEl.innerHTML = iconHtml + `<span>${title}</span>`;
            } else {
                titleEl.textContent = title;
            }
            const titleRow = Utils.create('div', { class: 'wb-page-head-row' }, [titleEl]);
            if (badgeText) {
                titleRow.appendChild(Utils.create('span', { class: 'wb-page-badge', text: badgeText }));
            }
            const left = Utils.create('div', {}, [
                titleRow,
                Utils.create('div', { class: 'wb-page-desc', text: desc || '' })
            ]);
            head.appendChild(left);
            if (trailing) head.appendChild(trailing);
            wrap.appendChild(head);
            return wrap;
        },

        async _viewTasks(root) {
            const task = this.task;
            const cases = task.cases || [];
            const statusLabel = task.status === 'SCOPE_DRAFT' ? '范围待完善' : '可分析';
            const statusTone = task.status === 'SCOPE_DRAFT' ? 'warn' : 'ok';
            const hasMaterials = (task.artifacts || []).some((a) =>
                a.type === 'MATERIAL_BATCH' || a.type === 'MATERIAL_DOC'
            );
            const canRun = task.status !== 'SCOPE_DRAFT' && cases.length >= 2;

            root.appendChild(this._crumb('分析任务'));

            const grid = Utils.create('div', { class: 'ref-tasks-grid' });
            const main = Utils.create('div', { class: 'ref-tasks-main' });

            const meta = Utils.create('div', { class: 'ref-meta-card' });
            meta.appendChild(Utils.create('div', { class: 'ref-meta-top' }, [
                Utils.create('div', {}, [
                    Utils.create('div', { class: 'ref-meta-title', text: task.title || '未命名任务' }),
                    Utils.create('div', { class: 'ref-meta-purpose', text: task.purpose || '—' })
                ]),
                this._statusTag(statusLabel, statusTone)
            ]));

            const stat = (iconName, k, v) => {
                const kEl = Utils.create('div', { class: 'ref-meta-stat-k' });
                if (window.Icons) kEl.innerHTML = Icons.svg(iconName, 'wb-ico') + `<span>${k}</span>`;
                else kEl.textContent = k;
                return Utils.create('div', {}, [
                    kEl,
                    Utils.create('div', { class: 'ref-meta-stat-v', text: v })
                ]);
            };
            meta.appendChild(Utils.create('div', { class: 'ref-meta-stats' }, [
                stat('usersRound', '负责人 / 成员', '检察官 · 演示账号'),
                stat('calendar', '授权有效期', task.authorized_until ? `至 ${task.authorized_until}` : '—'),
                stat('fileCheck2', '当前版本', `范围 v1 · 分析产物 ${(task.artifacts || []).length}`)
            ]));
            main.appendChild(meta);

            const runBar = Utils.create('div', { class: 'ref-run-bar' });
            runBar.appendChild(Utils.create('p', {
                text: canRun
                    ? (hasMaterials
                        ? '案件范围、授权与材料检查均已通过，可发起本轮跨案关联分析（由右侧智能体执行）'
                        : '案件范围已具备，建议先在「材料中心」确认卷宗可用后再发起分析')
                    : '请先完成案件范围与材料处理，满足前置条件后才能发起分析'
            }));
            const runBtn = this._iconBtn('wb-btn wb-btn-primary', 'play', '发起跨案关联分析');
            if (!canRun) runBtn.disabled = true;
            runBtn.addEventListener('click', () => this._executeAnalysis(runBtn));
            runBar.appendChild(runBtn);
            main.appendChild(runBar);

            const scopeWrap = Utils.create('div', { class: 'wb-panel-card', style: 'padding:0;overflow:hidden' });
            const scopeHead = Utils.create('div', { class: 'ref-scope-head', style: 'padding:14px 16px;margin:0' });
            scopeHead.appendChild(Utils.create('div', {}, [
                Utils.create('h2', { text: '案件范围' }),
                Utils.create('p', { text: `已纳入 ${cases.length} 起案件 · ${cases.length} 起已完成授权确认` })
            ]));
            const addCaseBtn = this._iconBtn('wb-btn wb-btn-outline', 'plus', '添加案件');
            addCaseBtn.addEventListener('click', () => {
                Toast.info('请通过「新建分析任务」调整案件范围');
                this.showStart();
            });
            scopeHead.appendChild(addCaseBtn);
            scopeWrap.appendChild(scopeHead);

            const tableWrap = Utils.create('div', { class: 'wb-table-wrap', style: 'border:0;border-radius:0;border-top:1px solid var(--border-color)' });
            const table = Utils.create('table', { class: 'wb-table' });
            table.appendChild(Utils.create('thead', {}, [
                Utils.create('tr', {}, [
                    Utils.create('th', { text: '案件' }),
                    Utils.create('th', { text: '脱敏案号' }),
                    Utils.create('th', { text: '所属院' }),
                    Utils.create('th', { text: '授权状态' }),
                    Utils.create('th', { text: '纳入时间' }),
                    Utils.create('th', { text: '材料数' }),
                    Utils.create('th', { text: '质量问题' })
                ])
            ]));
            const tbody = Utils.create('tbody');
            const batch = await this._ensureMaterialBatch();
            const matByCase = {};
            ((batch && batch.payload && batch.payload.groups) || []).forEach((g) => {
                const mats = (g.materials || []).filter((m) => m.status !== 'DELETED');
                matByCase[g.case_id] = {
                    count: mats.length,
                    issues: mats.filter((m) => ATTENTION.includes(m.status)).length
                };
            });
            cases.forEach((c) => {
                const stats = matByCase[c.case_id] || { count: 0, issues: 0 };
                const issueCell = stats.issues > 0
                    ? Utils.create('td', {}, [this._statusTag(String(stats.issues), 'warn')])
                    : Utils.create('td', { text: '—' });
                tbody.appendChild(Utils.create('tr', {}, [
                    Utils.create('td', { text: c.display_name || c.name || '—' }),
                    Utils.create('td', { text: (c.case_id || '—').slice(0, 12) }),
                    Utils.create('td', { text: '演示检察院' }),
                    Utils.create('td', {}, [this._statusTag('已授权', 'ok')]),
                    Utils.create('td', { text: (c.created_at || task.created_at || '—').toString().slice(0, 10) }),
                    Utils.create('td', { text: String(stats.count) }),
                    issueCell
                ]));
            });
            if (!cases.length) {
                tbody.appendChild(Utils.create('tr', {}, [
                    Utils.create('td', { text: '尚未纳入案件', colspan: '7' })
                ]));
            }
            table.appendChild(tbody);
            tableWrap.appendChild(table);
            scopeWrap.appendChild(tableWrap);
            main.appendChild(scopeWrap);
            grid.appendChild(main);

            const switcher = Utils.create('div', { class: 'wb-panel-card ref-task-switcher' });
            switcher.appendChild(Utils.create('div', { class: 'wb-panel-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '全部分析任务' })
            ]));
            const listBody = Utils.create('div', { class: 'wb-panel-card-body' });
            (this.tasks || []).forEach((t) => {
                const st = t.status === 'SCOPE_DRAFT' ? '待材料复核' : '可分析';
                const tone = t.status === 'SCOPE_DRAFT' ? 'warn' : 'ok';
                const row = Utils.create('button', {
                    type: 'button',
                    class: `wb-list-item${this.task && this.task.id === t.id ? ' active' : ''}`
                }, [
                    Utils.create('div', {
                        style: 'display:flex;justify-content:space-between;gap:8px;align-items:flex-start'
                    }, [
                        Utils.create('div', { class: 'wb-list-item-title', text: t.title || '未命名' }),
                        this._statusTag(st, tone)
                    ]),
                    Utils.create('div', {
                        class: 'wb-list-item-meta',
                        text: `${t.case_count || 0} 起案件 · 更新于 ${(t.updated_at || '').toString().slice(0, 10) || '—'}`
                    })
                ]);
                row.addEventListener('click', () => this.openTask(t.id));
                listBody.appendChild(row);
            });
            switcher.appendChild(listBody);
            grid.appendChild(switcher);
            root.appendChild(grid);
        },

        async _ensureMaterialBatch() {
            const batch = (this.task.artifacts || []).find((a) => a.type === 'MATERIAL_BATCH');
            if (!batch) return null;
            if (!this.artifactCache[batch.id]) {
                await fetch(`/api/tasks/${this.task.id}/materials`).catch(() => null);
                await this._fetchArtifact(batch.id);
            } else {
                await this._fetchArtifact(batch.id);
            }
            return this.artifactCache[batch.id];
        },

        async _viewMaterials(root) {
            const data = await this._ensureMaterialBatch();
            const payload = (data && data.payload) || { groups: [], totals: {} };
            const totals = payload.totals || {};
            const rows = [];
            (payload.groups || []).forEach((g) => {
                (g.materials || []).filter((m) => m.status !== 'DELETED').forEach((m) => {
                    rows.push({
                        ...m,
                        case_name: g.case_name,
                        case_id: g.case_id
                    });
                });
            });
            const blocked = rows.filter((m) => ATTENTION.includes(m.status)).length;
            const uploadBtn = this._iconBtn('wb-btn wb-btn-primary', 'upload', '上传材料');
            uploadBtn.addEventListener('click', () => {
                const box = this._uploadBox(payload);
                root.appendChild(box);
                box.scrollIntoView({ behavior: 'smooth' });
            });
            root.appendChild(this._pageHead(
                '材料中心',
                `共 ${rows.length} 份材料 · ${blocked} 份存在处理阻断问题，需处理后才能纳入分析`,
                uploadBtn,
                blocked ? `${blocked} 待处理` : `${rows.length} 份`
            ));

            const toolbar = Utils.create('div', { class: 'wb-toolbar' });
            const searchWrap = Utils.create('div', { class: 'wb-input-ico' });
            if (window.Icons) searchWrap.appendChild(Icons.el('search', 'wb-input-ico-mark'));
            const search = Utils.create('input', {
                type: 'search',
                placeholder: '搜索文件名',
                value: this.materialsQuery || ''
            });
            searchWrap.appendChild(search);
            search.addEventListener('input', () => {
                this.materialsQuery = search.value;
                this._renderCurrentView();
            });
            const caseSelect = Utils.create('select');
            caseSelect.appendChild(Utils.create('option', { value: 'all', text: '全部案件' }));
            (this.task.cases || []).forEach((c) => {
                caseSelect.appendChild(Utils.create('option', {
                    value: c.case_id,
                    text: c.display_name || c.name
                }));
            });
            caseSelect.value = this.materialsCaseFilter || 'all';
            caseSelect.addEventListener('change', () => {
                this.materialsCaseFilter = caseSelect.value;
                this._renderCurrentView();
            });
            toolbar.appendChild(searchWrap);
            toolbar.appendChild(caseSelect);
            root.appendChild(toolbar);

            const q = (this.materialsQuery || '').toLowerCase();
            const filtered = rows.filter((m) => {
                if (this.materialsCaseFilter !== 'all' && m.case_id !== this.materialsCaseFilter) return false;
                if (q && !(m.filename || '').toLowerCase().includes(q)) return false;
                return true;
            });

            const wrap = Utils.create('div', { class: 'wb-table-wrap' });
            const table = Utils.create('table', { class: 'wb-table' });
            table.appendChild(Utils.create('thead', {}, [
                Utils.create('tr', {}, [
                    Utils.create('th', { text: '文件名' }),
                    Utils.create('th', { text: '所属案件' }),
                    Utils.create('th', { text: '材料类型' }),
                    Utils.create('th', { text: '版本' }),
                    Utils.create('th', { text: '处理状态' }),
                    Utils.create('th', { text: '脱敏' }),
                    Utils.create('th', { text: '分析可用性' }),
                    Utils.create('th', { text: '上传时间' })
                ])
            ]));
            const tbody = Utils.create('tbody');
            if (!filtered.length) {
                tbody.appendChild(Utils.create('tr', {}, [
                    Utils.create('td', { text: '暂无材料，请上传卷宗', colspan: '8' })
                ]));
            }
            filtered.forEach((m) => {
                const statusText = STAGE_TEXT[m.status] || m.status;
                const ready = m.status === 'PARSED';
                const warn = ATTENTION.includes(m.status);
                const fail = m.status === 'FAILED' || m.status === 'OCR_FAILED';
                const statusTone = fail ? 'danger' : (warn ? 'warn' : (ready ? 'ok' : 'neutral'));
                const availTone = ready ? 'ok' : (warn || fail ? 'danger' : 'warn');
                const availText = ready ? '可用于分析' : (warn || fail ? '暂不允许' : '处理中');
                const redacted = m.redacted !== false;
                const tr = Utils.create('tr');
                tr.appendChild(Utils.create('td', { text: m.filename || '—' }));
                tr.appendChild(Utils.create('td', { text: m.case_name || '—' }));
                tr.appendChild(Utils.create('td', { text: m.doc_type || m.material_type || '卷宗' }));
                tr.appendChild(Utils.create('td', { text: m.version != null ? `v${m.version}` : '—' }));
                tr.appendChild(Utils.create('td', {}, [this._statusTag(statusText, statusTone)]));
                const shield = Utils.create('td');
                if (window.Icons) {
                    shield.innerHTML = Icons.svg(redacted ? 'shieldCheck' : 'shield', `wb-ico wb-shield ${redacted ? 'ok' : 'off'}`);
                    shield.title = redacted ? '已脱敏' : '未脱敏';
                } else {
                    shield.textContent = redacted ? '已脱敏' : '未脱敏';
                }
                tr.appendChild(shield);
                tr.appendChild(Utils.create('td', {}, [this._statusTag(availText, availTone)]));
                tr.appendChild(Utils.create('td', {
                    text: (m.uploaded_at || m.created_at || '—').toString().slice(0, 19)
                }));
                tr.addEventListener('click', async () => {
                    if (m.document_id) {
                        const preview = await fetch(`/api/materials/${m.document_id}/preview`).then((r) => r.json()).catch(() => null);
                        if (preview && !preview.error_code) {
                            this._renderCitePane({
                                error: false,
                                title: m.filename || '材料预览',
                                text: preview.text || preview.preview || JSON.stringify(preview).slice(0, 2000),
                                meta: `${m.case_name || ''} · ${statusText}`
                            }, { quote: '' });
                        } else {
                            Toast.info('暂无法预览该材料正文');
                        }
                    }
                });
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            wrap.appendChild(table);
            root.appendChild(wrap);
            root.appendChild(this._uploadBox(payload));
            this._schedulePoll(payload);
        },

        async _ensureEntitySet() {
            const art = (this.task.artifacts || []).find((a) => a.type === 'ENTITY_CANDIDATE_SET');
            if (!art) return null;
            return this.artifactCache[art.id] || this._fetchArtifact(art.id);
        },

        async _viewEntities(root) {
            const data = await this._ensureEntitySet();
            const payload = (data && data.payload) || {};
            const candidates = payload.candidates || [];
            const version = data ? data.version : 1;
            const artifact = data ? data.artifact : null;
            const status = data ? data.status : 'DRAFT';

            const pendingCount = candidates.filter((c) => !c.decision || c.decision === 'PENDING' || c.decision === 'DEFER').length;
            root.appendChild(this._pageHead(
                '实体复核',
                '逐条核验跨案实体是否为同一主体；确认结果影响线索与图谱。',
                null,
                `${pendingCount} 个待选`
            ));
            root.appendChild(Utils.create('div', { class: 'wb-alert warn' }, [
                window.Icons ? Icons.el('info') : Utils.create('span', { text: 'ℹ' }),
                Utils.create('span', {
                    text: payload.boundary || '复核提示：系统仅提供可解释的关联候选。请结合原文与字段差异作出判断，不要将候选直接作为事实结论。'
                })
            ]));

            if (!candidates.length) {
                root.appendChild(Utils.create('div', { class: 'wb-empty', text: '尚无跨案对象候选。请先在「分析任务」发起跨案分析或标识比对。' }));
                const run = this._iconBtn('wb-btn wb-btn-primary', 'play', '开始跨案分析');
                run.style.marginTop = '12px';
                run.addEventListener('click', () => this._executeAnalysis(run));
                root.appendChild(run);
                return;
            }

            if (!this.selectedEntityId || !candidates.some((c) => c.candidate_id === this.selectedEntityId)) {
                this.selectedEntityId = candidates[0].candidate_id;
            }
            const selected = candidates.find((c) => c.candidate_id === this.selectedEntityId) || candidates[0];

            const split = Utils.create('div', { class: 'wb-split-view' });
            const listCard = Utils.create('div', { class: 'wb-list-card' });
            listCard.appendChild(Utils.create('div', { class: 'wb-list-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '候选列表' })
            ]));
            const listBody = Utils.create('div', { class: 'wb-list-card-body' });
            candidates.forEach((c) => {
                const decision = c.decision || 'PENDING';
                const label = { PENDING: '待确认', MERGE: '视为同一', KEEP_SEPARATE: '保留独立', CORRECT: '已更正', DEFER: '待重新核验' }[decision] || decision;
                const tone = decision === 'PENDING' || decision === 'DEFER' ? 'warn' : (decision === 'KEEP_SEPARATE' ? 'danger' : 'ok');
                const typeIcon = window.Icons
                    ? Icons.el(Icons.forEntityType(c.object_type || c.title), 'wb-list-type-ico')
                    : null;
                const titleRow = Utils.create('div', {
                    style: 'display:flex;justify-content:space-between;gap:8px;align-items:center'
                });
                const titleLeft = Utils.create('div', {
                    style: 'display:flex;align-items:center;gap:8px;min-width:0'
                });
                if (typeIcon) titleLeft.appendChild(typeIcon);
                titleLeft.appendChild(Utils.create('div', {
                    class: 'wb-list-item-title',
                    text: c.title || c.display_name || '跨案对象'
                }));
                titleRow.appendChild(titleLeft);
                titleRow.appendChild(this._statusTag(label, tone));
                const item = Utils.create('button', {
                    type: 'button',
                    class: `wb-list-item${c.candidate_id === selected.candidate_id ? ' active' : ''}`
                }, [
                    titleRow,
                    Utils.create('div', {
                        class: 'wb-list-item-meta',
                        text: `${(c.cases || []).length || 1} 案件 · ${(c.cases || []).map((x) => x.case_name || x.case_id).slice(0, 2).join(' · ') || '多案'}`
                    })
                ]);
                item.addEventListener('click', () => {
                    this.selectedEntityId = c.candidate_id;
                    this._renderCurrentView();
                });
                listBody.appendChild(item);
            });
            listCard.appendChild(listBody);
            split.appendChild(listCard);

            const detail = Utils.create('div', { class: 'wb-detail-card' });
            detail.appendChild(Utils.create('div', { class: 'wb-detail-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: selected.title || selected.display_name || '对象详情' }),
                Utils.create('div', { class: 'wb-file-meta', text: selected.confidence_label || '待核验' })
            ]));
            const detailBody = Utils.create('div', { class: 'wb-detail-card-body' });
            detailBody.appendChild(Utils.create('div', {
                class: 'wb-callout warn',
                style: 'margin-bottom:10px'
            }, [
                Utils.create('span', {
                    text: selected.question || `请核验：相关案件中该标识是否指向同一对象？`
                })
            ]));

            const fields = selected.field_compare || selected.fields || [];
            if (fields.length) {
                const grid = Utils.create('div', { class: 'wb-field-grid' });
                fields.forEach((f) => {
                    const diff = f.same === false || f.status === 'diff';
                    const miss = f.status === 'missing' || (!f.detail && !f.values);
                    grid.appendChild(Utils.create('div', { class: 'wb-field-row-cmp' }, [
                        Utils.create('span', { text: f.label || f.name || '字段' }),
                        Utils.create('span', {
                            class: miss ? 'wb-match-na' : (diff ? 'wb-match-bad' : 'wb-match-ok'),
                            text: miss ? '未记载' : (diff ? '不一致' : '一致')
                        }),
                        Utils.create('span', { text: f.detail || f.values || '—' })
                    ]));
                });
                detailBody.appendChild(grid);
            } else if (selected.evidence || selected.records) {
                (selected.evidence || selected.records || []).slice(0, 6).forEach((rec) => {
                    const row = Utils.create('div', { class: 'wb-entity-record' }, [
                        Utils.create('div', { class: 'case', text: rec.case_name || rec.case_id || '案件' }),
                        Utils.create('div', { class: 'value', text: rec.display_value || rec.value || selected.display_name || '标识' }),
                        Utils.create('div', { class: 'source', text: rec.filename || rec.quote || '' })
                    ]);
                    if (rec.chunk_id && rec.quote_hash) {
                        row.style.cursor = 'pointer';
                        row.addEventListener('click', () => this._openCitation(rec));
                    }
                    detailBody.appendChild(row);
                });
            }

            const openCite = this._iconBtn('wb-btn wb-btn-outline', 'externalLink', '打开原文');
            const firstEv = ((selected.evidence || selected.records || [])[0]) || null;
            openCite.addEventListener('click', () => {
                if (firstEv) this._openCitation(firstEv);
                else Toast.info('暂无可用原文定位');
            });
            detailBody.appendChild(openCite);

            if (artifact && (selected.decision || 'PENDING') === 'PENDING' && status !== 'STALE') {
                const actions = Utils.create('div', { class: 'wb-entity-actions', style: 'justify-content:flex-start;margin-top:12px' });
                [
                    ['MERGE', '确认关联', 'check', 'wb-btn wb-btn-primary'],
                    ['KEEP_SEPARATE', '保留为独立主体', 'x', 'wb-btn wb-btn-outline'],
                    ['CORRECT', '更正', 'fileCheck2', 'wb-btn wb-btn-ghost'],
                    ['DEFER', '暂缓', 'info', 'wb-btn wb-btn-ghost']
                ].forEach(([decision, label, icon, cls]) => {
                    const btn = this._iconBtn(cls, icon, label);
                    btn.addEventListener('click', () => {
                        this._showEntityDecisionForm(detailBody, selected, decision, label, version);
                    });
                    actions.appendChild(btn);
                });
                detailBody.appendChild(actions);
            } else if (selected.decision && selected.decision !== 'PENDING') {
                detailBody.appendChild(Utils.create('div', {
                    class: 'wb-file-meta',
                    text: `已记录：${selected.decision} · ${selected.reason || ''}`
                }));
            }
            detail.appendChild(detailBody);
            split.appendChild(detail);
            root.appendChild(split);
        },

        async _collectClueItems() {
            const arts = (this.task.artifacts || []).filter((a) => a.type === 'CLUE_ITEM');
            const items = [];
            for (const a of arts) {
                const data = this.artifactCache[a.id] || await this._fetchArtifact(a.id);
                if (data) items.push(data);
            }
            return items;
        },

        async _viewLeads(root) {
            const items = await this._collectClueItems();
            const exportBtn = this._iconBtn('wb-btn wb-btn-outline', 'filter', '导出筛选结果');
            exportBtn.addEventListener('click', () => Toast.info('线索导出即将接入'));
            root.appendChild(this._pageHead(
                '关联线索中心',
                '按证据与核验状态管理疑似关联线索；处置结果写入核验留痕。',
                exportBtn,
                `${items.length} 条线索`
            ));

            if (!items.length) {
                root.appendChild(Utils.create('div', { class: 'wb-empty', text: '尚无线索。请先发起跨案分析或生成疑似关联线索。' }));
                return;
            }

            const toolbar = Utils.create('div', { class: 'wb-toolbar' });
            const searchWrap = Utils.create('div', { class: 'wb-input-ico' });
            if (window.Icons) searchWrap.appendChild(Icons.el('search', 'wb-input-ico-mark'));
            const search = Utils.create('input', {
                type: 'search',
                placeholder: '搜索线索、对象或案件',
                value: this.leadsQuery || ''
            });
            searchWrap.appendChild(search);
            search.addEventListener('input', () => {
                this.leadsQuery = search.value;
                this._renderCurrentView();
            });
            const statusSel = Utils.create('select');
            [
                ['all', '全部状态'],
                ['PENDING', '待处理'],
                ['CONTINUE', '继续核查'],
                ['NEED_MATERIAL', '待补材料'],
                ['EXCLUDE', '已排除'],
                ['DEFER', '暂缓']
            ].forEach(([v, t]) => statusSel.appendChild(Utils.create('option', { value: v, text: t })));
            statusSel.value = this.leadsStatusFilter || 'all';
            statusSel.addEventListener('change', () => {
                this.leadsStatusFilter = statusSel.value;
                this._renderCurrentView();
            });
            toolbar.appendChild(searchWrap);
            toolbar.appendChild(statusSel);
            root.appendChild(toolbar);

            const q = (this.leadsQuery || '').toLowerCase();
            const filtered = items.filter((d) => {
                const p = d.payload || {};
                const disp = p.disposition || 'PENDING';
                if (this.leadsStatusFilter !== 'all' && disp !== this.leadsStatusFilter) return false;
                const hay = `${p.title || ''} ${(p.objects || []).join(' ')} ${(p.cases || []).join(' ')}`.toLowerCase();
                return !q || hay.includes(q);
            });

            if (!this.selectedLeadId || !filtered.some((d) => d.artifact.id === this.selectedLeadId)) {
                this.selectedLeadId = (filtered[0] && filtered[0].artifact.id) || null;
            }
            const selected = filtered.find((d) => d.artifact.id === this.selectedLeadId) || filtered[0];

            const split = Utils.create('div', { class: 'wb-split-view leads' });
            const listCard = Utils.create('div', { class: 'wb-list-card' });
            listCard.appendChild(Utils.create('div', { class: 'wb-list-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '线索列表' })
            ]));
            const listBody = Utils.create('div', { class: 'wb-list-card-body' });
            filtered.forEach((d) => {
                const p = d.payload || {};
                const disp = p.disposition || 'PENDING';
                const label = {
                    PENDING: '待确认', CONTINUE: '已确认关联', NEED_MATERIAL: '待补证',
                    EXCLUDE: '已排除', DEFER: '暂缓'
                }[disp] || disp;
                const tone = disp === 'EXCLUDE' ? 'danger' : (disp === 'PENDING' || disp === 'NEED_MATERIAL' || disp === 'DEFER' ? 'warn' : 'ok');
                const item = Utils.create('button', {
                    type: 'button',
                    class: `wb-list-item${d.artifact.id === selected.artifact.id ? ' active' : ''}`
                }, [
                    Utils.create('div', { class: 'wb-list-item-title', text: p.title || d.artifact.title }),
                    Utils.create('div', {
                        style: 'display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center'
                    }, [
                        this._statusTag(label, tone),
                        Utils.create('span', {
                            class: 'wb-file-meta',
                            text: (p.objects || []).slice(0, 3).join(' · ')
                        })
                    ])
                ]);
                item.addEventListener('click', () => {
                    this.selectedLeadId = d.artifact.id;
                    this._renderCurrentView();
                });
                listBody.appendChild(item);
            });
            listCard.appendChild(listBody);
            split.appendChild(listCard);

            const p = selected.payload || {};
            const detail = Utils.create('div', { class: 'wb-detail-card' });
            detail.appendChild(Utils.create('div', { class: 'wb-detail-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '线索处置' }),
                Utils.create('div', { class: 'wb-file-meta', text: selected.artifact.id })
            ]));
            const detailBody = Utils.create('div', { class: 'wb-detail-card-body' });
            detailBody.appendChild(Utils.create('div', {
                class: 'wb-callout',
                text: p.uncertainty || p.boundary || '标识重合仅为待核验线索，不代表同一人或共同犯罪。'
            }));
            const support = (p.evidence || []).length;
            const counter = (p.counter_evidence || []).length;
            detailBody.appendChild(Utils.create('div', {
                class: 'wb-entity-title',
                text: '证据概览',
                style: 'font-size:13px;margin:12px 0 6px'
            }));
            const evMeta = Utils.create('div', { class: 'wb-file-meta' });
            evMeta.innerHTML = `<span>${support} 条支持材料</span> · <span class="wb-match-bad">${counter} 条反向材料</span>`;
            detailBody.appendChild(evMeta);
            const openEv = this._iconBtn('wb-btn wb-btn-outline', 'externalLink', '打开原文依据');
            openEv.style.width = '100%';
            openEv.style.marginTop = '10px';
            openEv.addEventListener('click', () => {
                const ev = (p.evidence || [])[0];
                if (ev) this._openCitation(ev);
                else Toast.info('暂无原文依据');
            });
            detailBody.appendChild(openEv);

            if (!p.disposition || p.disposition === 'PENDING') {
                const review = Utils.create('div', { class: 'wb-entity-review', style: 'margin-top:12px' });
                const reason = Utils.create('textarea', {
                    class: 'wb-decision-reason',
                    placeholder: '填写处置意见、补证要求或排除理由'
                });
                review.appendChild(reason);
                const actions = Utils.create('div', { class: 'wb-entity-actions', style: 'justify-content:flex-start' });
                [
                    ['EXCLUDE', '排除', 'x', 'wb-btn wb-btn-destructive'],
                    ['NEED_MATERIAL', '待补证', 'fileStack', 'wb-btn wb-btn-outline'],
                    ['CONTINUE', '确认关联', 'check', 'wb-btn wb-btn-primary']
                ].forEach(([disp, label, icon, cls]) => {
                    const btn = this._iconBtn(cls, icon, label);
                    btn.addEventListener('click', () => {
                        this._disposeClue(selected.artifact.id, disp, reason.value, selected.version, btn);
                    });
                    actions.appendChild(btn);
                });
                review.appendChild(actions);
                detailBody.appendChild(review);
            } else {
                detailBody.appendChild(Utils.create('div', {
                    class: 'wb-file-meta',
                    text: `已处置：${p.disposition} · ${p.disposition_reason || ''}`
                }));
            }
            detail.appendChild(detailBody);
            split.appendChild(detail);
            root.appendChild(split);
        },

        async _viewTimeline(root) {
            root.appendChild(this._crumb('角色时间线'));
            const page = Utils.create('div', { class: 'ref-tl-page' });
            root.appendChild(page);

            const art = (this.task.artifacts || []).find((a) => a.type === 'ROLE_TIMELINE');
            if (!art) {
                page.appendChild(Utils.create('div', { class: 'ref-tl-head' }, [
                    Utils.create('div', {}, [
                        Utils.create('div', { class: 'title-row' }, [
                            Utils.create('h1', { text: '角色时间线' }),
                            Utils.create('span', { class: 'ref-tl-node-badge', text: '0 个节点' })
                        ]),
                        Utils.create('div', {
                            class: 'sub',
                            text: '围绕人员、账户与行为记载，按时间重建跨案角色变化与证据来源。'
                        })
                    ])
                ]));
                page.appendChild(Utils.create('div', { class: 'wb-empty', text: '尚未生成事件时间线。' }));
                const btn = this._iconBtn('wb-btn wb-btn-primary', 'waypoints', '整理事件时间线');
                btn.addEventListener('click', () => this._runTimeline(btn));
                page.appendChild(btn);
                return;
            }

            const data = await this._fetchArtifact(art.id);
            const payload = (data && data.payload) || {};
            const items = payload.items || [];

            const normalized = items.map((item, idx) => {
                const parties = item.parties || [];
                const subject = parties[0] || item.case_name || item.case_id || `事件 ${idx + 1}`;
                const uncertain = !item.time_text || item.time_precision === 'UNKNOWN';
                let sourceMode = item.source_mode || item.sourceMode;
                if (!sourceMode) {
                    sourceMode = uncertain ? 'inferred' : 'recorded';
                }
                const source = item.source || {};
                const evidences = Array.isArray(item.evidences) && item.evidences.length
                    ? item.evidences
                    : (source.filename || source.chunk_id
                        ? [{
                            materialName: source.filename || '材料依据',
                            page: source.page_start,
                            ...source
                        }]
                        : []);
                const caseList = Array.isArray(item.cases) && item.cases.length
                    ? item.cases
                    : (item.case_name ? [item.case_name] : (item.case_id ? [item.case_id] : []));
                return {
                    ...item,
                    subject,
                    sourceMode,
                    timeCertain: !uncertain,
                    roleOrAction: item.role_or_action
                        || item.roleOrAction
                        || item.summary_text
                        || (item.event_type === 'TRANSFER' ? '转账记载' : '联络记载'),
                    cases: caseList,
                    evidences,
                    source
                };
            });

            if (this.timelineSubject == null) this.timelineSubject = 'all';
            const subjects = Array.from(new Set(normalized.map((e) => e.subject)));
            const filtered = normalized.filter((e) =>
                this.timelineSubject === 'all' || e.subject === this.timelineSubject
            );

            const head = Utils.create('div', { class: 'ref-tl-head' });
            const titleH1 = Utils.create('h1', { class: 'ref-tl-title' });
            if (window.Icons) {
                titleH1.innerHTML = Icons.svg('waypoints', 'wb-ico wb-page-title-ico') + '<span>角色时间线</span>';
            } else {
                titleH1.textContent = '角色时间线';
            }
            head.appendChild(Utils.create('div', {}, [
                Utils.create('div', { class: 'title-row' }, [
                    titleH1,
                    Utils.create('span', {
                        class: 'ref-tl-node-badge',
                        text: `${normalized.length} 个节点`
                    })
                ]),
                Utils.create('div', {
                    class: 'sub',
                    text: '围绕人员、账户与行为记载，按时间重建跨案角色变化与证据来源。'
                })
            ]));
            const headActions = Utils.create('div', { class: 'ref-tl-head-actions' });
            const rangeBtn = this._iconBtn('wb-btn wb-btn-outline', 'calendar', '时间范围');
            rangeBtn.addEventListener('click', () => Toast.info('时间范围筛选即将接入'));
            const filterBtn = this._iconBtn('wb-btn wb-btn-outline', 'filter', '筛选');
            filterBtn.addEventListener('click', () => Toast.info('高级筛选即将接入'));
            const runBtn = this._iconBtn('wb-btn wb-btn-outline', 'waypoints', '重新整理');
            runBtn.addEventListener('click', () => this._runTimeline(runBtn));
            headActions.appendChild(rangeBtn);
            headActions.appendChild(filterBtn);
            headActions.appendChild(runBtn);
            head.appendChild(headActions);
            page.appendChild(head);

            page.appendChild(Utils.create('div', { class: 'ref-tl-alert' }, [
                window.Icons
                    ? Icons.el('info', 'ref-tl-alert-ico')
                    : Utils.create('span', {
                        class: 'ref-tl-alert-ico',
                        html: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
                    }),
                Utils.create('span', {
                    text: '时间线同时展示材料明确记载与系统推测节点。虚线连接不代表事实上的连续行为。'
                })
            ]));

            const card = Utils.create('div', { class: 'ref-tl-card' });
            const cardHead = Utils.create('div', { class: 'ref-tl-card-head' });
            cardHead.appendChild(Utils.create('div', {}, [
                Utils.create('h2', { text: '主体视角' }),
                Utils.create('p', { text: '选择主体以聚焦相关时间节点' })
            ]));
            const select = Utils.create('select');
            select.appendChild(Utils.create('option', { value: 'all', text: '全部主体' }));
            subjects.forEach((name) => {
                select.appendChild(Utils.create('option', { value: name, text: name }));
            });
            select.value = this.timelineSubject;
            select.addEventListener('change', () => {
                this.timelineSubject = select.value;
                this._renderCurrentView();
            });
            cardHead.appendChild(select);
            card.appendChild(cardHead);

            const body = Utils.create('div', { class: 'ref-tl-body' });
            if (!filtered.length) {
                body.appendChild(Utils.create('div', {
                    class: 'wb-empty',
                    text: '暂无匹配节点'
                }));
            } else {
                const rail = Utils.create('div', { class: 'ref-tl-rail' });
                filtered.forEach((event) => {
                    const modeLabel = {
                        recorded: '材料明确记载',
                        inferred: '系统推测',
                        confirmed: '人工确认'
                    }[event.sourceMode] || '材料明确记载';
                    const modeClass = event.sourceMode === 'inferred'
                        ? 'inferred'
                        : (event.sourceMode === 'confirmed' ? 'confirmed' : 'recorded');
                    const source = event.source || {};
                    const evidCount = (event.evidences || []).length;
                    const firstMat = (event.evidences && event.evidences[0]
                        && (event.evidences[0].materialName || event.evidences[0].filename))
                        || '';

                    const row = Utils.create('div', { class: 'ref-tl-row' });
                    row.appendChild(Utils.create('div', {
                        class: 'ref-tl-time',
                        text: event.time_text || event.time || '时间不明'
                    }));
                    row.appendChild(Utils.create('div', { class: 'ref-tl-dot-wrap' }, [
                        Utils.create('span', { class: `ref-tl-dot ${modeClass}` })
                    ]));

                    const cardEl = Utils.create('div', { class: 'ref-tl-event' });
                    const top = Utils.create('div', { class: 'ref-tl-event-top' });
                    const leftBits = [
                        Utils.create('span', { class: 'subj', text: event.subject }),
                        !event.timeCertain
                            ? Utils.create('span', { class: 'ref-tl-outline-pill', text: '时间不确定' })
                            : null,
                        Utils.create('span', {
                            class: `ref-source-badge ${modeClass}`,
                            text: modeLabel
                        })
                    ].filter(Boolean);
                    top.appendChild(Utils.create('div', { class: 'left' }, leftBits));
                    top.appendChild(Utils.create('span', {
                        class: 'ref-tl-case-badge',
                        text: `${Math.max(event.cases.length, 1)} 案件`
                    }));
                    cardEl.appendChild(top);
                    cardEl.appendChild(Utils.create('div', {
                        class: 'ref-tl-event-action',
                        text: event.roleOrAction || '—'
                    }));
                    cardEl.appendChild(Utils.create('hr', { class: 'ref-tl-event-sep' }));

                    const foot = Utils.create('div', { class: 'ref-tl-event-foot' });
                    foot.appendChild(Utils.create('span', {
                        text: firstMat
                            ? `${evidCount} 条依据 · ${firstMat}`
                            : `${evidCount} 条依据`
                    }));
                    const citeBtn = this._iconBtn('wb-btn wb-btn-link', 'fileCheck2', '核验依据');
                    citeBtn.addEventListener('click', () => {
                        if (source.chunk_id && source.document_version_id) {
                            this._openCitation(source);
                        } else {
                            Toast.info('暂无可用原文定位');
                        }
                    });
                    foot.appendChild(citeBtn);
                    cardEl.appendChild(foot);
                    row.appendChild(cardEl);
                    rail.appendChild(row);
                });
                body.appendChild(rail);
            }
            card.appendChild(body);
            page.appendChild(card);
        },

        _buildGraphModel() {
            const nodes = [];
            const edges = [];
            const seen = new Set();
            const addNode = (id, label, type) => {
                if (!id || seen.has(id)) return;
                seen.add(id);
                nodes.push({ id, label, type });
            };
            (this.task.cases || []).forEach((c, i) => {
                addNode(`case:${c.case_id}`, c.display_name || c.name || `案件${i + 1}`, '案件');
            });
            Object.values(this.artifactCache).forEach((data) => {
                if (!data || !data.artifact) return;
                if (data.artifact.type === 'ENTITY_CANDIDATE_SET') {
                    (data.payload.candidates || []).forEach((c) => {
                        const nid = `ent:${c.candidate_id}`;
                        addNode(nid, c.title || c.display_name || '对象', c.object_type || '标识');
                        (c.cases || []).forEach((cs) => {
                            edges.push({
                                from: nid,
                                to: `case:${cs.case_id}`,
                                label: '出现于',
                                source: '实体候选'
                            });
                        });
                    });
                }
                if (data.artifact.type === 'CLUE_ITEM') {
                    const p = data.payload || {};
                    const lid = `clue:${data.artifact.id}`;
                    addNode(lid, p.title || data.artifact.title, '线索');
                    (p.objects || []).forEach((obj, idx) => {
                        const oid = `obj:${obj}`;
                        addNode(oid, obj, '对象');
                        edges.push({ from: lid, to: oid, label: '涉及', source: '线索' });
                    });
                }
            });
            return { nodes, edges };
        },

        async _viewGraph(root) {
            // preload entity/clue caches for graph edges
            await this._ensureEntitySet();
            await this._collectClueItems();
            const { nodes, edges } = this._buildGraphModel();
            const graphActions = Utils.create('div', { class: 'wb-page-head-actions', style: 'display:flex;gap:8px;flex-wrap:wrap' });
            const weakBtn = this._iconBtn('wb-btn wb-btn-outline', 'filter', '收起弱关系');
            weakBtn.addEventListener('click', () => Toast.info('弱关系筛选即将接入'));
            const exportGraph = this._iconBtn('wb-btn wb-btn-primary', 'download', '导出图谱');
            exportGraph.addEventListener('click', () => Toast.info('图谱导出即将接入'));
            graphActions.appendChild(weakBtn);
            graphActions.appendChild(exportGraph);
            root.appendChild(this._pageHead(
                '链条图谱',
                '将实体、案件与行为关系合并，用于发现资金、联络与人员路径。',
                graphActions,
                '简化关系视图'
            ));
            if (!nodes.length) {
                root.appendChild(Utils.create('div', { class: 'wb-empty', text: '暂无关系可展示。请先完成标识比对或生成线索。' }));
                return;
            }
            if (!this.selectedGraphNodeId || !nodes.some((n) => n.id === this.selectedGraphNodeId)) {
                this.selectedGraphNodeId = nodes[0].id;
            }
            const selected = nodes.find((n) => n.id === this.selectedGraphNodeId) || nodes[0];

            root.appendChild(Utils.create('div', { class: 'ref-graph-legend' }, [
                Utils.create('span', {}, [
                    Utils.create('i', { class: 'ref-graph-dot' }),
                    Utils.create('span', { text: '材料记载' })
                ]),
                Utils.create('span', {}, [
                    Utils.create('i', { class: 'ref-graph-dot warn' }),
                    Utils.create('span', { text: '待确认' })
                ])
            ]));

            const layout = Utils.create('div', { class: 'wb-graph-layout' });
            const canvas = Utils.create('div', { class: 'wb-graph-canvas' });
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('width', '100%');
            svg.setAttribute('height', '100%');
            svg.style.position = 'absolute';
            svg.style.inset = '0';
            canvas.appendChild(svg);

            const caseNodes = nodes.filter((n) => n.type === '案件');
            const other = nodes.filter((n) => n.type !== '案件');
            const positions = {};
            caseNodes.forEach((n, i) => {
                positions[n.id] = { x: 18 + (i * 28), y: 22 };
            });
            other.forEach((n, i) => {
                const col = i % 3;
                const row = Math.floor(i / 3);
                positions[n.id] = { x: 22 + col * 28, y: 48 + row * 22 };
            });

            edges.forEach((e) => {
                const a = positions[e.from];
                const b = positions[e.to];
                if (!a || !b) return;
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', `${a.x}%`);
                line.setAttribute('y1', `${a.y}%`);
                line.setAttribute('x2', `${b.x}%`);
                line.setAttribute('y2', `${b.y}%`);
                line.setAttribute('stroke', 'currentColor');
                line.setAttribute('stroke-opacity', '0.25');
                line.setAttribute('stroke-width', '2');
                svg.appendChild(line);
            });

            nodes.forEach((n) => {
                const pos = positions[n.id] || { x: 50, y: 50 };
                const typeIcon = n.type === '案件'
                    ? 'fileText'
                    : (window.Icons ? Icons.forEntityType(n.type || n.label) : 'users');
                const titleRow = Utils.create('div', { class: 't', style: 'display:flex;align-items:center;gap:6px' });
                if (window.Icons) {
                    titleRow.appendChild(Icons.el(typeIcon, 'wb-list-type-ico'));
                }
                titleRow.appendChild(document.createTextNode(n.label));
                const node = Utils.create('div', {
                    class: `wb-graph-node${n.id === selected.id ? ' active' : ''}`,
                    style: `left:${pos.x}%;top:${pos.y}%`
                }, [
                    titleRow,
                    Utils.create('div', { class: 's', text: n.type })
                ]);
                node.addEventListener('click', () => {
                    this.selectedGraphNodeId = n.id;
                    this._renderCurrentView();
                });
                canvas.appendChild(node);
            });
            layout.appendChild(canvas);

            const side = Utils.create('div', { class: 'wb-detail-card' });
            side.appendChild(Utils.create('div', { class: 'wb-detail-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '节点详情' }),
                Utils.create('div', { class: 'wb-file-meta', text: `${selected.type} · ${selected.label}` })
            ]));
            const sideBody = Utils.create('div', { class: 'wb-detail-card-body' });
            sideBody.appendChild(Utils.create('div', { class: 'wb-alert warn' }, [
                window.Icons ? Icons.el('info') : Utils.create('span', { text: 'ℹ' }),
                Utils.create('span', { text: '需人工判断：图谱关系来自字段碰撞与线索，不代表主体身份或共同犯罪事实已经成立。' })
            ]));
            const related = edges.filter((e) => e.from === selected.id || e.to === selected.id);
            related.forEach((e) => {
                const otherId = e.from === selected.id ? e.to : e.from;
                const other = nodes.find((n) => n.id === otherId);
                const row = Utils.create('button', {
                    type: 'button',
                    class: 'wb-list-item',
                    style: 'width:100%;margin-bottom:6px'
                }, [
                    Utils.create('div', {
                        class: 'wb-list-item-title',
                        text: `${(other && other.label) || otherId} · ${e.label}`
                    }),
                    Utils.create('div', { class: 'wb-list-item-meta', text: e.source })
                ]);
                row.addEventListener('click', () => {
                    this.selectedGraphNodeId = otherId;
                    this._renderCurrentView();
                });
                sideBody.appendChild(row);
            });
            const toEntities = this._iconBtn('wb-btn wb-btn-outline', 'fileStack', '查看支持材料 / 实体复核');
            toEntities.style.width = '100%';
            toEntities.style.marginTop = '12px';
            toEntities.addEventListener('click', () => this.setView('entities'));
            const toLeads = this._iconBtn('wb-btn wb-btn-outline', 'link2', '查看生成线索');
            toLeads.style.width = '100%';
            toLeads.style.marginTop = '8px';
            toLeads.addEventListener('click', () => this.setView('leads'));
            sideBody.appendChild(toEntities);
            sideBody.appendChild(toLeads);
            side.appendChild(sideBody);
            layout.appendChild(side);
            root.appendChild(layout);

            // relation table
            const tableCard = Utils.create('div', { class: 'wb-panel-card', style: 'margin-top:14px' });
            tableCard.appendChild(Utils.create('div', { class: 'wb-panel-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '关系列表' })
            ]));
            const wrap = Utils.create('div', { class: 'wb-table-wrap' });
            const table = Utils.create('table', { class: 'wb-table' });
            table.appendChild(Utils.create('thead', {}, [
                Utils.create('tr', {}, [
                    Utils.create('th', { text: '节点 A' }),
                    Utils.create('th', { text: '关系' }),
                    Utils.create('th', { text: '节点 B' }),
                    Utils.create('th', { text: '来源' })
                ])
            ]));
            const tbody = Utils.create('tbody');
            edges.forEach((e) => {
                const a = nodes.find((n) => n.id === e.from);
                const b = nodes.find((n) => n.id === e.to);
                tbody.appendChild(Utils.create('tr', {}, [
                    Utils.create('td', { text: (a && a.label) || e.from }),
                    Utils.create('td', { text: e.label }),
                    Utils.create('td', { text: (b && b.label) || e.to }),
                    Utils.create('td', { text: e.source })
                ]));
            });
            table.appendChild(tbody);
            wrap.appendChild(table);
            tableCard.appendChild(wrap);
            root.appendChild(tableCard);
        },

        async _viewReports(root) {
            const verifyArt = (this.task.artifacts || []).find((a) => a.type === 'SOURCE_VERIFY');
            const reportArts = (this.task.artifacts || []).filter((a) =>
                a.type === 'REPORT_DRAFT' || a.type === 'REPORT_EXPORT'
            );
            const draftBtn = this._iconBtn('wb-btn wb-btn-primary', 'plus', '新建报告');
            draftBtn.addEventListener('click', () => this._generateReportDraft(draftBtn));
            root.appendChild(this._pageHead(
                '报告与审计',
                '生成可追溯的跨案关联线索核验单，并查看核验留痕。',
                draftBtn,
                `${reportArts.length} 份报告`
            ));

            const grid = Utils.create('div', { class: 'wb-report-grid' });

            const reportCard = Utils.create('div', { class: 'wb-panel-card' });
            reportCard.appendChild(Utils.create('div', { class: 'wb-panel-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '报告列表' })
            ]));
            const reportBody = Utils.create('div', { class: 'wb-panel-card-body' });
            if (!reportArts.length) {
                reportBody.appendChild(Utils.create('div', {
                    class: 'wb-file-meta',
                    text: '尚未生成报告。完成实体/线索核验后可生成「跨案关联线索核验单」。'
                }));
            } else {
                for (const art of reportArts) {
                    const data = await this._fetchArtifact(art.id);
                    const p = (data && data.payload) || {};
                    const valid = p.valid !== false;
                    const row = Utils.create('div', {
                        style: 'display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--border-color)'
                    }, [
                        Utils.create('div', {}, [
                            Utils.create('div', {
                                style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap'
                            }, [
                                Utils.create('div', { class: 'wb-list-item-title', text: art.title || '核验单' }),
                                this._statusTag(valid ? '有效版本' : '存在失效引用', valid ? 'ok' : 'danger')
                            ]),
                            Utils.create('div', {
                                class: 'wb-file-meta',
                                text: `v${art.current_version || (data && data.version) || 1} · ${(art.updated_at || '').toString().slice(0, 10) || '—'}`
                            })
                        ])
                    ]);
                    const dl = this._iconBtn('wb-btn wb-btn-outline', 'download', '下载');
                    if (!valid) dl.disabled = true;
                    dl.addEventListener('click', () => this._downloadReport(art.id, p));
                    row.appendChild(dl);
                    reportBody.appendChild(row);
                }
            }
            reportCard.appendChild(reportBody);
            grid.appendChild(reportCard);

            const summaryCard = Utils.create('div', { class: 'wb-panel-card' });
            summaryCard.appendChild(Utils.create('div', { class: 'wb-panel-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '审计摘要' })
            ]));
            const sumBody = Utils.create('div', { class: 'wb-panel-card-body' });
            let eventCount = 0;
            if (verifyArt) {
                const data = await this._fetchArtifact(verifyArt.id);
                eventCount = (((data && data.payload) || {}).events || []).length;
            }
            sumBody.appendChild(Utils.create('div', { class: 'wb-alert' }, [
                window.Icons ? Icons.el('shieldCheck') : null,
                Utils.create('span', {
                    text: eventCount
                        ? `审计链路完整：已记录 ${eventCount} 条操作，有效报告 ${reportArts.length} 份。`
                        : '完成实体决策、线索处置或打开原文后，审计链路将自动补齐。'
                })
            ].filter(Boolean)));
            sumBody.appendChild(Utils.create('div', {
                class: 'wb-file-meta',
                text: '导出内容将保持脱敏，不得回填原始敏感标识。',
                style: 'margin-top:10px'
            }));
            summaryCard.appendChild(sumBody);
            grid.appendChild(summaryCard);
            root.appendChild(grid);

            const auditCard = Utils.create('div', { class: 'wb-panel-card', style: 'margin-top:14px' });
            auditCard.appendChild(Utils.create('div', { class: 'wb-panel-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '近期操作记录' })
            ]));
            const wrap = Utils.create('div', { class: 'wb-table-wrap', style: 'border:0;border-radius:0' });
            const table = Utils.create('table', { class: 'wb-table' });
            table.appendChild(Utils.create('thead', {}, [
                Utils.create('tr', {}, [
                    Utils.create('th', { text: '时间' }),
                    Utils.create('th', { text: '操作者' }),
                    Utils.create('th', { text: '操作' }),
                    Utils.create('th', { text: '结果' })
                ])
            ]));
            const tbody = Utils.create('tbody');
            if (!verifyArt) {
                tbody.appendChild(Utils.create('tr', {}, [
                    Utils.create('td', { text: '暂无操作记录', colspan: '4' })
                ]));
            } else {
                const data = await this._fetchArtifact(verifyArt.id);
                const events = ((data && data.payload) || {}).events || [];
                if (!events.length) {
                    tbody.appendChild(Utils.create('tr', {}, [
                        Utils.create('td', { text: '暂无操作记录', colspan: '4' })
                    ]));
                }
                events.slice().reverse().forEach((ev) => {
                    const ok = ev.result !== 'fail' && ev.result !== 'interrupted';
                    tbody.appendChild(Utils.create('tr', {}, [
                        Utils.create('td', { text: (ev.at || '').toString().slice(0, 19) || '—' }),
                        Utils.create('td', { text: ev.actor || '检察官' }),
                        Utils.create('td', { text: ev.summary || ev.action || ev.type || '事件' }),
                        Utils.create('td', {}, [
                            this._statusTag(ok ? '成功' : (ev.result === 'interrupted' ? '已中断' : '失败'), ok ? 'ok' : 'warn')
                        ])
                    ]));
                });
            }
            table.appendChild(tbody);
            wrap.appendChild(table);
            auditCard.appendChild(wrap);
            root.appendChild(auditCard);
        },

        async _generateReportDraft(button) {
            if (!this.task) return;
            if (button) {
                button.disabled = true;
                button.textContent = '生成中…';
            }
            try {
                const resp = await fetch(`/api/tasks/${this.task.id}/report/draft`, { method: 'POST' });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '报告生成失败');
                    return;
                }
                this.task = data.task || this.task;
                Toast.success('核验单草稿已生成');
                await this.setView('reports');
            } catch (e) {
                Toast.error('报告生成失败：' + e.message);
            } finally {
                if (button) {
                    button.disabled = false;
                    button.innerHTML = (window.Icons ? Icons.svg('plus', 'wb-ico') : '') + '<span>新建报告</span>';
                }
            }
        },

        _downloadReport(artifactId, payload) {
            const text = (payload && (payload.markdown || payload.text)) || '';
            if (!text) {
                Toast.warning('报告内容为空');
                return;
            }
            if (payload && payload.valid === false) {
                Toast.warning('存在失效引用，禁止下载');
                return;
            }
            const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `跨案关联线索核验单-${artifactId.slice(0, 8)}.md`;
            a.click();
            URL.revokeObjectURL(url);
        }

    };

    global.Workbench = Workbench;
})(window);
