"""DeepSeek 实体复核 Agent：单候选独立分析，输出结构化建议。"""

from __future__ import annotations

import json
from typing import Any

from agents.base_agent import BaseAgent
from agents.prompts.entity_review import ENTITY_REVIEW_SYSTEM_PROMPT
from app.config import API_KEY, DEEPSEEK_EXTERNAL_CALLS_ENABLED
from app.tasks import get_task_service
from tools.entity_review import (
    build_candidate_field_table,
    compare_candidate_fields,
    get_entity_candidate_context,
    list_candidate_relations,
    list_entity_candidates,
    propose_entity_review,
    search_candidate_evidence,
    validate_candidate_evidence,
)

ENTITY_REVIEW_TOOLS = {
    "list_entity_candidates": list_entity_candidates,
    "get_entity_candidate_context": get_entity_candidate_context,
    "compare_candidate_fields": compare_candidate_fields,
    "build_candidate_field_table": build_candidate_field_table,
    "search_candidate_evidence": search_candidate_evidence,
    "list_candidate_relations": list_candidate_relations,
    "validate_candidate_evidence": validate_candidate_evidence,
    "propose_entity_review": propose_entity_review,
}

MAX_TOOL_ROUNDS = 8


class EntityReviewAgent(BaseAgent):
    """单候选复核 Agent。DeepSeek 不可用时回退确定性摘要。"""

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id
        # 仅暴露实体复核工具
        from pathlib import Path

        schema_path = Path(__file__).resolve().parent.parent / "tools" / "tools_schema.json"
        with open(schema_path, "r", encoding="utf-8") as f:
            all_tools = json.load(f)
        allowed = set(ENTITY_REVIEW_TOOLS)
        self.tools = [
            t
            for t in all_tools
            if t.get("type") == "function" and t.get("function", {}).get("name") in allowed
        ]

    def execute_tool(self, data: dict[str, Any]) -> str:
        name = data["function_name"]
        fn = ENTITY_REVIEW_TOOLS.get(name)
        if not fn:
            return json.dumps({"ok": False, "error": f"不允许的工具：{name}"}, ensure_ascii=False)
        try:
            args = json.loads(data.get("arguments") or "{}")
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "参数不是合法 JSON"}, ensure_ascii=False)
        if "task_id" not in args:
            args["task_id"] = self.task_id
        try:
            return fn(**args)
        except TypeError as exc:
            return json.dumps({"ok": False, "error": f"参数错误：{exc}"}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def _deterministic_fallback(self, candidate_id: str) -> dict[str, Any]:
        raw = get_entity_candidate_context(self.task_id, candidate_id)
        data = json.loads(raw)
        if not data.get("ok"):
            return {"ok": False, "fallback": True, "error": data.get("error") or data}
        cand = data["candidate"]
        conflicts = cand.get("conflicts") or []
        supporting = cand.get("supporting_facts") or []
        recommendation = "DEFER"
        if conflicts:
            recommendation = "DEFER"
        elif supporting and not conflicts:
            recommendation = "DEFER"  # 仍需人工确认
        summary = (
            f"{cand.get('display_name') or '该对象'}涉及 "
            f"{(cand.get('impact') or {}).get('case_count') or len(cand.get('cases') or [])} "
            f"起案件。"
            + ("存在字段差异，建议人工核验。" if conflicts else "字段基本一致，仍须人工确认。")
        )[:150]
        suggestion = {
            "recommendation": recommendation,
            "agent_summary": summary,
            "supporting_facts": supporting,
            "conflicts": conflicts,
            "missing_fields": cand.get("missing_fields") or [],
            "field_compare": cand.get("field_compare") or [],
            "evidence": cand.get("evidence") or [],
            "confidence": "LOW",
        }
        # 确定性回退不强制 quote 校验通过时跳过写入失败
        try:
            result = json.loads(
                propose_entity_review(self.task_id, candidate_id, suggestion, user_id="system")
            )
        except Exception:
            # 证据校验失败时仅返回建议，不落库
            return {"ok": True, "fallback": True, "persisted": False, "suggestion": suggestion}
        return {"ok": True, "fallback": True, "persisted": bool(result.get("ok")), "result": result, "suggestion": suggestion}

    def review_one(self, candidate_id: str) -> dict[str, Any]:
        if not DEEPSEEK_EXTERNAL_CALLS_ENABLED or not API_KEY:
            return self._deterministic_fallback(candidate_id)

        self.messages = [
            {"role": "system", "content": ENTITY_REVIEW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"请对候选 {candidate_id} 做实体复核。"
                    "按流程调用工具，最后必须 propose_entity_review。"
                ),
            },
        ]
        last_propose = None
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.messages,
                    tools=self.tools,
                    tool_choice="auto",
                    temperature=0,
                    max_tokens=2048,
                    stream=False,
                )
                msg = stream.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None) or []
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or "",
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ]
                self.messages.append(assistant_msg)
                if not tool_calls:
                    break
                for tc in tool_calls:
                    result = self.execute_tool(
                        {
                            "function_name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        }
                    )
                    if tc.function.name == "propose_entity_review":
                        try:
                            last_propose = json.loads(result)
                        except json.JSONDecodeError:
                            last_propose = {"raw": result}
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        }
                    )
                if last_propose and last_propose.get("ok"):
                    break
            if last_propose and last_propose.get("ok"):
                return {"ok": True, "fallback": False, "result": last_propose}
            return self._deterministic_fallback(candidate_id)
        except Exception as exc:
            fallback = self._deterministic_fallback(candidate_id)
            fallback["error"] = str(exc)
            return fallback

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
