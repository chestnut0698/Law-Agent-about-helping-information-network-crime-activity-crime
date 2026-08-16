import pytest

from tools.files import (
    ERROR_CODES,
    MaterialError,
    MaterialService,
    _insert,
    allow_all_auth,
    ensure_demo_case,
    get_connection,
    init_db,
    new_id,
    replace_chunks,
    utc_now,
)


@pytest.fixture()
def svc(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    service = MaterialService(
        db_path=db,
        auth_check=allow_all_auth,
        redaction_dir=tmp_path / "redact",
    )
    conn = get_connection(db)
    service._case_id = ensure_demo_case(conn)
    conn.commit()
    conn.close()
    return service


def test_egress_allows_redacted(svc, tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("联系电话13812345678与身份证110101199001011234", encoding="utf-8")
    result = svc.upload_one(
        case_id=svc._case_id,
        filename="a.txt",
        path=path,
        user_id="u1",
        parse=True,
    )
    payload = svc.read_redacted_chunk(
        result["version"]["id"], chunk_id=result["chunks"][0]["id"], user_id="u1"
    )
    assert "13812345678" not in payload["text"]
    assert payload["redacted"] is True


def test_egress_denies_unredacted_chunk(svc, tmp_path):
    conn = get_connection(svc.db_path)
    doc_id = new_id()
    ver_id = new_id()
    now = utc_now()
    _insert(
        conn,
        "documents",
        {
            "id": doc_id,
            "case_id": svc._case_id,
            "filename": "raw.txt",
            "stored_name": "raw.txt",
            "content_type": "text/plain",
            "size": 10,
            "sha256": "abc",
            "uploaded_by": "u1",
            "created_at": now,
            "status": "PARSED",
            "current_version_id": ver_id,
            "deleted_at": None,
            "quality_summary_json": "{}",
        },
    )
    _insert(
        conn,
        "document_versions",
        {
            "id": ver_id,
            "document_id": doc_id,
            "version_no": 1,
            "parent_version_id": None,
            "source_type": "UPLOAD",
            "sha256": "abc",
            "storage_path": str(tmp_path / "raw.txt"),
            "content_type": "text/plain",
            "size": 10,
            "parser_version": "t",
            "status": "PARSED",
            "is_current": 1,
            "is_active": 1,
            "quality_summary_json": "{}",
            "error_code": None,
            "error_message": None,
            "created_by": "u1",
            "created_at": now,
        },
    )
    chunk_id = "bad-chunk"
    replace_chunks(
        conn,
        ver_id,
        [
            {
                "id": chunk_id,
                "document_version_id": ver_id,
                "ordinal": 0,
                "page_start": 1,
                "page_end": 1,
                "char_start": 0,
                "char_end": 20,
                "bbox_json": "[]",
                "text_raw": "手机13800001111",
                "text_redacted": "手机13800001111",
                "text_sha256": "x",
                "parser_version": "t",
                "quality_flags_json": "[]",
                "is_active": 1,
                "stale": 0,
            }
        ],
    )
    conn.commit()
    conn.close()

    with pytest.raises(MaterialError) as ei:
        svc.read_redacted_chunk(ver_id, chunk_id=chunk_id, user_id="u1")
    assert ei.value.code == ERROR_CODES["EGRESS_DENIED"]
