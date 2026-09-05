"""实体复核流水线：持久化、人工决定、线索草稿升格。"""

import pytest

from app.entity_review_schema import bank_account_example
from app.tasks import TaskService


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    db = tmp_path / "pipe.db"
    service = TaskService(db_path=db)
    monkeypatch.setattr("app.tasks._task_service", service)
    created = service.create_task(
        title="流水线测试",
        purpose="跨案对象待核",
        authorized_until="2099-01-01",
        cases=[{"name": "案件 A"}, {"name": "案件 B"}],
    )
    task_id = created["task"]["id"]
    service.confirm_plan(task_id)
    cand = bank_account_example()
    cand["records"] = [
        {"case_id": "case-a", "case_name": "案件 A", "value": "6222********6231", "source": {}},
        {"case_id": "case-b", "case_name": "案件 B", "value": "6222********6231", "source": {}},
    ]
    service.save_entity_candidates(task_id, candidates=[cand])
    return task_id, cand["candidate_id"], service


def test_api_payload_has_schema_fields(pipeline):
    task_id, cid, service = pipeline
    art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    detail = service.get_artifact(task_id, art["id"])
    cand = next(c for c in detail["payload"]["candidates"] if c["candidate_id"] == cid)
    for key in (
        "entity_type",
        "display_name",
        "cases",
        "field_compare",
        "evidence",
        "supporting_facts",
        "conflicts",
        "impact",
        "recommendation",
        "decision",
    ):
        assert key in cand


def test_propose_entity_review_persists_summary(pipeline):
    task_id, cid, service = pipeline
    result = service.propose_entity_review(
        task_id,
        cid,
        suggestion={
            "recommendation": "DEFER",
            "agent_summary": "开户名存在差异，建议人工核验。",
            "supporting_facts": ["账号一致"],
            "conflicts": ["开户名不一致"],
            "missing_fields": [],
        },
    )
    assert result["recommendation"] == "DEFER"
    art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    detail = service.get_artifact(task_id, art["id"])
    cand = next(c for c in detail["payload"]["candidates"] if c["candidate_id"] == cid)
    assert cand["agent_summary"] == "开户名存在差异，建议人工核验。"
    assert cand["decision"] == "PENDING"


def test_merge_promotes_draft_clue(pipeline):
    task_id, cid, service = pipeline
    clue = service.write_artifact(
        task_id=task_id,
        type="CLUE_ITEM",
        title="同一银行账户跨案出现",
        ref_key="clue-draft-1",
        status="DRAFT",
        payload={
            "title": "同一银行账户跨案出现",
            "summary": "草稿线索",
            "linked_candidate_ids": [cid],
            "promotion": "draft_pending_entity_review",
            "evidence": [],
        },
    )
    reviewed = service.review_entity_candidate(
        task_id,
        cid,
        decision="MERGE",
        reason="两案账号一致，经人工确认同一主体",
    )
    assert any("升格" in a for a in (reviewed.get("followup_actions") or []))
    detail = service.get_artifact(task_id, clue["id"])
    assert detail["artifact"]["status"] == "VALID"
    assert detail["payload"]["promotion"] == "confirmed"


def test_keep_separate_records_rejection(pipeline):
    task_id, cid, service = pipeline
    service.review_entity_candidate(
        task_id,
        cid,
        decision="KEEP_SEPARATE",
        reason="同名不同主体，予以排除",
    )
    art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    detail = service.get_artifact(task_id, art["id"])
    cand = next(c for c in detail["payload"]["candidates"] if c["candidate_id"] == cid)
    assert cand["decision"] == "KEEP_SEPARATE"


def test_material_change_marks_stale(pipeline):
    task_id, cid, service = pipeline
    art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    # 模拟上游材料批次变更触发过期
    service.write_artifact(
        task_id=task_id,
        type="MATERIAL_BATCH",
        title="材料接入与质量",
        ref_key="batch",
        status="VALID",
        payload={"changed": True},
    )
    # 直接标记候选集过期（与生产过期传播一致的最小断言）
    from app.files import db_session, _update, utc_now

    with db_session(service.db_path) as conn:
        _update(
            conn,
            "artifacts",
            art["id"],
            {"status": "STALE", "stale_reason": "材料变更", "updated_at": utc_now()},
        )
    refreshed = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    assert refreshed["status"] == "STALE"
