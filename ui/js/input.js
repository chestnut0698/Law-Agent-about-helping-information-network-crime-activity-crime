/* ========================================
   input.js — 输入区域交互
   ======================================== */
(function (global) {
    'use strict';

    const Input = {
        init() {
            this.textarea = Utils.$('#input-textarea');
            this.sendBtn = Utils.$('#btn-send');

            if (!this.textarea || !this.sendBtn) return;

            this._bindInput();
            this._bindSend();
            this._bindPromptCards();
            this._bindShortcuts();
        },

        _bindInput() {
            // 自动增高
            this.textarea.addEventListener('input', () => {
                this.textarea.style.height = 'auto';
                this.textarea.style.height = Math.min(this.textarea.scrollHeight, 200) + 'px';
                this.sendBtn.disabled = this.textarea.value.trim().length === 0;
            });
        },

        _bindSend() {
            this.sendBtn.addEventListener('click', () => this._submit());
        },

        _bindPromptCards() {
            // 使用事件委托，因为欢迎页可能被重建
            document.addEventListener('click', (e) => {
                const card = e.target.closest('.prompt-card');
                if (card) {
                    const prompt = card.getAttribute('data-prompt');
                    if (this.textarea) {
                        this.textarea.value = prompt;
                        this.textarea.dispatchEvent(new Event('input'));
                        this.textarea.focus();
                    }
                }
            });
        },

        _bindShortcuts() {
            this.textarea.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this._submit();
                }
                // Esc 失焦
                if (e.key === 'Escape') {
                    this.textarea.blur();
                }
            });
        },

        async _submit() {
            const text = this.textarea.value.trim();
            if (!text || State.agentState === 'thinking' || State.agentState === 'working') return;

            // 先上传待发送的文件
            const uploadedFiles = await FileUpload.uploadPending();


            if (!State.currentConversationId || !State.conversations.find(c => c.id === State.currentConversationId)) {
                // 静默创建一个新对话（不弹出 toast）
                await Sidebar._createNewConversation(true);
                // 注意：_createNewConversation 是 async 的，会更新 State
            }
            // 清空输入
            this.textarea.value = '';
            this.textarea.style.height = 'auto';
            this.sendBtn.disabled = true;

            // 更新会话标题（取前 20 字）
            const conv = State.conversations.find(c => c.id === State.currentConversationId);
            if (conv && conv.title === '新对话') {
                const newTitle = text.length > 20 ? text.slice(0, 20) + '...' : text;
                try {
                    await fetch(`/conversations/${conv.id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title: newTitle })
                    });
                    conv.title = newTitle;
                    conv.time = '刚刚';
                    Sidebar._renderConversationList();  // 刷新侧边栏
                } catch (e) {
                    console.warn('Failed to rename conversation:', e);
                }
            }

            // 调用 Agent 处理
            try {
                await Agent.process(text);
            } catch (err) {
                console.error('Agent error:', err);
                Toast.error('处理出错：' + err.message);
                State.setAgentState('idle');
                this._hideStatus();
            }
        },

        _hideStatus() {
            const bar = Utils.$('#status-bar');
            if (bar) bar.style.display = 'none';
        }
    };

    global.Input = Input;
})(window);
