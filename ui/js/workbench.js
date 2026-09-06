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
                    null
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
            this._entityRefreshTried = false;
            this._timelineRefreshTried = false;
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

        async refreshTask() {
            if (!this.task || !this.task.id) return this.task;
            const resp = await fetch(`/api/tasks/${this.task.id}`);
            const task = await resp.json();
            if (task.error_code) {
                throw new Error(task.message || '刷新任务失败');
            }
            this.task = task;
            this._renderNav();
            this._renderContext();
            return task;
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

            panel.appendChild(this._iconBtn('wb-btn wb-btn-ghost', 'upload', '上传材料'));
            const lastBtn = panel.lastChild;
            lastBtn.addEventListener('click', () => this._openUploadModal());
            this._schedulePoll(payload);
        },

        _renderEntityCandidates(panel, payload, status, artifact, version) {
            // deprecated: 产物面板旧卡片流；主入口已改为 _viewEntities
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
                const followups = data.followup_actions || [];
                if (followups.length) {
                    Toast.success(followups.slice(0, 2).join('；'));
                } else {
                    Toast.success('判断已记录');
                }
                if (data.analysis_gate === 'ENTITY_REVIEW' && data.pending > 0) {
                    Toast.info(`仍有 ${data.pending} 条待核，确认后方可继续后续分析`);
                } else if (!data.analysis_gate && data.pending === 0) {
                    Toast.info('实体复核已完成，可继续整理线索与报告');
                }
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
                this.artifactCache = {};
                this._entityRefreshTried = false;
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

        _normalizeParties(parties) {
            return (parties || []).map((p) => {
                if (typeof p === 'string') {
                    const surface = p.trim();
                    if (!surface) return null;
                    return {
                        object_type: 'NAME',
                        surface,
                        display_name: surface,
                        subject_id: `auto:NAME:${surface}`
                    };
                }
                if (!p || typeof p !== 'object') return null;
                const surface = (p.surface || p.display_name || '').trim();
                if (!surface) return null;
                return {
                    object_type: p.object_type || 'NAME',
                    surface,
                    normalized_value: p.normalized_value || '',
                    display_name: p.display_name || surface,
                    subject_id: p.subject_id || `auto:${p.object_type || 'NAME'}:${p.normalized_value || surface}`
                };
            }).filter(Boolean);
        },

        _formatParties(parties) {
            return this._normalizeParties(parties)
                .map((p) => p.display_name || p.surface)
                .filter(Boolean)
                .join('；');
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
                this.artifactCache = {};
                this._timelineRefreshTried = false;
                await this.openArtifact(data.artifact.id);
                Toast.success(`事件时间线已整理 ${data.event_count || 0} 条`);
            } catch (e) {
                Toast.error('事件整理未能完成：' + e.message);
            } finally {
                button.disabled = false;
            }
        },

        async _openCitation(source, list, index) {
            if (Array.isArray(list) && list.length) {
                this.citeList = list;
                this.citeIndex = Math.max(0, Math.min(index || 0, list.length - 1));
            } else {
                this.citeList = [source];
                this.citeIndex = 0;
            }
            await this._loadCitation();
        },

        async _loadCitation() {
            const source = (this.citeList || [])[this.citeIndex || 0];
            if (!source) return;
            const versionId = source.document_version_id;
            const chunkId = source.chunk_id;
            this._openCiteDrawerShell();
            const body = Utils.$('#wb-cite-drawer-body');
            if (body) {
                body.innerHTML = '';
                body.appendChild(Utils.create('div', { class: 'wb-cite-loading', text: '正在核对原文…' }));
            }
            if (!versionId || !chunkId) {
                this._renderCiteVerify({
                    error: true,
                    message: '该条依据尚未取得原文定位，不能用于确认或导出。'
                }, source, {});
                return;
            }
            const anchors = [];
            const pushAnchor = (v) => {
                const s = String(v || '').trim();
                if (!s || s.length < 2 || /^(PERSON|PHONE|ACCOUNT|ORG|DEVICE|ID)_/i.test(s)) return;
                if (!anchors.includes(s)) anchors.push(s);
            };
            (source.highlight_terms || []).forEach(pushAnchor);
            pushAnchor(source.value);
            pushAnchor(source.extracted_value);
            pushAnchor(source.quote_display);
            // 从展示摘录里再抽可读词作锚点（禁止把存储态 PERSON_xxx 当锚点）
            String(source.quote_display || '').split(/[\s，。；、,.；\n]+/).forEach(pushAnchor);

            const storageQuote = source.quote_storage || source.quote || '';
            const tryVerify = async (quote, quoteHash) => {
                const resp = await fetch(
                    `/api/materials/versions/${versionId}/chunks/${chunkId}/verify`,
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            quote: quote || '',
                            quote_hash: quoteHash || '',
                            restore: 0,
                            anchor_terms: anchors
                        })
                    }
                );
                return resp.json();
            };

            try {
                let data = await tryVerify(storageQuote, source.quote_hash || '');
                // 旧线索脏 hash：仅凭锚点再试一次
                if (data.error_code && anchors.length) {
                    data = await tryVerify('', '');
                }
                if (data.error_code) {
                    this._renderCiteVerify({
                        error: true,
                        message: data.message || '原文已变更或哈希不匹配，禁止展示旧内容'
                    }, source, data);
                    return;
                }
                // 存储态只留在内存供下次校验；界面只用展示态
                if (data.quote_storage) source.quote_storage = data.quote_storage;
                if (data.quote_hash) source.quote_hash = data.quote_hash;
                if (data.quote) source.quote_display = data.quote;
                this._renderCiteVerify(
                    { error: false },
                    { ...source, quote_display: data.quote || source.quote_display },
                    data
                );
            } catch (e) {
                this._renderCiteVerify({ error: true, message: '回链失败：' + e.message }, source, {});
            }
        },

        _displayQuote(source) {
            // 界面一律展示态；禁止落存储态 PERSON_xxx
            const raw = String(
                (source && (source.quote_display || source.display_quote)) || ''
            ).trim();
            if (raw && !/PERSON_|PHONE_|ACCOUNT_|ORG_|DEVICE_/i.test(raw)) return raw;
            const fallback = String((source && source.quote) || '').trim();
            if (!fallback) return '脱敏片段';
            // 最后兜底：前端也遮罩占位符，绝不把存储态原文亮给用户
            return fallback.replace(
                /(?:PERSON|NAME|PHONE|ACCOUNT|ID|ORG|ORGANIZATION|DEVICE|BANK_CARD|CREDIT_CARD)_[a-f0-9]{1,16}/gi,
                (tok) => {
                    const u = tok.toUpperCase();
                    if (u.startsWith('PERSON') || u.startsWith('NAME')) return '脱敏人员';
                    if (u.startsWith('PHONE')) return '脱敏手机号';
                    if (u.startsWith('ACCOUNT') || u.startsWith('BANK') || u.startsWith('CREDIT')) return '脱敏账户';
                    if (u.startsWith('ORG')) return '脱敏组织';
                    return '脱敏标识';
                }
            );
        },

        _collectHighlightTerms(source) {
            const terms = [];
            const push = (v) => {
                const s = String(v || '').trim();
                if (!s || s.length < 2) return;
                if (/^(PERSON|PHONE|ACCOUNT|ORG|DEVICE|ID)_/i.test(s)) return;
                if (!terms.includes(s)) terms.push(s);
            };
            (source && source.highlight_terms || []).forEach(push);
            push(source && source.value);
            push(source && source.extracted_value);
            push(source && source.quote_display);
            const display = this._displayQuote(source || {});
            if (display && display.length <= 80) push(display);
            return terms.sort((a, b) => b.length - a.length).slice(0, 8);
        },

        _escapeHtml(text) {
            return String(text || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        },

        _renderHighlightedText(container, text, source) {
            const raw = String(text || '');
            const terms = this._collectHighlightTerms(source || {});
            if (!raw || !terms.length) {
                container.textContent = raw;
                return;
            }
            // 在原文中定位：优先完整 quote，再定位被提取字段值
            let html = this._escapeHtml(raw);
            terms.forEach((term) => {
                const esc = this._escapeHtml(term);
                if (!esc || html.indexOf(esc) < 0) return;
                // 只替换首次出现，避免整篇刷黄
                html = html.replace(
                    esc,
                    `<mark class="wb-cite-mark"><strong>${esc}</strong></mark>`
                );
            });
            container.innerHTML = html;
        },

        _renderVerifyText(container, fullText, source) {
            const raw = String(fullText || '');
            const terms = this._collectHighlightTerms(source || {});
            const anchor = terms.map((t) => raw.indexOf(t)).filter((i) => i >= 0).sort((a, b) => a - b)[0];
            if (!raw) {
                container.textContent = '原文为空';
                return;
            }
            let start = 0;
            let end = raw.length;
            if (anchor != null && raw.length > 460) {
                start = Math.max(0, anchor - 180);
                end = Math.min(raw.length, anchor + 280);
            } else if (raw.length > 900) {
                end = 900;
            }
            const window_ = raw.slice(start, end);
            let html = this._escapeHtml(window_);
            terms.forEach((term) => {
                const esc = this._escapeHtml(term);
                if (!esc || html.indexOf(esc) < 0) return;
                html = html.replace(esc, `<mark class="wb-cite-mark"><strong>${esc}</strong></mark>`);
            });
            const prefix = start > 0 ? '<span class="wb-cite-omit">……（上文省略）……</span>' : '';
            const suffix = end < raw.length ? '<span class="wb-cite-omit">……（下文省略）……</span>' : '';
            container.innerHTML = prefix + html + suffix;
        },

        _renderCiteVerify(view, source, data) {
            const bodyEl = Utils.$('#wb-cite-drawer-body');
            const title = Utils.$('#wb-cite-title');
            const kicker = Utils.$('#wb-cite-kicker');
            if (!bodyEl) return;
            this._openCiteDrawerShell();
            const list = this.citeList || [source];
            const index = this.citeIndex || 0;
            if (kicker) kicker.textContent = view.error ? '引用失效' : '原文核验';
            if (title) title.textContent = source.case_name || data.case_name || '原文核验';
            bodyEl.innerHTML = '';

            bodyEl.appendChild(Utils.create('div', {
                class: 'wb-cite-desc',
                text: `第 ${index + 1} / ${list.length} 条依据 · 逐字核验原文材料后再作处置判断`
            }));

            const conf = data.ocr_confidence != null ? data.ocr_confidence : source.ocr_confidence;
            const pageNo = data.page_start || source.page_start || source.page_no;
            const meta = Utils.create('div', { class: 'wb-cite-meta' });
            [
                ['所属案件', data.case_name || source.case_name || '—'],
                ['材料名称', data.filename || source.filename || source.document_name || '—'],
                ['材料版本', data.version_no != null ? `v${data.version_no}` : '—'],
                ['页码', pageNo ? `第 ${pageNo} 页` : '—'],
                ['识别质量', conf != null ? `${Math.round(conf * 100)}%` : '—']
            ].forEach(([label, value]) => {
                meta.appendChild(Utils.create('span', { class: 'wb-cite-meta-item' }, [
                    Utils.create('span', { class: 'wb-cite-meta-label', text: `${label}：` }),
                    Utils.create('b', { text: String(value) })
                ]));
            });
            bodyEl.appendChild(meta);

            if (view.error) {
                bodyEl.appendChild(Utils.create('div', { class: 'wb-cite-invalid' }, [
                    Utils.create('div', { class: 'wb-cite-invalid-title', text: '该引用当前已失效，不得用于确认或导出报告' }),
                    Utils.create('div', { text: view.message || '材料版本已变更，请重新核验' })
                ]));
            }

            const panel = Utils.create('section', { class: 'wb-cite-panel' });
            panel.appendChild(Utils.create('div', { class: 'wb-cite-panel-head', text: '识别文本 · 高亮原句' }));
            const textBox = Utils.create('div', { class: `wb-cite-body${view.error ? ' is-error' : ''}` });
            if (view.error) {
                // 失效时也只展示展示态摘要，禁止落存储态 quote
                textBox.textContent = this._displayQuote(source) || view.message || '原文不可展示';
            } else {
                // data.text 已是展示态；高亮词也只用展示态
                this._renderVerifyText(
                    textBox,
                    data.text || this._displayQuote(source) || '',
                    { ...source, quote_display: data.quote || source.quote_display }
                );
            }
            panel.appendChild(textBox);
            if (conf != null && conf < 0.85) {
                panel.appendChild(Utils.create('div', {
                    class: 'wb-cite-lowconf',
                    text: '识别置信度较低，建议人工复核'
                }));
            }
            bodyEl.appendChild(panel);

            const nav = Utils.create('div', { class: 'wb-cite-nav' });
            const prev = Utils.create('button', { type: 'button', class: 'wb-btn wb-btn-outline', text: '‹ 上一条' });
            const next = Utils.create('button', { type: 'button', class: 'wb-btn wb-btn-outline', text: '下一条 ›' });
            prev.disabled = list.length <= 1;
            next.disabled = list.length <= 1;
            prev.addEventListener('click', () => {
                this.citeIndex = (index - 1 + list.length) % list.length;
                this._loadCitation();
            });
            next.addEventListener('click', () => {
                this.citeIndex = (index + 1) % list.length;
                this._loadCitation();
            });
            const drawer = Utils.$('#wb-cite-drawer');
            const isFull = drawer && drawer.classList.contains('is-fullscreen');
            const full = Utils.create('button', {
                type: 'button',
                class: 'wb-btn wb-btn-ghost',
                text: isFull ? '退出全屏核验' : '进入全屏核验模式'
            });
            full.addEventListener('click', () => {
                if (drawer) drawer.classList.toggle('is-fullscreen');
                this._loadCitation();
            });
            nav.appendChild(prev);
            nav.appendChild(full);
            nav.appendChild(next);
            bodyEl.appendChild(nav);

            requestAnimationFrame(() => {
                const mark = textBox.querySelector('.wb-cite-mark');
                if (mark && typeof mark.scrollIntoView === 'function') {
                    mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
                }
            });
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
            const displayQuote = this._displayQuote(source || {});
            const text = view.text || '';
            if (!view.error && !displayQuote && !(source && (source.value || (source.highlight_terms || []).length)) && window.Markdown) {
                body.classList.add('md-content');
                body.innerHTML = Markdown.parse(text);
            } else if (!view.error) {
                this._renderHighlightedText(body, text, {
                    ...source,
                    quote_display: displayQuote,
                    quote: displayQuote
                });
            } else {
                body.textContent = text || displayQuote || '原文不可展示';
            }
            bodyEl.appendChild(body);
            // 滚到首个高亮
            requestAnimationFrame(() => {
                const mark = body.querySelector('.wb-cite-mark');
                if (mark && typeof mark.scrollIntoView === 'function') {
                    mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
                }
            });
        },

        _orgStyleEvidenceQuote(ev) {
            // 与组织实体字段表短 q 同款：卡片只展示展示态短摘录
            const raw = String(this._displayQuote(ev) || '').replace(/\s+/g, ' ').trim();
            const value = String(ev.value || ev.extracted_value || '').trim();
            const maxLen = 40;
            if (!raw) return (value || '脱敏片段').slice(0, maxLen);
            if (raw.length <= maxLen) return raw;
            let needle = value;
            if (!needle || needle.length > 40 || !raw.includes(needle)) {
                const parts = value.split(/[、,，;/]/).map((p) => p.trim()).filter((p) => p.length >= 2);
                needle = parts.find((p) => raw.includes(p)) || '';
            }
            if (needle && !/PERSON_|PHONE_|ACCOUNT_/i.test(needle)) {
                const at = raw.indexOf(needle);
                if (at >= 0) {
                    const left = Math.max(0, at - Math.max(0, Math.floor((maxLen - needle.length) / 4)));
                    const right = Math.min(raw.length, left + maxLen);
                    const start = Math.max(0, right - maxLen);
                    return raw.slice(start, right);
                }
            }
            return raw.slice(0, maxLen);
        },

        _collectEntityEvidence(selected) {
            // 原文回溯只保留：字段对照表「有值格子」出处 + 主标识每案最多 1 条兜底。
            // 不再倾倒全部 records（旧逻辑会把「问：取过什么」这类切坏窗口塞进来）。
            const list = [];
            const seen = new Set();
            const caseCovered = new Set();
            const push = (ev, { primary = false } = {}) => {
                if (!ev) return;
                const quote = String(ev.quote || '').trim();
                const value = String(ev.value || ev.extracted_value || '').trim();
                // 有值时要求片段里能看见该值（或足够长的可核验 quote）
                if (value && quote) {
                    const compactQuote = quote.replace(/\s+/g, '');
                    const compactDisplay = String(ev.quote_display || '').replace(/\s+/g, '');
                    const compactValue = value.replace(/\s+/g, '');
                    // 多值单元格（顿号拼接）任一子值出现即可
                    const parts = compactValue.split(/[、,，;/]/).filter((p) => p.length >= 2);
                    const hay = compactQuote + compactDisplay;
                    const valueVisible = parts.length
                        ? parts.some((p) => hay.includes(p))
                        : (compactValue.length >= 2 && (
                            hay.includes(compactValue)
                            || compactValue.includes(compactQuote)
                        ));
                    // 短摘录（组织风格）里称谓常被脱敏成占位符，不能因字面看不到值就丢掉
                    if (!valueVisible && quote.replace(/\s+/g, '').length > 40) return;
                }
                if (!ev.chunk_id || !ev.quote_hash || !quote) {
                    // 失效条仍可展示，但排到后面
                    ev = { ...ev, _invalid: true };
                }
                const key = `${ev.chunk_id || ''}|${ev.quote_hash || ''}|${quote}|${value}|${ev.field_label || ''}`;
                if (seen.has(key)) return;
                seen.add(key);
                list.push(ev);
                if (primary && ev.case_id && !ev._invalid) caseCovered.add(ev.case_id);
            };

            (selected.field_compare || []).forEach((field) => {
                // 别名底表的「材料出处」只是路径文案，不进证据卡（避免长窗口破坏组织风格）
                if (field.field_key === 'alias_material') return;
                (field.per_case || []).forEach((cell) => {
                    if (!cell.value && !(cell.sources || []).length) return;
                    (cell.sources || []).forEach((src) => {
                        const value = src.extracted_value || (
                            String(cell.value || '').length <= 40 ? cell.value : ''
                        ) || cell.value;
                        push({
                            case_name: src.case_name || cell.case_name,
                            case_id: src.case_id || cell.case_id,
                            filename: src.filename,
                            page_start: src.page_start,
                            page_end: src.page_end,
                            quote: src.quote,
                            quote_display: src.quote_display,
                            quote_hash: src.quote_hash,
                            chunk_id: src.chunk_id,
                            document_version_id: src.document_version_id,
                            document_id: src.document_id,
                            ocr_confidence: src.ocr_confidence,
                            value,
                            field_label: field.label || src.field_label || field.field_key,
                            highlight_terms: [src.extracted_value, cell.value].filter(Boolean)
                        }, { primary: true });
                    });
                });
            });

            // 主标识兜底：每案 1 条，优先 evidence，其次 records 中 quote 含 value 的
            const fallbacks = [];
            (selected.evidence || []).forEach((ev) => {
                fallbacks.push({
                    case_name: ev.case_name,
                    case_id: ev.case_id,
                    filename: ev.filename,
                    page_start: ev.page_start,
                    page_end: ev.page_end,
                    quote: ev.quote,
                    quote_display: ev.quote_display,
                    quote_hash: ev.quote_hash,
                    chunk_id: ev.chunk_id,
                    document_version_id: ev.document_version_id,
                    document_id: ev.document_id,
                    ocr_confidence: ev.ocr_confidence,
                    value: ev.value,
                    field_label: ev.field_label || '主标识',
                    highlight_terms: [ev.value].filter(Boolean),
                    _rank: 0
                });
            });
            (selected.records || []).forEach((rec) => {
                const src = rec.source || {};
                fallbacks.push({
                    case_name: rec.case_name,
                    case_id: rec.case_id,
                    filename: src.document_name,
                    page_start: src.page_no,
                    quote: src.quote,
                    quote_display: src.quote_display,
                    quote_hash: src.quote_hash,
                    chunk_id: src.chunk_id,
                    document_version_id: src.document_version_id,
                    ocr_confidence: src.ocr_confidence,
                    value: rec.value,
                    field_label: '主标识',
                    highlight_terms: [rec.value].filter(Boolean),
                    _rank: 1
                });
            });
            fallbacks
                .sort((a, b) => (a._rank - b._rank) || String(b.quote || '').length - String(a.quote || '').length)
                .forEach((ev) => {
                    if (!ev.case_id || caseCovered.has(ev.case_id)) return;
                    push(ev, { primary: true });
                });

            list.sort((a, b) => Number(!!a._invalid) - Number(!!b._invalid));
            return list;
        },

        async _buildFieldTable(candidateId, force) {
            if (!candidateId || !this.task || !this.task.id) return;
            if (this._fieldTableBusy) return;
            this._fieldTableTried = this._fieldTableTried || {};
            if (!force && this._fieldTableTried[candidateId]) return;
            this._fieldTableTried[candidateId] = true;
            this._fieldTableBusy = candidateId;
            if (force) Toast.info('正在按材料重建字段对照表…');
            try {
                const resp = await fetch(
                    `/api/tasks/${this.task.id}/entity-candidates/${candidateId}/field-table`,
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ force: !!force })
                    }
                );
                const result = await resp.json();
                if (!result.ok) {
                    // 模型不可用或身份锚定失败时保留规则字段表，不清空页面
                    if (force) Toast.warning(result.message || result.error || '字段表生成未完成，仍保留原表');
                    return;
                }
                this.artifactCache = {};
                await this.refreshTask();
                this._renderCurrentView();
                if (force) Toast.success('字段对照表已按材料重建');
            } catch (e) {
                if (force) Toast.error('字段表生成失败：' + e.message);
            } finally {
                this._fieldTableBusy = null;
            }
        },

        _closeCiteDrawer() {
            const drawer = Utils.$('#wb-cite-drawer');
            const center = Utils.$('#wb-center');
            if (drawer) {
                drawer.hidden = true;
                drawer.classList.remove('is-fullscreen');
            }
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
                const displayQuote = this._displayQuote(ev);
                const row = Utils.create('div', { class: 'wb-entity-record' }, [
                    Utils.create('div', { class: 'case', text: ev.case_name || ev.case_id || '' }),
                    Utils.create('div', { class: 'value', text: displayQuote }),
                    Utils.create('div', {
                        class: 'source',
                        text: [ev.filename, ev.page_start ? `第 ${ev.page_start} 页` : ''].filter(Boolean).join(' · ')
                    })
                ]);
                row.style.cursor = 'pointer';
                row.addEventListener('click', () => this._openCitation({
                    ...ev,
                    quote_storage: ev.quote_storage || ev.quote,
                    quote_display: ev.quote_display || displayQuote,
                    highlight_terms: [ev.value, ev.extracted_value].filter(Boolean)
                }));
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
                                this._formatParties(item.parties || []),
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
        async _saveMappingChanges(documentId, changes) {
            try {
                const resp = await fetch('/api/mappings/batch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        document_id: documentId,
                        updates: changes.updates,
                        deletions: Array.from(changes.deletions),
                        additions: changes.additions
                    })
                });
                const result = await resp.json();
                if (result.ok) {
                    Toast.success(result.message);
                    // 刷新映射列表
                    this._openMappingManager(documentId);
                } else {
                    Toast.error(result.error || '保存失败');
                }
                return result;
            } catch (e) {
                Toast.error('保存失败：' + e.message);
                throw e;
            }
        },
        async _openMappingManager(documentId) {
            // 1. 创建模态框
            const modal = Utils.create('div', { class: 'wb-modal-overlay' });
            const content = Utils.create('div', { class: 'wb-modal-content', style: 'max-width: 1000px; max-height: 90vh; overflow-y: auto;' });
            content.addEventListener('click', (e) => e.stopPropagation());
            modal.appendChild(content);
            document.body.appendChild(modal);

            // 2. 状态
            const changes = {
                updates: {},      // fingerprint -> { sens_type }
                deletions: new Set(),
                additions: []     // { original, sens_type }
            };

            // 3. 加载数据
            const resp = await fetch(`/api/mappings?limit=200`);
            const data = await resp.json();
            const items = data.items || [];

            // 收集所有已使用的类型，并补充常见类型
            const typeSet = new Set();
            items.forEach(item => {
                if (item.sens_type) typeSet.add(item.sens_type);
            });
            // 补充常见脱敏类型
            const commonTypes = ['PERSON', 'PHONE', 'ID', 'BANK_CARD', 'EMAIL', 'URL', 'IP'];
            commonTypes.forEach(t => typeSet.add(t));
            const allTypes = Array.from(typeSet).sort();

            // 4. 渲染表格
            const table = Utils.create('table', { class: 'wb-mapping-table' });
            const thead = Utils.create('thead', {}, [
                Utils.create('tr', {}, [
                    Utils.create('th', { text: '匿名ID' }),
                    Utils.create('th', { text: '化名' }),
                    Utils.create('th', { text: '原文样例' }),
                    Utils.create('th', { text: '类型' }),
                    Utils.create('th', { text: '最后出现' }),
                    Utils.create('th', { text: '操作' })
                ])
            ]);
            table.appendChild(thead);

            const tbody = Utils.create('tbody');
            items.forEach(item => {
                const tr = Utils.create('tr');
                const fp = item.fingerprint;

                const isDeleted = changes.deletions.has(fp);
                if (isDeleted) tr.style.opacity = '0.5';

                // 匿名ID
                tr.appendChild(Utils.create('td', {
                    text: item.anonymous_id || '',
                    style: 'font-family: monospace; font-size: 12px;'
                }));

                // 化名（对外展示用）
                tr.appendChild(Utils.create('td', {
                    text: item.display_alias || '—',
                    style: 'font-weight: 600; color: var(--text-primary);'
                }));

                // 原文
                tr.appendChild(Utils.create('td', {
                    text: item.sample_raw || '—',
                    style: 'font-family: monospace; font-size: 12px;'
                }));

                // 类型（下拉选择框）
                const typeSelect = Utils.create('select', {
                    style: 'width: 80px; padding: 2px 4px; border: 1px solid var(--border-color); border-radius: 4px; background: var(--bg-input); color: var(--text-primary);'
                });
                allTypes.forEach(t => {
                    const opt = Utils.create('option', { value: t, text: t });
                    if (t === item.sens_type) opt.selected = true;
                    typeSelect.appendChild(opt);
                });
                typeSelect.addEventListener('change', () => {
                    changes.updates[fp] = { sens_type: typeSelect.value };
                });
                tr.appendChild(Utils.create('td', {}, [typeSelect]));

                // 最后出现
                tr.appendChild(Utils.create('td', {
                    text: item.last_seen_at ? item.last_seen_at.slice(0, 16) : '—'
                }));

                // 操作
                const btnGroup = Utils.create('div', { style: 'display: flex; gap: 4px;' });

                const delBtn = Utils.create('button', {
                    class: 'wb-btn wb-btn-danger',
                    text: isDeleted ? '恢复' : '删除',
                    style: 'padding: 2px 8px; font-size: 11px;'
                });
                delBtn.addEventListener('click', () => {
                    if (changes.deletions.has(fp)) {
                        changes.deletions.delete(fp);
                        delBtn.textContent = '删除';
                        tr.style.opacity = '1';
                    } else {
                        changes.deletions.add(fp);
                        delBtn.textContent = '恢复';
                        tr.style.opacity = '0.5';
                    }
                });
                btnGroup.appendChild(delBtn);
                tr.appendChild(Utils.create('td', {}, [btnGroup]));
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            content.appendChild(table);

            // 5. 新增映射区域（也改用下拉选择框）
            const addSection = Utils.create('div', {
                style: 'margin-top: 16px; padding: 12px; background: var(--bg-secondary); border-radius: 8px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;'
            });
            addSection.appendChild(Utils.create('span', { text: '新增映射：', style: 'font-size: 12px; font-weight: 600;' }));

            const addOriginal = Utils.create('input', {
                placeholder: '原文（如“张三”）',
                style: 'flex: 1; min-width: 120px; padding: 4px 8px;'
            });
            addSection.appendChild(addOriginal);

            const addTypeSelect = Utils.create('select', {
                style: 'width: 120px; padding: 4px 8px; border: 1px solid var(--border-color); border-radius: 4px; background: var(--bg-input); color: var(--text-primary);'
            });
            allTypes.forEach(t => {
                const opt = Utils.create('option', { value: t, text: t });
                if (t === 'PERSON') opt.selected = true;  // 默认选 PERSON
                addTypeSelect.appendChild(opt);
            });
            addSection.appendChild(addTypeSelect);

            const addBtn = Utils.create('button', { class: 'wb-btn wb-btn-primary', text: '添加' });
            addBtn.addEventListener('click', () => {
                const original = addOriginal.value.trim();
                const sens_type = addTypeSelect.value;
                if (!original || !sens_type) {
                    Toast.warning('请填写原文并选择类型');
                    return;
                }
                // 记录到变更列表
                changes.additions.push({ original, sens_type });
                // 在表格中临时显示
                const newRow = Utils.create('tr', { style: 'background: var(--wb-accent-pale);' });
                newRow.innerHTML = `
                    <td style="color: var(--wb-accent-strong);">（待应用）</td>
                    <td>${original}</td>
                    <td>${sens_type}</td>
                    <td>—</td>
                    <td><span style="color: var(--wb-accent-strong);">✅ 新增</span></td>
                `;
                tbody.appendChild(newRow);
                addOriginal.value = '';
                Toast.success(`已暂存新增映射：${original} → ${sens_type}`);
            });
            addSection.appendChild(addBtn);

            // 添加“清空”按钮方便调试（可选）
            const clearBtn = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '清空新增' });
            clearBtn.addEventListener('click', () => {
                if (changes.additions.length === 0) {
                    Toast.info('暂无新增映射');
                    return;
                }
                changes.additions = [];
                // 移除临时行
                const rows = tbody.querySelectorAll('tr');
                rows.forEach(row => {
                    if (row.style && row.style.background === 'var(--wb-accent-pale)') {
                        row.remove();
                    }
                });
                Toast.info('已清空新增列表');
            });
            addSection.appendChild(clearBtn);

            content.appendChild(addSection);

            // 6. 底部操作按钮
            const footer = Utils.create('div', {
                style: 'margin-top: 16px; display: flex; justify-content: flex-end; gap: 8px;'
            });
            const saveBtn = Utils.create('button', {
                class: 'wb-btn wb-btn-primary',
                text: '💾 保存并重脱敏'
            });
            saveBtn.addEventListener('click', async () => {
                const hasChanges = Object.keys(changes.updates).length > 0 ||
                                  changes.deletions.size > 0 ||
                                  changes.additions.length > 0;
                if (!hasChanges) {
                    Toast.info('没有变更需要保存');
                    return;
                }
                saveBtn.disabled = true;
                saveBtn.textContent = '处理中…';
                try {
                    await this._saveMappingChanges(null, changes);
                    modal.remove();
                } catch (e) {
                    // 错误已在 _saveMappingChanges 中处理
                } finally {
                    saveBtn.disabled = false;
                    saveBtn.textContent = '💾 保存并重脱敏';
                }
            });
            footer.appendChild(saveBtn);

            const closeBtn = Utils.create('button', { class: 'wb-btn', text: '取消（关闭）' });
            closeBtn.addEventListener('click', () => modal.remove());
            footer.appendChild(closeBtn);
            content.appendChild(footer);

            // 点击遮罩关闭
            modal.addEventListener('click', (e) => {
                if (e.target === modal && !confirm('有未保存的变更，确定关闭吗？')) return;
                modal.remove();
            });
        },
        _renderMarkdownBody(text, className = 'wb-doc-preview md-content') {
            const el = Utils.create('div', { class: className });
            if (window.Markdown && typeof Markdown.parse === 'function') {
                el.innerHTML = Markdown.parse(text || '');
            } else {
                el.classList.add('wb-doc-preview');
                el.textContent = text || '';
            }
            return el;
        },

        async _renderMaterialDoc(panel, artifact, payload) {

            // 1. 先获取 documentId
            const documentId = artifact.ref_key || (payload && payload.document_id);
            if (!documentId) {
                panel.appendChild(Utils.create('div', { class: 'wb-empty', text: '缺少材料标识，无法加载内容' }));
                return;
            }
            // 2. 创建按钮栏（现在 documentId 已经可用）
            const actionBar = Utils.create('div', {
                style: 'display: flex; gap: 8px; margin-bottom: 12px;'
            });

            const manageBtn = Utils.create('button', {
                class: 'wb-btn wb-btn-ghost',
                text: '⚙️ 管理脱敏映射'
            });
            manageBtn.addEventListener('click', () => {
                this._openMappingManager(documentId);
            });
            actionBar.appendChild(manageBtn);
            panel.appendChild(actionBar);

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
                    Utils.create('div', { class: 'wb-panel-sub', text: `结构化预览 · ${data.chunk_count} 处片段` })
                ]);
                panel.appendChild(head);

                panel.appendChild(this._renderMarkdownBody(data.text || ''));

                // 底部提示
                panel.appendChild(Utils.create('div', {
                    class: 'wb-file-meta',
                    text: '以上为结构化预览（标题/表格分级显示）。存储原文未改动。',
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
            if (!confirm('确定删除该材料？删除后下游分析产物将标记为过期。')) return;
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
                if (this.currentView === 'materials') {
                    await this._renderCurrentView();
                    return;
                }
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
            // 兼容旧入口：改为打开上传弹窗
            this._openUploadModal();
            return Utils.create('div');
        },

        _openUploadModal() {
            if (!this.task) {
                Toast.error('任务尚未创建，无法上传材料');
                return;
            }
            const existing = document.querySelector('.wb-upload-modal-overlay');
            if (existing) existing.remove();

            const overlay = Utils.create('div', { class: 'wb-modal-overlay wb-upload-modal-overlay' });
            const modal = Utils.create('div', { class: 'wb-modal-content wb-upload-modal' });

            const head = Utils.create('div', { class: 'wb-upload-modal-head' }, [
                Utils.create('div', {}, [
                    Utils.create('div', { class: 'wb-upload-modal-title', text: '上传案件材料' }),
                    Utils.create('div', {
                        class: 'wb-upload-modal-sub',
                        text: '支持 PDF、Word、Excel、图片与文本，上传后将自动排队处理'
                    })
                ])
            ]);
            const closeBtn = this._iconBtn('wb-btn wb-btn-ghost wb-upload-modal-close', 'x', '');
            closeBtn.title = '关闭';
            closeBtn.addEventListener('click', () => overlay.remove());
            head.appendChild(closeBtn);
            modal.appendChild(head);

            const caseLabel = Utils.create('label', { class: 'wb-upload-field-label', text: '所属案件' });
            const select = Utils.create('select', { class: 'wb-upload-select' });
            (this.task.cases || []).forEach((c) => {
                select.appendChild(Utils.create('option', {
                    value: c.case_id,
                    text: c.display_name || c.name || c.case_id
                }));
            });
            if (this.materialsCaseFilter && this.materialsCaseFilter !== 'all') {
                select.value = this.materialsCaseFilter;
            }
            modal.appendChild(caseLabel);
            modal.appendChild(select);

            const fileLabel = Utils.create('label', { class: 'wb-upload-field-label', text: '材料文件' });
            const input = Utils.create('input', {
                type: 'file',
                multiple: 'multiple',
                accept: '.pdf,.docx,.txt,.png,.jpg,.jpeg,.xlsx,.xls',
                class: 'wb-upload-file-input'
            });
            input.hidden = true;
            const drop = Utils.create('div', { class: 'wb-upload-dropzone' });
            const dropInner = Utils.create('div', { class: 'wb-upload-drop-inner' });
            if (window.Icons) dropInner.appendChild(Icons.el('cloudUpload', 'wb-upload-cloud'));
            dropInner.appendChild(Utils.create('div', {
                class: 'wb-upload-drop-title',
                text: '点击选择文件或拖拽到此处'
            }));
            dropInner.appendChild(Utils.create('div', {
                class: 'wb-upload-drop-hint',
                text: '支持 PDF、DOCX、XLSX、JPG、PNG，单个文件不超过 100MB'
            }));
            const fileList = Utils.create('div', { class: 'wb-upload-file-list' });
            drop.appendChild(dropInner);
            drop.appendChild(fileList);
            drop.appendChild(input);

            const syncFileList = () => {
                fileList.innerHTML = '';
                const files = input.files ? Array.from(input.files) : [];
                if (!files.length) {
                    dropInner.hidden = false;
                    return;
                }
                dropInner.hidden = true;
                files.forEach((f) => {
                    fileList.appendChild(Utils.create('div', {
                        class: 'wb-upload-file-item',
                        text: `${f.name}（${this._sizeText(f.size)}）`
                    }));
                });
            };
            drop.addEventListener('click', () => input.click());
            input.addEventListener('change', syncFileList);
            drop.addEventListener('dragover', (e) => {
                e.preventDefault();
                drop.classList.add('dragover');
            });
            drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
            drop.addEventListener('drop', (e) => {
                e.preventDefault();
                drop.classList.remove('dragover');
                if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
                    const dt = new DataTransfer();
                    Array.from(e.dataTransfer.files).forEach((f) => dt.items.add(f));
                    input.files = dt.files;
                    syncFileList();
                }
            });

            modal.appendChild(fileLabel);
            modal.appendChild(drop);

            const foot = Utils.create('div', { class: 'wb-upload-modal-foot' });
            const notice = Utils.create('div', { class: 'wb-upload-legal' });
            if (window.Icons) notice.appendChild(Icons.el('fileText', 'wb-upload-legal-ico'));
            notice.appendChild(Utils.create('span', {
                text: '上传即视为您确认已获得该材料的合法查阅与分析授权'
            }));
            const actions = Utils.create('div', { class: 'wb-upload-modal-actions' });
            const cancel = this._iconBtn('wb-btn wb-btn-outline', 'x', '取消');
            cancel.addEventListener('click', () => overlay.remove());
            const start = this._iconBtn('wb-btn wb-btn-primary', 'upload', '开始上传');
            start.addEventListener('click', async () => {
                const ok = await this._uploadMaterials(select.value, input.files, start);
                if (ok) {
                    overlay.remove();
                    if (this.currentView === 'materials') await this._renderCurrentView();
                }
            });
            actions.appendChild(cancel);
            actions.appendChild(start);
            foot.appendChild(notice);
            foot.appendChild(actions);
            modal.appendChild(foot);

            overlay.appendChild(modal);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) overlay.remove();
            });
            document.body.appendChild(overlay);
        },

        async _uploadMaterials(caseId, files, btn) {
            if (!files || !files.length) {
                Toast.warning('请先选择材料文件');
                return false;
            }
            Toast.info('正在上传文件，请稍候…');
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
                const label = btn.querySelector('span:last-child');
                if (label) label.textContent = '上传中…';
                else btn.textContent = '上传中…';
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
                if (batch) {
                    delete this.artifactCache[batch.id];
                    if (!Utils.$('#wb-workspace').hidden && this.currentView !== 'materials') {
                        await this.openArtifact(batch.id);
                        this._postArtifactCard(batch.id, '材料接入与质量', `已接收 ${files.length} 份材料，可在此查看逐份处理进度。`);
                    }
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
                    const label = btn.querySelector('span:last-child');
                    if (label) label.textContent = '开始上传';
                }
            }
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
            if (!this.task || !this.task.id) return null;
            // 每次进入材料中心都刷新批次，保证上传时间/材料类型等字段最新
            try {
                await fetch(`/api/tasks/${this.task.id}/materials`);
            } catch (_) { /* ignore */ }
            const taskResp = await fetch(`/api/tasks/${this.task.id}`);
            const task = await taskResp.json();
            if (!task.error_code) this.task = task;
            const batch = (this.task.artifacts || []).find((a) => a.type === 'MATERIAL_BATCH');
            if (!batch) return null;
            delete this.artifactCache[batch.id];
            return this._fetchArtifact(batch.id);
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
            const actions = Utils.create('div', { class: 'wb-page-actions' });
            const uploadBtn = this._iconBtn('wb-btn wb-btn-primary', 'upload', '上传材料');
            uploadBtn.addEventListener('click', () => this._openUploadModal());
            const refreshBtn = this._iconBtn('wb-btn wb-btn-ghost', 'refresh', '刷新进度');
            refreshBtn.addEventListener('click', async () => {
                refreshBtn.disabled = true;
                try {
                    await this._refreshBatch();
                    Toast.success('进度已刷新');
                } finally {
                    refreshBtn.disabled = false;
                }
            });
            actions.appendChild(uploadBtn);
            actions.appendChild(refreshBtn);
            root.appendChild(this._pageHead(
                '材料中心',
                `共 ${rows.length} 份材料 · ${blocked} 份存在处理阻断问题，需处理后才能纳入分析`,
                actions,
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
                    Utils.create('th', { text: '分析授权' }),
                    Utils.create('th', { text: '上传时间' }),
                    Utils.create('th', { text: '操作', class: 'wb-th-actions' })
                ])
            ]));
            const tbody = Utils.create('tbody');
            if (!filtered.length) {
                tbody.appendChild(Utils.create('tr', {}, [
                    Utils.create('td', { text: '暂无材料，请点击右上角上传', colspan: '9' })
                ]));
            }
            filtered.forEach((m) => {
                const statusText = STAGE_TEXT[m.status] || m.status;
                const ready = m.status === 'PARSED';
                const warn = ATTENTION.includes(m.status);
                const fail = m.status === 'FAILED' || m.status === 'OCR_FAILED';
                const statusTone = fail ? 'danger' : (warn ? 'warn' : (ready ? 'ok' : 'neutral'));
                const availTone = ready ? 'ok' : (warn || fail ? 'danger' : 'warn');
                const availText = ready ? '允许分析' : (warn || fail ? '暂不允许' : '处理中');
                const redacted = m.redacted !== false;
                const tr = Utils.create('tr');
                tr.appendChild(Utils.create('td', { text: m.filename || '—' }));
                tr.appendChild(Utils.create('td', { text: m.case_name || '—' }));
                tr.appendChild(Utils.create('td', { text: m.material_type || m.doc_type || '其他材料' }));
                tr.appendChild(Utils.create('td', { text: m.version != null ? `v${m.version}` : (m.version_count != null ? `v${m.version_count}` : '—') }));
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
                    text: (m.uploaded_at || m.created_at || '—').toString().replace('T', ' ').slice(0, 16)
                }));
                const actionsTd = Utils.create('td', { class: 'wb-td-actions' });
                const delBtn = this._iconBtn('wb-btn wb-btn-ghost wb-btn-icon-danger', 'trash', '');
                delBtn.title = '删除材料';
                delBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    this._deleteMaterial(m.document_id);
                });
                actionsTd.appendChild(delBtn);
                tr.appendChild(actionsTd);
                tr.addEventListener('click', async (e) => {
                    if (e.target.closest('.wb-td-actions')) return;
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
            this._schedulePoll(payload);
        },

        async _ensureEntitySet() {
            const art = (this.task.artifacts || []).find((a) => a.type === 'ENTITY_CANDIDATE_SET');
            if (!art) return null;
            delete this.artifactCache[art.id];
            let data = await this._fetchArtifact(art.id);
            const ver = (((data || {}).payload || {}).summary || {}).extractor_version || '';
            // 旧产物用过时抽取器时自动重跑，否则页面会一直显示噪声人名
            if (!this._entityRefreshTried && this.task && this.task.id && ver !== 'stage9-quote-v1') {
                try {
                    Toast.info('实体识别规则已升级，正在重新抽取比对…');
                    const resp = await fetch(`/api/tasks/${this.task.id}/collision/run`, { method: 'POST' });
                    const result = await resp.json();
                    if (result.error_code) {
                        Toast.warning('自动重抽未完成，仍显示旧结果：' + (result.message || ''));
                        return data;
                    }
                    this._entityRefreshTried = true;
                    this.artifactCache = {};
                    await this.refreshTask();
                    const next = (this.task.artifacts || []).find((a) => a.type === 'ENTITY_CANDIDATE_SET');
                    if (next) {
                        data = await this._fetchArtifact(next.id);
                    }
                    Toast.success(`已按新规则更新，待核 ${result.candidate_count || 0} 条`);
                } catch (e) {
                    Toast.warning('自动重抽未完成，仍显示旧结果：' + (e.message || ''));
                }
            } else if (ver === 'stage9-quote-v1') {
                this._entityRefreshTried = true;
            }
            return data;
        },

        async _ensureTimelineSet() {
            const art = (this.task.artifacts || []).find((a) => a.type === 'ROLE_TIMELINE');
            if (!art) return { art: null, data: null };
            delete this.artifactCache[art.id];
            let data = await this._fetchArtifact(art.id);
            const ver = (((data || {}).payload || {}).summary || {}).extractor_version || '';
            if (!this._timelineRefreshTried && this.task && this.task.id && ver !== 'stage6-party-v2') {
                try {
                    Toast.info('时间线规则已升级，正在重新整理事件…');
                    const resp = await fetch(`/api/tasks/${this.task.id}/timeline/run`, { method: 'POST' });
                    const result = await resp.json();
                    if (result.error_code) {
                        Toast.warning('时间线重跑未完成，仍显示旧结果：' + (result.message || ''));
                        return { art, data };
                    }
                    this._timelineRefreshTried = true;
                    this.artifactCache = {};
                    this.task = result.task || this.task;
                    if (typeof this.refreshTask === 'function') {
                        await this.refreshTask();
                    }
                    const next = (this.task.artifacts || []).find((a) => a.type === 'ROLE_TIMELINE');
                    Toast.success(`时间线已更新 ${result.event_count || 0} 条`);
                    if (next) {
                        data = await this._fetchArtifact(next.id);
                        return { art: next, data };
                    }
                } catch (e) {
                    Toast.warning('时间线重跑未完成，仍显示旧结果：' + (e.message || ''));
                }
            } else if (ver === 'stage6-party-v2') {
                this._timelineRefreshTried = true;
            }
            return { art, data };
        },

        async _viewEntities(root) {
            const data = await this._ensureEntitySet();
            const payload = (data && data.payload) || {};
            const entityConfig = {
                BANK_ACCOUNT: {
                    label: '银行账户',
                    fields: [['account_no', '账号'], ['holder_name', '开户姓名'], ['bank_name', '开户行'], ['reserved_phone', '预留电话'], ['merchant', '关联商户']]
                },
                PHONE: {
                    label: '手机号码',
                    fields: [['phone_no', '号码'], ['registrant', '登记人'], ['linked_account', '关联账户'], ['linked_device', '关联设备'], ['contact_context', '联络语境']]
                },
                PERSON: {
                    label: '人物',
                    fields: [['name', '姓名'], ['id_card', '证件'], ['phone', '手机号'], ['account', '账户'], ['organization', '组织'], ['role_in_material', '材料记载角色']]
                },
                DEVICE: {
                    label: '电子设备',
                    fields: [['device_id', '设备号'], ['linked_phone', '关联手机号'], ['linked_account', '关联账户'], ['linked_person', '关联人员'], ['login_time', '登录时间']]
                },
                ORGANIZATION: {
                    label: '组织主体',
                    fields: [['org_name', '名称'], ['credit_code', '统一社会信用代码'], ['legal_person', '法人'], ['address', '地址'], ['phone', '电话'], ['account', '账户']]
                },
                MERCHANT: {
                    label: '商户',
                    fields: [['merchant_id', '商户号'], ['merchant_name', '商户名称'], ['settle_account', '结算账户'], ['pay_channel', '支付通道'], ['linked_org', '关联组织']]
                },
                ID_CARD: { label: '身份证件', fields: [['id_no', '证件号'], ['name', '姓名'], ['address', '地址']] },
                IP: { label: '网络地址', fields: [['ip_address', 'IP 地址'], ['linked_account', '关联账户'], ['linked_device', '关联设备']] }
            };
            const publicType = (raw) => ({
                ACCOUNT: 'BANK_ACCOUNT',
                NAME: 'PERSON',
                ORG: 'ORGANIZATION'
            }[String(raw || '').toUpperCase()] || String(raw || 'PERSON').toUpperCase());
                const candidateName = (candidate, type) => {
                const existing = String(candidate.display_name || candidate.title || '').trim();
                if (existing && !/^同一.+跨案出现$/.test(existing) && !/^(实体)?候选/.test(existing)) return existing;
                const first = (candidate.records || [])[0] || {};
                const raw = String(first.value || candidate.value || '').trim();
                const digits = raw.replace(/\D/g, '');
                if (type === 'BANK_ACCOUNT') return digits.length >= 4 ? `尾号 ${digits.slice(-4)} 银行账户` : '银行账户（同一脱敏标识）';
                if (type === 'PHONE') return digits.length >= 4 ? `尾号 ${digits.slice(-4)} 手机号码` : '手机号码（同一脱敏标识）';
                if (type === 'DEVICE') return digits.length >= 4 ? `IMEI 尾号 ${digits.slice(-4)} 设备` : '电子设备（同一设备标识）';
                if (type === 'ID_CARD') return digits.length >= 4 ? `尾号 ${digits.slice(-4)} 身份证件` : '身份证件（同一脱敏标识）';
                if (type === 'MERCHANT') return raw ? `商户号 ${raw} 商户` : '商户（同一商户标识）';
                if (type === 'IP') return raw ? `${raw} 网络地址` : '网络地址（同一脱敏标识）';
                if (type === 'ORGANIZATION') return raw ? `“${raw}”组织` : '组织主体（同一脱敏名称）';
                return raw ? `“${raw}”人物` : '人物（同一脱敏姓名）';
            };
            const normalizeCandidate = (raw) => {
                const candidate = { ...raw };
                candidate.entity_type = publicType(candidate.entity_type || candidate.object_type);
                candidate.match_tier = candidate.match_tier || 'STRONG';
                candidate.aliases = candidate.aliases || [];
                candidate.display_name = candidateName(candidate, candidate.entity_type);
                const cases = candidate.cases && candidate.cases.length
                    ? candidate.cases
                    : (candidate.records || []).reduce((acc, rec) => {
                        if (rec.case_id && !acc.some((item) => item.case_id === rec.case_id)) {
                            acc.push({ case_id: rec.case_id, case_name: rec.case_name || rec.case_id });
                        }
                        return acc;
                    }, []);
                candidate.cases = cases;
                if (!(candidate.field_compare || []).length) {
                    const config = entityConfig[candidate.entity_type] || entityConfig.PERSON;
                    candidate.field_compare = config.fields.map(([fieldKey, label], index) => ({
                        field_key: fieldKey,
                        label,
                        per_case: cases.map((caseItem) => {
                            const values = index === 0
                                ? (candidate.records || [])
                                    .filter((rec) => rec.case_id === caseItem.case_id)
                                    .map((rec) => rec.value)
                                    .filter(Boolean)
                                : [];
                            const value = [...new Set(values)].join('、') || null;
                            return {
                                case_id: caseItem.case_id,
                                case_name: caseItem.case_name,
                                value,
                                status: value ? 'same' : 'missing'
                            };
                        })
                    }));
                }
                return candidate;
            };
            const candidates = (payload.candidates || []).map(normalizeCandidate);
            const version = data ? data.version : 1;
            const artifact = data ? data.artifact : null;
            const status = data ? data.status : 'DRAFT';

            const pendingCount = candidates.filter((c) => !c.decision || c.decision === 'PENDING' || c.decision === 'DEFER').length;
            const typeFilter = this.entityTypeFilter || 'all';
            const entitySearch = String(this.entitySearchQuery || '').trim().toLowerCase();
            const typeLabel = {
                BANK_ACCOUNT: '银行账户', ACCOUNT: '银行账户',
                PHONE: '手机号码', PERSON: '人物', NAME: '人物',
                DEVICE: '电子设备', ORGANIZATION: '组织主体', MERCHANT: '商户',
                ID_CARD: '身份证件', IP: '网络地址'
            };
            let filtered = typeFilter === 'all'
                ? candidates
                : candidates.filter((c) => {
                    const t = c.entity_type || '';
                    if (typeFilter === 'BANK_ACCOUNT') return t === 'BANK_ACCOUNT' || t === 'ACCOUNT';
                    if (typeFilter === 'PERSON') return t === 'PERSON' || t === 'NAME';
                    return t === typeFilter;
                });
            if (entitySearch) {
                filtered = filtered.filter((candidate) => {
                    const searchable = [
                        candidate.display_name,
                        candidate.entity_type,
                        ...(candidate.cases || []).map((item) => item.case_name || item.case_id),
                        ...(candidate.records || []).map((item) => item.value)
                    ].filter(Boolean).join(' ').toLowerCase();
                    return searchable.includes(entitySearch);
                });
            }

            const reviewBtn = this._iconBtn('wb-btn wb-btn-outline', 'sparkles', 'Agent 复核建议');
            reviewBtn.addEventListener('click', async () => {
                reviewBtn.disabled = true;
                try {
                    const resp = await fetch(`/api/tasks/${this.task.id}/entity-candidates/review`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            candidate_id: this.selectedEntityId || null
                        })
                    });
                    const result = await resp.json();
                    if (result.error_code || result.ok === false) {
                        Toast.error(result.message || result.error || '复核未能完成');
                        return;
                    }
                    Toast.success('复核建议已更新');
                    await this.refreshTask();
                    this._renderCurrentView();
                } catch (e) {
                    Toast.error(e.message || '复核未能完成');
                } finally {
                    reviewBtn.disabled = false;
                }
            });

            root.appendChild(this._pageHead(
                '实体复核',
                '逐条核验跨案实体是否为同一主体；确认结果影响线索与图谱。',
                reviewBtn,
                `${pendingCount} 个待选`
            ));
            root.appendChild(Utils.create('div', { class: 'wb-alert warn' }, [
                window.Icons ? Icons.el('info') : Utils.create('span', { text: 'ℹ' }),
                Utils.create('span', {
                    text: payload.boundary || '复核提示：系统仅提供可解释的关联候选。请结合原文与字段差异作出判断，不要将候选直接作为事实结论。'
                })
            ]));
            const gate = payload.analysis_gate || (payload.summary || {}).analysis_gate || '';
            if (pendingCount > 0 || gate === 'ENTITY_REVIEW') {
                root.appendChild(Utils.create('div', { class: 'wb-alert info' }, [
                    window.Icons ? Icons.el('sparkles') : Utils.create('span', { text: '→' }),
                    Utils.create('span', {
                        text: `分析确认门闩：仍有 ${pendingCount} 条待核。请逐条作出「视为同一」或「保留独立」；全部确认后，右侧助手才会继续整理线索与报告。`
                    })
                ]));
            } else if (candidates.length) {
                root.appendChild(Utils.create('div', { class: 'wb-alert ok' }, [
                    window.Icons ? Icons.el('check') : Utils.create('span', { text: '✓' }),
                    Utils.create('span', {
                        text: '实体复核已完成。可继续整理线索、刷新时间线主体，或生成报告。'
                    })
                ]));
            }

            if (!candidates.length) {
                root.appendChild(Utils.create('div', { class: 'wb-empty', text: '尚无跨案对象候选。请先在「分析任务」发起跨案分析或标识比对。' }));
                const run = this._iconBtn('wb-btn wb-btn-primary', 'play', '开始跨案分析');
                run.style.marginTop = '12px';
                run.addEventListener('click', () => this._executeAnalysis(run));
                root.appendChild(run);
                return;
            }

            const tabs = Utils.create('div', { class: 'wb-toolbar', style: 'margin-bottom:10px;gap:6px;flex-wrap:wrap' });
            [
                ['all', '全部'],
                ['BANK_ACCOUNT', '银行账户'],
                ['PHONE', '手机号码'],
                ['PERSON', '人物'],
                ['DEVICE', '设备'],
                ['ORGANIZATION', '组织']
            ].forEach(([key, label]) => {
                const count = key === 'all' ? candidates.length : candidates.filter((c) => {
                    const t = c.entity_type || '';
                    if (key === 'BANK_ACCOUNT') return t === 'BANK_ACCOUNT' || t === 'ACCOUNT';
                    if (key === 'PERSON') return t === 'PERSON' || t === 'NAME';
                    return t === key;
                }).length;
                const btn = Utils.create('button', {
                    type: 'button',
                    class: `wb-btn ${typeFilter === key ? 'wb-btn-primary' : 'wb-btn-ghost'}`,
                    text: `${label} ${count}`
                });
                btn.addEventListener('click', () => {
                    this.entityTypeFilter = key;
                    this._renderCurrentView();
                });
                tabs.appendChild(btn);
            });
            root.appendChild(tabs);

            if (!filtered.length) {
                const empty = Utils.create('div', { class: 'wb-empty' }, [
                    Utils.create('div', { text: entitySearch ? '没有找到匹配的候选实体。' : '当前类型下无候选。' })
                ]);
                if (entitySearch) {
                    const clear = this._iconBtn('wb-btn wb-btn-ghost', 'x', '清除搜索');
                    clear.style.marginTop = '8px';
                    clear.addEventListener('click', () => {
                        this.entitySearchQuery = '';
                        this._renderCurrentView();
                    });
                    empty.appendChild(clear);
                }
                root.appendChild(empty);
                return;
            }

            if (!this.selectedEntityId || !filtered.some((c) => c.candidate_id === this.selectedEntityId)) {
                this.selectedEntityId = filtered[0].candidate_id;
            }
            const selected = filtered.find((c) => c.candidate_id === this.selectedEntityId) || filtered[0];

            const split = Utils.create('div', { class: 'wb-split-view' });
            const listCard = Utils.create('div', { class: 'wb-list-card' });
            listCard.appendChild(Utils.create('div', { class: 'wb-list-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '候选实体' }),
                Utils.create('div', { class: 'wb-file-meta', text: `${pendingCount} 个待核` })
            ]));
            const listBody = Utils.create('div', { class: 'wb-list-card-body' });
            const searchBox = Utils.create('div', { class: 'wb-entity-search' }, [
                window.Icons ? Icons.el('search') : Utils.create('span', { text: '⌕' })
            ]);
            const searchInput = Utils.create('input', {
                type: 'search',
                placeholder: '搜索实体、案件或召回方式',
                value: this.entitySearchQuery || ''
            });
            searchInput.addEventListener('input', (event) => {
                this.entitySearchQuery = event.target.value;
                clearTimeout(this.entitySearchTimer);
                this.entitySearchTimer = setTimeout(() => this._renderCurrentView(), 180);
            });
            searchBox.appendChild(searchInput);
            listBody.appendChild(searchBox);
            filtered.forEach((c) => {
                const decision = c.decision || 'PENDING';
                const label = { PENDING: '待确认', MERGE: '视为同一', KEEP_SEPARATE: '保留独立', CORRECT: '已更正', DEFER: '待重新核验' }[decision] || decision;
                const tone = decision === 'PENDING' || decision === 'DEFER' ? 'warn' : (decision === 'KEEP_SEPARATE' ? 'danger' : 'ok');
                const typeIcon = window.Icons
                    ? Icons.el(Icons.forEntityType(c.entity_type || c.object_type || c.title), 'wb-list-type-ico')
                    : null;
                const cases = c.cases && c.cases.length
                    ? c.cases
                    : (c.records || []).reduce((acc, r) => {
                        if (!acc.find((x) => x.case_id === r.case_id)) {
                            acc.push({ case_id: r.case_id, case_name: r.case_name });
                        }
                        return acc;
                    }, []);
                const titleRow = Utils.create('div', {
                    style: 'display:flex;justify-content:space-between;gap:8px;align-items:center'
                });
                const titleLeft = Utils.create('div', {
                    style: 'display:flex;align-items:center;gap:8px;min-width:0'
                });
                if (typeIcon) titleLeft.appendChild(typeIcon);
                titleLeft.appendChild(Utils.create('div', {
                    class: 'wb-list-item-title',
                    text: c.display_name || c.title || '跨案对象'
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
                        text: (() => {
                            const tier = c.match_tier === 'SUSPECTED' ? '疑似化名' : '强标识';
                            const aliasPart = (c.aliases || []).length > 1
                                ? ` · ${(c.aliases || []).slice(0, 3).join('/')}`
                                : '';
                            return `${typeLabel[c.entity_type] || c.entity_type || '对象'} · ${tier} · ${cases.length} 案件 · ${cases.map((x) => x.case_name || x.case_id).slice(0, 2).join(' · ') || '多案'}${aliasPart}`;
                        })()
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

            const detail = Utils.create('div', { class: 'wb-entity-detail-stack' });
            const overview = Utils.create('section', { class: 'wb-detail-card wb-entity-overview-card' });
            const overviewHead = Utils.create('div', { class: 'wb-detail-card-head wb-entity-overview-head' });
            const titleLine = Utils.create('div', { class: 'wb-entity-title-line' }, [
                Utils.create('div', { class: 'wb-entity-title wb-entity-main-title', text: selected.display_name }),
                Utils.create('span', {
                    class: 'wb-entity-type-badge',
                    text: typeLabel[selected.entity_type] || selected.entity_type || '对象'
                })
            ]);
            const recallMethod = selected.recall_method
                || (selected.match_tier === 'SUSPECTED' ? '姓名相似 + 关联字段召回' : '跨案标识召回');
            const heading = Utils.create('div', { class: 'wb-entity-heading' }, [
                window.Icons ? Icons.el(Icons.forEntityType(selected.entity_type), 'wb-entity-main-icon') : Utils.create('span'),
                Utils.create('div', {}, [
                    titleLine,
                    Utils.create('div', {
                        class: 'wb-file-meta',
                        text: `${recallMethod}${selected.recalled_at ? ` · 召回于 ${selected.recalled_at}` : ''}`
                    })
                ])
            ]);
            overviewHead.appendChild(heading);
            const overviewActions = Utils.create('div', { class: 'wb-entity-actions' });
            if (artifact && (selected.decision || 'PENDING') === 'PENDING' && status !== 'STALE') {
                [
                    ['KEEP_SEPARATE', '保留为独立主体', 'shield', 'wb-btn wb-btn-outline'],
                    ['MERGE', '确认关联', 'link2', 'wb-btn wb-btn-primary']
                ].forEach(([decision, label, icon, cls]) => {
                    const btn = this._iconBtn(cls, icon, label);
                    btn.addEventListener('click', () => {
                        this._showEntityDecisionForm(overviewBody, selected, decision, label, version);
                    });
                    overviewActions.appendChild(btn);
                });
            }
            overviewHead.appendChild(overviewActions);
            overview.appendChild(overviewHead);

            const overviewBody = Utils.create('div', { class: 'wb-detail-card-body' });
            const impact = selected.impact || {};
            const caseCount = impact.case_count || (selected.cases || []).length || 0;
            const relationCount = impact.relation_count || 0;
            const clueCount = impact.clue_count || (selected.generated_clues || []).length || 0;
            overviewBody.appendChild(Utils.create('div', { class: 'wb-entity-metrics' }, [
                this._metric('涉及案件', caseCount),
                this._metric('受影响关联', relationCount),
                this._metric('生成线索', clueCount)
            ]));
            if (selected.agent_summary) {
                overviewBody.appendChild(Utils.create('div', { class: 'wb-agent-summary' }, [
                    Utils.create('span', { class: 'wb-agent-summary-label', text: 'Agent 复核：' }),
                    Utils.create('span', { text: selected.agent_summary })
                ]));
            }
            if ((selected.decision || 'PENDING') !== 'PENDING') {
                overviewBody.appendChild(Utils.create('div', {
                    class: 'wb-file-meta',
                    text: `已记录：${selected.decision} · ${selected.reason || ''}`
                }));
            }
            overview.appendChild(overviewBody);
            detail.appendChild(overview);

            // 模块二：字段表由 DeepSeek 依材料现场设计，不同实体表头不同
            const compareCard = Utils.create('section', { class: 'wb-detail-card wb-entity-section-card' });
            const compareHead = Utils.create('div', { class: 'wb-detail-card-head' }, [
                Utils.create('div', {
                    class: 'wb-entity-title',
                    text: ((selected.field_table_meta || {}).table_title) || '字段对照与差异说明'
                })
            ]);
            const rebuild = this._iconBtn('wb-btn wb-btn-ghost wb-btn-sm', 'refresh', '重建字段表');
            rebuild.addEventListener('click', () => this._buildFieldTable(selected.candidate_id, true));
            compareHead.appendChild(rebuild);
            compareCard.appendChild(compareHead);
            const compareBody = Utils.create('div', { class: 'wb-detail-card-body wb-compare-body' });
            const compareCases = selected.field_compare_columns || selected.cases || [];
            const fields = selected.field_compare || [];
            const compareTable = Utils.create('div', { class: 'wb-compare-table' });
            const columns = `140px repeat(${Math.max(compareCases.length, 1)}, minmax(140px, 1fr))`;
            const tableHead = Utils.create('div', { class: 'wb-compare-row wb-compare-head' });
            tableHead.style.gridTemplateColumns = columns;
            tableHead.appendChild(Utils.create('div', { text: '字段' }));
            const aliasMode = !!(selected.field_compare_columns || []).length;
            compareCases.forEach((caseItem, index) => {
                tableHead.appendChild(Utils.create('div', {
                    text: aliasMode
                        ? `称谓 · ${caseItem.case_name || caseItem.label || ''}`
                        : `来源 ${String.fromCharCode(65 + index)}${caseItem.case_name ? ` · ${caseItem.case_name}` : ''}`
                }));
            });
            compareTable.appendChild(tableHead);
            fields.forEach((field) => {
                const row = Utils.create('div', { class: 'wb-compare-row' });
                row.style.gridTemplateColumns = columns;
                row.appendChild(Utils.create('div', {
                    class: 'wb-compare-field-name',
                    text: field.label || field.field_key || '字段'
                }));
                const cells = compareCases.map((caseItem) =>
                    (field.per_case || []).find((item) => item.case_id === caseItem.case_id) || {}
                );
                cells.forEach((value) => {
                    const rawStatus = value.status || (!value.value ? 'missing' : 'same');
                    const state = !value.value || rawStatus === 'missing'
                        ? 'missing'
                        : (rawStatus === 'diff' ? 'diff' : (rawStatus === 'partial' ? 'partial' : 'same'));
                    const stateLabel = {
                        same: '一致',
                        diff: '不一致',
                        missing: '未记载',
                        partial: '部分一致'
                    }[state];
                    const cell = Utils.create('div', { class: 'wb-compare-cell' });
                    cell.appendChild(Utils.create('span', {
                        class: `wb-compare-status ${state}`,
                        text: stateLabel
                    }));
                    cell.appendChild(Utils.create('span', {
                        class: `wb-compare-value${state === 'missing' ? ' wb-match-na' : ''}${state === 'diff' ? ' is-diff' : ''}`,
                        text: value.value || '未记载'
                    }));
                    row.appendChild(cell);
                });
                compareTable.appendChild(row);
            });
            compareBody.appendChild(compareTable);
            if (!fields.length) {
                compareBody.appendChild(Utils.create('div', {
                    class: 'wb-empty',
                    text: this._fieldTableBusy === selected.candidate_id ? '正在按材料生成字段对照表…' : '暂无字段对照，可点「重建字段表」。'
                }));
            }
            compareCard.appendChild(compareBody);
            detail.appendChild(compareCard);
            if (!(selected.field_table_meta || {}).producer) {
                this._buildFieldTable(selected.candidate_id, false);
            }

            // 模块三：依据材料与原文片段（各类实体同一卡片：案名+页码徽标 / 引用片段 / 材料·字段·打开原文）
            const evidenceCard = Utils.create('section', { class: 'wb-detail-card wb-entity-section-card' });
            evidenceCard.appendChild(Utils.create('div', { class: 'wb-detail-card-head' }, [
                Utils.create('div', { class: 'wb-entity-title', text: '依据材料与原文片段' })
            ]));
            const evidenceBody = Utils.create('div', { class: 'wb-detail-card-body wb-evidence-list' });
            const evidenceList = this._collectEntityEvidence(selected);
            const showAllEvidence = this.expandedEntityEvidenceId === selected.candidate_id;
            evidenceList.slice(0, showAllEvidence ? 99 : 4).forEach((ev, evIndex) => {
                const linkable = !!(ev.chunk_id && ev.quote_hash);
                const badgeText = linkable
                    ? [
                        ev.page_start ? `第 ${ev.page_start} 页` : '已定位',
                        ev.ocr_confidence != null ? `OCR ${Math.round(ev.ocr_confidence * 100)}%` : ''
                    ].filter(Boolean).join(' · ')
                    : '引用失效';
                const fieldPart = ev.field_label ? `字段·${ev.field_label}` : '';
                const row = Utils.create('article', { class: 'wb-evidence-card' }, [
                    Utils.create('div', { class: 'wb-evidence-card-head' }, [
                        Utils.create('strong', { text: ev.case_name || ev.case_id || '案件材料' }),
                        Utils.create('span', {
                            class: `wb-evidence-page${linkable ? '' : ' is-invalid'}`,
                            text: badgeText
                        })
                    ]),
                    Utils.create('blockquote', {
                        text: `“${this._orgStyleEvidenceQuote(ev)}”`
                    }),
                    Utils.create('div', { class: 'wb-evidence-card-foot' }, [
                        Utils.create('span', {
                            text: [
                                ev.filename || '材料原文',
                                ev.version_no != null ? `v${ev.version_no}` : '',
                                fieldPart
                            ].filter(Boolean).join(' · ')
                        }),
                        Utils.create('span', {
                            class: 'wb-open-source',
                            text: linkable ? '打开原文 ↗' : '待补定位'
                        })
                    ])
                ]);
                if (linkable) {
                    row.classList.add('clickable');
                    row.addEventListener('click', () => this._openCitation(ev, evidenceList, evIndex));
                }
                evidenceBody.appendChild(row);
            });
            if (!evidenceList.length) {
                evidenceBody.appendChild(Utils.create('div', { class: 'wb-empty', text: '暂无可回链原文，本候选不应确认关联。' }));
            }
            if (evidenceList.length > 4 && !showAllEvidence) {
                const more = this._iconBtn('wb-btn wb-btn-ghost', 'chevronDown', `展开全部 ${evidenceList.length} 条`);
                more.addEventListener('click', () => {
                    this.expandedEntityEvidenceId = selected.candidate_id;
                    this._renderCurrentView();
                });
                evidenceBody.appendChild(more);
            }
            evidenceCard.appendChild(evidenceBody);
            detail.appendChild(evidenceCard);
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
                Utils.create('div', { class: 'wb-entity-title', text: '线索处置' })
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
                if (ev) {
                    this._openCitation({
                        ...ev,
                        quote_storage: ev.quote_storage || ev.quote,
                        quote_display: ev.quote_display || this._displayQuote(ev),
                        highlight_terms: [ev.value, ev.extracted_value].filter(Boolean)
                    });
                } else Toast.info('暂无原文依据');
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

            const ensured = await this._ensureTimelineSet();
            const art = ensured.art || (this.task.artifacts || []).find((a) => a.type === 'ROLE_TIMELINE');
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

            const data = ensured.data || await this._fetchArtifact(art.id);
            const payload = (data && data.payload) || {};
            const items = payload.items || [];

            const normalized = items.map((item, idx) => {
                const parties = this._normalizeParties(item.parties || []);
                const primary = parties[0];
                const subject = item.subject
                    || (primary && (primary.display_name || primary.surface))
                    || item.case_name
                    || item.case_id
                    || `事件 ${idx + 1}`;
                const subjectId = item.subject_id
                    || (primary && primary.subject_id)
                    || subject;
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
                    parties,
                    subject,
                    subjectId,
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
            const subjectMap = new Map();
            normalized.forEach((e) => {
                const key = e.subjectId || e.subject;
                if (!subjectMap.has(key)) subjectMap.set(key, e.subject);
            });
            const subjectEntries = Array.from(subjectMap.entries());
            const filtered = normalized.filter((e) =>
                this.timelineSubject === 'all'
                || e.subjectId === this.timelineSubject
                || e.subject === this.timelineSubject
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
            subjectEntries.forEach(([id, name]) => {
                select.appendChild(Utils.create('option', { value: id, text: name }));
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
