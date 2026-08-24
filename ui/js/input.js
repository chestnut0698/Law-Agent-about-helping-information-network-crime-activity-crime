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

            // 新架构：必须有当前任务才能发送消息
            const taskId = global.Workbench?.task?.id || State.currentTaskId;
            if (!taskId) {
                Toast.warning('请先打开一个监督分析任务');
                return;
            }

            // 清空输入
            this.textarea.value = '';
            this.textarea.style.height = 'auto';
            this.sendBtn.disabled = true;

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
