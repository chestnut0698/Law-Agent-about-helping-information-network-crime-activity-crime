/* ========================================
   sidebar.js — 侧边栏交互
   ======================================== */
(function (global) {
    'use strict';

    const Sidebar = {
        init() {
            this._loadConversationList();
            this._bindNewChat();
            this._bindConversationList();
            this._bindModelList();
            this._bindToggle();
            this._bindModelSelector();
            this._bindModelModal();
        },
        async _loadConversationList() {
            try {
                const resp = await fetch('/conversations');
                const data = await resp.json();
                State.conversations = data.conversations || [];
                if (State.conversations.length === 0) {
                    await this._createNewConversation(true); // silent=true
                } else {
                    State.currentConversationId = State.conversations[0].id;
                    this._renderConversationList();
                    // 自动加载第一个对话的消息
                    await this._loadMessages(State.currentConversationId);
                }
            } catch (e) {
                console.warn('Failed to load conversations:', e);
            }
        },
        async _loadMessages(convId) {
            const chatMessages = Utils.$('#chat-messages');
            chatMessages.innerHTML = '';
            try {
                const resp = await fetch(`/conversations/${convId}/messages`);
                const data = await resp.json();
                const msgs = data.messages || [];
                if (msgs.length === 0) {
                    this._clearChatArea();
                } else {
                    msgs.forEach(msg => {
                        if (msg.role === 'user') {
                            chatMessages.appendChild(Message.renderUser(msg.content));
                        } else if (msg.role === 'assistant') {
                            if (!msg.content || msg.content.trim() === '') return;
                            const { wrap, content } = Message.renderAssistantContainer();
                            content.innerHTML = Markdown.parse(msg.content);
                            chatMessages.appendChild(wrap);
                        }
                    });
                    chatMessages.scrollTop = chatMessages.scrollHeight;
                }
            } catch (e) {
                console.warn('Failed to load messages:', e);
                this._clearChatArea();
            }
        },

        _bindNewChat() {
            const btn = Utils.$('#btn-new-chat');
            if (btn) {
                btn.addEventListener('click', () => this._createNewConversation());
            }
            // Cmd+K 快捷键
            document.addEventListener('keydown', (e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                    e.preventDefault();
                    this._createNewConversation();
                }
            });
        },

        async _createNewConversation() {
            try {
                const resp = await fetch('/conversations', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ title: '新对话' })
                });
                const data = await resp.json();
                const conv = { id: data.id, title: data.title, time: '刚刚' };
                State.conversations.unshift(conv);
                State.currentConversationId = data.id;
                this._renderConversationList();
                this._clearChatArea();
                Toast.info('已创建新对话');
            } catch (e) {
                console.error('Failed to create conversation:', e);
            }
        },

        _renderConversationList() {
            const list = Utils.$('#conversation-list');
            if (!list) return;

            list.innerHTML = '';
            State.conversations.forEach(conv => {
                const item = Utils.create('div', {
                    class: `conversation-item${conv.id === State.currentConversationId ? ' active' : ''}`,
                    'data-id': conv.id
                }, [
                    Utils.create('svg', { class: 'conv-icon', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>' }),
                    Utils.create('span', { class: 'conv-title', text: conv.title }),
                    Utils.create('span', { class: 'conv-time', text: conv.time }),
                    // ★ 删除按钮放在 item 内部
                    Utils.create('span', { class: 'conv-delete', html: '×', title: '删除对话' })
                ]);

                // ★ 删除按钮事件绑定
                const delBtn = Utils.$('.conv-delete', item);
                if (delBtn) {
                    delBtn.addEventListener('click', async (e) => {
                        e.stopPropagation();
                        if (confirm(`删除「${conv.title}」？`)) {
                            await fetch(`/conversations/${conv.id}`, { method: 'DELETE' });
                            State.conversations = State.conversations.filter(c => c.id !== conv.id);
                            if (State.conversations.length === 0) {
                                await this._createNewConversation(true);
                            } else {
                                State.currentConversationId = State.conversations[0].id;
                                this._renderConversationList();
                                await this._loadMessages(State.currentConversationId);
                            }
                        }
                    });
                }

                // 点击切换对话
                item.addEventListener('click', async (e) => {
                    if (e.target.classList.contains('conv-delete')) return;
                    State.currentConversationId = conv.id;
                    this._renderConversationList();
                    await this._loadMessages(conv.id);
                });

                list.appendChild(item);
            });
        },

        _bindConversationList() {
            // 初始渲染
            this._renderConversationList();
        },

        _bindModelList() {
            Utils.$$('.model-item').forEach(item => {
                item.addEventListener('click', () => {
                    const model = item.getAttribute('data-model');
                    State.setModel(model);
                    // 更新选中状态
                    Utils.$$('.model-item').forEach(i => i.classList.remove('active'));
                    item.classList.add('active');
                    // 更新顶部显示
                    const currentModel = Utils.$('#current-model');
                    if (currentModel) currentModel.textContent = State.modelNames[model];
                    Toast.success(`已切换到 ${State.modelNames[model]}`);
                });
            });
        },

        _bindToggle() {
            const btn = Utils.$('#btn-toggle-sidebar');
            if (btn) {
                btn.addEventListener('click', () => {
                    State.toggleSidebar();
                });
            }

            // 响应侧边栏状态变化
            Events.on('sidebar:toggle', (collapsed) => {
                const sidebar = Utils.$('#sidebar');
                const main = Utils.$('.main-content');
                if (Utils.isMobile()) {
                    sidebar.classList.toggle('mobile-open', !collapsed);
                } else {
                    sidebar.classList.toggle('collapsed', collapsed);
                    if (main) main.style.marginLeft = collapsed ? '0' : '';
                }
            });

            // Cmd+B 快捷键
            document.addEventListener('keydown', (e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === 'b') {
                    e.preventDefault();
                    State.toggleSidebar();
                }
            });
        },

        _bindModelSelector() {
            const selector = Utils.$('#model-selector');
            const modal = Utils.$('#model-modal');
            if (selector && modal) {
                selector.addEventListener('click', () => {
                    modal.style.display = 'flex';
                });
            }
        },

        _bindModelModal() {
            const modal = Utils.$('#model-modal');
            const closeBtn = Utils.$('#btn-close-modal');
            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    modal.style.display = 'none';
                });
            }
            if (modal) {
                modal.addEventListener('click', (e) => {
                    if (e.target === modal) modal.style.display = 'none';
                });
            }

            Utils.$$('.model-option').forEach(opt => {
                opt.addEventListener('click', () => {
                    const model = opt.getAttribute('data-model');
                    State.setModel(model);
                    // 更新选中标记
                    Utils.$$('.model-option .check-icon').forEach(ic => ic.remove());
                    opt.appendChild(Utils.create('svg', { class: 'check-icon', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M20 6L9 17l-5-5"/>' }));
                    // 更新顶部显示
                    const currentModel = Utils.$('#current-model');
                    if (currentModel) currentModel.textContent = State.modelNames[model];
                    // 更新侧边栏
                    Utils.$$('.model-item').forEach(i => {
                        i.classList.toggle('active', i.getAttribute('data-model') === model);
                    });
                    modal.style.display = 'none';
                    Toast.success(`已切换到 ${State.modelNames[model]}`);
                });
            });

            // 初始选中标记
            const activeOpt = Utils.$(`.model-option[data-model="${State.currentModel}"]`);
            if (activeOpt && !Utils.$('.check-icon', activeOpt)) {
                activeOpt.appendChild(Utils.create('svg', { class: 'check-icon', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', html: '<path d="M20 6L9 17l-5-5"/>' }));
            }
        },

        _clearChatArea() {
            const container = Utils.$('#chat-messages');
            if (!container) return;
            // 保留欢迎页
            container.innerHTML = `
                <div class="welcome-screen" id="welcome-screen">
                    <div class="welcome-logo">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                            <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                            <path d="M2 17l10 5 10-5"/>
                            <path d="M2 12l10 5 10-5"/>
                        </svg>
                    </div>
                    <h1 class="welcome-title">你好，我是 Agent</h1>
                    <p class="welcome-subtitle">我可以思考、规划、调用工具来帮你完成复杂任务</p>
                    <div class="welcome-prompts">
                        <button class="prompt-card" data-prompt="帮我搜索2026年AI Agent领域的最新技术趋势">
                            <span class="prompt-icon">🔍</span>
                            <span>搜索AI Agent最新趋势</span>
                        </button>
                        <button class="prompt-card" data-prompt="帮我写一个Python脚本，爬取天气预报数据并生成可视化图表">
                            <span class="prompt-icon">📊</span>
                            <span>写代码+数据可视化</span>
                        </button>
                        <button class="prompt-card" data-prompt="帮我规划一次从北京到东京的5天商务旅行">
                            <span class="prompt-icon">✈️</span>
                            <span>规划商务旅行</span>
                        </button>
                        <button class="prompt-card" data-prompt="分析这段代码的时间复杂度并给出优化建议">
                            <span class="prompt-icon">🧮</span>
                            <span>代码分析优化</span>
                        </button>
                    </div>
                </div>`;
            this._bindPromptCards();
        },

        _bindPromptCards() {
            Utils.$$('.prompt-card').forEach(card => {
                card.addEventListener('click', () => {
                    const prompt = card.getAttribute('data-prompt');
                    const textarea = Utils.$('#input-textarea');
                    if (textarea) {
                        textarea.value = prompt;
                        textarea.focus();
                        // 触发输入事件以调整高度
                        textarea.dispatchEvent(new Event('input'));
                    }
                });
            });
        }
    };

    global.Sidebar = Sidebar;
})(window);
