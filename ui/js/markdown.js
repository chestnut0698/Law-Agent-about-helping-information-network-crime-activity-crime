/* ========================================
   markdown.js — 轻量 Markdown 渲染
   ======================================== */
(function (global) {
    'use strict';

    /**
     * 轻量级 Markdown → HTML 转换器
     * 支持：标题、加粗、斜体、行内代码、代码块、链接、列表、引用、表格、分割线
     */
    function parse(md) {
        if (!md) return '';

        let html = Utils.escapeHtml(md);

        // 代码块（```...```）
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code class="language-$1">$2</code></pre>');

        // 表格（简单支持）
        html = html.replace(/(\|.+\|\n\|[-:| ]+\|\n(?:\|.+\|\n?)+)/g, (match) => {
            const lines = match.trim().split('\n');
            const headers = lines[0].split('|').filter(c => c.trim()).map(c => c.trim());
            const rows = lines.slice(2).map(l => l.split('|').filter(c => c.trim()).map(c => c.trim()));
            let table = '<table><thead><tr>';
            headers.forEach(h => table += `<th>${h}</th>`);
            table += '</tr></thead><tbody>';
            rows.forEach(row => {
                table += '<tr>';
                row.forEach(cell => table += `<td>${cell}</td>`);
                table += '</tr>';
            });
            table += '</tbody></table>';
            return table;
        });

        // 处理行内和段落
        const lines = html.split('\n');
        let result = '';
        let inList = false;
        let listType = '';
        let inQuote = false;
        let quoteContent = '';

        const closeList = () => {
            if (inList) {
                result += `</${listType}>`;
                inList = false;
            }
        };
        const closeQuote = () => {
            if (inQuote) {
                result += `<blockquote>${quoteContent.trim()}</blockquote>`;
                inQuote = false;
                quoteContent = '';
            }
        };

        for (let i = 0; i < lines.length; i++) {
            let line = lines[i];

            // 空行
            if (!line.trim()) {
                closeList();
                closeQuote();
                continue;
            }

            // 水平线
            if (/^---+\s*$/.test(line.trim())) {
                closeList(); closeQuote();
                result += '<hr>';
                continue;
            }

            // 标题
            const hMatch = line.match(/^(#{1,4})\s+(.+)$/);
            if (hMatch) {
                closeList(); closeQuote();
                const level = hMatch[1].length;
                result += `<h${level}>${parseInline(hMatch[2])}</h${level}>`;
                continue;
            }

            // 引用
            if (line.startsWith('> ')) {
                closeList();
                inQuote = true;
                quoteContent += parseInline(line.slice(2)) + ' ';
                continue;
            } else if (inQuote) {
                closeQuote();
            }

            // 无序列表
            const ulMatch = line.match(/^[-*]\s+(.+)$/);
            if (ulMatch) {
                closeQuote();
                if (!inList || listType !== 'ul') {
                    closeList();
                    result += '<ul>';
                    inList = true;
                    listType = 'ul';
                }
                result += `<li>${parseInline(ulMatch[1])}</li>`;
                continue;
            }

            // 有序列表
            const olMatch = line.match(/^\d+\.\s+(.+)$/);
            if (olMatch) {
                closeQuote();
                if (!inList || listType !== 'ol') {
                    closeList();
                    result += '<ol>';
                    inList = true;
                    listType = 'ol';
                }
                result += `<li>${parseInline(olMatch[1])}</li>`;
                continue;
            }

            closeList();

            // 普通段落
            result += `<p>${parseInline(line)}</p>`;
        }

        closeList();
        closeQuote();

        // 处理引用标记 [1] [2]
        result = result.replace(/\[(\d+)\]/g, '<span class="citation-chip" data-citation="$1">$1</span>');

        return result;
    }

    /** 行内解析：加粗、斜体、行内代码、链接 */
    function parseInline(text) {
        // 已经转义过了，这里只处理行内标记
        // 行内代码
        text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
        // 加粗
        text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        // 斜体
        text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        // 链接
        text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

        return text;
    }

    global.Markdown = { parse, parseInline };
})(window);
