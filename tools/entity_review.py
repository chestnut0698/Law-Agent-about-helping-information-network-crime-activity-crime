"""实体复核专用窄粒度工具：只返回单候选最小上下文，供 DeepSeek Agent 调用。

字段对照表由 DeepSeek 按实体类型与材料现场设计（build_candidate_field_table），
规则矩阵仅作兜底；模型给的每个值都必须能在脱敏材料里逐字命中，否则丢弃。
"""

from __future__ import annotations

import json
import re
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
        # 白名单过滤；模型现场设计的表按 producer 放行
        try:
            et = normalize_entity_type(str(cand.get("entity_type")))
            allowed = FIELD_WHITELIST[et]
            rows = [
                r
                for r in rows
                if r.get("producer") == FIELD_TABLE_PRODUCER
                or r.get("field_key") in allowed
                or r.get("field_key") == "value"
            ]
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


# ---------- DeepSeek 现场设计字段对照表 ----------

FIELD_TABLE_PRODUCER = "DEEPSEEK_FIELD_TABLE"

_TABLE_SYSTEM_PROMPT = (
    "你是检察官办案助手，负责为「跨案实体复核」设计并填写字段对照表。"
    "你只能依据给定的脱敏材料摘录填写；材料里没写的一律留空。"
    "材料已脱敏，人名/号码以 PERSON_xxx、PHONE_xxx 等占位符出现，"
    "占位符就是材料的真实记载，必须照常摘录，不算编造；"
    "若给了占位符对照，v 用对照里的可读名称，q 仍抄材料里的占位符原文。"
    "若任务是「不同称谓是否同一人」，必须按称谓分列摘录各称谓自身信息，"
    "禁止把同案其他被告人/证人填进该称谓列。"
    "每条 q 必须是材料正文连续原文，且不超过 40 字（短摘录，供证据卡展示）；"
    "禁止把整段笔录/判决粘进 q。"
    "禁止编造材料里没有的账号、姓名、页码、原文；禁止输出定罪、并案或同一人已确认的结论。"
    "只输出 JSON。"
)


def _all_cells_empty(parsed: Any) -> bool:
    """模型返回了表头但一个值都没填。"""
    rows = parsed.get("rows") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        for cell in row.get("cells") or row.get("per_case") or []:
            if isinstance(cell, dict) and (cell.get("v") or cell.get("value")):
                return False
    return True


def _candidate_hint(candidate: dict[str, Any]) -> str:
    """从候选名里取可用于材料检索的关键词。"""
    text = str(candidate.get("display_name") or "").strip()
    text = re.sub(r"[“”\"']", "", text)
    text = re.sub(
        r"(尾号|银行账户|手机号码|电子设备|身份证件|网络地址|组织主体|组织|人物|商户|设备|疑似同一人)",
        " ",
        text,
    )
    parts = [p for p in re.split(r"[\s/·、,，]+", text) if len(p) >= 2]
    if parts:
        return max(parts, key=len)[:40]
    for record in candidate.get("records") or []:
        value = str(record.get("value") or "").strip()
        if value:
            return value[:40]
    return ""


def _locate_quote(text: str, quote: str) -> str | None:
    """把模型给的片段对回材料原文：允许空白差异，但必须落在存储文本里。"""
    from tools.entities import whitespace_locate_quote

    return whitespace_locate_quote(text, quote)


def _candidate_materials(
    candidate: dict[str, Any], per_case_limit: int = 4
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """收集候选各案的脱敏材料摘录，供模型阅读。"""
    from tools.entities import _load_chunks_for_field_enrichment

    seeds: list[str] = []
    for ev in candidate.get("evidence") or []:
        if ev.get("chunk_id"):
            seeds.append(ev["chunk_id"])
    for rec in candidate.get("records") or []:
        src = rec.get("source") or {}
        if src.get("chunk_id"):
            seeds.append(src["chunk_id"])

    hint = _candidate_hint(candidate)
    materials: list[dict[str, Any]] = []
    chunk_by_id: dict[str, dict[str, Any]] = {}
    for case in _candidate_cases(candidate):
        case_id = case.get("case_id")
        if not case_id:
            continue
        for row in _load_chunks_for_field_enrichment(
            case_id=case_id,
            hint=hint,
            seed_chunk_ids=seeds,
            limit=per_case_limit,
        ):
            text = (row.get("text_redacted") or "").strip()
            if not text:
                continue
            chunk_by_id[row["chunk_id"]] = row
            materials.append(
                {
                    "case_id": case_id,
                    "case_name": case.get("case_name") or case_id,
                    "chunk_id": row["chunk_id"],
                    "filename": row.get("filename"),
                    "page_start": row.get("page_start"),
                    "text": text[:1800],
                }
            )
    return materials, chunk_by_id


def _alias_axis_id(alias: str) -> str:
    return f"alias:{alias}"


def _org_style_quote_snippet(text: str, term: str = "", *, max_len: int = 40) -> str:
    """兼容旧调用：统一走 entities 短摘录（不切碎占位符）。"""
    from tools.entities import _org_style_quote_snippet as _snip

    return _snip(text, term, max_len=max_len) if text else ""


def _seed_alias_material_row(
    candidate: dict[str, Any],
    aliases: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """规则只预填「材料出处」路径（案名·文件·页码）。

    原文要点与其它语义字段交给 DeepSeek（统一短 q）；打开原文的上下文不靠这里堆长窗口。
    """
    columns = [
        {"case_id": _alias_axis_id(a), "case_name": a, "kind": "alias"} for a in aliases
    ]
    by_alias: dict[str, dict[str, Any]] = {}
    for rec in candidate.get("records") or []:
        value = str(rec.get("value") or "").strip()
        if value in aliases and value not in by_alias:
            by_alias[value] = rec

    material_row: dict[str, Any] = {
        "field_key": "alias_material",
        "label": "材料出处",
        "producer": "RULE_ALIAS_SEED",
        "per_case": [],
    }
    for alias in aliases:
        rec = by_alias.get(alias) or {}
        src = rec.get("source") or {}
        filename = src.get("document_name") or src.get("filename") or ""
        page = src.get("page_no") or src.get("page_start")
        parts = [
            p
            for p in [
                rec.get("case_name") or "",
                filename,
                f"第{page}页" if page else "",
            ]
            if p
        ]
        material = " · ".join(parts)
        material_row["per_case"].append(
            {
                "case_id": _alias_axis_id(alias),
                "case_name": alias,
                "value": material or None,
                "status": "same" if material else "missing",
                "sources": [],
            }
        )
    return columns, [material_row]


def _seed_alias_person_table(
    candidate: dict[str, Any],
    aliases: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """兼容旧名：仅预填材料出处路径。"""
    return _seed_alias_material_row(candidate, aliases)


def _enrich_alias_placeholder_map(
    alias_map: dict[str, str],
    *,
    text: str,
    aliases: list[str],
) -> dict[str, str]:
    """把主身份称谓对应的占位符补进对照表。

    一类问题：脱敏正文里主身份常是 PERSON_xxx 且无 display_alias，
    而同段其他当事人反而有可读化名，模型会误读「谁才是本候选」。
    """
    out = dict(alias_map or {})
    if not aliases or not text:
        return out
    try:
        from tools.entities import get_global_mapper

        originals = get_global_mapper().original_map() or {}
    except Exception:
        return out
    for ph, original in originals.items():
        if not ph or ph not in text:
            continue
        orig = str(original or "").strip()
        for alias in aliases:
            if orig == alias or orig.startswith(alias):
                out[ph] = alias
                break
    return out


def _off_identity_display_names(
    alias_map: dict[str, str], identity_aliases: list[str]
) -> set[str]:
    """占位符对照里出现、但不属于本候选主身份的可读名（同段其他当事人）。"""
    identity = set(identity_aliases or [])
    off: set[str] = set()
    for name in (alias_map or {}).values():
        text = str(name or "").strip()
        if not text or text in identity or text.startswith("PERSON_"):
            continue
        off.add(text)
    return off


def _value_is_foreign_party_list(
    value: str,
    *,
    axis_alias: str,
    identity_aliases: list[str],
    off_identity: set[str],
) -> bool:
    """别名分列时：单元格若主要在罗列「非本列主身份」的其他人，则丢弃。"""
    text = (value or "").strip()
    if not text:
        return False
    if axis_alias and axis_alias in text:
        return False
    # 命中多个非主身份可读名，或整段由非主身份名顿号拼接
    foreign_hits = [n for n in off_identity if n and n in text]
    if len(foreign_hits) >= 2:
        return True
    parts = [p.strip() for p in re.split(r"[、,/，\s]+", text) if len(p.strip()) >= 2]
    if len(parts) >= 2:
        identity = set(identity_aliases or [])
        if all(
            p not in identity and any(p == n or p in n or n in p for n in off_identity)
            for p in parts
        ):
            return True
    # 单值：恰好是某个非主身份可读名
    if text in off_identity:
        return True
    return False


def _candidate_identity_aliases(candidate: dict[str, Any]) -> list[str]:
    """候选主身份称谓（疑似同一人 aliases / records 去重）。"""
    aliases: list[str] = []
    for item in candidate.get("aliases") or []:
        text = str(item or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    if not aliases:
        for record in candidate.get("records") or []:
            text = str(record.get("value") or "").strip()
            if text and text not in aliases and not text.startswith("PERSON_"):
                aliases.append(text)
    # 从 display_name「疑似同一人：A / B」兜底
    if not aliases:
        name = str(candidate.get("display_name") or "")
        if "疑似同一人" in name or "/" in name:
            raw = re.sub(r"^.*?[：:]", "", name)
            for part in re.split(r"[/·、,，\s]+", raw):
                part = part.strip().strip("“”\"'")
                if len(part) >= 2 and part not in aliases:
                    aliases.append(part)
    return aliases[:8]



def _is_primary_name_field(field_key: str, label: str) -> bool:
    text = f"{field_key} {label}".lower()
    if any(x in text for x in ("关联", "related", "associated", "同段", "其他人员", "对手")):
        return False
    return bool(
        re.search(r"(^|_)(name|person_name|primary_name)(_|$)", field_key)
        or re.search(r"姓名|称谓|主标识|人物名称", label)
    )


def _value_overlaps_identity(value: str, aliases: list[str]) -> bool:
    text = (value or "").strip()
    if not text or not aliases:
        return False
    parts = [p.strip() for p in re.split(r"[、,/，\s]+", text) if p.strip()]
    for part in parts or [text]:
        for alias in aliases:
            if part == alias or alias in part or part in alias:
                return True
    return False


def _force_primary_identity_row(
    field_compare: list[dict[str, Any]],
    *,
    aliases: list[str],
    case_ids: list[str],
    case_name_map: dict[str, str],
    preserved_sources: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """人物候选：第一行主姓名强制为 aliases，模型填错或留空则覆盖。"""
    if not aliases or not case_ids:
        return field_compare
    identity_value = "、".join(aliases)
    primary_idx = next(
        (
            i
            for i, row in enumerate(field_compare)
            if _is_primary_name_field(row.get("field_key") or "", row.get("label") or "")
        ),
        None,
    )
    forced_row = {
        "field_key": "name",
        "label": "姓名/称谓",
        "producer": FIELD_TABLE_PRODUCER,
        "per_case": [
            {
                "case_id": cid,
                "case_name": case_name_map.get(cid) or cid,
                "value": identity_value,
                "status": "same" if len(case_ids) > 1 else "partial",
                "sources": list((preserved_sources or {}).get(cid) or []),
            }
            for cid in case_ids
        ],
    }
    # 单案也标 same 更直观（仅一案有值）
    if len(case_ids) == 1:
        forced_row["per_case"][0]["status"] = "same"

    if primary_idx is None:
        return [forced_row] + field_compare
    # 保留模型在主姓名行上找到的、且与 aliases 有交集的 sources
    old = field_compare[primary_idx]
    for cell in old.get("per_case") or []:
        cid = cell.get("case_id")
        if not cid:
            continue
        keep = []
        for src in cell.get("sources") or []:
            extracted = str(src.get("extracted_value") or cell.get("value") or "")
            if _value_overlaps_identity(extracted, aliases) or _value_overlaps_identity(
                str(src.get("quote_display") or ""), aliases
            ):
                keep.append(src)
        if keep:
            for forced_cell in forced_row["per_case"]:
                if forced_cell["case_id"] == cid:
                    forced_cell["sources"] = keep
                    break
    out = list(field_compare)
    out[primary_idx] = forced_row
    return out


def _filter_aux_identity_cells(
    field_compare: list[dict[str, Any]], aliases: list[str]
) -> list[dict[str, Any]]:
    """辅字段里若整格值与主身份完全重合，不重复；主姓名行已强制，跳过。"""
    if not aliases:
        return field_compare
    identity_set = set(aliases)
    cleaned: list[dict[str, Any]] = []
    for row in field_compare:
        if _is_primary_name_field(row.get("field_key") or "", row.get("label") or ""):
            cleaned.append(row)
            continue
        label = str(row.get("label") or "")
        # 关联类辅字段：允许记载同段其他当事人，但改标签避免误解为同一人
        is_assoc = bool(re.search(r"关联|related|associated|同段|其他人员", f"{row.get('field_key')} {label}", re.I))
        new_cells = []
        for cell in row.get("per_case") or []:
            value = str(cell.get("value") or "").strip()
            if not value:
                new_cells.append(cell)
                continue
            parts = [p.strip() for p in re.split(r"[、,/，]+", value) if p.strip()]
            # 去掉与主身份完全相同的称呼，避免辅字段重复主标识
            kept = [p for p in parts if p not in identity_set]
            if not kept:
                new_cells.append({**cell, "value": None, "status": "missing", "sources": []})
                continue
            new_cells.append({**cell, "value": "、".join(kept)})
        updated = {**row, "per_case": new_cells}
        if is_assoc and "同段" not in label and "其他" not in label:
            updated["label"] = "同段其他人员"
            updated["field_key"] = "co_mentioned_persons"
        cleaned.append(updated)
    return cleaned


def _accept_table_value(field_key: str, label: str, value: str) -> bool:
    """账户类字段守门：拒绝 8 位碎片等明显不是卡号的值。"""
    if str(value).startswith("PERSON_"):
        return False
    if not re.search(r"account|card|账户|账号|卡号", f"{field_key} {label}", re.I):
        return True
    digits = re.sub(r"\D", "", value)
    if not digits or len(digits) <= 6:
        # 尾号 6231 之类的表述保留，交由人工看原文判断
        return True
    if "*" in value or "＊" in value:
        return True
    return 11 <= len(digits) <= 19


def build_candidate_field_table(
    task_id: str,
    candidate_id: str,
    force: bool = False,
    user_id: str | None = None,
) -> str:
    """让 DeepSeek 依据材料为该候选设计并填写字段对照表，写回候选集。"""
    from app.config import (
        API_KEY,
        BASE_URL,
        DEEPSEEK_EXTERNAL_CALLS_ENABLED,
        MODEL_NAME,
    )
    from tools.entities import _field_cell_status, _review_display_value

    try:
        _, detail = _load_candidate_set(task_id)
    except TaskError as exc:
        return _tool_json(exc.to_dict())
    cand = _find_candidate(detail.get("payload") or {}, candidate_id)
    if not cand:
        return _tool_json({"ok": False, "error": "候选不存在"})

    meta = cand.get("field_table_meta") or {}
    if not force and meta.get("producer") == FIELD_TABLE_PRODUCER:
        return _tool_json(
            {
                "ok": True,
                "cached": True,
                "candidate_id": candidate_id,
                "field_table_meta": meta,
                "field_compare": cand.get("field_compare") or [],
            }
        )

    if not DEEPSEEK_EXTERNAL_CALLS_ENABLED:
        return _tool_json(
            {
                "ok": False,
                "error": "材料外呼开关已关闭（DEEPSEEK_EXTERNAL_CALLS_ENABLED），当前使用规则字段表",
                "fallback": True,
            }
        )
    if not API_KEY:
        return _tool_json(
            {"ok": False, "error": "未配置模型密钥，当前使用规则字段表", "fallback": True}
        )

    materials, chunk_by_id = _candidate_materials(cand)
    if not materials:
        return _tool_json(
            {"ok": False, "error": "未取到可读材料摘录，无法生成字段表", "fallback": True}
        )

    cases = _candidate_cases(cand)
    entity_type = _public_entity_type(cand)
    type_label = {
        "BANK_ACCOUNT": "银行账户",
        "PHONE": "手机号码",
        "PERSON": "人物",
        "DEVICE": "电子设备",
        "ORGANIZATION": "组织主体",
        "MERCHANT": "商户",
        "ID_CARD": "身份证件",
        "IP": "网络地址",
    }.get(entity_type, entity_type)

    # 用短代号替代 UUID：既省输出 token，也避免模型抄错长 ID 导致引用作废
    case_ids = [c.get("case_id") for c in cases if c.get("case_id")]
    case_name_map = {c.get("case_id"): c.get("case_name") or c.get("case_id") for c in cases}
    case_code = {cid: chr(65 + i) for i, cid in enumerate(case_ids)}
    code_to_case = {code: cid for cid, code in case_code.items()}
    mat_code: dict[str, str] = {}
    brief_materials = []
    for i, mat in enumerate(materials):
        code = f"m{i + 1}"
        mat_code[code] = mat["chunk_id"]
        brief_materials.append(
            {
                "m": code,
                "案": case_code.get(mat["case_id"], "?"),
                "材料": mat.get("filename") or "",
                "页": mat.get("page_start"),
                "正文": mat["text"],
            }
        )

    # 材料是脱敏文本，人名等已变成 PERSON_xxx 占位符。把占位符对照表一并给模型，
    # 它才能在 v 里写回可读姓名，同时 q 仍保持与材料逐字一致以便回链校验。
    alias_map: dict[str, str] = {}
    try:
        from tools.entities import get_global_mapper

        all_text = "".join(b["正文"] for b in brief_materials)
        alias_map = {
            ph: name
            for ph, name in (get_global_mapper().alias_map() or {}).items()
            if ph in all_text
        }
    except Exception:
        alias_map = {}

    identity_aliases = _candidate_identity_aliases(cand)
    alias_mode = entity_type == "PERSON" and len(identity_aliases) >= 2

    # 别名人物：表头按各待核称谓分列，而不是把多种写法糊进同一案列
    if alias_mode:
        axis_ids = [_alias_axis_id(a) for a in identity_aliases]
        axis_name_map = {_alias_axis_id(a): a for a in identity_aliases}
        axis_code = {aid: chr(65 + i) for i, aid in enumerate(axis_ids)}
        code_to_axis = {code: aid for aid, code in axis_code.items()}
        # 同时允许用称谓汉字当代号
        for aid, name in axis_name_map.items():
            code_to_axis[name] = aid
        compare_ids = axis_ids
        compare_name_map = axis_name_map
        compare_code = axis_code
        code_to_compare = code_to_axis
    else:
        compare_ids = case_ids
        compare_name_map = case_name_map
        compare_code = case_code
        code_to_compare = code_to_case

    all_text = "".join(b["正文"] for b in brief_materials)
    if alias_mode:
        alias_map = _enrich_alias_placeholder_map(
            alias_map, text=all_text, aliases=identity_aliases
        )

    if alias_mode:
        payload_in = {
            "任务": (
                "检察官要判断这些不同称谓是否为同一人。"
                "请按「称谓分列」填写对照表：每一列对应一个称谓，摘录该称谓在材料中的自身信息。"
            ),
            "实体类型": type_label,
            "实体名称": _readable_candidate_name(cand),
            "待核对称谓": [
                {"列": compare_code[_alias_axis_id(a)], "称谓": a} for a in identity_aliases
            ],
            "材料摘录": brief_materials,
            "占位符对照": alias_map or None,
            "输出JSON": {
                "table_title": "不同称谓对照（供同一性判断）",
                "rows": [
                    {
                        "key": "alias_quote",
                        "label": "原文要点",
                        "cells": [
                            {
                                "列": "A",
                                "v": "该称谓相关短要点；未记载 null",
                                "q": "原文≤40字短摘录",
                                "m": "m1",
                            }
                        ],
                    },
                    {
                        "key": "role_act",
                        "label": "记载角色/行为",
                        "cells": [
                            {
                                "列": "A",
                                "v": "该称谓在材料中的角色或行为；未记载 null",
                                "q": "原文≤40字短摘录",
                                "m": "m1",
                            }
                        ],
                    },
                ],
            },
            "规则": [
                "列代号对应待核对称谓；每行 cells 必须覆盖全部称谓列",
                "只摘录该列称谓自身的信息（原文要点、角色、时间、组织、办卡/取现等），用于判断这些称谓是否同一人",
                "禁止填写同案其他被告人/证人作为该称谓列的属性",
                "禁止输出「同段其他人员」「关联人员」等把非本列当事人塞进表的字段",
                "「材料出处」已由系统按路径预填，不要重复输出该行",
                "你必须输出「原文要点」行，再补充记载角色/行为、活动时间、所属组织或资金行为等 2–4 行",
                "q 必须是材料正文连续原文（可含 PERSON_xxx），长度≤40字；禁止粘贴整段笔录/判决",
                "v 用占位符对照换成可读中文；只做摘录，不判断是否同一人，不写结论",
            ],
        }
    else:
        payload_in = {
            "任务": "为该跨案实体设计一张最能支撑同一性判断的字段对照表，并逐案填值",
            "实体类型": type_label,
            "实体名称": _readable_candidate_name(cand),
            "候选主身份称谓": identity_aliases or None,
            "案件": [{"案": compare_code[cid], "名称": compare_name_map[cid]} for cid in compare_ids],
            "材料摘录": brief_materials,
            "占位符对照": alias_map or None,
            "输出JSON": {
                "table_title": "表标题",
                "rows": [
                    {
                        "key": "name" if entity_type == "PERSON" else "account_no",
                        "label": "姓名/称谓" if entity_type == "PERSON" else "账户号码",
                        "cells": [
                            {
                                "案": "A",
                                "v": "展示值，未记载填 null",
                                "q": "原文≤40字短摘录",
                                "m": "m1",
                            }
                        ],
                    }
                ],
            },
            "规则": [
                "rows 3–6 行；第一行必须是该实体主标识（账号/号码/姓名/设备号/组织名称等）",
                "字段要贴合材料实际记载，可用使用时间段、登记使用人、资金对手等",
                "每行 cells 必须覆盖全部案件代号，一个案件一条 cell，不能只写一个案件",
                "材料里写到的就要填进 v，只有确实找不到才 v=null 并省略 q、m",
                "q 必须是对应材料正文里的连续原文，长度≤40字，不得改写、拼接或粘贴整段",
                "m 必须是该 q 所在材料的代号（m1/m2…）",
                "q 里保留材料原样（含 PERSON_xxx 等占位符）；v 里按占位符对照换成可读中文名",
                "只做摘录，不判断是否同一主体，不写理由",
            ],
        }

    try:
        from openai import OpenAI

        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": _TABLE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload_in, ensure_ascii=False)},
            ],
            temperature=0,
            max_tokens=4000,
            response_format={"type": "json_object"},
            # 照材料填表不是推理题；V4 默认开思维链会把输出预算吃光，返回空正文
            extra_body={"thinking": {"type": "disabled"}},
            stream=False,
        )
        choice = resp.choices[0]
        raw = (choice.message.content or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        if not raw:
            return _tool_json(
                {
                    "ok": False,
                    "error": f"模型未返回内容（finish_reason={choice.finish_reason}）",
                    "fallback": True,
                }
            )
        parsed = json.loads(raw)
        # 模型偶尔整表交白卷（只给表头不填值），补一次带催填提示的重试
        if _all_cells_empty(parsed):
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": _TABLE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload_in, ensure_ascii=False),
                    },
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "整张表都是 null，说明你漏读了材料。请重新逐条通读材料摘录，"
                            "凡是材料写到的内容都要填进 v 并给出 q 与 m，仅在确实查无记载时才留 null。"
                            "只输出 JSON。"
                        ),
                    },
                ],
                temperature=0,
                max_tokens=4000,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                stream=False,
            )
            retry_raw = (resp.choices[0].message.content or "").strip()
            if retry_raw:
                retry_parsed = json.loads(retry_raw)
                if not _all_cells_empty(retry_parsed):
                    parsed = retry_parsed
    except Exception as exc:
        # 别名分列：模型失败时仍落盘规则预填表，保证检察官能看到各称谓出处
        if alias_mode:
            columns, seeded = _seed_alias_person_table(cand, identity_aliases)
            try:
                get_task_service().propose_entity_review(
                    task_id,
                    candidate_id,
                    suggestion={
                        "recommendation": cand.get("recommendation") or "DEFER",
                        "agent_summary": "模型未完成补全，已按各称谓材料出处预填对照表",
                        "supporting_facts": [],
                        "conflicts": [],
                        "missing_fields": [],
                        "field_compare": seeded,
                        "evidence": cand.get("evidence") or [],
                        "confidence": "LOW",
                    },
                    user_id=user_id or "system",
                )
                art, detail = _load_candidate_set(task_id)
                payload = detail.get("payload") or {}
                target = _find_candidate(payload, candidate_id)
                if target is not None:
                    target["field_compare_columns"] = columns
                    target["field_table_meta"] = {
                        "producer": "RULE_ALIAS_SEED",
                        "table_title": "不同称谓对照（供同一性判断）",
                        "row_count": len(seeded),
                        "compare_mode": "alias",
                        "partial": True,
                        "error": str(exc)[:120],
                    }
                    get_task_service().write_artifact(
                        task_id=task_id,
                        type="ENTITY_CANDIDATE_SET",
                        title=art.get("title") or "跨案对象待核·待判断",
                        ref_key="entity-candidates",
                        status=art.get("status") or "PENDING_REVIEW",
                        parent_ids=json.loads(art.get("parent_ids_json") or "[]"),
                        payload=payload,
                    )
                return _tool_json(
                    {
                        "ok": True,
                        "partial": True,
                        "candidate_id": candidate_id,
                        "table_title": "不同称谓对照（供同一性判断）",
                        "fields": [
                            {
                                "label": row["label"],
                                "values": {
                                    cell.get("case_name"): cell.get("value")
                                    for cell in row["per_case"]
                                },
                            }
                            for row in seeded
                        ],
                        "error": f"模型补全失败，已保留称谓出处底表：{exc}",
                    }
                )
            except Exception:
                pass
        return _tool_json(
            {"ok": False, "error": f"DeepSeek 生成字段表失败：{exc}", "fallback": True}
        )

    rows_in = parsed.get("rows") if isinstance(parsed, dict) else parsed
    if not isinstance(rows_in, list):
        rows_in = []
    if not rows_in and not alias_mode:
        return _tool_json({"ok": False, "error": "模型未返回字段行", "fallback": True})

    field_compare: list[dict[str, Any]] = []
    field_compare_columns: list[dict[str, Any]] | None = None
    evidence_map: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in cand.get("evidence") or []:
        if ev.get("chunk_id") and ev.get("quote_hash"):
            evidence_map[(ev["chunk_id"], ev["quote_hash"])] = ev

    if alias_mode:
        field_compare_columns, seeded = _seed_alias_person_table(cand, identity_aliases)
        field_compare.extend(seeded)
        used_keys = {row.get("field_key") for row in seeded}
        # 把预填原文也并入 evidence
        for row in seeded:
            for cell in row.get("per_case") or []:
                for src in cell.get("sources") or []:
                    if src.get("chunk_id") and src.get("quote_hash"):
                        evidence_map.setdefault(
                            (src["chunk_id"], src["quote_hash"]),
                            {
                                **src,
                                "field_label": row.get("label"),
                                "value": cell.get("value") or src.get("extracted_value"),
                            },
                        )
    else:
        used_keys = set()

    proposed = 0
    matched = 0
    banned_label = re.compile(r"同段其他|关联人员|其他被告人|其他人员")
    off_identity = (
        _off_identity_display_names(alias_map, identity_aliases) if alias_mode else set()
    )
    for index, row in enumerate(rows_in[:7]):
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        if alias_mode and banned_label.search(label):
            continue
        field_key = str(row.get("key") or row.get("field_key") or "").strip().lower()
        field_key = re.sub(r"[^a-z0-9_]", "_", field_key).strip("_") or f"field_{index}"
        if field_key in used_keys:
            field_key = f"{field_key}_{index}"
        used_keys.add(field_key)

        rendered: dict[str, str] = {}
        sources_by_case: dict[str, list[dict[str, Any]]] = {}
        for cell in row.get("cells") or row.get("per_case") or []:
            if not isinstance(cell, dict):
                continue
            code = str(
                cell.get("列")
                or cell.get("案")
                or cell.get("case")
                or cell.get("case_id")
                or cell.get("称谓")
                or ""
            ).strip()
            axis_id = (
                code_to_compare.get(code.upper())
                or code_to_compare.get(code)
                or (code if code in compare_ids else None)
            )
            if not axis_id:
                continue
            value = _review_display_value(str(cell.get("v") or cell.get("value") or "").strip())
            if not value or value.lower() in {"null", "none", "未记载", "无"}:
                continue
            if not _accept_table_value(field_key, label, value):
                continue
            # 别名分列：拒绝「非本列主身份」的其他人名单塞进该称谓列
            if alias_mode:
                axis_alias = compare_name_map.get(axis_id) or ""
                if _value_is_foreign_party_list(
                    value,
                    axis_alias=axis_alias,
                    identity_aliases=identity_aliases,
                    off_identity=off_identity,
                ):
                    continue
            elif (
                identity_aliases
                and entity_type == "PERSON"
                and _is_primary_name_field(field_key, label)
                and not _value_overlaps_identity(value, identity_aliases)
            ):
                continue
            proposed += 1
            raw_quote = str(cell.get("q") or cell.get("quote") or "").strip()
            ref = str(cell.get("m") or cell.get("chunk_id") or "").strip()
            chunk = chunk_by_id.get(mat_code.get(ref, ref))
            quote = _locate_quote((chunk or {}).get("text_redacted") or "", raw_quote)
            if not quote:
                # 模型偶尔标错材料代号：在摘录里再找一次
                for _code, cid in mat_code.items():
                    other = chunk_by_id.get(cid)
                    if not other:
                        continue
                    # 别名模式不按真实 case_id 限制；跨案模式仍限本案
                    if not alias_mode and other.get("case_id") != axis_id:
                        continue
                    quote = _locate_quote(other.get("text_redacted") or "", raw_quote)
                    if quote:
                        chunk = other
                        break
            if not chunk or not quote:
                continue
            # 证据卡统一短摘录（组织字段表同款）；校验用短串，打开原文再看整段上下文
            value_for_snip = value if len(value) <= 40 else ""
            short_quote = _org_style_quote_snippet(quote, value_for_snip, max_len=40) or quote[:40]
            qhash = quote_hash(short_quote)
            real_case_id = chunk.get("case_id") or ("" if alias_mode else axis_id)
            real_case_name = (
                case_name_map.get(real_case_id)
                or chunk.get("case_name")
                or ("" if alias_mode else compare_name_map.get(axis_id))
                or real_case_id
                or axis_id
            )
            source = {
                "case_id": real_case_id or axis_id,
                "case_name": real_case_name,
                "filename": chunk.get("filename") or "",
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "chunk_id": chunk["chunk_id"],
                "document_version_id": chunk.get("document_version_id") or "",
                "document_id": chunk.get("document_id") or "",
                "version_no": chunk.get("version_no"),
                "ocr_confidence": chunk.get("ocr_confidence"),
                "quote": short_quote,
                "quote_display": _review_display_value(short_quote)[:80],
                "quote_hash": qhash,
                "field_key": field_key,
                "field_label": label,
                "extracted_value": value,
                "producer": FIELD_TABLE_PRODUCER,
                "alias": compare_name_map.get(axis_id) if alias_mode else None,
            }
            matched += 1
            rendered[axis_id] = value
            sources_by_case.setdefault(axis_id, []).append(source)
            evidence_map.setdefault(
                (chunk["chunk_id"], qhash),
                {
                    "case_id": real_case_id or axis_id,
                    "case_name": real_case_name,
                    "chunk_id": chunk["chunk_id"],
                    "document_version_id": chunk.get("document_version_id") or "",
                    "document_id": chunk.get("document_id"),
                    "filename": chunk.get("filename"),
                    "page_start": chunk.get("page_start"),
                    "page_end": chunk.get("page_end"),
                    "version_no": chunk.get("version_no"),
                    "ocr_confidence": chunk.get("ocr_confidence"),
                    "quote": short_quote,
                    "quote_display": source["quote_display"],
                    "quote_hash": qhash,
                    "field_label": label,
                    "value": value,
                },
            )

        status_map = _field_cell_status(compare_ids, rendered)
        field_compare.append(
            {
                "field_key": field_key,
                "label": label,
                "producer": FIELD_TABLE_PRODUCER,
                "per_case": [
                    {
                        "case_id": cid,
                        "case_name": compare_name_map.get(cid) or cid,
                        "value": rendered.get(cid) or None,
                        "status": status_map.get(cid) or "missing",
                        "sources": sources_by_case.get(cid) or [],
                    }
                    for cid in compare_ids
                ],
            }
        )

    # 非别名分列的人物候选：仍强制主姓名=aliases
    if (not alias_mode) and identity_aliases and entity_type == "PERSON":
        preserved_sources: dict[str, list[dict[str, Any]]] = {}
        for ev in cand.get("evidence") or []:
            cid = ev.get("case_id")
            if cid and ev.get("chunk_id"):
                preserved_sources.setdefault(cid, []).append(
                    {
                        "case_id": cid,
                        "case_name": ev.get("case_name") or case_name_map.get(cid) or cid,
                        "filename": ev.get("filename") or "",
                        "page_start": ev.get("page_start"),
                        "page_end": ev.get("page_end"),
                        "chunk_id": ev["chunk_id"],
                        "document_version_id": ev.get("document_version_id") or "",
                        "document_id": ev.get("document_id") or "",
                        "quote": ev.get("quote") or "",
                        "quote_display": ev.get("quote_display")
                        or _review_display_value(ev.get("quote") or ""),
                        "quote_hash": ev.get("quote_hash") or "",
                        "field_key": "name",
                        "field_label": "姓名/称谓",
                        "extracted_value": "、".join(identity_aliases),
                        "producer": "IDENTITY_LOCK",
                    }
                )
        field_compare = _force_primary_identity_row(
            field_compare,
            aliases=identity_aliases,
            case_ids=case_ids,
            case_name_map=case_name_map,
            preserved_sources=preserved_sources,
        )
        # 丢掉「同段其他人员」路人行，不再改名保留
        field_compare = [
            row
            for row in field_compare
            if not banned_label.search(str(row.get("label") or ""))
        ]

    if not any(
        cell.get("value") for row in field_compare for cell in row.get("per_case") or []
    ):
        return _tool_json(
            {
                "ok": False,
                "error": (
                    f"模型给出 {proposed} 个字段值，均无法在材料中逐字命中，已全部丢弃"
                    if proposed
                    else "模型未从材料中摘出任何字段值"
                ),
                "fallback": True,
            }
        )

    supporting: list[str] = []
    conflicts: list[str] = []
    missing_fields: list[str] = []
    for row in field_compare:
        statuses = {cell.get("status") for cell in row["per_case"]}
        label = row["label"]
        if statuses == {"same"}:
            supporting.append(f"{label}一致")
        elif "diff" in statuses:
            conflicts.append(f"{label}记载存在差异")
        elif "partial" in statuses:
            absent = [
                cell.get("case_name") or cell.get("case_id")
                for cell in row["per_case"]
                if cell.get("status") == "missing"
            ]
            missing_fields.append(f"{'、'.join(absent)} 未记载{label}")
        else:
            missing_fields.append(f"各案均未记载{label}")

    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        summary = (
            f"已按材料重建{len(field_compare)}项字段对照："
            + ("；".join((supporting + conflicts)[:3]) or "多数字段材料未记载")
        )
    suggestion = {
        "recommendation": cand.get("recommendation") or "DEFER",
        "agent_summary": summary[:150],
        "supporting_facts": supporting,
        "conflicts": conflicts,
        "missing_fields": missing_fields,
        "field_compare": field_compare,
        "evidence": list(evidence_map.values()),
        "confidence": "MEDIUM",
    }
    try:
        get_task_service().propose_entity_review(
            task_id,
            candidate_id,
            suggestion=suggestion,
            user_id=user_id or "system",
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())

    # 记录产出来源，避免每次打开都重跑模型
    try:
        art, detail = _load_candidate_set(task_id)
        payload = detail.get("payload") or {}
        target = _find_candidate(payload, candidate_id)
        if target is not None:
            target["field_table_meta"] = {
                "producer": FIELD_TABLE_PRODUCER,
                "model": MODEL_NAME,
                "table_title": str(
                    parsed.get("table_title")
                    or (
                        "不同称谓对照（供同一性判断）"
                        if alias_mode
                        else "字段对照与差异说明"
                    )
                ),
                "row_count": len(field_compare),
                "compare_mode": "alias" if alias_mode else "case",
            }
            if field_compare_columns:
                target["field_compare_columns"] = field_compare_columns
            else:
                target.pop("field_compare_columns", None)
            get_task_service().write_artifact(
                task_id=task_id,
                type="ENTITY_CANDIDATE_SET",
                title=art.get("title") or "跨案对象待核·待判断",
                ref_key="entity-candidates",
                status=art.get("status") or "PENDING_REVIEW",
                parent_ids=json.loads(art.get("parent_ids_json") or "[]"),
                payload=payload,
            )
    except Exception:
        pass

    # 表已落盘到候选，这里只回摘要；整表带上 sources 会撑爆工具返回长度
    return _tool_json(
        {
            "ok": True,
            "cached": False,
            "candidate_id": candidate_id,
            "table_title": str(parsed.get("table_title") or "字段对照与差异说明"),
            "fields": [
                {
                    "label": row["label"],
                    "values": {
                        cell.get("case_name") or cell.get("case_id"): cell.get("value")
                        for cell in row["per_case"]
                    },
                }
                for row in field_compare
            ],
            "supporting_facts": supporting,
            "conflicts": conflicts,
            "missing_fields": missing_fields,
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
