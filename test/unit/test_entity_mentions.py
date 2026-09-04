from app.files import db_session, init_db, _rows
from tools.entities import init_entity_db, persist_mentions


def test_persist_mentions_inserts_every_hit(tmp_path):
    db = tmp_path / "mentions.db"
    init_db(db)
    init_entity_db(db)
    chunk = {
        "chunk_id": "chunk-1",
        "case_id": "case-a",
        "document_id": "doc-1",
        "document_version_id": "ver-1",
        "page_start": 1,
        "page_end": 1,
        "text_raw": "phone and card",
        "text_redacted": "phone and card",
    }
    hits = [
        {
            "object_type": "PHONE",
            "surface_raw": "13812345678",
            "normalized_value": "13812345678",
            "mask_info": {},
            "possible_forms": [],
            "char_start": 0,
            "char_end": 11,
            "producer": "RULE",
            "quote_redacted": "13812345678",
            "quote_hash": "h1",
        },
        {
            "object_type": "ACCOUNT",
            "surface_raw": "6222021234567890123",
            "normalized_value": "6222021234567890123",
            "mask_info": {},
            "possible_forms": [],
            "char_start": 12,
            "char_end": 31,
            "producer": "RULE",
            "quote_redacted": "6222021234567890123",
            "quote_hash": "h2",
        },
    ]
    with db_session(db) as conn:
        inserted = persist_mentions(conn, task_id="task-1", chunk=chunk, hits=hits)
        rows = _rows(conn, "SELECT object_type FROM entity_mentions WHERE task_id = ?", ("task-1",))
    assert inserted == 2
    assert {row["object_type"] for row in rows} == {"PHONE", "ACCOUNT"}
