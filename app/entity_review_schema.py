"""实体复核数据契约：候选结构、字段白名单、校验与示例。

其他模块必须以本文件为唯一接口标准；禁止自由扩展字段。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EntityType(str, Enum):
    BANK_ACCOUNT = "BANK_ACCOUNT"
    PHONE = "PHONE"
    PERSON = "PERSON"
    DEVICE = "DEVICE"
    ORGANIZATION = "ORGANIZATION"
    MERCHANT = "MERCHANT"
    ID_CARD = "ID_CARD"
    IP = "IP"


class FieldCompareStatus(str, Enum):
    SAME = "same"
    DIFF = "diff"
    MISSING = "missing"


class Recommendation(str, Enum):
    MERGE = "MERGE"
    KEEP_SEPARATE = "KEEP_SEPARATE"
    CORRECT = "CORRECT"
    DEFER = "DEFER"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"


class Decision(str, Enum):
    PENDING = "PENDING"
    MERGE = "MERGE"
    KEEP_SEPARATE = "KEEP_SEPARATE"
    CORRECT = "CORRECT"
    DEFER = "DEFER"


# 内部类型 → 对外展示类型（兼容旧碰撞产物）
INTERNAL_TO_PUBLIC_TYPE = {
    "ACCOUNT": EntityType.BANK_ACCOUNT,
    "BANK_ACCOUNT": EntityType.BANK_ACCOUNT,
    "PHONE": EntityType.PHONE,
    "NAME": EntityType.PERSON,
    "PERSON": EntityType.PERSON,
    "DEVICE": EntityType.DEVICE,
    "ORGANIZATION": EntityType.ORGANIZATION,
    "ORG": EntityType.ORGANIZATION,
    "MERCHANT": EntityType.MERCHANT,
    "ID_CARD": EntityType.ID_CARD,
    "IP": EntityType.IP,
}

ENTITY_TYPE_LABELS = {
    EntityType.BANK_ACCOUNT: "银行账户",
    EntityType.PHONE: "手机号码",
    EntityType.PERSON: "人物",
    EntityType.DEVICE: "电子设备",
    EntityType.ORGANIZATION: "组织主体",
    EntityType.MERCHANT: "商户",
    EntityType.ID_CARD: "身份证件",
    EntityType.IP: "网络地址",
}

# 各实体类型允许出现在 field_compare 中的字段键
FIELD_WHITELIST: dict[EntityType, frozenset[str]] = {
    EntityType.BANK_ACCOUNT: frozenset(
        {"account_no", "holder_name", "bank_name", "reserved_phone", "merchant"}
    ),
    EntityType.PHONE: frozenset(
        {"phone_no", "registrant", "linked_account", "linked_device", "contact_context"}
    ),
    EntityType.PERSON: frozenset(
        {"name", "id_card", "phone", "account", "organization", "role_in_material"}
    ),
    EntityType.DEVICE: frozenset(
        {"device_id", "linked_phone", "linked_account", "linked_person", "login_time"}
    ),
    EntityType.ORGANIZATION: frozenset(
        {"org_name", "credit_code", "legal_person", "address", "phone", "account"}
    ),
    EntityType.MERCHANT: frozenset(
        {"merchant_id", "merchant_name", "settle_account", "pay_channel", "linked_org"}
    ),
    EntityType.ID_CARD: frozenset({"id_no", "name", "address"}),
    EntityType.IP: frozenset({"ip_address", "linked_account", "linked_device"}),
}

FIELD_LABELS: dict[str, str] = {
    "account_no": "账号",
    "holder_name": "开户名",
    "bank_name": "开户行",
    "reserved_phone": "预留电话",
    "merchant": "关联商户",
    "phone_no": "号码",
    "registrant": "登记人",
    "linked_account": "关联账户",
    "linked_device": "关联设备",
    "contact_context": "联络语境",
    "name": "姓名",
    "id_card": "证件",
    "phone": "手机号",
    "account": "账户",
    "organization": "组织",
    "role_in_material": "材料记载角色",
    "device_id": "设备号",
    "linked_phone": "关联手机号",
    "linked_person": "关联人员",
    "login_time": "登录时间",
    "org_name": "名称",
    "credit_code": "统一社会信用代码",
    "legal_person": "法人",
    "address": "地址",
    "merchant_id": "商户号",
    "merchant_name": "商户名称",
    "settle_account": "结算账户",
    "pay_channel": "支付通道",
    "linked_org": "关联组织",
    "id_no": "证件号",
    "ip_address": "IP 地址",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class CaseRef(StrictModel):
    case_id: str
    case_name: str = ""


class EvidenceRef(StrictModel):
    case_id: str
    case_name: str = ""
    chunk_id: str
    document_version_id: str
    document_id: str | None = None
    filename: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    quote: str
    quote_hash: str

    @field_validator("quote_hash")
    @classmethod
    def _hash_required(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("quote_hash 必填")
        return value.strip()

    @field_validator("chunk_id", "document_version_id", "quote")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("证据定位字段不能为空")
        return value.strip()


class FieldCaseValue(StrictModel):
    case_id: str
    case_name: str = ""
    value: str | None = None
    status: FieldCompareStatus


class FieldCompareRow(StrictModel):
    field_key: str
    label: str
    per_case: list[FieldCaseValue] = Field(min_length=1)
    citation: EvidenceRef | None = None


class ImpactStats(StrictModel):
    case_count: int = 0
    relation_count: int = 0
    event_count: int = 0
    clue_count: int = 0
    mention_count: int = 0


class GeneratedClueRef(StrictModel):
    artifact_id: str
    title: str = ""
    status: str = "DRAFT"


class EntityCandidate(StrictModel):
    """实体复核页消费的唯一候选结构。"""

    candidate_id: str
    fingerprint: str
    entity_type: EntityType
    display_name: str
    cases: list[CaseRef] = Field(min_length=2)
    field_compare: list[FieldCompareRow] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(min_length=1)
    supporting_facts: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    impact: ImpactStats = Field(default_factory=ImpactStats)
    agent_summary: str = ""
    recommendation: Recommendation = Recommendation.DEFER
    generated_clues: list[GeneratedClueRef] = Field(default_factory=list)
    decision: Decision = Decision.PENDING
    reason: str = ""
    confidence_label: str = "待核验"
    match_basis: list[str] = Field(default_factory=list)
    question: str = ""

    @field_validator("agent_summary")
    @classmethod
    def _summary_limit(cls, value: str) -> str:
        text = (value or "").strip()
        if len(text) > 150:
            raise ValueError("agent_summary 不得超过 150 字")
        return text

    @model_validator(mode="after")
    def _check_whitelist_and_cases(self) -> "EntityCandidate":
        allowed = FIELD_WHITELIST[EntityType(self.entity_type)]
        for row in self.field_compare:
            if row.field_key not in allowed:
                raise ValueError(
                    f"实体类型 {self.entity_type} 不允许字段 {row.field_key}"
                )
        case_ids = {c.case_id for c in self.cases}
        if len(case_ids) < 2:
            raise ValueError("候选必须覆盖至少两起不同案件")
        evidence_cases = {e.case_id for e in self.evidence}
        if len(evidence_cases) < 2:
            raise ValueError("每起案件至少需要一条证据（合计至少两案）")
        missing = case_ids - evidence_cases
        if missing:
            raise ValueError(f"以下案件缺少证据：{sorted(missing)}")
        if self.impact.case_count == 0:
            self.impact.case_count = len(case_ids)
        return self


class EntityCandidateSetPayload(StrictModel):
    summary: dict[str, Any] = Field(default_factory=dict)
    candidates: list[EntityCandidate] = Field(default_factory=list)
    boundary: str = (
        "强标识等值仅为待核验候选。系统不自动合并，是否同一对象由人工决定。"
    )


def normalize_entity_type(raw: str | None) -> EntityType:
    key = (raw or "").strip().upper()
    if key in INTERNAL_TO_PUBLIC_TYPE:
        return INTERNAL_TO_PUBLIC_TYPE[key]
    raise ValueError(f"非法实体类型：{raw}")


def validate_candidate(data: dict[str, Any]) -> EntityCandidate:
    """校验并返回严格候选；失败抛出 ValidationError。"""
    payload = dict(data)
    if "entity_type" in payload:
        payload["entity_type"] = normalize_entity_type(str(payload["entity_type"])).value
    return EntityCandidate.model_validate(payload)


def bank_account_example() -> dict[str, Any]:
    """完整银行账户示例，供前端与测试使用。"""
    return {
        "candidate_id": "cand-bank-6231",
        "fingerprint": "fp-account-6231-ab",
        "entity_type": "BANK_ACCOUNT",
        "display_name": "尾号 6231 银行账户",
        "cases": [
            {"case_id": "case-a", "case_name": "案件 A"},
            {"case_id": "case-b", "case_name": "案件 B"},
        ],
        "field_compare": [
            {
                "field_key": "account_no",
                "label": "账号",
                "per_case": [
                    {
                        "case_id": "case-a",
                        "case_name": "案件 A",
                        "value": "6222********6231",
                        "status": "same",
                    },
                    {
                        "case_id": "case-b",
                        "case_name": "案件 B",
                        "value": "6222********6231",
                        "status": "same",
                    },
                ],
            },
            {
                "field_key": "holder_name",
                "label": "开户名",
                "per_case": [
                    {
                        "case_id": "case-a",
                        "case_name": "案件 A",
                        "value": "王某某",
                        "status": "diff",
                    },
                    {
                        "case_id": "case-b",
                        "case_name": "案件 B",
                        "value": "王某伟",
                        "status": "diff",
                    },
                ],
            },
            {
                "field_key": "bank_name",
                "label": "开户行",
                "per_case": [
                    {
                        "case_id": "case-a",
                        "case_name": "案件 A",
                        "value": "某银行",
                        "status": "same",
                    },
                    {
                        "case_id": "case-b",
                        "case_name": "案件 B",
                        "value": "某银行",
                        "status": "same",
                    },
                ],
            },
            {
                "field_key": "reserved_phone",
                "label": "预留电话",
                "per_case": [
                    {
                        "case_id": "case-a",
                        "case_name": "案件 A",
                        "value": "138****5678",
                        "status": "missing",
                    },
                    {
                        "case_id": "case-b",
                        "case_name": "案件 B",
                        "value": None,
                        "status": "missing",
                    },
                ],
            },
        ],
        "evidence": [
            {
                "case_id": "case-a",
                "case_name": "案件 A",
                "chunk_id": "chunk-a-1",
                "document_version_id": "ver-a-1",
                "filename": "bank_flow_icbc.xlsx",
                "page_start": 1,
                "quote": "账户 6222********6231 于 2026 年 1 月 12 日收款 48000 元。",
                "quote_hash": "a" * 64,
            },
            {
                "case_id": "case-b",
                "case_name": "案件 B",
                "chunk_id": "chunk-b-1",
                "document_version_id": "ver-b-1",
                "filename": "起诉意见书.pdf",
                "page_start": 3,
                "quote": "涉案账户尾号 6231 与支付接口资金流向相关。",
                "quote_hash": "b" * 64,
            },
        ],
        "supporting_facts": ["账号一致", "开户行一致"],
        "conflicts": ["开户名不一致：王某某 vs 王某伟"],
        "missing_fields": ["案件 B 未记载预留电话"],
        "impact": {
            "case_count": 2,
            "relation_count": 4,
            "event_count": 3,
            "clue_count": 2,
            "mention_count": 4,
        },
        "agent_summary": "两案出现同一账号，开户行一致但开户名存在差异，建议人工核验是否同一主体。",
        "recommendation": "DEFER",
        "generated_clues": [
            {"artifact_id": "clue-1", "title": "同一银行账户跨案出现", "status": "DRAFT"}
        ],
        "decision": "PENDING",
        "confidence_label": "待核验",
        "match_basis": ["规范化账号在两起案件中等值出现"],
        "question": "请核验：两案中该银行账户是否指向同一主体？",
    }


def example_candidates_by_type() -> dict[str, dict[str, Any]]:
    """五类最小可过校验示例。"""
    bank = bank_account_example()

    def _base(entity_type: str, display: str, field_key: str, label: str, values: list[str]):
        return {
            "candidate_id": f"cand-{entity_type.lower()}",
            "fingerprint": f"fp-{entity_type.lower()}",
            "entity_type": entity_type,
            "display_name": display,
            "cases": [
                {"case_id": "case-a", "case_name": "案件 A"},
                {"case_id": "case-b", "case_name": "案件 B"},
            ],
            "field_compare": [
                {
                    "field_key": field_key,
                    "label": label,
                    "per_case": [
                        {
                            "case_id": "case-a",
                            "case_name": "案件 A",
                            "value": values[0],
                            "status": "same",
                        },
                        {
                            "case_id": "case-b",
                            "case_name": "案件 B",
                            "value": values[1],
                            "status": "same",
                        },
                    ],
                }
            ],
            "evidence": bank["evidence"],
            "supporting_facts": [f"{label}一致"],
            "conflicts": [],
            "missing_fields": [],
            "impact": {"case_count": 2, "relation_count": 1, "event_count": 0, "clue_count": 0},
            "agent_summary": f"{display}在两案中出现，待人工核验。",
            "recommendation": "DEFER",
            "decision": "PENDING",
        }

    return {
        "BANK_ACCOUNT": bank,
        "PHONE": _base("PHONE", "手机号 138****5678", "phone_no", "号码", ["138****5678", "138****5678"]),
        "PERSON": _base("PERSON", "王某某", "name", "姓名", ["王某某", "王某某"]),
        "DEVICE": _base("DEVICE", "设备尾号 7742", "device_id", "设备号", ["****7742", "****7742"]),
        "ORGANIZATION": _base(
            "ORGANIZATION", "某某科技工作室", "org_name", "名称", ["某某科技工作室", "某某科技工作室"]
        ),
    }
