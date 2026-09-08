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


def _evidence():
    return [
        {"chunk_id": "c1", "document_version_id": "v1", "quote": "甲案账户记载", "case_name": "甲案"},
        {"chunk_id": "c2", "document_version_id": "v2", "quote": "乙案账户记载", "case_name": "乙案"},
    ]


def _clue(aspect, title, analysis, obj="尾号6231"):
    return {
        "title": title,
        "aspect": aspect,
        "analysis": analysis,
        "match_basis": "强标识账户碰撞",
        "objects": [obj],
        "evidence": _evidence(),
        "counter_evidence": [],
        "linked_candidate_ids": ["cand-1"],
    }


def test_put_clue_item_incremental_keeps_set(svc):
    task_id, service = svc
    r1 = service.put_clue_item(
        task_id, _clue("ID", "请核验两案账户是否同一", "尾号一致；未见反向材料。")
    )
    assert r1["clue_count"] == 1 and r1["retired_count"] == 0
    r2 = service.put_clue_item(
        task_id,
        _clue("FUND", "请核验两案资金是否汇聚", "同收款侧汇聚；未见反向材料。"),
    )
    assert r2["clue_count"] == 1 and r2["retired_count"] == 0

    snap = service.list_clue_items_for_model(task_id)
    assert snap["clue_count"] == 2
    assert {c["aspect"] for c in snap["clues"]} == {"ID", "FUND"}

    # 同对象同维度重写 → 顶替旧条，不新增
    r3 = service.put_clue_item(
        task_id, _clue("ID", "请核验账户控制人是否同一", "开户名差异较大；未见反向材料。")
    )
    assert r3["clue_count"] == 1 and r3["retired_count"] == 1
    snap = service.list_clue_items_for_model(task_id)
    assert snap["clue_count"] == 2
    assert {c["aspect"] for c in snap["clues"]} == {"ID", "FUND"}


def test_put_replace_all_and_delete(svc):
    task_id, service = svc
    service.put_clue_item(task_id, _clue("ID", "请核验两案账户是否同一", "尾号一致；未见反向材料。"))
    service.put_clue_item(task_id, _clue("FUND", "请核验两案资金是否汇聚", "同收款侧汇聚；未见反向材料。"))

    rr = service.put_clue_item(
        task_id, _clue("ROLE", "请核验开户姓名冲突", "开户名王某某与王某伟；未见反向材料。"), replace_all=True
    )
    assert rr["retired_count"] >= 2
    snap = service.list_clue_items_for_model(task_id)
    assert snap["clue_count"] == 1 and snap["clues"][0]["aspect"] == "ROLE"

    deleted = service.delete_clue_item(task_id, 0)
    assert deleted["ok"] is True and deleted["remaining"] == 0
    assert service.list_clue_items_for_model(task_id)["clue_count"] == 0
    with pytest.raises(TaskError):
        service.delete_clue_item(task_id, 5)


def test_delete_and_overwrite_disposed_refused(svc):
    task_id, service = svc
    service.put_clue_item(task_id, _clue("ID", "请核验两案账户是否同一", "尾号一致；未见反向材料。"))
    art_id = service.list_clue_items_for_model(task_id)["clues"][0]["artifact_id"]
    service.dispose_clue_item(task_id, art_id, "EXCLUDE", "测试排除")

    with pytest.raises(TaskError) as ei:
        service.delete_clue_item(task_id, 0)
    assert "人工处置" in str(ei.value)
    with pytest.raises(TaskError) as ei:
        service.put_clue_item(task_id, _clue("ID", "请核验两案账户是否同一", "尾号一致；未见反向材料。"))
    assert "人工处置" in str(ei.value)

    # 换一个核验维度仍可追加
    ok = service.put_clue_item(
        task_id, _clue("FUND", "请核验两案资金是否汇聚", "同收款侧汇聚；未见反向材料。")
    )
    assert ok["clue_count"] == 1
    assert service.list_clue_items_for_model(task_id)["clue_count"] == 2


def test_minimal_clue_auto_fills_optional(svc):
    """只给必填四项（title/aspect/analysis/evidence）时，系统自动兜底其余字段。"""
    task_id, service = svc
    result = service.put_clue_item(
        task_id,
        {
            "title": "两案账户资金是否衔接",
            "aspect": "FUND",
            "analysis": "两案流水在同一侧汇聚；未见反向材料。",
            "evidence": [
                {"chunk_id": "c1", "document_version_id": "v1", "quote": "甲案账户记载", "case_name": "甲案"},
                {"chunk_id": "c2", "document_version_id": "v2", "quote": "乙案账户记载", "case_name": "乙案"},
            ],
        },
    )
    assert result["clue_count"] == 1 and result["retired_count"] == 0
    snap = service.list_clue_items_for_model(task_id)
    assert snap["clue_count"] == 1
    # 服务端自动规范化：标题补「请核验」前缀、对象由支持材料回填
    payload = service.get_artifact(task_id, snap["clues"][0]["artifact_id"])["payload"]
    assert payload["title"].startswith("请核验")
    assert payload["match_basis"]
    assert payload["objects"]
    assert payload["counter_evidence"] == []
