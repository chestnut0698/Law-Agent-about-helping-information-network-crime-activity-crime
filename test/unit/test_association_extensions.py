"""通用关联扩展：案內一致性、弱平台、路径合成。"""

from __future__ import annotations

import json

from app.files import db_session, _insert
from app.tasks import TaskService, new_id, utc_now
from tools.entities import (
    EXTRACTOR_VERSION,
    collect_weak_platform_hints,
    collect_within_case_consistency,
    init_entity_db,
)


def _seed_mentions(db_path, task_id: str) -> None:
    init_entity_db(db_path)
    now = utc_now()
    rows = [
        ("case-a", "PHONE", "13800001111", "13800001111", "ch1"),
        ("case-a", "PHONE", "138****1111", "13800001111", "ch2"),
        ("case-a", "ACCOUNT", "622200001", "622200001", "ch3"),
        ("case-a", "ACCOUNT", "622200002", "622200002", "ch4"),
        ("case-a", "ORGANIZATION", "某第三方支付股份有限公司", "pay-a", "ch5"),
        ("case-b", "ORGANIZATION", "某第三方支付股份有限公司", "pay-b", "ch6"),
    ]
    with db_session(db_path) as conn:
        for case_id, otype, surface, norm, chunk in rows:
            _insert(
                conn,
                "entity_mentions",
                {
                    "id": new_id(),
                    "task_id": task_id,
                    "case_id": case_id,
                    "document_id": "d1",
                    "document_version_id": "v1",
                    "chunk_id": chunk,
                    "object_type": otype,
                    "surface_raw": surface,
                    "normalized_value": norm,
                    "mask_info_json": "{}",
                    "possible_forms_json": "[]",
                    "producer": "RULE",
                    "extractor_version": EXTRACTOR_VERSION,
                    "payload_hash": new_id().replace("-", "")[:32],
                    "char_start": 0,
                    "char_end": 1,
                    "page_start": 1,
                    "page_end": 1,
                    "quote_redacted": surface,
                    "quote_hash": "h",
                    "run_id": None,
                    "created_at": now,
                },
            )


def test_within_case_consistency(tmp_path):
    db = tmp_path / "e.db"
    tid = "t1"
    _seed_mentions(db, tid)
    notes = collect_within_case_consistency(tid, db_path=db)
    assert notes
    assert notes[0]["kind"] == "within_case_consistency"
    assert notes[0]["scope"] == "case_internal"
    assert notes[0]["account_count"] >= 2


def test_weak_platform_hints(tmp_path):
    db = tmp_path / "e.db"
    tid = "t1"
    _seed_mentions(db, tid)
    hints = collect_weak_platform_hints(
        tid,
        [{"case_id": "case-a", "display_name": "甲案"}, {"case_id": "case-b", "display_name": "乙案"}],
        db_path=db,
    )
    assert hints
    assert hints[0]["suggested_aspect"] == "PLAT"


def test_path_synthesis_from_confirmed_clues(tmp_path):
    svc = TaskService(db_path=tmp_path / "t.db")
    created = svc.create_task(
        title="路径合成",
        purpose="跨案",
        authorized_until="2099-01-01",
        cases=[{"name": "甲案"}, {"name": "乙案"}],
    )
    tid = created["task"]["id"]
    svc.confirm_plan(tid)
    clues = [
        {
            "payload": {
                "title": "请核验资金衔接",
                "aspect": "FUND",
                "disposition": "CONTINUE",
                "objects": ["某商户"],
            }
        },
        {
            "payload": {
                "title": "请核验时间断点",
                "aspect": "TIME",
                "disposition": "CONTINUE",
                "objects": ["某商户"],
            }
        },
        {
            "payload": {
                "title": "请核验同名",
                "aspect": "QUAL",
                "disposition": "PENDING",
                "objects": ["某商户"],
            }
        },
    ]
    lines = svc._compose_path_synthesis(clues)
    assert lines
    assert "材料路径合成" in lines[0]
    assert "资金" in lines[0] and "时空" in lines[0]
    assert "犯罪" not in lines[0]
