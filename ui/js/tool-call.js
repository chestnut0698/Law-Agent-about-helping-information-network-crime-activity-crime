/* ========================================
   tool-call.js — 本步动作的中文说明（不展示参数、JSON、技术明细）
   ======================================== */
(function (global) {
    'use strict';

    const TOOL_LABELS = {
        get_task_overview: '查看任务与材料情况',
        confirm_task_plan: '确认分析计划',
        refresh_task_materials: '刷新材料接入情况',
        delete_task_material: '删除材料',
        run_task_collision: '跨案标识比对',
        put_task_entity_candidate: '写入待核对象',
        compare_material_images: '对照证件页面',
        run_task_timeline: '整理事件时间线',
        list_association_hints: '查看关联提示',
        write_ai_clues: '形成疑似关联线索',
        list_task_clues: '查看待核线索',
        put_task_clue: '写入待核线索',
        delete_task_clue: '删除待核线索',
        read_artifact: '查阅分析成果',
        read_material_chunk: '回原文查阅材料片段',
        list_case_materials: '列出案件材料',
        read_report: '读取核验单素材',
        write_report: '撰写跨案关联线索核验单',
        search_lawlibrary: '检索法规库',
        search_policy: '检索规范性文件'
    };

    const TOOL_HINTS = {
        get_task_overview: '先核对监督任务的范围、授权案件和材料是否齐备。',
        confirm_task_plan: '确认分析计划后，再开展跨案比对与核验。',
        refresh_task_materials: '刷新材料接入情况，确认卷宗是否已可分析。',
        delete_task_material: '按要求从本任务中移除指定材料。',
        run_task_collision: '按卡号、手机号等稳定标识做跨案比对，列出需要人工确认是否同一的对象。',
        put_task_entity_candidate: '把材料上需要判断是否同一对象的疑似项写入实体复核，供人工确认。',
        compare_material_images: '对照证件页面的可见外观，只作提示，不代替是否同一人的判断。',
        run_task_timeline: '按材料记载整理资金往来和联络先后，便于对照核验。',
        list_association_hints: '查看材料中可能相关、但尚不能直接认定为同一的提示。',
        write_ai_clues: '把可回原文核验的疑似关联写成待核线索。',
        list_task_clues: '查看当前待核线索，避免重复或遗漏。',
        put_task_clue: '写入一条可回原文核验的待核线索，供人工判断。',
        delete_task_clue: '删去一条尚未处置的待核线索。',
        read_artifact: '查阅已形成的分析成果，核对其是否可回原文。',
        read_material_chunk: '回到材料原文核对记载内容。',
        list_case_materials: '列出本案已接入的卷宗材料。',
        read_report: '读取撰写核验单所需的范围、实体结论和线索来源。',
        write_report: '撰写《跨案关联线索核验单》正文，供核对与导出。',
        search_lawlibrary: '检索相关法规，供对照理解，不作定罪依据。',
        search_policy: '检索相关规范性文件，供对照理解。'
    };

    const ToolCall = {
        TOOL_LABELS,

        displayName(name) {
            if (!name) return '核验动作';
            return TOOL_LABELS[name] || '查阅与分析';
        },

        hintFor(name) {
            return TOOL_HINTS[name] || '按当前监督任务继续核验。';
        },

        summarizeResult(raw) {
            if (raw == null || raw === '') return '已完成';
            let data = raw;
            if (typeof raw === 'string') {
                try { data = JSON.parse(raw); } catch {
                    return this._plainChinese(raw) || '本步已完成，请在中间工作区核验';
                }
            }
            if (typeof data !== 'object' || !data) {
                return this._plainChinese(String(raw)) || '本步已完成，请在中间工作区核验';
            }
            if (data.message) {
                const cleaned = this._plainChinese(String(data.message));
                if (cleaned) return cleaned;
            }
            if (data.artifact_type === 'ENTITY_CANDIDATE_SET' || (data.title && String(data.title).includes('实体'))) {
                return '已生成跨案对象待核清单，请到实体复核页确认';
            }
            if (data.artifact_type === 'ROLE_TIMELINE') {
                return data.event_count != null
                    ? `事件时间线已整理（${data.event_count} 条）`
                    : '事件时间线已整理';
            }
            if (data.artifact_type === 'CLUE_SET' || data.clue_count != null) {
                return data.clue_count != null
                    ? `已写入疑似关联线索（${data.clue_count} 条）`
                    : '已写入疑似关联线索';
            }
            if (data.artifact_type === 'MATERIAL_BATCH') return '材料接入情况已更新';
            if (data.title) {
                const title = this._plainChinese(String(data.title));
                if (title) return title;
            }
            return '本步已完成，请在中间工作区核验';
        },

        _plainChinese(text) {
            if (global.Thinking && typeof Thinking.legalize === 'function') {
                return Thinking.legalize(text);
            }
            const t = String(text || '').trim();
            return /[\u4e00-\u9fff]/.test(t) ? t : '';
        },

        create(data) {
            const line = Utils.create('div', { class: 'tool-call-line' });
            const label = data.label || this.displayName(data.name);
            line._label = label;
            line._current = Number(data.current || 1);
            line._total = Number(data.total || 0);
            line.appendChild(Utils.create('div', { class: 'tool-line-text', text: '' }));
            this.render(line, data.status || 'running');
            return line;
        },

        render(line, status) {
            if (!line) return;
            const text = Utils.$('.tool-line-text', line);
            if (!text) return;
            const current = Math.max(1, Number(line._current || 1));
            const total = Number(line._total || 0);
            let s = line._label || '本步';
            if (total > 1) s += ` · ${Math.min(current, total)}/${total}`;
            else if (current > 1) s += ` · ${current}`;
            else if (status === 'running') s += ' · 进行中…';
            else if (status === 'error') s += ' · 未能完成';
            text.textContent = s;
        },

        setCount(line, current, total, status) {
            if (!line) return;
            if (current != null) line._current = Number(current);
            if (total != null && Number(total) > 0) line._total = Number(total);
            this.render(line, status || 'running');
        },

        updateStatus(line, status) {
            this.render(line, status);
        }
    };

    global.ToolCall = ToolCall;
})(window);
