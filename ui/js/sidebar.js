/* ========================================
   sidebar.js — 侧边栏交互
   ======================================== */
(function (global) {
    'use strict';

    const Sidebar = {
        init() {
            this._bindModelList();
            this._bindToggle();
            this._bindModelSelector();
            this._bindModelModal();
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
                } else if (sidebar) {
                    sidebar.classList.toggle('collapsed', collapsed);
                    State.sidebarCollapsed = collapsed;
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
            // 空会话提示：只读提问即时执行，改动业务状态需确认
            container.innerHTML = `
                <div class="welcome-screen" id="welcome-screen">
                    <p class="welcome-subtitle">可以询问当前分析成果、材料处理情况，或提出下一步要求。</p>
                    <div class="welcome-prompts">
                        <button class="prompt-card" data-prompt="解释当前材料处理进度">
                            <span>解释当前处理进度</span>
                        </button>
                        <button class="prompt-card" data-prompt="列出需要人工处理的材料及原因">
                            <span>列出需人工处理的材料</span>
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
