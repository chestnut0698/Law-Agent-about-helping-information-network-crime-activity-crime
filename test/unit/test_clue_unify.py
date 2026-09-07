"""关联线索：维度互斥、再生替换、规则落库下线。"""

from __future__ import annotations

import pytest

from app.tasks import TaskError, TaskService


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    service = TaskService(db_path=tmp_path / "clue.db")
    monkeypatch.setattr("app.tasks._task_service", service)
    created = service.create_task(
        title="线索统一测试",
        purpose="跨案关联",
        authorized_until="2099-01-01",
        cases=[{"name": "案件 A"}, {"name": "案件 B"}],
    )
    task_id = created["task"]["id"]
    service.confirm_plan(task_id)

    def fake_canon(**kwargs):
        return {
            "quote": kwargs.get("quote") or "摘录",
            "quote_hash": "h" * 64,
            "quote_display": kwargs.get("quote") or "摘录",
            "quote_storage": kwargs.get("quote") or "摘录",
        }

    monkeypatch.setattr("tools.entities.canonicalize_evidence_citation", fake_canon)
    return task_id, service


def test_generate_clues_offline(svc):
    task_id, service = svc
    with pytest.raises(TaskError) as ei:
        service.generate_clues(task_id)
    assert "停用" in str(ei.value) or "线索" in str(ei.value)


def test_aspect_required_and_retire_on_rewrite(svc):
    task_id, service = svc
    evidence = [
        {
            "chunk_id": "c1",
            "document_version_id": "v1",
            "quote": "甲案账户记载",
            "case_name": "甲案",
        },
        {
            "chunk_id": "c2",
            "document_version_id": "v2",
            "quote": "乙案账户记载",
            "case_name": "乙案",
        },
    ]
    base = {
        "title": "请核验两案账户是否同一",
        "analysis": "尾号一致，开户名略有差异；未见反向材料。",
        "match_basis": "强标识账户碰撞",
        "objects": ["尾号6231"],
        "evidence": evidence,
        "counter_evidence": [],
        "linked_candidate_ids": ["cand-1"],
    }

    with pytest.raises(TaskError) as ei:
        service.write_ai_clues(
            task_id,
            [
                {**base, "aspect": "ID"},
                {**base, "title": "请核验两案账户控制人", "aspect": "ID"},
            ],
        )
    assert "重复" in str(ei.value) or "核验" in str(ei.value)

    first = service.write_ai_clues(
        task_id,
        [
            {**base, "aspect": "ID"},
            {
                **base,
                "title": "请核验两案资金汇聚是否贯通",
                "aspect": "FUND",
                "analysis": "两案流水在同一收款侧汇聚，时间接近；未见反向材料。",
                "match_basis": "资金流水汇聚",
            },
        ],
    )
    assert first["clue_count"] == 2
    assert first["retired_count"] == 0

    second = service.write_ai_clues(
        task_id,
        [
            {
                **base,
                "title": "请核验开户姓名记载是否冲突",
                "aspect": "ROLE",
                "analysis": "开户姓名王某某与王某伟不一致；未见反向材料。",
                "match_basis": "角色记载冲突",
            }
        ],
    )
    assert second["clue_count"] == 1
    assert second["retired_count"] >= 2

    live = [
        a
        for a in second["task"]["artifacts"]
        if a["type"] == "CLUE_ITEM" and a["status"] not in {"STALE", "INVALID"}
    ]
    assert len(live) == 1
    assert (service.get_artifact(task_id, live[0]["id"])["payload"] or {}).get("aspect") == "ROLE"


def test_list_association_hints_ok(svc):
    task_id, service = svc
    result = service.list_association_hints(task_id)
    assert result["ok"] is True
    assert "hints" in result
