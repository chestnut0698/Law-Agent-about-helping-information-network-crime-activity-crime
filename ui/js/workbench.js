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
                const resp = await fetch('/api/tasks?limit=8');
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
            if (!list) return;
            list.innerHTML = '';
            (this.tasks || []).forEach(task => {
                const chip = Utils.create('div', {
                    class: `wb-task-chip${this.task && this.task.id === task.id ? ' active' : ''}`,
                    title: `${task.title}（${task.case_count} 起案件）`,
                    text: (task.title || '任务').slice(0, 2)
                });
                chip.addEventListener('click', () => this.openTask(task.id));
                list.appendChild(chip);
            });
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
        },

        _bindShell() {
            const newTask = Utils.$('#wb-new-task');
            if (newTask) newTask.addEventListener('click', () => {
                this.task = null;
                this.draftTaskId = null;
                this._renderRail();
                this.showStart();
            });

            // 只有工作台里同时存在全局栏和任务目录时，才允许收起全局栏；任务目录常驻
            const railBtn = Utils.$('#wb-toggle-rail');
            if (railBtn) railBtn.addEventListener('click', () => State.toggleSidebar());

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

            const del = Utils.create('button', { class: 'wb-case-del', text: '×', title: '移除' });
            const row = Utils.create('div', { class: 'wb-case-row' }, [input, del]);
            del.addEventListener('click', () => {
                row.remove();
                this._checkScope();
            });
            list.appendChild(row);
            this._checkScope();
        },

        _scopeValues() {
            const cases = Utils.$$('.wb-case-row input')
                .map(i => i.value.trim())
                .filter(Boolean)
                .map(name => ({ name }));
            return {
                title: (Utils.$('#wb-title') || {}).value || '',
                purpose: (Utils.$('#wb-purpose') || {}).value || '',
                authorized_until: (Utils.$('#wb-until') || {}).value || '',
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
                const editing = Boolean(this.draftTaskId);
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

        async _confirmPlan() {
            if (!this.draftTaskId) return;
            try {
                const resp = await fetch(`/api/tasks/${this.draftTaskId}/plan/confirm`, { method: 'POST' });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '计划确认失败');
                    return;
                }
                await this.loadTasksAndOpen(this.draftTaskId, data.batch_artifact_id);
            } catch (e) {
                Toast.error('计划确认失败：' + e.message);
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

            const target = artifactId || this._defaultArtifactId();
            if (target) await this.openArtifact(target);
            if (task.status === 'SCOPE_DRAFT') {
                this.draftTaskId = task.id;
                const planResp = await fetch(`/api/tasks/${task.id}/plan`);
                this._renderAgentPlan(await planResp.json());
            }
        },

        /** 每个任务绑定自己的智能体会话：聊天历史随任务走，而不是随一次性对话 */
        async _bindConversation(task) {
            State.taskBoundConversation = true;
            State.currentConversationId = task.id;
            if (!State.conversations.find(c => c.id === task.id)) {
                State.conversations.unshift({ id: task.id, title: task.title, time: '刚刚' });
            }
            const messages = Utils.$('#chat-messages');
            if (messages) messages.innerHTML = '';
            try {
                const response = await fetch(`/conversations/${task.id}/messages`);
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
                // 新任务还没有聊天消息时保持空白，由计划卡接管首屏。
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
                text: '确认并开始'
            });
            confirm.addEventListener('click', () => this._confirmPlan());

            const card = Utils.create('div', { class: 'wb-agent-plan-card' }, [
                Utils.create('div', { class: 'wb-agent-plan-kicker', text: '分析计划已生成' }),
                Utils.create('div', { class: 'wb-agent-plan-title', text: plan.title }),
                Utils.create('div', {
                    class: 'wb-agent-plan-desc',
                    text: `${plan.cases.length} 起案件 · 授权至 ${plan.authorized_until}`
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
                panel.appendChild(Utils.create('div', { class: 'wb-empty', text: '从左侧任务目录打开一个产物' }));
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

            if (status === 'STALE') {
                panel.appendChild(Utils.create('div', { class: 'wb-callout warn' }, [
                    Utils.create('span', { text: '输入已变化，当前结果可能过时，不能用于处置或正式导出。' })
                ]));
            }

            if (artifact.type === 'MATERIAL_BATCH') this._renderMaterialBatch(panel, payload);
            else if (artifact.type === 'TASK_SCOPE') this._renderScopeArtifact(panel, payload);
            else if (artifact.type === 'ENTITY_CANDIDATE_SET') {
                this._renderEntityCandidates(panel, payload, status);
            }
            else this._renderGeneric(panel, payload);
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

        _renderEntityCandidates(panel, payload, status) {
            const summary = payload.summary || {};
            panel.appendChild(Utils.create('div', { class: 'wb-callout' }, [
                Utils.create('span', {
                    text: payload.boundary || '候选相似仅用于辅助复核，不代表系统已认定为同一实体。'
                })
            ]));

            const metrics = Utils.create('div', { class: 'wb-entity-metrics' }, [
                this._metric('候选总数', summary.total || 0),
                this._metric('待复核', summary.pending || 0),
                this._metric('已处置', summary.reviewed || 0)
            ]);
            panel.appendChild(metrics);

            const candidates = payload.candidates || [];
            if (!candidates.length) {
                panel.appendChild(Utils.create('div', {
                    class: 'wb-empty',
                    text: '当前没有需要复核的实体候选'
                }));
                return;
            }
            candidates.forEach((candidate, index) => {
                panel.appendChild(this._entityCandidateCard(candidate, index, status));
            });
        },

        _metric(label, value) {
            return Utils.create('div', { class: 'wb-entity-metric' }, [
                Utils.create('div', { class: 'v', text: String(value) }),
                Utils.create('div', { class: 'k', text: label })
            ]);
        },

        _entityCandidateCard(candidate, index, artifactStatus) {
            const records = Utils.create('div', { class: 'wb-entity-records' });
            (candidate.records || []).forEach(record => {
                const source = record.source || {};
                records.appendChild(Utils.create('div', { class: 'wb-entity-record' }, [
                    Utils.create('div', { class: 'case', text: record.case_name || record.case_id || '案件' }),
                    Utils.create('div', {
                        class: 'value',
                        text: record.value || record.normalized_value || '未记录'
                    }),
                    Utils.create('div', {
                        class: 'source',
                        text: [source.document_name, source.page_no ? `第 ${source.page_no} 页` : '']
                            .filter(Boolean).join(' · ') || '待补原文定位'
                    })
                ]));
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
                        this._showEntityDecisionForm(reviewArea, candidate, decision, label);
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

        _showEntityDecisionForm(container, candidate, decision, label) {
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
                this._submitEntityDecision(candidate.candidate_id, decision, reason.value, submit);
            });
            container.appendChild(reason);
            container.appendChild(Utils.create('div', { class: 'wb-entity-actions' }, [cancel, submit]));
            reason.focus();
        },

        async _submitEntityDecision(candidateId, decision, reason, button) {
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
                        body: JSON.stringify({ decision, reason: reason.trim() })
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

        _materialRow(row) {
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

            return Utils.create('div', { class: 'wb-file-row' }, [
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
                Utils.create('div', { class: 'wb-file-status', text: STAGE_TEXT[status] || status })
            ]);
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

            return Utils.create('div', { class: 'wb-upload-box' }, [
                Utils.create('span', { class: 'wb-file-meta', text: '材料必须归属案件：' }),
                select, input, btn, refresh
            ]);
        },

        async _uploadMaterials(caseId, files, btn) {
            if (!files || !files.length) {
                Toast.warning('请先选择材料文件');
                return;
            }
            const form = new FormData();
            form.append('case_id', caseId);
            Array.from(files).forEach(f => form.append('files', f));

            btn.disabled = true;
            btn.textContent = '上传中…';
            try {
                const resp = await fetch(`/api/tasks/${this.task.id}/materials`, {
                    method: 'POST',
                    body: form
                });
                const data = await resp.json();
                if (data.error_code) {
                    Toast.error(data.message || '上传失败');
                    return;
                }
                this.task = data.task;
                this._renderDirectory();
                const batch = (this.task.artifacts || []).find(a => a.type === 'MATERIAL_BATCH');
                if (batch) {
                    await this.openArtifact(batch.id);
                    this._postArtifactCard(batch.id, '材料接入与质量', `已接收 ${files.length} 份材料，可在此查看逐份处理进度。`);
                }
                Toast.success('材料已接入，正在处理');
            } catch (e) {
                Toast.error('上传失败：' + e.message);
            } finally {
                btn.disabled = false;
                btn.textContent = '上传到该案件';
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
