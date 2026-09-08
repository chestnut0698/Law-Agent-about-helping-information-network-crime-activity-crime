"""实体复核窄粒度工具测试。"""

import json

import pytest

from app.entity_review_schema import bank_account_example
from app.tasks import TaskService
from tools.entity_review import (
    build_candidate_field_table,
    compare_candidate_fields,
    get_entity_candidate_context,
    list_candidate_relations,
    list_entity_candidates,
    search_candidate_evidence,
    validate_candidate_evidence,
)


@pytest.fixture
def task_with_candidates(tmp_path, monkeypatch):
    db = tmp_path / "tools.db"
    service = TaskService(db_path=db)
    monkeypatch.setattr("app.tasks._task_service", service)
    monkeypatch.setattr("tools.entity_review.get_task_service", lambda: service)

    created = service.create_task(
        title="实体工具测试",
        purpose="跨案对象待核",
        authorized_until="2099-01-01",
        cases=[{"name": "案件 A"}, {"name": "案件 B"}],
    )
    task_id = created["task"]["id"]
    service.confirm_plan(task_id)
    cand = bank_account_example()
    cand["decision"] = "PENDING"
    cand["records"] = [
        {"case_id": "case-a", "case_name": "案件 A", "value": "6222********6231", "source": {}},
        {"case_id": "case-b", "case_name": "案件 B", "value": "6222********6231", "source": {}},
    ]
    service.save_entity_candidates(task_id, candidates=[cand])
    return task_id, cand["candidate_id"], service


def test_list_entity_candidates(task_with_candidates):
    task_id, cid, _ = task_with_candidates
    data = json.loads(list_entity_candidates(task_id, decision="PENDING"))
    assert data["ok"] is True
    assert data["total_matched"] >= 1
    assert any(r["candidate_id"] == cid for r in data["candidates"])


def test_get_entity_candidate_context(task_with_candidates):
    task_id, cid, _ = task_with_candidates
    data = json.loads(get_entity_candidate_context(task_id, cid))
    assert data["ok"] is True
    assert data["candidate"]["candidate_id"] == cid
    assert "field_compare" in data["candidate"]
    assert len(data["candidate"]["evidence"]) >= 1
    text = json.dumps(data, ensure_ascii=False)
    assert len(text) < 6000


def test_compare_and_search_evidence(task_with_candidates):
    task_id, cid, _ = task_with_candidates
    fields = json.loads(compare_candidate_fields(task_id, cid))
    assert fields["ok"] is True
    assert fields["field_compare"]
    ev = json.loads(search_candidate_evidence(task_id, cid, limit=2))
    assert ev["ok"] is True
    assert len(ev["evidence"]) <= 2
    assert all(item.get("quote_hash") for item in ev["evidence"])


def test_list_candidate_relations(task_with_candidates):
    task_id, cid, _ = task_with_candidates
    data = json.loads(list_candidate_relations(task_id, cid))
    assert data["ok"] is True
    assert "impact" in data


def test_validate_candidate_evidence_rejects_missing(task_with_candidates):
    task_id, _, _ = task_with_candidates
    data = json.loads(
        validate_candidate_evidence(
            task_id,
            chunk_id="missing",
            document_version_id="missing-ver",
            quote="nope",
            quote_hash_value="a" * 64,
        )
    )
    assert data["ok"] is False
    assert data["verified"] is False


def test_field_table_failure_marks_once_and_caches(task_with_candidates, monkeypatch):
    """生成失败也要落一次尝试标记，避免同一候选每次打开页面都重新触发模型。"""
    import app.config as config

    monkeypatch.setattr(config, "DEEPSEEK_EXTERNAL_CALLS_ENABLED", False)
    monkeypatch.setattr(config, "API_KEY", None)

    task_id, cid, service = task_with_candidates

    first = json.loads(build_candidate_field_table(task_id, cid))
    assert first["ok"] is False and first["fallback"] is True

    art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    detail = service.get_artifact(task_id, art["id"])
    cand = next(c for c in detail["payload"]["candidates"] if c["candidate_id"] == cid)
    assert cand["field_table_meta"]["producer"] == "RULE_FALLBACK"

    # 已标记过：再次调用直接命中缓存，不再尝试
    second = json.loads(build_candidate_field_table(task_id, cid))
    assert second["ok"] is True and second.get("cached") is True
