/* ========================================
   markdown.js — 轻量 Markdown 渲染
   ======================================== */
(function (global) {
    'use strict';

    const MARK_START = '\uE000';
    const MARK_END = '\uE001';
    const WS = /[\s\u00a0\u3000]/;

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

    function compactNeedle(s) {
        return String(s || '')
            .replace(/……+/g, '')
            .replace(/\.{3,}/g, '')
            .replace(/^#{1,6}\s+/gm, '')
            .replace(/^\s*[-*•]\s+/gm, '')
            .replace(/^\s*\d+[\.．、)]\s+/gm, '')
            .replace(/\|/g, '')
            .replace(/\*\*/g, '')
            .replace(/[\s\u00a0\u3000]+/g, '');
    }

    function emitInlineVisible(lineStart, line, emit) {
        const s = String(line || '');
        let i = 0;
        while (i < s.length) {
            const ch = s[i];
            if (ch === MARK_START || ch === MARK_END) {
                i += 1;
                continue;
            }
            if (s.startsWith('**', i)) {
                i += 2;
                continue;
            }
            if (ch === '`' || ch === '*') {
                i += 1;
                continue;
            }
            if (ch === '[') {
                const close = s.indexOf('](', i);
                const end = close >= 0 ? s.indexOf(')', close + 2) : -1;
                if (close > i && end > close) {
                    emitInlineVisible(lineStart + i + 1, s.slice(i + 1, close), emit);
                    i = end + 1;
                    continue;
                }
            }
            if (!WS.test(ch)) emit(ch, lineStart + i);
            i += 1;
        }
    }

    function emitTableRowVisible(lineStart, line, emit) {
        const raw = String(line || '');
        let i = 0;
        while (i < raw.length && WS.test(raw[i])) i += 1;
        if (raw[i] === '|' || raw[i] === '\uFF5C') i += 1;
        let cellStart = i;
        while (i <= raw.length) {
            const atEnd = i === raw.length;
            const isPipe = !atEnd && (raw[i] === '|' || raw[i] === '\uFF5C');
            if (atEnd || isPipe) {
                emitInlineVisible(lineStart + cellStart, raw.slice(cellStart, i), emit);
                if (atEnd) break;
                i += 1;
                cellStart = i;
                continue;
            }
            i += 1;
        }
    }

    function splitSourceLines(text) {
        const lines = [];
        let offset = 0;
        const rawLines = String(text || '').split('\n');
        rawLines.forEach((line, idx) => {
            lines.push({ line, start: offset });
            offset += line.length + (idx < rawLines.length - 1 ? 1 : 0);
        });
        return lines;
    }

    /** 与预览渲染同一套可见字，并记下每个字在未预览原文中的位置 */
    function buildVisibleMap(md) {
        const text = String(md || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        const compactChars = [];
        const map = [];
        const emit = (ch, src) => {
            compactChars.push(ch);
            map.push(src);
        };
        const lines = splitSourceLines(text);
        const isUl = (s) => /^[\s\u3000]{0,7}[-*•]\s+\S/.test(s || '');
        const isOl = (s) => /^[\s\u3000]{0,7}\d+[\.．、)]\s+\S/.test(s || '');

        for (let i = 0; i < lines.length; i += 1) {
            const { line, start } = lines[i];
            if (!line.trim()) continue;
            if (/^---+\s*$/.test(line.trim())) continue;

            if (isTableRow(line)) {
                const block = [];
                let k = i;
                while (k < lines.length) {
                    if (!lines[k].line.trim()) {
                        let j = k + 1;
                        while (j < lines.length && !lines[j].line.trim()) j += 1;
                        if (j < lines.length && isTableRow(lines[j].line)) {
                            k = j;
                            continue;
                        }
                        break;
                    }
                    if (!isTableRow(lines[k].line)) break;
                    block.push(lines[k]);
                    k += 1;
                }
                if (block.length >= 2) {
                    block.forEach((row) => {
                        if (isSeparatorRow(row.line)) return;
                        emitTableRowVisible(row.start, row.line, emit);
                    });
                    i = k - 1;
                    continue;
                }
            }

            const heading = line.match(/^(#{1,4})\s+(.+)$/);
            if (heading) {
                emitInlineVisible(start + heading[0].length - heading[2].length, heading[2], emit);
                continue;
            }
            if (line.startsWith('> ')) {
                emitInlineVisible(start + 2, line.slice(2), emit);
                continue;
            }
            if (isUl(line)) {
                const m = line.match(/^([\s\u3000]{0,7}[-*•]\s+)(.+)$/);
                if (m) emitInlineVisible(start + m[1].length, m[2], emit);
                continue;
            }
            if (isOl(line)) {
                const m = line.match(/^([\s\u3000]{0,7}\d+[\.．、)]\s+)(.+)$/);
                if (m) emitInlineVisible(start + m[1].length, m[2], emit);
                continue;
            }
            emitInlineVisible(start, line, emit);
        }
        return { text, compact: compactChars.join(''), map };
    }

    function locateNeedle(compact, needles) {
        const unique = [];
        const seen = new Set();
        (needles || []).forEach((raw) => {
            const n = compactNeedle(raw);
            if (n.length < 2 || seen.has(n)) return;
            seen.add(n);
            unique.push(n);
        });
        unique.sort((a, b) => b.length - a.length);
        const find = (needle, requireUnique) => {
            if (!needle) return null;
            const at = compact.indexOf(needle);
            if (at < 0) return null;
            if (requireUnique && compact.indexOf(needle, at + 1) >= 0) return null;
            return { at, length: needle.length };
        };
        for (let i = 0; i < unique.length; i += 1) {
            if (unique[i].length < 16) continue;
            const hit = find(unique[i], false);
            if (hit) return hit;
        }
        for (let i = 0; i < unique.length; i += 1) {
            const hit = find(unique[i], true);
            if (hit) return hit;
        }
        const primary = unique[0];
        if (primary && primary.length >= 8) {
            for (let len = primary.length - 1; len >= 8; len -= 1) {
                const hit = find(primary.slice(0, len), true);
                if (hit) return hit;
            }
            for (let len = primary.length - 1; len >= 8; len -= 1) {
                const hit = find(primary.slice(primary.length - len), true);
                if (hit) return hit;
            }
        }
        for (let i = 0; i < unique.length; i += 1) {
            const hit = find(unique[i], false);
            if (hit && unique[i].length >= 8) return hit;
        }
        return null;
    }

    function injectHighlightMarks(md, needles) {
        const { text, compact, map } = buildVisibleMap(md);
        const hit = locateNeedle(compact, needles);
        if (!hit || !map.length) return text;
        const from = map[hit.at];
        const last = map[hit.at + hit.length - 1];
        if (from == null || last == null) return text;
        const to = last + 1;
        if (from >= to) return text;
        return text.slice(0, from) + MARK_START + text.slice(from, to) + MARK_END + text.slice(to);
    }

    function wrapMarkedSlice(node, from, to) {
        if (!node || !node.parentNode) return;
        const text = node.nodeValue || '';
        const safeFrom = Math.max(0, Math.min(from, text.length));
        const safeTo = Math.max(safeFrom, Math.min(to, text.length));
        const strip = (s) => String(s || '').replace(/\uE000|\uE001/g, '');
        const before = strip(text.slice(0, safeFrom));
        const match = strip(text.slice(safeFrom, safeTo));
        const after = strip(text.slice(safeTo));
        const frag = document.createDocumentFragment();
        if (before) frag.appendChild(document.createTextNode(before));
        if (match) {
            const mark = document.createElement('mark');
            mark.className = 'wb-cite-mark';
            const strong = document.createElement('strong');
            strong.textContent = match;
            mark.appendChild(strong);
            frag.appendChild(mark);
        }
        if (after) frag.appendChild(document.createTextNode(after));
        node.parentNode.replaceChild(frag, node);
    }

    function applyHighlights(container) {
        if (!container) return;
        const skip = { SCRIPT: 1, STYLE: 1 };
        const nodes = [];
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
            acceptNode(node) {
                const tag = node.parentElement && node.parentElement.tagName;
                if (tag && skip[tag]) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        while (walker.nextNode()) nodes.push(walker.currentNode);
        let start = null;
        let end = null;
        nodes.forEach((node) => {
            const t = node.nodeValue || '';
            const a = t.indexOf(MARK_START);
            const b = t.indexOf(MARK_END);
            if (a >= 0) start = { node, offset: a };
            if (b >= 0) end = { node, offset: b };
        });
        if (!start || !end) {
            nodes.forEach((node) => {
                const t = node.nodeValue || '';
                if (t.indexOf(MARK_START) >= 0 || t.indexOf(MARK_END) >= 0) {
                    node.nodeValue = t.replace(/\uE000|\uE001/g, '');
                }
            });
            return;
        }
        if (start.node === end.node) {
            wrapMarkedSlice(start.node, start.offset + 1, end.offset);
            return;
        }
        const startIdx = nodes.indexOf(start.node);
        const endIdx = nodes.indexOf(end.node);
        wrapMarkedSlice(end.node, 0, end.offset);
        for (let i = endIdx - 1; i > startIdx; i -= 1) {
            const node = nodes[i];
            if (!node || !node.parentNode) continue;
            wrapMarkedSlice(node, 0, (node.nodeValue || '').length);
        }
        wrapMarkedSlice(start.node, start.offset + 1, (start.node.nodeValue || '').length);
    }

    /**
     * 轻量级 Markdown → HTML 转换器
     * 支持：标题、加粗、斜体、行内代码、代码块、链接、列表、引用、表格、分割线
     * options.highlightNeedles：在未预览原文上定位后再渲染，避免预览错位
     */
    function parse(md, options) {
        if (!md) return '';

        let source = String(md).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        const needles = options && options.highlightNeedles;
        if (needles && needles.length) {
            source = injectHighlightMarks(source, needles);
        }

        let html = Utils.escapeHtml(source);

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

    global.Markdown = { parse, parseInline, applyHighlights, compactNeedle };
})(window);
