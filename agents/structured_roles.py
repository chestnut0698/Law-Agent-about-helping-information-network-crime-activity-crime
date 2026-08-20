"""四个结构化智能体角色：抽取、归一说明、线索表述、输出校核。

每个角色只接收最小 ContextEnvelope，输入输出均为 Schema 对象。
模型响应先校验再交给任务落库；证据 quote_hash 必须能在对应脱敏 chunk 中反向匹配。
校核角色独立运行，只判定不改写，不读取前序模型隐性推理。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agents.gateway import (
    GATEWAY_ERROR_CODES,
    GatewayError,
    GatewayResult,
    canonical_hash,
    get_gateway,
    quote_hash,
)
from app.config import PROMPT_VERSIONS

# ---------- 边界词与上下文信封 ----------

FORBIDDEN_CLAIMS = (
    "是同一人",
    "为同一人",
    "系同一人",
    "同一犯罪嫌疑人",
    "构成犯罪",
    "应当并案",
    "建议并案",
    "应予并案",
    "系主犯",
    "是主犯",
    "建议量刑",
    "应当量刑",
    "建议逮捕",
    "应予逮捕",
    "移送起诉",
)


class ContextEnvelope(BaseModel):
    """按角色裁剪的最小上下文，禁止塞入整卷、会话历史或前序推理。"""

    model_config = ConfigDict(extra="forbid")

    purpose: str
    prompt_version: str
    task_id: str | None = None
    approval_id: str | None = None
    chunk_ids: list[str] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    quote: str
    quote_hash: str
    page_start: int | None = None
    page_end: int | None = None


class ExtractedObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: Literal[
        "PERSON", "ACCOUNT", "PHONE", "DEVICE", "IP", "ORG", "BEHAVIOR", "ROLE_EVENT"
    ]
    surface: str
    attributes: dict[str, str] = Field(default_factory=dict)
    evidence: list[EvidenceRef]
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence")
    @classmethod
    def _need_evidence(cls, value: list[EvidenceRef]) -> list[EvidenceRef]:
        if not value:
            raise ValueError("each extracted object needs at least one evidence")
        return value


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objects: list[ExtractedObject] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class NormalizationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consistencies: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    questions_for_human: list[str] = Field(default_factory=list)


class ClueWordingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str
    cited_chunk_ids: list[str] = Field(default_factory=list)


class VerifyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    over_bound: bool
    candidate_as_fact: bool
    missing_reverse: bool
    issues: list[str] = Field(default_factory=list)


# ---------- 版本化提示词 ----------

EXTRACTION_PROMPT = """你是抽取智能体。只从用户给出的脱敏 chunk 中抽取材料已经写明的对象。
只抽写了的内容，禁止推理、禁止补全、禁止把相似对象写成同一人。
对象类型仅限 PERSON/ACCOUNT/PHONE/DEVICE/IP/ORG/BEHAVIOR/ROLE_EVENT。
每个对象至少一条 evidence：quote 必须是对应 chunk 文本的连续子串，quote_hash 为该 quote 的 SHA-256 十六进制。
confidence 仅用于排序，取值 0 到 1。
禁止输出定罪、并案、主从犯、量刑或“是同一人”等结论。
只返回 JSON：{"objects":[...],"notes":[...]}"""

NORMALIZATION_PROMPT = """你是归一说明智能体。比较两个实体的属性与出处，列出一致项、差异项、需要人工核对的问题。
禁止给出是否同一人的结论，禁止建议合并或拆分。
禁止输出定罪、并案、主从犯、量刑表述。
只返回 JSON：{"consistencies":[],"differences":[],"questions_for_human":[]}"""

CLUE_PROMPT = """你是线索表述智能体。仅根据已成立的规则命中与既有证据，套用受控模板生成线索标题与摘要。
标题必须停留在“疑似……（待核验）”层级。
禁止新增事实，禁止新增引用；cited_chunk_ids 只能来自输入证据。
禁止输出定罪、并案、主从犯、量刑或把候选写成事实。
只返回 JSON：{"title":"","summary":"","cited_chunk_ids":[]}"""

VERIFY_PROMPT = """你是输出校核智能体。独立判定待发布线索是否越界、是否把候选写成事实、是否遗漏反向材料。
只判定，不改写，不补充新表述。
只返回 JSON：{"passed":true,"over_bound":false,"candidate_as_fact":false,"missing_reverse":false,"issues":[]}"""


# ---------- 校验 ----------

def collect_forbidden_hits(payload: Any) -> list[str]:
    hits: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            for phrase in FORBIDDEN_CLAIMS:
                if phrase in value:
                    hits.append(phrase)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return sorted(set(hits))


def match_quote(chunk_text: str, quote: str, expected_hash: str) -> bool:
    if not quote or quote not in (chunk_text or ""):
        return False
    return quote_hash(quote) == expected_hash


def _chunk_map(chunks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["chunk_id"]: item for item in chunks if item.get("chunk_id")}


def _assert_boundary(payload: Any, *, run_id: str, purpose: str, prompt_version: str, input_hash: str):
    hits = collect_forbidden_hits(payload)
    if hits:
        get_gateway().mark_logic_failure(
            run_id,
            purpose=purpose,
            prompt_version=prompt_version,
            input_hash=input_hash,
            failure_class="BOUNDARY",
            message="forbidden claim: " + ",".join(hits),
        )
        raise GatewayError(
            GATEWAY_ERROR_CODES["BOUNDARY"],
            "output contains forbidden claims",
            run_id=run_id,
            details={"phrases": hits},
        )


def _parse_role(model: type[BaseModel], parsed: Any, result: GatewayResult) -> BaseModel:
    try:
        return model.model_validate(parsed)
    except ValidationError as exc:
        get_gateway().mark_logic_failure(
            result.run_id,
            purpose=result.purpose,
            prompt_version=result.prompt_version,
            input_hash=result.input_hash,
            failure_class="SCHEMA",
            message="schema validation failed",
            raw=result.content,
        )
        raise GatewayError(
            GATEWAY_ERROR_CODES["SCHEMA"],
            "schema validation failed",
            run_id=result.run_id,
            details={"errors": exc.errors()},
        ) from exc


def _complete_role(
    *,
    purpose: str,
    prompt_version: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    input_payload: dict[str, Any],
    approval_id: str | None,
    simulate: str | None = None,
    fake_content: str | None = None,
) -> GatewayResult:
    envelope = ContextEnvelope(
        purpose=purpose,
        prompt_version=prompt_version,
        task_id=(input_payload.get("task_id") if isinstance(input_payload, dict) else None),
        approval_id=approval_id,
        chunk_ids=list(input_payload.get("chunk_ids") or [])
        if isinstance(input_payload, dict)
        else [],
        fields=user_payload,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(envelope.fields, ensure_ascii=False)},
    ]
    return get_gateway().complete(
        purpose=purpose,
        prompt_version=prompt_version,
        messages=messages,
        input_payload=input_payload,
        approval_id=approval_id,
        expect_json=True,
        simulate=simulate,
        fake_content=fake_content,
    )


# ---------- 角色执行器 ----------

def run_extraction(
    chunks: list[dict[str, Any]],
    *,
    task_id: str | None = None,
    approval_id: str | None = None,
    simulate: str | None = None,
    fake_content: str | None = None,
) -> dict[str, Any]:
    compact = [
        {
            "chunk_id": item["chunk_id"],
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "text": item.get("text") or "",
        }
        for item in chunks
    ]
    input_payload = {
        "task_id": task_id,
        "chunk_ids": [item["chunk_id"] for item in compact],
        "text_hashes": [canonical_hash(item["text"]) for item in compact],
        "chunks": compact,
    }
    result = _complete_role(
        purpose="extraction",
        prompt_version=PROMPT_VERSIONS["extraction"],
        system_prompt=EXTRACTION_PROMPT,
        user_payload={"chunks": compact},
        input_payload=input_payload,
        approval_id=approval_id or (f"task:{task_id}" if task_id else None),
        simulate=simulate,
        fake_content=fake_content,
    )
    if result.degraded:
        return {"degraded": True, "run_id": result.run_id, "output": ExtractionOutput().model_dump()}

    output = _parse_role(ExtractionOutput, result.parsed, result)
    lookup = _chunk_map(compact)
    invalid = []
    for obj in output.objects:
        for ev in obj.evidence:
            chunk = lookup.get(ev.chunk_id)
            if chunk is None or not match_quote(chunk.get("text") or "", ev.quote, ev.quote_hash):
                invalid.append(ev.chunk_id)
    if invalid:
        get_gateway().mark_logic_failure(
            result.run_id,
            purpose=result.purpose,
            prompt_version=result.prompt_version,
            input_hash=result.input_hash,
            failure_class="EVIDENCE",
            message="quote_hash mismatch",
            raw=result.content,
        )
        raise GatewayError(
            GATEWAY_ERROR_CODES["EVIDENCE"],
            "quote_hash did not reverse-match chunk",
            run_id=result.run_id,
            details={"chunk_ids": invalid},
        )
    _assert_boundary(
        output.model_dump(),
        run_id=result.run_id,
        purpose=result.purpose,
        prompt_version=result.prompt_version,
        input_hash=result.input_hash,
    )
    return {
        "degraded": False,
        "run_id": result.run_id,
        "reused": result.reused,
        "output": output.model_dump(),
    }


def run_normalization(
    entity_a: dict[str, Any],
    entity_b: dict[str, Any],
    *,
    task_id: str | None = None,
    approval_id: str | None = None,
    simulate: str | None = None,
    fake_content: str | None = None,
) -> dict[str, Any]:
    input_payload = {
        "task_id": task_id,
        "entity_a": _strip_entity(entity_a),
        "entity_b": _strip_entity(entity_b),
    }
    result = _complete_role(
        purpose="normalization",
        prompt_version=PROMPT_VERSIONS["normalization"],
        system_prompt=NORMALIZATION_PROMPT,
        user_payload=input_payload,
        input_payload=input_payload,
        approval_id=approval_id or (f"task:{task_id}" if task_id else None),
        simulate=simulate,
        fake_content=fake_content,
    )
    if result.degraded:
        return {
            "degraded": True,
            "run_id": result.run_id,
            "output": NormalizationOutput(
                questions_for_human=["外呼关闭，仅保留人工核对。"]
            ).model_dump(),
        }
    output = _parse_role(NormalizationOutput, result.parsed, result)
    dumped = output.model_dump()
    _assert_boundary(
        dumped,
        run_id=result.run_id,
        purpose=result.purpose,
        prompt_version=result.prompt_version,
        input_hash=result.input_hash,
    )
    conclusion_like = re.compile(r"(同一人|应当合并|建议合并|可认定为)")
    joined = " ".join(
        dumped["consistencies"] + dumped["differences"] + dumped["questions_for_human"]
    )
    if conclusion_like.search(joined):
        get_gateway().mark_logic_failure(
            result.run_id,
            purpose=result.purpose,
            prompt_version=result.prompt_version,
            input_hash=result.input_hash,
            failure_class="BOUNDARY",
            message="normalization attempted a same-person conclusion",
            raw=result.content,
        )
        raise GatewayError(
            GATEWAY_ERROR_CODES["BOUNDARY"],
            "normalization must not conclude identity",
            run_id=result.run_id,
        )
    return {"degraded": False, "run_id": result.run_id, "reused": result.reused, "output": dumped}


def run_clue_wording(
    rule_hits: list[dict[str, Any]],
    *,
    task_id: str | None = None,
    approval_id: str | None = None,
    simulate: str | None = None,
    fake_content: str | None = None,
) -> dict[str, Any]:
    allowed: list[str] = []
    compact_hits = []
    for hit in rule_hits:
        evidence = []
        for ev in hit.get("evidence") or []:
            if ev.get("chunk_id"):
                allowed.append(ev["chunk_id"])
                evidence.append(
                    {
                        "chunk_id": ev["chunk_id"],
                        "quote": ev.get("quote"),
                        "quote_hash": ev.get("quote_hash"),
                    }
                )
        compact_hits.append(
            {
                "rule_id": hit.get("rule_id"),
                "label": hit.get("label"),
                "evidence": evidence,
            }
        )
    input_payload = {"task_id": task_id, "rule_hits": compact_hits, "chunk_ids": allowed}
    result = _complete_role(
        purpose="clue_wording",
        prompt_version=PROMPT_VERSIONS["clue_wording"],
        system_prompt=CLUE_PROMPT,
        user_payload={"rule_hits": compact_hits},
        input_payload=input_payload,
        approval_id=approval_id or (f"task:{task_id}" if task_id else None),
        simulate=simulate,
        fake_content=fake_content,
    )
    if result.degraded:
        return {
            "degraded": True,
            "run_id": result.run_id,
            "output": ClueWordingOutput(
                title="疑似跨案标识重合（待核验）",
                summary="当前为仅确定性规则模式，线索表述未调用模型。",
                cited_chunk_ids=allowed,
            ).model_dump(),
        }
    output = _parse_role(ClueWordingOutput, result.parsed, result)
    extra = [cid for cid in output.cited_chunk_ids if cid not in set(allowed)]
    if extra:
        get_gateway().mark_logic_failure(
            result.run_id,
            purpose=result.purpose,
            prompt_version=result.prompt_version,
            input_hash=result.input_hash,
            failure_class="EVIDENCE",
            message="clue wording added citations",
            raw=result.content,
        )
        raise GatewayError(
            GATEWAY_ERROR_CODES["EVIDENCE"],
            "clue wording must not add citations",
            run_id=result.run_id,
            details={"extra_chunk_ids": extra},
        )
    _assert_boundary(
        output.model_dump(),
        run_id=result.run_id,
        purpose=result.purpose,
        prompt_version=result.prompt_version,
        input_hash=result.input_hash,
    )
    return {
        "degraded": False,
        "run_id": result.run_id,
        "reused": result.reused,
        "output": output.model_dump(),
    }


def run_output_verify(
    clue_text: str,
    evidence: list[dict[str, Any]],
    reverse_materials: list[dict[str, Any]] | None = None,
    *,
    task_id: str | None = None,
    approval_id: str | None = None,
    simulate: str | None = None,
    fake_content: str | None = None,
) -> dict[str, Any]:
    compact_evidence = [
        {
            "chunk_id": item.get("chunk_id"),
            "quote_hash": item.get("quote_hash"),
        }
        for item in evidence
        if item.get("chunk_id")
    ]
    compact_reverse = [
        {"id": item.get("id"), "note": item.get("note")}
        for item in (reverse_materials or [])
    ]
    user_payload = {
        "clue_text": clue_text,
        "evidence": compact_evidence,
        "reverse_materials": compact_reverse,
    }
    input_payload = {
        "task_id": task_id,
        "chunk_ids": [item["chunk_id"] for item in compact_evidence],
        "clue_hash": canonical_hash(clue_text),
        "evidence": compact_evidence,
        "reverse_materials": compact_reverse,
    }
    result = _complete_role(
        purpose="output_verify",
        prompt_version=PROMPT_VERSIONS["output_verify"],
        system_prompt=VERIFY_PROMPT,
        user_payload=user_payload,
        input_payload=input_payload,
        approval_id=approval_id or (f"task:{task_id}" if task_id else None),
        simulate=simulate,
        fake_content=fake_content,
    )
    if result.degraded:
        local_hits = collect_forbidden_hits(clue_text)
        passed = not local_hits
        return {
            "degraded": True,
            "run_id": result.run_id,
            "output": VerifyOutput(
                passed=passed,
                over_bound=bool(local_hits),
                candidate_as_fact=bool(local_hits),
                missing_reverse=not compact_reverse,
                issues=[f"forbidden:{p}" for p in local_hits],
            ).model_dump(),
        }
    output = _parse_role(VerifyOutput, result.parsed, result)
    dumped = output.model_dump()
    local_hits = collect_forbidden_hits(clue_text)
    if local_hits:
        dumped["passed"] = False
        dumped["over_bound"] = True
        dumped["issues"] = list(
            dict.fromkeys(dumped["issues"] + [f"forbidden:{p}" for p in local_hits])
        )
    return {
        "degraded": False,
        "run_id": result.run_id,
        "reused": result.reused,
        "output": dumped,
    }


def _strip_entity(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": entity.get("entity_type") or entity.get("object_type"),
        "surface": entity.get("surface") or entity.get("display_name"),
        "attributes": entity.get("attributes") or {},
        "sources": [
            {
                "chunk_id": item.get("chunk_id"),
                "quote_hash": item.get("quote_hash"),
                "case_id": item.get("case_id"),
            }
            for item in entity.get("records") or entity.get("sources") or []
            if item.get("chunk_id")
        ],
    }
