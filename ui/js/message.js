/* ========================================
   message.js — 消息渲染与操作
   ======================================== */
(function (global) {
    'use strict';

    const Message = {
        /**
         * 渲染用户消息
         */
        renderUser(text) {
            const wrap = Utils.create('div', { class: 'message' }, [
                Utils.create('div', { class: 'message-role' }, [
                    Utils.create('div', { class: 'message-avatar user', text: 'U' }),
                    Utils.create('span', { class: 'message-name', text: '你' })
                ]),
                Utils.create('div', { class: 'message-content' }, [
                    Utils.create('p', { text: text })
                ])
            ]);
            return wrap;
        },

        /**
         * 渲染助手消息容器（用于后续流式填充）
         */
        renderAssistantContainer() {
            const content = Utils.create('div', { class: 'message-content md-content' });
            const actions = Utils.create('div', { class: 'message-actions' }, [
                Utils.create('button', { class: 'action-btn', title: '复制', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>' }),
                Utils.create('button', { class: 'action-btn', title: '赞', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 9V5a3 3 0 00-3-3l-4 9v11h11.28a2 2 0 002-1.7l1.38-9a2 2 0 00-2-2.3zM7 22H4a2 2 0 01-2-2v-7a2 2 0 012-2h3"/></svg>' }),
                Utils.create('button', { class: 'action-btn', title: '踩', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 15v4a3 3 0 003 3l4-9V2H5.72a2 2 0 00-2 1.7l-1.38 9a2 2 0 002 2.3zm7-13h3a2 2 0 012 2v7a2 2 0 01-2 2h-3"/></svg>' }),
                Utils.create('button', { class: 'action-btn', title: '重新生成', html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>' })
            ]);

            const wrap = Utils.create('div', { class: 'message' }, [
                Utils.create('div', { class: 'message-role' }, [
                    Utils.create('div', { class: 'message-avatar assistant', text: 'A' }),
                    Utils.create('span', { class: 'message-name', text: 'Agent' })
                ]),
                content,
                actions
            ]);

            // 绑定操作事件
            const copyBtn = Utils.$$('.action-btn', actions)[0];
            if (copyBtn) {
                copyBtn.addEventListener('click', () => {
                    const text = content.textContent;
                    Utils.copyText(text).then(() => Toast.success('已复制到剪贴板'));
                });
            }

            return { wrap, content };
        },
        appendDelta(container, delta) {
            // 如果还没有累积缓冲区，创建
            if (!container._buffer) container._buffer = '';
            container._buffer += delta;
            // 用 Markdown 重新渲染全部内容
            const html = Markdown.parse(container._buffer);
            container.innerHTML = html;
            // 保持光标（如果有）
            // 滚动到底部
            const chatContainer = Utils.$('#chat-container');
            if (chatContainer) chatContainer.scrollTop = chatContainer.scrollHeight;
        },

        /**
         * 流式写入文本到消息内容区域
         * @param {HTMLElement} container 消息内容容器
         * @param {string} fullText 完整文本
         * @param {number} speed 每个字符的延迟(ms)
         * @returns {Promise}
         */
        async streamText(container, fullText, speed = 15) {
            // 先放一个光标
            const cursor = Utils.create('span', { class: 'cursor-blink' });
            container.appendChild(cursor);

            // 逐字符追加（按 token 节奏）
            const tokens = this._tokenize(fullText);
            let accumulated = '';

            for (const token of tokens) {
                accumulated += token;
                // 用 markdown 渲染当前累积文本 + 光标
                const html = Markdown.parse(accumulated);
                container.innerHTML = html;
                container.appendChild(cursor);
                // 滚动到底部
                const chatContainer = Utils.$('#chat-container');
                if (chatContainer) chatContainer.scrollTop = chatContainer.scrollHeight;
                await Utils.sleep(speed + Math.random() * 20);
            }

            // 完成后移除光标，做最终渲染
            cursor.remove();
            container.innerHTML = Markdown.parse(fullText);

            // 绑定引用点击
            this._bindCitations(container);

            return container;
        },

        /**
         * 渲染引用列表
         */
        renderCitations(citations) {
            if (!citations || !citations.length) return null;
            const list = Utils.create('div', { class: 'citation-list' });
            citations.forEach((c, i) => {
                list.appendChild(Utils.create('div', { class: 'citation-item' }, [
                    Utils.create('div', { class: 'citation-num', text: String(i + 1) }),
                    Utils.create('div', { class: 'citation-text' }, [
                        Utils.create('a', { href: c.url || '#', target: '_blank', rel: 'noopener', text: c.title || c.url || '来源' })
                    ])
                ]));
            });
            return list;
        },

        /** 简单分词（按标点和空格） */
        _tokenize(text) {
            // 按字符分组，但保留标点后的停顿感
            const tokens = [];
            const chars = text.split('');
            let buf = '';
            for (const c of chars) {
                buf += c;
                if (/[，。！？；：、\n,.!?;:]/.test(c) || buf.length >= 4) {
                    tokens.push(buf);
                    buf = '';
                }
            }
            if (buf) tokens.push(buf);
            return tokens;
        },

        _bindCitations(container) {
            Utils.$$('.citation-chip', container).forEach(chip => {
                chip.addEventListener('click', () => {
                    const num = chip.getAttribute('data-citation');
                    Toast.info(`查看引用 [${num}]`);
                });
            });
        }
    };

    global.Message = Message;
})(window);
