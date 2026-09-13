/* ========================================
   markdown.js — 轻量 Markdown 渲染
   ======================================== */
(function (global) {
    'use strict';

    function pipeLine(s) {
        return String(s || '').replace(/\uFF5C/g, '|');
    }

    function isTableRow(s) {
        const t = pipeLine(s).trim();
        return t.startsWith('|') && t.indexOf('|', 1) >= 0;
    }

    function isSeparatorRow(s) {
        const t = pipeLine(s).trim();
        if (!isTableRow(t)) return false;
        let inner = t;
        if (inner.startsWith('|')) inner = inner.slice(1);
        if (inner.endsWith('|')) inner = inner.slice(0, -1);
        const cells = inner.split('|').map((c) => c.trim());
        if (!cells.length) return false;
        return cells.every((c) => /^:?-{2,}:?$/.test(c));
    }

    function splitCells(s) {
        let t = pipeLine(s).trim();
        if (t.startsWith('|')) t = t.slice(1);
        if (t.endsWith('|')) t = t.slice(0, -1);
        return t.split('|').map((c) => c.trim());
    }

    function renderTable(block) {
        if (!block || block.length < 2) return '';
        let headerLine = block[0];
        let bodyStart = 1;
        if (isSeparatorRow(block[0]) && block.length >= 3) {
            headerLine = block[1];
            bodyStart = 2;
        } else if (isSeparatorRow(block[1])) {
            bodyStart = 2;
        }
        const headers = splitCells(headerLine);
        const colCount = Math.max(headers.length, 1);
        let table = '<div class="md-table-wrap"><table><thead><tr>';
        headers.forEach((h) => {
            table += `<th>${parseInline(h)}</th>`;
        });
        table += '</tr></thead><tbody>';
        for (let r = bodyStart; r < block.length; r += 1) {
            if (isSeparatorRow(block[r])) continue;
            const cells = splitCells(block[r]);
            table += '<tr>';
            for (let c = 0; c < colCount; c += 1) {
                table += `<td>${parseInline(cells[c] || '')}</td>`;
            }
            table += '</tr>';
        }
        table += '</tbody></table></div>';
        return table;
    }

    /**
     * 轻量级 Markdown → HTML 转换器
     * 支持：标题、加粗、斜体、行内代码、代码块、链接、列表、引用、表格、分割线
     */
    function parse(md) {
        if (!md) return '';

        let html = Utils.escapeHtml(String(md).replace(/\r\n/g, '\n').replace(/\r/g, '\n'));

        // 代码块（```...```）
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code class="language-$1">$2</code></pre>');

        // 处理行内和段落
        const lines = html.split('\n');
        let result = '';
        let inList = false;
        let listType = '';
        let inQuote = false;
        let quoteContent = '';

        const isUl = (s) => /^[\s\u3000]{0,7}[-*•]\s+\S/.test(s || '');
        const isOl = (s) => /^[\s\u3000]{0,7}\d+[\.．、)]\s+\S/.test(s || '');
        const ulText = (s) => {
            const m = (s || '').match(/^[\s\u3000]{0,7}[-*•]\s+(.+)$/);
            return m ? m[1] : '';
        };
        const olText = (s) => {
            const m = (s || '').match(/^[\s\u3000]{0,7}\d+[\.．、)]\s+(.+)$/);
            return m ? m[1] : '';
        };
        const nextNonEmpty = (idx) => {
            for (let j = idx + 1; j < lines.length; j += 1) {
                if (lines[j].trim()) return lines[j];
            }
            return '';
        };

        const closeList = () => {
            if (inList) {
                result += `</${listType}>`;
                inList = false;
                listType = '';
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

            // 空行：松散列表仍属同一 <ol>/<ul>，不能拆开（否则会连续三个「1.」）
            if (!line.trim()) {
                const nxt = nextNonEmpty(i);
                if (inList && ((listType === 'ul' && isUl(nxt)) || (listType === 'ol' && isOl(nxt)))) {
                    continue;
                }
                closeList();
                closeQuote();
                continue;
            }

            if (/^<(div class="md-table-wrap"|table|pre|blockquote|ul|ol|h[1-4]|hr)\b/i.test(line.trim())) {
                closeList();
                closeQuote();
                result += line;
                continue;
            }

            // 表格：允许行间空行、可无分隔行（Excel/Word 抽取后常被空成一段一段）
            if (isTableRow(line)) {
                const block = [];
                let k = i;
                while (k < lines.length) {
                    if (!lines[k].trim()) {
                        let j = k + 1;
                        while (j < lines.length && !lines[j].trim()) j += 1;
                        if (j < lines.length && isTableRow(lines[j])) {
                            k = j;
                            continue;
                        }
                        break;
                    }
                    if (!isTableRow(lines[k])) break;
                    block.push(lines[k]);
                    k += 1;
                }
                if (block.length >= 2) {
                    closeList();
                    closeQuote();
                    result += renderTable(block);
                    i = k - 1;
                    continue;
                }
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

            // 无序列表（允许最多 7 位缩进，兼容「1.」下的子项）
            if (isUl(line)) {
                closeQuote();
                if (!inList || listType !== 'ul') {
                    closeList();
                    result += '<ul>';
                    inList = true;
                    listType = 'ul';
                }
                result += `<li>${parseInline(ulText(line))}</li>`;
                continue;
            }

            // 有序列表：源文即使都写「1.」，同一 <ol> 会显示 1、2、3
            if (isOl(line)) {
                closeQuote();
                if (!inList || listType !== 'ol') {
                    closeList();
                    result += '<ol>';
                    inList = true;
                    listType = 'ol';
                }
                result += `<li>${parseInline(olText(line))}</li>`;
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
