"""实体复核 Agent：DeepSeek 不可用时确定性回退。"""

import json

import pytest

from app.entity_review_schema import bank_account_example
from app.tasks import TaskService


@pytest.fixture
def task_ready(tmp_path, monkeypatch):
    db = tmp_path / "agent.db"
    service = TaskService(db_path=db)
    monkeypatch.setattr("app.tasks._task_service", service)
    monkeypatch.setattr("tools.entity_review.get_task_service", lambda: service)
    monkeypatch.setattr("agents.entity_review_agent.DEEPSEEK_EXTERNAL_CALLS_ENABLED", False)
    monkeypatch.setattr("agents.entity_review_agent.API_KEY", "")

    created = service.create_task(
        title="Agent 测试",
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


def test_deterministic_fallback_stable(task_ready):
    from agents.entity_review_agent import EntityReviewAgent

    task_id, cid, _ = task_ready
    agent = EntityReviewAgent(task_id)
    r1 = agent.review_one(cid)
    r2 = agent.review_one(cid)
    assert r1["ok"] is True
    assert r1.get("fallback") is True
    assert r2["ok"] is True
    s1 = r1["suggestion"]["agent_summary"]
    s2 = r2["suggestion"]["agent_summary"]
    assert s1 == s2
    assert len(s1) <= 150
    assert r1["suggestion"]["recommendation"] in {
        "MERGE",
        "KEEP_SEPARATE",
        "CORRECT",
        "DEFER",
        "NEED_MORE_EVIDENCE",
    }


def test_fallback_does_not_invent_identifiers(task_ready):
    from agents.entity_review_agent import EntityReviewAgent

    task_id, cid, service = task_ready
    detail = service.get_artifact(
        task_id, service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")["id"]
    )
    before = json.dumps(detail["payload"], ensure_ascii=False)
    agent = EntityReviewAgent(task_id)
    result = agent.review_one(cid)
    suggestion = json.dumps(result["suggestion"], ensure_ascii=False)
    assert "FOOBAR_ACCOUNT_999" not in suggestion
    assert "6222" not in suggestion or "6222" in before
