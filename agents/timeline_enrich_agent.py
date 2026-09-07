"""角色时间线：DeepSeek 角色/冲突/推测增强（失败则保留规则事实层）。"""

from __future__ import annotations

import json
import re
from typing import Any

from agents.base_agent import BaseAgent
from agents.prompts.timeline_role import TIMELINE_ROLE_SYSTEM_PROMPT
from app.config import API_KEY, DEEPSEEK_EXTERNAL_CALLS_ENABLED


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _slim_events_for_model(items: list[dict[str, Any]], *, max_items: int = 40) -> list[dict[str, Any]]:
    slim: list[dict[str, Any]] = []
    for item in items[:max_items]:
        if not isinstance(item, dict):
            continue
        if item.get("source_mode") == "inferred":
            continue
        quote = ""
        source = item.get("source") or {}
        if isinstance(source, dict):
            quote = str(source.get("quote") or "")[:120]
        slim.append(
            {
                "event_id": item.get("event_id"),
                "event_type": item.get("event_type"),
                "time_text": item.get("time_text") or "",
                "time_uncertain": bool(item.get("time_uncertain")),
                "case_name": item.get("case_name") or "",
                "subject_id": item.get("subject_id") or "",
                "subject": item.get("subject") or "",
                "subject_kind": item.get("subject_kind") or "",
                "summary_text": str(item.get("summary_text") or "")[:120],
                "role_or_action": str(item.get("role_or_action") or "")[:40],
                "quote": quote,
            }
        )
    return slim


def apply_timeline_enrichment(
    items: list[dict[str, Any]],
    parsed: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """将模型 JSON 合并进事实事件；越界推测丢弃。返回 (新 items, meta)。"""
    meta = {
        "applied": False,
        "enriched_count": 0,
        "inferred_count": 0,
        "rejected_inferred": 0,
    }
    if not parsed or not isinstance(parsed, dict):
        return list(items), meta

    by_id = {str(it.get("event_id")): dict(it) for it in items if it.get("event_id")}
    known_ids = set(by_id.keys())

    for row in parsed.get("enrichments") or []:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("event_id") or "").strip()
        if not eid or eid not in by_id:
            continue
        target = by_id[eid]
        role = str(row.get("role_or_action") or "").strip()[:40]
        if role:
            target["role_or_action"] = role
            meta["enriched_count"] += 1
        conflicts = row.get("conflict_with")
        if isinstance(conflicts, list) and conflicts:
            cleaned = [str(c).strip()[:80] for c in conflicts if str(c).strip()][:6]
            if cleaned:
                target["conflict_with"] = cleaned
                meta["enriched_count"] += 1
        # 不得把事实节点改成 inferred
        if target.get("source_mode") != "inferred":
            target["source_mode"] = target.get("source_mode") or "recorded"

    inferred_nodes: list[dict[str, Any]] = []
    for row in parsed.get("inferred_nodes") or []:
        if not isinstance(row, dict):
            continue
        based = row.get("based_on_event_ids") or []
        if not isinstance(based, list):
            meta["rejected_inferred"] += 1
            continue
        based_ids = [str(x).strip() for x in based if str(x).strip()]
        if not based_ids or any(b not in known_ids for b in based_ids):
            meta["rejected_inferred"] += 1
            continue
        role = str(row.get("role_or_action") or "").strip()
        if not role.startswith("系统推测"):
            role = f"系统推测：{role}" if role else ""
        if not role or len(role) < 5:
            meta["rejected_inferred"] += 1
            continue
        role = role[:60]
        anchor = by_id[based_ids[0]]
        time_text = str(row.get("time_text") or "").strip()
        if time_text and time_text not in {
            str(by_id[b].get("time_text") or "") for b in based_ids
        }:
            # 只能复用已有时间
            time_text = str(anchor.get("time_text") or "")
        if not time_text:
            time_text = ""
        case_name = str(row.get("case_name") or "").strip() or (anchor.get("case_name") or "")
        subject_id = str(row.get("subject_id") or "").strip() or (anchor.get("subject_id") or "")
        subject = str(row.get("subject") or "").strip() or (anchor.get("subject") or "")
        subject_kind = str(row.get("subject_kind") or "").strip() or (anchor.get("subject_kind") or "")
        if subject_kind not in {"PERSON", "ACCOUNT"}:
            subject_kind = anchor.get("subject_kind") or "PERSON"
        node_id = f"inferred:{based_ids[0]}:{len(inferred_nodes)}"
        uncertain = not time_text or time_text in {"时间不明", "未知"}
        inferred_nodes.append(
            {
                "event_id": node_id,
                "title": "系统推测",
                "event_type": "INFERRED",
                "time_text": time_text,
                "event_time": time_text,
                "time_precision": "UNKNOWN" if uncertain else (anchor.get("time_precision") or "DAY"),
                "time_uncertain": uncertain,
                "amount_text": "",
                "channel": "",
                "summary_text": role,
                "role_or_action": role,
                "parties": [],
                "subject_id": subject_id,
                "subject": subject,
                "subject_kind": subject_kind,
                "person_subject_id": anchor.get("person_subject_id") or "",
                "person_subject": anchor.get("person_subject") or "",
                "account_subject_id": anchor.get("account_subject_id") or "",
                "account_subject": anchor.get("account_subject") or "",
                "objects": [],
                "case_id": anchor.get("case_id") or "",
                "case_name": case_name,
                "cases": [case_name] if case_name else list(anchor.get("cases") or []),
                "source_mode": "inferred",
                "based_on_event_ids": based_ids,
                "conflict_with": [],
                "source": dict(anchor.get("source") or {}),
            }
        )
        meta["inferred_count"] += 1

    merged = list(by_id.values()) + inferred_nodes
    meta["applied"] = meta["enriched_count"] > 0 or meta["inferred_count"] > 0
    return merged, meta


class TimelineEnrichAgent(BaseAgent):
    """对 ROLE_TIMELINE 事实事件做角色表述/冲突/推测增强。"""

    def __init__(self, task_id: str = ""):
        super().__init__()
        self.task_id = task_id
        self.tools = []

    def enrich_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        base_meta = {
            "ok": False,
            "fallback": True,
            "items": list(items),
            "enrich_meta": {
                "applied": False,
                "enriched_count": 0,
                "inferred_count": 0,
                "rejected_inferred": 0,
            },
        }
        if not items:
            return {**base_meta, "ok": True, "fallback": False}
        if not DEEPSEEK_EXTERNAL_CALLS_ENABLED or not API_KEY:
            return {
                **base_meta,
                "error": "未启用模型或缺少密钥，保留材料记载事实层。",
            }

        slim = _slim_events_for_model(items)
        if not slim:
            return {**base_meta, "ok": True, "fallback": False}

        user_payload = {
            "events": slim,
            "instruction": "请仅输出 JSON；推测须带「系统推测：」且引用 based_on_event_ids。",
        }
        self.messages = [
            {"role": "system", "content": TIMELINE_ROLE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                temperature=0,
                max_tokens=2048,
                stream=False,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = (stream.choices[0].message.content or "").strip()
            if not content:
                reasoning = getattr(stream.choices[0].message, "reasoning_content", None) or ""
                content = str(reasoning).strip()
            parsed = _extract_json_object(content)
            if not parsed:
                return {**base_meta, "error": "模型未返回可解析 JSON，保留事实层。"}
            merged, enrich_meta = apply_timeline_enrichment(items, parsed)
            return {
                "ok": True,
                "fallback": False,
                "items": merged,
                "enrich_meta": enrich_meta,
            }
        except Exception as exc:
            return {
                **base_meta,
                "error": f"模型增强失败：{exc}",
            }


def enrich_role_timeline_items(items: list[dict[str, Any]], *, task_id: str = "") -> dict[str, Any]:
    return TimelineEnrichAgent(task_id).enrich_items(items)
