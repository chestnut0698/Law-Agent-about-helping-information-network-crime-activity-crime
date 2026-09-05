"""实体复核专用窄粒度工具：只返回单候选最小上下文，供 DeepSeek Agent 调用。"""

from __future__ import annotations

import json
from typing import Any

from app.entity_review_schema import (
    FIELD_LABELS,
    FIELD_WHITELIST,
    EntityType,
    normalize_entity_type,
)
from app.files import MaterialError, get_material_service
from app.tasks import TaskError, get_task_service
from tools.entities import quote_hash

TOOL_RESULT_LIMIT = 6000

_FIELD_ORDER = {
    EntityType.BANK_ACCOUNT: ["account_no", "holder_name", "bank_name", "reserved_phone", "merchant"],
    EntityType.PHONE: ["phone_no", "registrant", "linked_account", "linked_device", "contact_context"],
    EntityType.PERSON: ["name", "id_card", "phone", "account", "organization", "role_in_material"],
    EntityType.DEVICE: ["device_id", "linked_phone", "linked_account", "linked_person", "login_time"],
    EntityType.ORGANIZATION: ["org_name", "credit_code", "legal_person", "address", "phone", "account"],
    EntityType.MERCHANT: ["merchant_id", "merchant_name", "settle_account", "pay_channel", "linked_org"],
    EntityType.ID_CARD: ["id_no", "name", "address"],
    EntityType.IP: ["ip_address", "linked_account", "linked_device"],
}


def _readable_candidate_name(candidate: dict[str, Any]) -> str:
    existing = str(candidate.get("display_name") or candidate.get("title") or "").strip()
    if existing and not (
        existing.startswith("同一") and existing.endswith("跨案出现")
    ) and "候选" not in existing:
        return existing
    try:
        entity_type = normalize_entity_type(str(candidate.get("entity_type")))
    except ValueError:
        entity_type = EntityType.PERSON
    records = candidate.get("records") or []
    raw = str((records[0] if records else {}).get("value") or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if entity_type == EntityType.BANK_ACCOUNT:
        return f"尾号 {digits[-4:]} 银行账户" if len(digits) >= 4 else "银行账户（同一脱敏标识）"
    if entity_type == EntityType.PHONE:
        return f"尾号 {digits[-4:]} 手机号码" if len(digits) >= 4 else "手机号码（同一脱敏标识）"
    if entity_type == EntityType.DEVICE:
        return f"IMEI 尾号 {digits[-4:]} 设备" if len(digits) >= 4 else "电子设备（同一设备标识）"
    if entity_type == EntityType.ID_CARD:
        return f"尾号 {digits[-4:]} 身份证件" if len(digits) >= 4 else "身份证件（同一脱敏标识）"
    if entity_type == EntityType.ORGANIZATION:
        return f"“{raw}”组织" if raw else "组织主体（同一脱敏名称）"
    if entity_type == EntityType.MERCHANT:
        return f"商户号 {raw} 商户" if raw else "商户（同一商户标识）"
    if entity_type == EntityType.IP:
        return f"{raw} 网络地址" if raw else "网络地址（同一脱敏标识）"
    return f"“{raw}”人物" if raw else "人物（同一脱敏姓名）"


def _public_entity_type(candidate: dict[str, Any]) -> str:
    try:
        return normalize_entity_type(str(candidate.get("entity_type"))).value
    except ValueError:
        return EntityType.PERSON.value


def _candidate_cases(candidate: dict[str, Any]) -> list[dict[str, str]]:
    cases = candidate.get("cases") or []
    if cases:
        return cases
    seen: dict[str, dict[str, str]] = {}
    for record in candidate.get("records") or []:
        case_id = record.get("case_id")
        if case_id and case_id not in seen:
            seen[case_id] = {
                "case_id": case_id,
                "case_name": record.get("case_name") or case_id,
            }
    return list(seen.values())


def _legacy_field_compare(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """将旧候选补成完整矩阵；非主字段明确为未记载，绝不猜值。"""
    try:
        entity_type = normalize_entity_type(str(candidate.get("entity_type")))
    except ValueError:
        entity_type = EntityType.PERSON
    records = candidate.get("records") or []
    cases = _candidate_cases(candidate)
    primary_key = _FIELD_ORDER[entity_type][0]
    rows = []
    for field_key in _FIELD_ORDER[entity_type]:
        per_case = []
        for case in cases:
            values = (
                [
                    str(record.get("value"))
                    for record in records
                    if record.get("case_id") == case.get("case_id") and record.get("value")
                ]
                if field_key == primary_key
                else []
            )
            value = "、".join(dict.fromkeys(values)) or None
            per_case.append(
                {
                    "case_id": case.get("case_id"),
                    "case_name": case.get("case_name") or case.get("case_id"),
                    "value": value,
                    "status": "same" if value else "missing",
                }
            )
        rows.append(
            {
                "field_key": field_key,
                "label": FIELD_LABELS[field_key],
                "per_case": per_case,
            }
        )
    return rows


def _tool_json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False)
    if len(text) > TOOL_RESULT_LIMIT:
        return json.dumps(
            {
                "ok": False,
                "error": "结果过大，请缩小查询范围",
                "truncated": text[:TOOL_RESULT_LIMIT] + "…",
            },
            ensure_ascii=False,
        )
    return text


def _load_candidate_set(task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    service = get_task_service()
    art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    if not art:
        raise TaskError("TASK_ARTIFACT_NOT_FOUND", "跨案对象待核清单不存在")
    detail = service.get_artifact(task_id, art["id"])
    return art, detail


def _find_candidate(payload: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    for item in payload.get("candidates") or []:
        if item.get("candidate_id") == candidate_id:
            return item
    return None


def list_entity_candidates(
    task_id: str,
    decision: str | None = "PENDING",
    entity_type: str | None = None,
    limit: int = 30,
) -> str:
    """分页列出实体候选摘要（默认仅待核）。"""
    try:
        art, detail = _load_candidate_set(task_id)
        payload = detail.get("payload") or {}
        rows = []
        for item in payload.get("candidates") or []:
            if decision and (item.get("decision") or "PENDING") != decision:
                continue
            if entity_type:
                try:
                    want = normalize_entity_type(entity_type).value
                except ValueError:
                    want = entity_type
                if item.get("entity_type") != want:
                    continue
            rows.append(
                {
                    "candidate_id": item.get("candidate_id"),
                    "entity_type": _public_entity_type(item),
                    "display_name": _readable_candidate_name(item),
                    "decision": item.get("decision") or "PENDING",
                    "case_count": len(_candidate_cases(item)),
                    "recommendation": item.get("recommendation") or "DEFER",
                    "conflict_count": len(item.get("conflicts") or []),
                }
            )
            if len(rows) >= max(1, min(limit, 50)):
                break
        return _tool_json(
            {
                "ok": True,
                "artifact_id": art["id"],
                "version": detail.get("version") or art.get("current_version"),
                "total_matched": len(rows),
                "candidates": rows,
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def get_entity_candidate_context(task_id: str, candidate_id: str) -> str:
    """读取单个候选的复核上下文（不含全量 mentions）。"""
    try:
        art, detail = _load_candidate_set(task_id)
        payload = detail.get("payload") or {}
        cand = _find_candidate(payload, candidate_id)
        if not cand:
            return _tool_json({"ok": False, "error": "候选不存在"})
        slim = {
            "candidate_id": cand.get("candidate_id"),
            "fingerprint": cand.get("fingerprint"),
            "entity_type": _public_entity_type(cand),
            "display_name": _readable_candidate_name(cand),
            "cases": _candidate_cases(cand),
            "field_compare": cand.get("field_compare") or _legacy_field_compare(cand),
            "evidence": (cand.get("evidence") or [])[:8],
            "supporting_facts": cand.get("supporting_facts") or [],
            "conflicts": cand.get("conflicts") or [],
            "missing_fields": cand.get("missing_fields") or [],
            "impact": cand.get("impact") or {},
            "decision": cand.get("decision") or "PENDING",
            "recommendation": cand.get("recommendation") or "DEFER",
            "agent_summary": cand.get("agent_summary") or "",
            "question": cand.get("question") or "",
            "match_basis": cand.get("match_basis") or [],
        }
        return _tool_json(
            {
                "ok": True,
                "artifact_id": art["id"],
                "version": detail.get("version"),
                "candidate": slim,
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def compare_candidate_fields(task_id: str, candidate_id: str) -> str:
    """返回逐字段跨案对照；若无 field_compare 则从 records 合成。"""
    try:
        _, detail = _load_candidate_set(task_id)
        cand = _find_candidate(detail.get("payload") or {}, candidate_id)
        if not cand:
            return _tool_json({"ok": False, "error": "候选不存在"})
        rows = list(cand.get("field_compare") or [])
        if not rows:
            rows = _legacy_field_compare(cand)
        # 白名单过滤
        try:
            et = normalize_entity_type(str(cand.get("entity_type")))
            allowed = FIELD_WHITELIST[et]
            rows = [r for r in rows if r.get("field_key") in allowed or r.get("field_key") == "value"]
        except Exception:
            pass
        return _tool_json({"ok": True, "candidate_id": candidate_id, "field_compare": rows})
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def search_candidate_evidence(task_id: str, candidate_id: str, limit: int = 6) -> str:
    """返回候选证据片段（已脱敏），含定位信息。"""
    try:
        _, detail = _load_candidate_set(task_id)
        cand = _find_candidate(detail.get("payload") or {}, candidate_id)
        if not cand:
            return _tool_json({"ok": False, "error": "候选不存在"})
        evidence = list(cand.get("evidence") or [])
        if not evidence:
            for rec in cand.get("records") or []:
                src = rec.get("source") or {}
                if src.get("chunk_id") and src.get("quote_hash"):
                    evidence.append(
                        {
                            "case_id": rec.get("case_id"),
                            "case_name": rec.get("case_name"),
                            "chunk_id": src.get("chunk_id"),
                            "document_version_id": src.get("document_version_id"),
                            "filename": src.get("document_name"),
                            "page_start": src.get("page_no"),
                            "quote": src.get("quote") or "",
                            "quote_hash": src.get("quote_hash") or "",
                        }
                    )
        return _tool_json(
            {
                "ok": True,
                "candidate_id": candidate_id,
                "evidence": evidence[: max(1, min(limit, 12))],
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def list_candidate_relations(task_id: str, candidate_id: str) -> str:
    """列出候选影响的线索与关联计数。"""
    try:
        service = get_task_service()
        _, detail = _load_candidate_set(task_id)
        cand = _find_candidate(detail.get("payload") or {}, candidate_id)
        if not cand:
            return _tool_json({"ok": False, "error": "候选不存在"})
        fingerprint = cand.get("fingerprint")
        clues = []
        for art in service.get_task(task_id).get("artifacts") or []:
            if art.get("type") != "CLUE_ITEM" or art.get("status") in {"INVALID"}:
                continue
            payload = (service.get_artifact(task_id, art["id"]).get("payload") or {})
            linked = payload.get("linked_candidate_ids") or []
            if candidate_id in linked or fingerprint and fingerprint in (payload.get("fingerprints") or []):
                clues.append(
                    {
                        "artifact_id": art["id"],
                        "title": art.get("title") or payload.get("title"),
                        "status": art.get("status"),
                    }
                )
        impact = dict(cand.get("impact") or {})
        impact["clue_count"] = len(clues)
        return _tool_json(
            {
                "ok": True,
                "candidate_id": candidate_id,
                "impact": impact,
                "generated_clues": clues,
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def validate_candidate_evidence(
    task_id: str,
    chunk_id: str,
    document_version_id: str,
    quote: str,
    quote_hash_value: str,
) -> str:
    """服务端校验单条引用是否真实可回链。"""
    try:
        # task_id 用于权限与审计上下文，材料本身按 version 读取
        _ = task_id
        result = get_material_service().read_redacted_chunk(
            document_version_id,
            chunk_id=chunk_id,
            user_id="system",
            quote=quote,
            quote_hash=quote_hash_value,
        )
        return _tool_json(
            {
                "ok": True,
                "verified": True,
                "chunk_id": result.get("chunk_id"),
                "page_start": result.get("page_start"),
                "quote": quote,
                "quote_hash": quote_hash_value,
            }
        )
    except MaterialError as exc:
        return _tool_json(
            {
                "ok": False,
                "verified": False,
                "error_code": exc.code,
                "message": exc.message,
            }
        )
    except Exception as exc:
        # 兜底：仅校验哈希格式一致性（无原文上下文时不能声称已回链）
        ok = bool(quote) and bool(quote_hash_value) and quote_hash(quote) == quote_hash_value
        return _tool_json(
            {
                "ok": ok,
                "verified": False,
                "hash_match": ok,
                "message": str(exc),
            }
        )


def propose_entity_review(
    task_id: str,
    candidate_id: str,
    suggestion: dict[str, Any],
    user_id: str | None = None,
) -> str:
    """写入 AI 复核建议（不改变人工 decision）。"""
    try:
        result = get_task_service().propose_entity_review(
            task_id,
            candidate_id,
            suggestion=suggestion,
            user_id=user_id or "system",
        )
        return _tool_json({"ok": True, **result})
    except TaskError as exc:
        return _tool_json(exc.to_dict())
