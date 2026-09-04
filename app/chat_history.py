"""对话历史清洗：保证发给模型的 tool_calls 与 tool 回包成对。"""

from __future__ import annotations

import json
from typing import Any


def _load_metadata(metadata_json: Any) -> Any:
    raw = metadata_json
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def parse_tool_calls_metadata(metadata_json: Any) -> list[dict] | None:
    """兼容旧格式（直接存 list）与新格式（{"tool_calls": [...], ...}）。"""
    raw = _load_metadata(metadata_json)
    if isinstance(raw, list) and raw:
        return raw
    if isinstance(raw, dict):
        tool_calls = raw.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return tool_calls
    return None


def parse_reasoning_metadata(metadata_json: Any) -> str | None:
    raw = _load_metadata(metadata_json)
    if isinstance(raw, dict):
        text = raw.get("reasoning_content")
        if isinstance(text, str) and text.strip():
            return text
    return None


def row_to_llm_message(row: dict[str, Any]) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": row["role"],
        "content": row.get("content") or "",
    }
    if row.get("id"):
        msg["_row_id"] = row["id"]
    if row.get("created_at"):
        msg["created_at"] = row["created_at"]
    if row.get("tool_call_id"):
        msg["tool_call_id"] = row["tool_call_id"]
    tool_calls = parse_tool_calls_metadata(row.get("metadata_json"))
    if tool_calls:
        msg["tool_calls"] = tool_calls
    reasoning = parse_reasoning_metadata(row.get("metadata_json"))
    if reasoning:
        msg["reasoning_content"] = reasoning
    return msg


def sanitize_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢弃不完整的 assistant/tool_calls 组，避免模型接口 400。"""
    out: list[dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            expected = [
                tc.get("id")
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("id")
            ]
            if not expected:
                cleaned = {k: v for k, v in msg.items() if k != "tool_calls"}
                if (cleaned.get("content") or "").strip():
                    out.append(cleaned)
                i += 1
                continue
            j = i + 1
            found: dict[str, dict[str, Any]] = {}
            while j < n and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id")
                if tid and tid not in found:
                    found[tid] = messages[j]
                j += 1
            if all(tid in found for tid in expected):
                out.append(msg)
                for tid in expected:
                    out.append(found[tid])
                i = j
            else:
                i = j
            continue
        if role == "tool":
            i += 1
            continue
        out.append(msg)
        i += 1
    return out


TOOL_CONTENT_LIMIT = 8000


def _clip_tool_content(text: str, limit: int = TOOL_CONTENT_LIMIT) -> str:
    if not text or len(text) <= limit:
        return text or ""
    return text[:limit] + "\n…(工具结果已截断)"


def messages_for_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留模型接口允许的字段，并确保 tool 序列合法。"""
    payload: list[dict[str, Any]] = []
    for msg in sanitize_tool_history(messages):
        item: dict[str, Any] = {
            "role": msg["role"],
            "content": msg.get("content") or "",
        }
        if item["role"] == "tool":
            item["content"] = _clip_tool_content(item["content"])
        if msg.get("tool_calls"):
            item["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            item["tool_call_id"] = msg["tool_call_id"]
        payload.append(item)
    return payload


def strip_internal_fields(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in msg.items() if not str(k).startswith("_")}
        for msg in messages
    ]
