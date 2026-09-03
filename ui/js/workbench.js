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
        NEEDS_OCR_REVIEW: 'OCR 待复核',
        OCR_FAILED: 'OCR 失败',
        DUPLICATE_PENDING: '重复待处理',
        FAILED: '解析失败',
        DELETED: '已删除'
    };
    const ATTENTION = ['NEEDS_OCR_REVIEW', 'OCR_FAILED', 'FAILED', 'DUPLICATE_PENDING'];

    const Workbench = {
        task: null,
        tabs: [],
        activeTabId: null,
        pollTimer: null,

        async init() {
            this._bindShell();
            this._bindScopeForm();
            await this.loadTasks();
        },

        // ---------- 任务列表 ----------

        async loadTasks() {
            try {
                const resp = await fetch('/api/tasks?limit=30');
                const data = await resp.json();
                this.tasks = data.tasks || [];
                this._renderRail();
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
            const list = Utils.$('#wb-task-list');
            const history = Utils.$('#wb-task-history');
            if (!list) return;
            list.innerHTML = '';
            if (history) history.innerHTML = '';

            const tasks = this.tasks || [];
            const recent = tasks.slice(0, 5);
            const older = tasks.slice(5);

            recent.forEach(task => list.appendChild(this._taskChip(task)));
            if (history) {
                if (!older.length) {
                    history.appendChild(Utils.create('div', {
                        class: 'wb-rail-empty',
                        text: '暂无更多'
                    }));
                } else {
                    older.forEach(task => history.appendChild(this._taskChip(task, true)));
                }
            }
            this._syncRailCollapseAvailability();
        },

        _taskChip(task, compact) {
            const chip = Utils.create('div', {
                class: `wb-task-chip${this.task && this.task.id === task.id ? ' active' : ''}${compact ? ' is-history' : ''}`,
                title: `${task.title}（${task.case_count || 0} 起案件）`
            }, [
                Utils.create('span', { class: 'wb-task-chip-abbr', text: (task.title || '任务').slice(0, 2) }),
                Utils.create('span', { class: 'wb-task-chip-name', text: task.title || '未命名任务' })
            ]);
            chip.addEventListener('click', () => this.openTask(task.id));

            // ★ 添加删除按钮
            const delBtn = Utils.create('span', {
                class: 'wb-task-del',
                text: '×',
                title: '删除任务'
            });
            delBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (!confirm(`确定删除任务「${task.title}」？\n该任务下的所有材料也将一并删除。`)) return;
                try {
                    const resp = await fetch(`/api/tasks/${task.id}`, { method: 'DELETE' });
                    const data = await resp.json();
                    if (data.error_code) {
                        Toast.error(data.message || '删除失败');
                        return;
                    }
                    Toast.success('任务已删除');
                    // 如果删除的是草稿任务，重置 draftTaskId
                    if (this.draftTaskId === task.id) {
                        this.draftTaskId = null;
                    }

                    if (this.task && this.task.id === task.id) {
                        this.task = null;
                        State.currentTaskId = null;
                        Utils.$('#wb-workspace').hidden = true;
                        this.showStart();
                    }
                    await this.loadTasks();
                } catch (err) {
                    Toast.error('删除失败：' + err.message);
                }
            });
            chip.appendChild(delBtn);

            return chip;
        },

        _syncRailCollapseAvailability() {
            const railBtn = Utils.$('#wb-toggle-rail');
            const workspace = Utils.$('#wb-workspace');
            const canCollapse = workspace && !workspace.hidden;
            if (railBtn) {
                railBtn.hidden = !canCollapse;
                railBtn.disabled = !canCollapse;
            }
            if (!canCollapse && State.sidebarCollapsed) {
                State.sidebarCollapsed = false;
                Events.emit('sidebar:toggle', false);
            }
        },

        // ---------- 状态 A：范围设置 ----------

        showStart() {
            Utils.$('#wb-start').hidden = false;
            Utils.$('#wb-start').classList.remove('is-leaving');
            Utils.$('#wb-workspace').hidden = true;
            Utils.$('#wb-scope').hidden = false;
            if (!Utils.$$('.wb-case-row').length) {
                this._addCaseRow('案件 A');
                this._addCaseRow('案件 B');
            }
            this._checkScope();
            this._syncRailCollapseAvailability();
        },

        _bindShell() {
            const collapseBtn = Utils.$('#wb-collapse-rail');
            const expandBtn = Utils.$('#wb-expand-rail');
            if (collapseBtn) collapseBtn.addEventListener('click', () => {
                Utils.$('#sidebar').classList.add('collapsed');
            });
            if (expandBtn) expandBtn.addEventListener('click', () => {
                Utils.$('#sidebar').classList.remove('collapsed');
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
                this._renderRail();
                this.showStart();
            });

            // 仅当工作台（含文件目录）出现时，才允许收起最左侧全局栏
            const railBtn = Utils.$('#wb-toggle-rail');
            if (railBtn) railBtn.addEventListener('click', () => {
                if (Utils.$('#wb-workspace').hidden) {
                    Toast.info('进入工作台后才可收起左侧栏');
                    return;
                }
                State.toggleSidebar();
            });

            const agentBtn = Utils.$('#wb-toggle-agent');
            if (agentBtn) agentBtn.addEventListener('click', () => {
                Utils.$('#wb-workspace').classList.toggle('agent-collapsed');
            });

            ['#wb-settings', '#wb-help'].forEach(sel => {
                const btn = Utils.$(sel);
                if (btn) btn.addEventListener('click', () => Toast.info('该入口将在后续阶段接入'));
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
            const fileBtn = Utils.create('button', {
                class: 'wb-btn wb-btn-ghost',
                text: '挂材料',
                type: 'button'
            });
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
                button.textContent = '正在生成计划…';
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
                this._renderRail();

                await this._uploadStartFiles(data.task);
                await this._transitionToDraftWorkspace(
                    data.task.id,
                    data.scope_artifact_id || this._scopeArtifactId(data.task)
                );
            } catch (e) {
                Toast.error('任务创建失败：' + e.message);
            } finally {
                if (button) {
                    button.textContent = '生成分析计划';
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
            await this.openTask(taskId, scopeArtifactId);
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
            this._renderRail();
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

            await this._bindConversation(task);
            Utils.$('#wb-start').hidden = true;
            Utils.$('#wb-workspace').hidden = false;
            this._renderRail();
            this._renderContext();
            this._renderDirectory();
            this._syncRailCollapseAvailability();
            this._renderPanel();

            if (task.status === 'SCOPE_DRAFT') {
                this.draftTaskId = task.id;
                const planResp = await fetch(`/api/tasks/${task.id}/plan`);
                this._renderAgentPlan(await planResp.json());
            } else {
                this.draftTaskId = task.id;
                this._maybeOfferRerun();
            }
        },

        _maybeOfferRerun() {
            if (!this.task || this.task.status === 'SCOPE_DRAFT') return;
            const hasCandidates = (this.task.artifacts || []).some(a => a.type === 'ENTITY_CANDIDATE_SET');
            const hasMaterials = (this.task.artifacts || []).some(a => a.type === 'MATERIAL_DOC' || a.type === 'MATERIAL_BATCH');
            if (!hasMaterials || hasCandidates) return;
            const messages = Utils.$('#chat-messages');
            if (!messages || messages.querySelector('[data-wb-rerun]')) return;
            const run = Utils.create('button', { class: 'wb-btn wb-btn-primary', text: '执行分析' });
            run.addEventListener('click', () => this._executeAnalysis(run));
            const card = Utils.create('div', { class: 'wb-agent-plan-card', 'data-wb-rerun': '1' }, [
                Utils.create('div', { class: 'wb-agent-plan-kicker', text: '材料已接入' }),
                Utils.create('div', { class: 'wb-agent-plan-title', text: '尚未跑完整分析' }),
                Utils.create('div', {
                    class: 'wb-agent-plan-desc',
                    text: '点下方按钮，由智能体在右侧思考并调用工具；产物会出现链接，可打开中间预览。'
                }),
                Utils.create('div', { class: 'wb-agent-plan-actions' }, [run])
            ]);
            messages.appendChild(card);
            messages.scrollTop = messages.scrollHeight;
        },

        /** 每个任务绑定自己的智能体会话：聊天历史随任务走，而不是随一次性对话 */
        async _bindConversation(task) {
            // 直接从数据库加载该任务的历史聊天记录
            const messages = Utils.$('#chat-messages');
            if (messages) messages.innerHTML = '';

            try {
                const response = await fetch(`/chat/${task.id}/messages`);
                const data = await response.json();
                (data.messages || []).forEach(msg => {
                    if (!messages) return;
                    if (msg.role === 'user') {
                        messages.appendChild(Message.renderUser(msg.content));
                    } else if (msg.role === 'assistant' && (msg.content || '').trim()) {
                        const { wrap, content } = Message.renderAssistantContainer();
                        content.innerHTML = Markdown.parse(msg.content);
                        messages.appendChild(wrap);
                    }
                });
            } catch (_) {
                // 新任务还没有聊天消息时保持空白
            }
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

            const edit = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '返回修改' });
            edit.addEventListener('click', () => this._returnToScope());
            const confirm = Utils.create('button', {
                class: 'wb-btn wb-btn-primary',
                text: '执行分析'
            });
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
            const ctx = Utils.$('#wb-topbar-context');
            const task = this.task;
            if (ctx) {
                ctx.innerHTML = '';
                ctx.appendChild(Utils.create('b', { text: task.title }));
                ctx.appendChild(Utils.create('span', {
                    text: `　${task.cases.length} 起案件 · 授权至 ${task.authorized_until} · ${task.status === 'SCOPE_DRAFT' ? '待确认计划' : '计划已确认'}`
                }));
            }
            const dirTitle = Utils.$('#wb-dir-title');
            if (dirTitle) dirTitle.textContent = task.title;
            const dirMeta = Utils.$('#wb-dir-meta');
            if (dirMeta) dirMeta.textContent = `${task.cases.length} 起案件 · 授权有效`;
            const agentCtx = Utils.$('#wb-agent-context');
            if (agentCtx) agentCtx.textContent = `当前绑定：${task.title}`;
            this._syncComposerCase();
        },

        _syncComposerCase() {
            const wrap = Utils.$('#wb-composer-case-wrap');
            const select = Utils.$('#wb-composer-case');
            if (!wrap || !select) return;
            const cases = (this.task && this.task.cases) || [];
            wrap.hidden = !cases.length;
            const current = select.value;
            select.innerHTML = '';
            cases.forEach(c => {
                select.appendChild(Utils.create('option', { value: c.case_id, text: c.display_name }));
            });
            if (current && cases.some(c => c.case_id === current)) select.value = current;
        },

        _renderDirectory() {
            const body = Utils.$('#wb-dir-body');
            if (!body) return;
            body.innerHTML = '';

            (this.task.directory || []).forEach(group => {
                const wrap = Utils.create('div', { class: 'wb-dir-group' });
                wrap.appendChild(Utils.create('div', { class: 'wb-dir-group-label' }, [
                    Utils.create('span', { text: group.label }),
                    Utils.create('span', { text: group.items.length ? String(group.items.length) : '待生成' })
                ]));

                if (!group.items.length) {
                    wrap.appendChild(Utils.create('div', { class: 'wb-dir-empty', text: '尚未生成' }));
                } else {
                    group.items.forEach(item => {
                        const row = Utils.create('div', {
                            class: `wb-dir-item${this.activeTabId === item.artifact_id ? ' active' : ''}`,
                            'data-artifact': item.artifact_id
                        }, [
                            Utils.create('span', { class: 'name', text: item.title }),
                            Utils.create('span', {
                                class: `wb-status-tag${item.status === 'STALE' ? ' stale' : ''}`,
                                text: item.status === 'STALE' ? '已过期' : `v${item.version}`
                            })
                        ]);
                        row.addEventListener('click', () => this.openArtifact(item.artifact_id));
                        wrap.appendChild(row);
                    });
                }
                body.appendChild(wrap);
            });
        },

        // ---------- 产物标签页 ----------

        /** 目录点击与智能体链接共用入口：同一 artifact_id 只会有一个标签 */
        async openArtifact(artifactId) {
            if (!this.task) return;
            const resp = await fetch(`/api/tasks/${this.task.id}/artifacts/${artifactId}`);
            const data = await resp.json();
            if (data.error_code) {
                Toast.error(data.message || '产物打开失败');
                return;
            }
            const existing = this.tabs.find(t => t.id === artifactId);
            if (existing) {
                existing.data = data;
            } else {
                this.tabs.push({ id: artifactId, title: data.artifact.title, data });
            }
            this.activeTabId = artifactId;
            this._renderTabs();
            this._renderPanel();
            this._renderDirectory();
        },

        _renderTabs() {
            const bar = Utils.$('#wb-tabs');
            if (!bar) return;
            bar.innerHTML = '';
            this.tabs.forEach(tab => {
                const el = Utils.create('div', {
                    class: `wb-tab${tab.id === this.activeTabId ? ' active' : ''}`
                }, [
                    Utils.create('span', { text: tab.title }),
                    Utils.create('span', { class: 'wb-tab-close', text: '×' })
                ]);
                el.addEventListener('click', (e) => {
                    if (e.target.classList.contains('wb-tab-close')) {
                        this.tabs = this.tabs.filter(t => t.id !== tab.id);
                        if (this.activeTabId === tab.id) {
                            this.activeTabId = this.tabs.length ? this.tabs[this.tabs.length - 1].id : null;
                        }
                        this._renderTabs();
                        this._renderPanel();
                        this._renderDirectory();
                        return;
                    }
                    this.activeTabId = tab.id;
                    this._renderTabs();
                    this._renderPanel();
                    this._renderDirectory();
                });
                bar.appendChild(el);
            });
        },

        _renderPanel() {
            const panel = Utils.$('#wb-panel');
            if (!panel) return;
            panel.innerHTML = '';
            const tab = this.tabs.find(t => t.id === this.activeTabId);
            if (!tab) {
                if (!this.task) {
                    panel.appendChild(Utils.create('div', { class: 'wb-empty', text: '从左侧任务目录打开一个产物' }));
                    return;
                }
                const batch = (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH');
                if (batch) {
                    panel.appendChild(Utils.create('div', { class: 'wb-empty', text: '加载材料批次…' }));
                    fetch(`/api/tasks/${this.task.id}/artifacts/${batch.id}`)
                        .then(r => r.json())
                        .then(data => {
                            panel.innerHTML = '';
                            if (data.payload) {
                                this._renderMaterialBatch(panel, data.payload);
                            }
                        })
                        .catch(() => {});
                } else {
                    panel.appendChild(this._uploadBox());
                }
                return;
            }
            const { artifact, payload, version, status } = tab.data;
            const statusLabel = {
                DRAFT: '草稿',
                PENDING_REVIEW: '待复核',
                VALID: '有效',
                STALE: '需更新',
                INVALID: '已失效'
            }[status] || status;

            const head = Utils.create('div', { class: 'wb-panel-head' }, [
                Utils.create('div', {}, [
                    Utils.create('div', { class: 'wb-panel-title', text: artifact.title }),
                    Utils.create('div', {
                        class: 'wb-panel-sub',
                        text: `${artifact.type} · 版本 v${version} · ${statusLabel}`
                    })
                ]),
                Utils.create('span', {
                    class: `wb-pill${['STALE', 'INVALID', 'PENDING_REVIEW'].includes(status) ? ' warn' : ' ok'}`,
                    text: statusLabel
                })
            ]);
            panel.appendChild(head);

            const split = Utils.create('div', { class: 'wb-split', id: 'wb-split' });
            const main = Utils.create('div', { class: 'wb-split-main', id: 'wb-split-main' });
            const cite = Utils.create('aside', { class: 'wb-cite-pane', id: 'wb-cite-pane' });
            cite.hidden = true;
            split.appendChild(main);
            split.appendChild(cite);
            panel.appendChild(split);

            if (status === 'STALE') {
                main.appendChild(Utils.create('div', { class: 'wb-callout warn' }, [
                    Utils.create('span', { text: '输入已变化，当前结果可能过时，不能用于处置或正式导出。' })
                ]));
            }

            if (artifact.type === 'MATERIAL_BATCH') this._renderMaterialBatch(main, payload);
            else if (artifact.type === 'TASK_SCOPE') this._renderScopeArtifact(main, payload);
            else if (artifact.type === 'ENTITY_CANDIDATE_SET') {
                this._renderEntityCandidates(main, payload, status, artifact, version);
            }
            else if (artifact.type === 'CLUE_SET') this._renderClueSet(main, payload);
            else if (artifact.type === 'CLUE_ITEM') this._renderClueItem(main, payload);
            else if (artifact.type === 'ROLE_TIMELINE') this._renderRoleTimeline(main, payload);
            else if (artifact.type === 'MATERIAL_DOC') this._renderMaterialDoc(main, artifact, payload);
            else this._renderGeneric(main, payload);
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
            const runBtn = Utils.create('button', { class: 'wb-btn wb-btn-primary', text: '执行分析' });
            runBtn.addEventListener('click', () => this._executeAnalysis(runBtn));
            const eventBtn = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '仅抽取事件时间线' });
            eventBtn.addEventListener('click', () => this._runTimeline(eventBtn));
            actions.appendChild(runBtn);
            actions.appendChild(eventBtn);
            panel.appendChild(actions);
            panel.appendChild(Utils.create('div', {
                class: 'wb-file-meta',
                text: '完整分析由右侧智能体编排（DeepSeek 调工具）。点「执行分析」会向智能体下达指令；也可在对话框直接说明需求。',
                style: 'margin-bottom:12px'
            }));

            (payload.groups || []).forEach(group => {
                panel.appendChild(Utils.create('div', {
                    class: 'wb-group-label',
                    text: `${group.case_name} · ${group.materials.length} 份材料`
                }));

                if (!group.materials.length) {
                    panel.appendChild(Utils.create('div', { class: 'wb-dir-empty', text: '尚未上传材料' }));
                }
                group.materials.forEach(row => panel.appendChild(this._materialRow(row)));
            });

            panel.appendChild(this._uploadBox(payload));
            this._schedulePoll(payload);
        },

        _renderEntityCandidates(panel, payload, status, artifact, version) {
            const summary = payload.summary || {};
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', {
                    text: payload.boundary || '候选相似仅用于辅助复核，不代表系统已认定为同一实体。'
                })
            ]));

            const actions = Utils.create('div', { class: 'wb-entity-actions', style: 'margin-bottom:12px' });
            const runBtn = Utils.create('button', { class: 'wb-btn wb-btn-primary', text: '执行分析' });
            runBtn.addEventListener('click', () => this._executeAnalysis(runBtn));
            const collideBtn = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '仅跑碰撞' });
            collideBtn.addEventListener('click', () => this._runCollision(collideBtn));
            const clueBtn = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '仅生成线索' });
            clueBtn.addEventListener('click', () => this._generateClues(clueBtn));
            actions.appendChild(runBtn);
            actions.appendChild(collideBtn);
            actions.appendChild(clueBtn);
            panel.appendChild(actions);

            const metrics = Utils.create('div', { class: 'wb-entity-metrics' }, [
                this._metric('候选总数', summary.total || 0),
                this._metric('待复核', summary.pending || 0),
                this._metric('提及', summary.mention_count || (payload.mentions || []).length)
            ]);
            panel.appendChild(metrics);

            const mentions = payload.mentions || [];
            if (mentions.length) {
                panel.appendChild(Utils.create('div', { class: 'wb-group-label', text: `规则提及 · ${mentions.length}` }));
                mentions.slice(0, 40).forEach(mention => {
                    const rec = (mention.records || [])[0] || {};
                    const kindLabel = {
                        tail_only: '仅尾号·不进强碰撞',
                        luhn_failed: '校验失败·不进强碰撞'
                    }[mention.mask_kind] || (mention.masked ? '掩码·不进强碰撞' : '可碰撞');
                    const row = Utils.create('div', { class: 'wb-entity-record' }, [
                        Utils.create('div', { class: 'case', text: `${mention.object_type || ''} · ${kindLabel}` }),
                        Utils.create('div', { class: 'value', text: mention.display_name || '脱敏提及' }),
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
                    ? '尚未扫到可碰撞的手机号/银行卡/设备号。可先确认材料已解析，再运行确定性碰撞。'
                    : luhnFailed
                        ? '已扫到卡号写法，但校验位未通过（例如 …4160），或仅有尾号/掩码号。完整卡须在 A/B 两案都出现且 Luhn 通过（可用 6228480177334163）才会生成待复核候选与 R001 线索。'
                        : '已扫到规则提及，但没有「完整强标识同时出现在 ≥2 起案件」。掩码号、仅尾号、未通过校验的卡号不会生成待复核候选。';
                panel.appendChild(Utils.create('div', { class: 'wb-empty', text: reason }));
                return;
            }
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
                PENDING: '待复核',
                MERGE: '已确认合并',
                KEEP_SEPARATE: '保持分离',
                CORRECT: '已修正',
                DEFER: '暂缓'
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
                    ['MERGE', '确认合并'],
                    ['KEEP_SEPARATE', '保持分离'],
                    ['CORRECT', '修正'],
                    ['DEFER', '暂缓']
                ];
                const buttons = Utils.create('div', { class: 'wb-entity-actions' });
                actions.forEach(([decision, label]) => {
                    const button = Utils.create('button', {
                        class: `wb-btn${decision === 'MERGE' ? ' wb-btn-primary' : ' wb-btn-ghost'}`,
                        text: label
                    });
                    button.addEventListener('click', () => {
                        this._showEntityDecisionForm(reviewArea, candidate, decision, label, version);
                    });
                    buttons.appendChild(button);
                });
                reviewArea.appendChild(buttons);
            }

            return Utils.create('section', { class: 'wb-entity-card' }, [
                Utils.create('div', { class: 'wb-entity-card-head' }, [
                    Utils.create('div', {}, [
                        Utils.create('div', {
                            class: 'wb-entity-title',
                            text: candidate.display_name || `候选 ${index + 1}`
                        }),
                        Utils.create('div', {
                            class: 'wb-file-meta',
                            text: `${candidate.entity_type || 'OTHER'} · ${candidate.confidence_label || '待核验'}`
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
            const cancel = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '取消' });
            cancel.addEventListener('click', () => this._renderPanel());
            const submit = Utils.create('button', { class: 'wb-btn wb-btn-primary', text: '确认提交' });
            submit.addEventListener('click', () => {
                this._submitEntityDecision(candidate.candidate_id, decision, reason.value, submit, version);
            });
            container.appendChild(reason);
            container.appendChild(Utils.create('div', { class: 'wb-entity-actions' }, [cancel, submit]));
            reason.focus();
        },

        async _submitEntityDecision(candidateId, decision, reason, button, version) {
            if (!(reason || '').trim()) {
                Toast.warning('请填写复核理由');
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
                    Toast.error(data.message || '复核提交失败');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success('实体复核决定已记录，新版本已生成');
            } catch (e) {
                Toast.error('复核提交失败：' + e.message);
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
                    Toast.error(data.message || '碰撞失败');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success(`碰撞完成，候选 ${data.candidate_count || 0} 条`);
            } catch (e) {
                Toast.error('碰撞失败：' + e.message);
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
                    Toast.error(data.message || '线索生成失败');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success(`线索生成完成 ${ (data.created || []).length } 条`);
            } catch (e) {
                Toast.error('线索生成失败：' + e.message);
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
                    Toast.error(data.message || '事件抽取失败');
                    return;
                }
                this.task = data.task;
                await this.openArtifact(data.artifact.id);
                Toast.success(`事件抽取完成 ${data.event_count || 0} 条`);
            } catch (e) {
                Toast.error('事件抽取失败：' + e.message);
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
            const pane = Utils.$('#wb-cite-pane');
            const split = Utils.$('#wb-split');
            if (pane) {
                pane.hidden = false;
                pane.innerHTML = '';
                pane.appendChild(Utils.create('div', { class: 'wb-cite-loading', text: '正在核对原文…' }));
                if (split) split.classList.add('is-open');
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
            const pane = Utils.$('#wb-cite-pane');
            const split = Utils.$('#wb-split');
            if (!pane) return;
            pane.hidden = false;
            if (split) split.classList.add('is-open');
            pane.innerHTML = '';
            const close = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '关闭对照' });
            close.addEventListener('click', () => {
                pane.hidden = true;
                pane.innerHTML = '';
                if (split) split.classList.remove('is-open');
            });
            pane.appendChild(Utils.create('div', { class: 'wb-cite-head' }, [
                Utils.create('div', {}, [
                    Utils.create('div', { class: 'wb-cite-kicker', text: view.error ? '核验未通过' : '左侧生成物 · 右侧原文' }),
                    Utils.create('div', { class: 'wb-cite-title', text: view.title })
                ]),
                close
            ]));
            if (view.meta) {
                pane.appendChild(Utils.create('div', { class: 'wb-file-meta', text: view.meta }));
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
            pane.appendChild(body);
        },

        _renderClueSet(panel, payload) {
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', { text: payload.boundary || '线索停留在待核验层级。' })
            ]));
            const summary = payload.summary || {};
            panel.appendChild(Utils.create('div', { class: 'wb-entity-metrics' }, [
                this._metric('线索', summary.total || 0),
                this._metric('新生成', summary.created || 0),
                this._metric('跳过', summary.skipped || 0)
            ]));
            const items = payload.items || [];
            if (!items.length) {
                const skipped = payload.skipped || [];
                const skipText = skipped.length
                    ? `本轮跳过 ${skipped.length} 条（${skipped.slice(0, 3).map(s => s.reason).join('、')}）。`
                    : '';
                panel.appendChild(Utils.create('div', {
                    class: 'wb-empty',
                    text: `尚无跨案线索。${skipText}线索来自 R001–R005：R001–R003 需完整卡号/手机号/设备号跨 ≥2 案；R004 需同账户出现在 ≥2 案的转账事件；R005 需同手机号出现在 ≥2 案的联络事件。仅有尾号、掩码号或 Luhn 失败卡号时为 0 是正常结果。`
                }));
                return;
            }
            items.forEach(item => {
                const open = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '打开' });
                if (item.artifact_id) {
                    open.addEventListener('click', () => this.openArtifact(item.artifact_id));
                }
                panel.appendChild(Utils.create('section', { class: 'wb-entity-card' }, [
                    Utils.create('div', { class: 'wb-entity-card-head' }, [
                        Utils.create('div', { class: 'wb-entity-title', text: item.title || item.rule_id || '线索' }),
                        Utils.create('span', { class: 'wb-pill ok', text: item.rule_id || 'RULE' })
                    ]),
                    Utils.create('div', { class: 'wb-entity-card-body' }, [
                        Utils.create('div', { class: 'wb-file-meta', text: `案件 ${item.case_count || ''} · chunk ${item.chunk_count || ''}` }),
                        open
                    ])
                ]));
            });
        },

        _renderClueItem(panel, payload) {
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', { text: payload.boundary || '' })
            ]));
            panel.appendChild(Utils.create('div', { class: 'wb-summary-v', text: payload.title || '' }));
            panel.appendChild(Utils.create('div', { class: 'wb-file-meta', text: payload.summary || '' }));
            panel.appendChild(Utils.create('div', { class: 'wb-group-label', text: '涉及案件' }));
            (payload.cases || []).forEach(c => {
                panel.appendChild(Utils.create('div', { class: 'wb-file-meta', text: c.case_name || c.case_id }));
            });
            panel.appendChild(Utils.create('div', { class: 'wb-group-label', text: '证据（点击回链）' }));
            (payload.evidence || []).forEach(ev => {
                const row = Utils.create('div', { class: 'wb-entity-record' }, [
                    Utils.create('div', { class: 'case', text: ev.case_name || ev.case_id || '' }),
                    Utils.create('div', { class: 'value', text: ev.quote || '脱敏片段' }),
                    Utils.create('div', { class: 'source', text: [ev.filename, ev.page_start ? `第 ${ev.page_start} 页` : ''].filter(Boolean).join(' · ') })
                ]);
                row.style.cursor = 'pointer';
                row.addEventListener('click', () => this._openCitation(ev));
                panel.appendChild(row);
            });
            if (payload.uncertainty) {
                panel.appendChild(Utils.create('div', { class: 'wb-callout warn', text: payload.uncertainty }));
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
                panel.appendChild(Utils.create('div', { class: 'wb-file-meta', text: `${typeText} · 扫描 chunk ${summary.scanned_chunks || 0}` }));
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
                    Utils.create('div', { class: 'wb-panel-sub', text: `脱敏全文 · ${data.chunk_count} 个片段` })
                ]);
                panel.appendChild(head);

                // 脱敏文本主体
                const pre = Utils.create('pre', {
                    style: 'white-space: pre-wrap; word-break: break-word; padding: 16px; background: #f8f9fa; border-radius: 6px; font-size: 13px; line-height: 1.7; max-height: calc(100vh - 250px); overflow-y: auto; margin-top: 12px;'
                });
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
                class: 'wb-file-del',
                title: '删除材料',
                text: '×'
            });
            del.addEventListener('click', (e) => {
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
                    Toast.warning('该材料尚未生成独立产物视图');
                }
            });

            return rowEl
        },

        async _deleteMaterial(documentId) {
            if (!documentId || !this.task) return;
            if (!confirm('确定删除该材料？')) return;
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
                // 关掉已打开的该材料相关标签，再整页刷新目录与批次
                const gone = new Set(
                    (this.task.artifacts || [])
                        .filter(a => a.type === 'MATERIAL_DOC' && a.ref_key === documentId)
                        .map(a => a.id)
                );
                this.tabs = this.tabs.filter(t => !gone.has(t.id));
                if (gone.has(this.activeTabId)) this.activeTabId = null;
                await this.openTask(this.task.id, data.batch_artifact_id || null);
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

            const btn = Utils.create('button', { class: 'wb-btn wb-btn-primary', text: '上传到该案件' });
            btn.addEventListener('click', () => this._uploadMaterials(select.value, input.files, btn));

            const refresh = Utils.create('button', { class: 'wb-btn wb-btn-ghost', text: '刷新进度' });
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
                '请对本监督分析任务执行完整跨案分析：\n',
                '(1) 先 get_task_overview 了解案件与材料；\n',
                '(2) 若仍为草稿则 confirm_task_plan；\n',
                '(3) 刷新材料后 run_task_collision；\n',
                '(4) run_task_timeline 抽取转账/联络事件；\n',
                '(5) write_ai_clues 生成可回原文的跨案线索。\n',
                '每完成一步根据观察决定是否继续；最终汇总产物并提示打开核验。',
                '禁止输出定罪、并案、主从犯或量刑结论。\n'
            ].join('');
            try {
                if (!window.Agent) throw new Error('智能体未就绪');
                await Agent.process(prompt);
                Toast.success('智能体本轮分析已结束');
            } catch (e) {
                Toast.error(e.message || '执行失败');
            } finally {
                if (button) {
                    button.disabled = false;
                    button.textContent = '执行分析';
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
                title: '执行分析',
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
            await fetch(`/api/tasks/${this.task.id}/materials`);
            const batch = (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH');
            if (batch) await this.openArtifact(batch.id);
        },

        _schedulePoll(payload) {
            clearTimeout(this.pollTimer);
            const pending = (payload.groups || []).some(g =>
                (g.materials || []).some(m => ['UPLOADED', 'PARSING'].includes(m.status))
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

        /** 在智能体消息流中投放产物卡片，点击后打开的是同一个 artifact */
        _postArtifactCard(artifactId, title, desc) {
            const messages = Utils.$('#chat-messages');
            if (!messages) return;
            const btn = Utils.create('button', { class: 'wb-artifact-open', text: '打开产物' });
            btn.addEventListener('click', () => this.openArtifact(artifactId));
            const card = Utils.create('div', { class: 'wb-artifact-card' }, [
                Utils.create('div', { class: 't', text: title }),
                Utils.create('div', { class: 'd', text: desc }),
                btn
            ]);
            messages.appendChild(card);
            messages.scrollTop = messages.scrollHeight;
        }


    };

    global.Workbench = Workbench;
})(window);
