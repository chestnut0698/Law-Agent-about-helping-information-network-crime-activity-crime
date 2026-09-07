"""实体复核：一次模型调用给出分析建议（不代操作改表/改 decision）。"""

from __future__ import annotations

import json
import re
from typing import Any

from agents.base_agent import BaseAgent
from agents.prompts.entity_review import ENTITY_REVIEW_SYSTEM_PROMPT
from app.config import API_KEY, DEEPSEEK_EXTERNAL_CALLS_ENABLED
from app.tasks import TaskError, get_task_service
from tools.entity_review import list_entity_candidates


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
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _slim_field_compare(rows: list[Any], *, max_rows: int = 12) -> list[dict[str, Any]]:
    slim: list[dict[str, Any]] = []
    for row in rows[:max_rows]:
        if not isinstance(row, dict):
            continue
        per_case = []
        for cell in (row.get("per_case") or [])[:8]:
            if not isinstance(cell, dict):
                continue
            per_case.append(
                {
                    "case_name": cell.get("case_name") or cell.get("case_id"),
                    "value": cell.get("value"),
                    "status": cell.get("status"),
                }
            )
        slim.append(
            {
                "label": row.get("label") or row.get("field_key"),
                "per_case": per_case,
            }
        )
    return slim


def load_candidate_advice_context(task_id: str, candidate_id: str) -> dict[str, Any]:
    """直接读候选精简上下文，不走工具 JSON 6000 字截断。"""
    service = get_task_service()
    art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    if not art:
        raise TaskError("TASK_ARTIFACT_NOT_FOUND", "跨案对象待核清单不存在")
    detail = service.get_artifact(task_id, art["id"])
    found = None
    for item in (detail.get("payload") or {}).get("candidates") or []:
        if item.get("candidate_id") == candidate_id:
            found = item
            break
    if not found:
        raise TaskError("TASK_ARTIFACT_NOT_FOUND", "待核对象不存在")

    evidence = []
    for ev in (found.get("evidence") or [])[:6]:
        if not isinstance(ev, dict):
            continue
        evidence.append(
            {
                "case_name": ev.get("case_name") or ev.get("case_id"),
                "field_label": ev.get("field_label") or ev.get("field_key"),
                "quote": (ev.get("quote_display") or ev.get("quote") or "")[:80],
            }
        )
    return {
        "candidate_id": found.get("candidate_id"),
        "entity_type": found.get("entity_type"),
        "display_name": found.get("display_name"),
        "cases": found.get("cases") or [],
        "field_compare": _slim_field_compare(found.get("field_compare") or []),
        "supporting_facts": (found.get("supporting_facts") or [])[:8],
        "conflicts": (found.get("conflicts") or [])[:8],
        "missing_fields": (found.get("missing_fields") or [])[:8],
        "impact": found.get("impact") or {},
        "decision": found.get("decision") or "PENDING",
        "match_basis": (found.get("match_basis") or [])[:6],
        "evidence": evidence,
        "question": found.get("question") or "",
    }


class EntityReviewAgent(BaseAgent):
    """单候选模型分析；失败时不落库、不展示本地模板冒充建议。"""

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id
        self.tools = []

    def _deterministic_fallback(self, candidate_id: str, *, reason: str = "") -> dict[str, Any]:
        """本地兜底：不落库、不冒充模型建议。"""
        return {
            "ok": False,
            "fallback": True,
            "persisted": False,
            "error": reason or "模型未返回有效分析，请稍后重试或直接查看字段对照与原文。",
        }

    def review_one(self, candidate_id: str) -> dict[str, Any]:
        if not DEEPSEEK_EXTERNAL_CALLS_ENABLED or not API_KEY:
            return self._deterministic_fallback(
                candidate_id,
                reason="未启用 DeepSeek 或缺少 API Key，无法生成模型建议。",
            )

        try:
            cand = load_candidate_advice_context(self.task_id, candidate_id)
        except TaskError as exc:
            return {"ok": False, "error": exc.message}

        user_payload = {
            "candidate": cand,
            "instruction": "请仅输出 JSON 分析建议，不要改写字段表或替人工做决定。",
        }
        self.messages = [
            {"role": "system", "content": ENTITY_REVIEW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
        ]
        try:
            # 与字段表重建一致：强制 JSON + 关闭 thinking，否则 flash 会把答案放进 reasoning_content，content 为空
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                temperature=0,
                max_tokens=1024,
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
                return self._deterministic_fallback(
                    candidate_id,
                    reason="模型未返回可解析的 JSON 建议。",
                )

            recommendation = str(parsed.get("recommendation") or "DEFER").strip().upper()
            allowed = {"MERGE", "KEEP_SEPARATE", "CORRECT", "DEFER", "NEED_MORE_EVIDENCE"}
            if recommendation not in allowed:
                recommendation = "DEFER"
            summary = str(parsed.get("agent_summary") or "").strip()[:150]
            if not summary:
                return self._deterministic_fallback(
                    candidate_id,
                    reason="模型未给出分析摘要。",
                )

            suggestion = {
                "recommendation": recommendation,
                "agent_summary": summary,
                "supporting_facts": parsed.get("supporting_facts") or [],
                "conflicts": parsed.get("conflicts") or [],
                "missing_fields": parsed.get("missing_fields") or [],
                "confidence": parsed.get("confidence") or "MEDIUM",
                "producer": "DEEPSEEK_ENTITY_REVIEW",
                "source": "model",
                "fallback": False,
            }
            try:
                result = get_task_service().propose_entity_review(
                    self.task_id,
                    candidate_id,
                    suggestion=suggestion,
                    user_id="system",
                    advise_only=True,
                )
            except TaskError as exc:
                return {
                    "ok": False,
                    "fallback": False,
                    "error": exc.message,
                }
            return {
                "ok": True,
                "fallback": False,
                "result": {
                    "ok": True,
                    "candidate_id": result.get("candidate_id"),
                    "recommendation": result.get("recommendation"),
                    "agent_summary": result.get("agent_summary"),
                },
                "suggestion": suggestion,
                "recommendation": recommendation,
                "agent_summary": summary,
            }
        except Exception as exc:
            return self._deterministic_fallback(
                candidate_id,
                reason=f"模型调用失败：{exc}",
            )
    def review_pending(self, limit: int = 10) -> dict[str, Any]:
        listed = json.loads(list_entity_candidates(self.task_id, decision="PENDING", limit=limit))
        if not listed.get("ok"):
            return listed
        results = []
        for item in listed.get("candidates") or []:
            cid = item.get("candidate_id")
            if not cid:
                continue
            results.append(self.review_one(cid))
        return {
            "ok": True,
            "reviewed": len(results),
            "results": results,
            "task_id": self.task_id,
        }


def run_entity_review_for_task(task_id: str, candidate_id: str | None = None) -> dict[str, Any]:
    agent = EntityReviewAgent(task_id)
    if candidate_id:
        return agent.review_one(candidate_id)
    return agent.review_pending()
