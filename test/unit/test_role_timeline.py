"""角色时间线：主体类型、source_mode、增强契约。"""

from agents.timeline_enrich_agent import apply_timeline_enrichment
from tools.entities import (
    classify_timeline_subject_kind,
    pick_timeline_subject_refs,
    timeline_event_sort_key,
)


def test_classify_subject_kind_person_account_only():
    assert classify_timeline_subject_kind("NAME") == "PERSON"
    assert classify_timeline_subject_kind("PERSON") == "PERSON"
    assert classify_timeline_subject_kind("ACCOUNT") == "ACCOUNT"
    assert classify_timeline_subject_kind("ORGANIZATION") is None
    assert classify_timeline_subject_kind("MERCHANT") is None
    assert classify_timeline_subject_kind("CASE") is None
    assert classify_timeline_subject_kind(None) is None


def test_pick_timeline_subject_never_case():
    refs = pick_timeline_subject_refs(
        [
            {
                "object_type": "ORGANIZATION",
                "surface": "某商户",
                "display_name": "某商户",
                "subject_id": "auto:ORG:1",
            },
            {
                "object_type": "NAME",
                "surface": "张某",
                "display_name": "张某",
                "subject_id": "auto:NAME:张某",
            },
            {
                "object_type": "ACCOUNT",
                "surface": "6222",
                "display_name": "尾号 6222 账户",
                "subject_id": "auto:ACCOUNT:6222",
            },
        ]
    )
    assert refs["person"]["subject_id"] == "auto:NAME:张某"
    assert refs["account"]["subject_id"] == "auto:ACCOUNT:6222"
    assert refs["objects"][0]["object_type"] == "ORGANIZATION"


def test_pick_account_only_when_no_person():
    refs = pick_timeline_subject_refs(
        [
            {
                "object_type": "ACCOUNT",
                "surface": "62220001",
                "display_name": "尾号 0001 账户",
                "subject_id": "auto:ACCOUNT:62220001",
            }
        ]
    )
    assert refs["person"] is None
    assert refs["account"]["subject_id"] == "auto:ACCOUNT:62220001"


def test_sort_puts_uncertain_last_not_inferred_first():
    items = [
        {"event_id": "a", "event_time": "", "time_uncertain": True, "source_mode": "recorded"},
        {"event_id": "b", "event_time": "2024-01-12", "time_uncertain": False, "source_mode": "recorded"},
        {
            "event_id": "c",
            "event_time": "2024-02-01",
            "time_uncertain": False,
            "source_mode": "inferred",
        },
        {"event_id": "d", "event_time": "2024-01-12", "time_uncertain": False, "source_mode": "recorded"},
    ]
    ordered = sorted(items, key=timeline_event_sort_key)
    assert [x["event_id"] for x in ordered] == ["b", "d", "c", "a"]


def test_enrich_applies_role_and_rejects_unrooted_infer():
    items = [
        {
            "event_id": "e1",
            "time_text": "2024-01-12",
            "event_time": "2024-01-12",
            "time_uncertain": False,
            "subject_id": "s1",
            "subject": "张某",
            "subject_kind": "PERSON",
            "case_name": "案件甲",
            "cases": ["案件甲"],
            "source_mode": "recorded",
            "role_or_action": "转账记载",
            "summary_text": "转账记载",
            "source": {"quote": "摘录", "filename": "a.pdf"},
        }
    ]
    parsed = {
        "enrichments": [
            {
                "event_id": "e1",
                "role_or_action": "材料记载向下游转账",
                "conflict_with": ["时间记载不一致"],
            }
        ],
        "inferred_nodes": [
            {
                "based_on_event_ids": ["e1"],
                "role_or_action": "资金可能经该账户向下游转移",
                "time_text": "2024-01-12",
            },
            {
                "based_on_event_ids": ["missing"],
                "role_or_action": "系统推测：无源",
            },
        ],
    }
    merged, meta = apply_timeline_enrichment(items, parsed)
    assert meta["enriched_count"] >= 1
    assert meta["inferred_count"] == 1
    assert meta["rejected_inferred"] == 1
    fact = next(x for x in merged if x["event_id"] == "e1")
    assert fact["source_mode"] == "recorded"
    assert fact["role_or_action"] == "材料记载向下游转账"
    assert fact["conflict_with"]
    inferred = [x for x in merged if x.get("source_mode") == "inferred"]
    assert len(inferred) == 1
    assert inferred[0]["role_or_action"].startswith("系统推测")
    assert inferred[0]["based_on_event_ids"] == ["e1"]


def test_enrich_rejects_invented_time():
    items = [
        {
            "event_id": "e1",
            "time_text": "2024-01-12",
            "event_time": "2024-01-12",
            "subject_id": "s1",
            "subject": "李某",
            "subject_kind": "PERSON",
            "case_name": "案件乙",
            "cases": ["案件乙"],
            "source_mode": "recorded",
            "source": {},
        }
    ]
    parsed = {
        "enrichments": [],
        "inferred_nodes": [
            {
                "based_on_event_ids": ["e1"],
                "role_or_action": "系统推测：可能继续转移",
                "time_text": "2099-12-31",
            }
        ],
    }
    merged, _ = apply_timeline_enrichment(items, parsed)
    inferred = next(x for x in merged if x.get("source_mode") == "inferred")
    assert inferred["time_text"] == "2024-01-12"
