import pytest

from app.files import (
    ERROR_CODES,
    MaterialError,
    MaterialService,
    allow_all_auth,
    deny_all_auth,
    ensure_demo_case,
    get_connection,
    init_db,
    list_chunks,
)


@pytest.fixture()
def svc(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    service = MaterialService(
        db_path=db,
        auth_check=allow_all_auth,
        redaction_dir=tmp_path / "maps",
    )
    conn = get_connection(db)
    service._case_id = ensure_demo_case(conn)
    conn.commit()
    conn.close()
    return service


def test_replace_creates_new_version_not_overwrite(svc, tmp_path):
    p = tmp_path / "v1.txt"
    p.write_text("version-one-content", encoding="utf-8")
    r1 = svc.upload_one(case_id=svc._case_id, filename="doc.txt", path=p, user_id="u1")
    doc_id = r1["document"]["id"]
    v1 = r1["version"]["id"]

    p2 = tmp_path / "v2.txt"
    p2.write_text("version-two-content-replaced", encoding="utf-8")
    r2 = svc.upload_one(
        case_id=svc._case_id,
        filename="doc.txt",
        path=p2,
        user_id="u1",
        replace_document_id=doc_id,
    )
    assert r2["version"]["version_no"] == 2
    assert r2["version"]["id"] != v1
    assert r2["version"]["parent_version_id"] == v1

    status = svc.get_status(doc_id, user_id="u1")
    assert len(status["versions"]) == 2
    old = next(v for v in status["versions"] if v["id"] == v1)
    assert old["is_current"] == 0


def test_duplicate_hash_pending(svc, tmp_path):
    p = tmp_path / "same.txt"
    p.write_text("same-bytes", encoding="utf-8")
    svc.upload_one(case_id=svc._case_id, filename="a.txt", path=p, user_id="u1")
    dup = svc.upload_one(case_id=svc._case_id, filename="b.txt", path=p, user_id="u1")
    assert dup["status"] == "DUPLICATE_PENDING"
    assert dup["error_code"] == ERROR_CODES["DUPLICATE_PENDING"]


def test_correction_creates_new_version_and_stales_old(svc, tmp_path):
    p = tmp_path / "ocr.txt"
    p.write_text("原始识别错误文本", encoding="utf-8")
    r = svc.upload_one(case_id=svc._case_id, filename="ocr.txt", path=p, user_id="u1")
    doc_id = r["document"]["id"]
    ver_id = r["version"]["id"]
    corr = svc.apply_correction(
        document_id=doc_id,
        source_version_id=ver_id,
        page_no=1,
        corrected_text="人工修正后的正确文本",
        user_id="u1",
    )
    assert corr["version"]["source_type"] == "CORRECTION"
    assert corr["parent_version_id"] == ver_id
    status = svc.get_status(doc_id, user_id="u1")
    assert status["current_version"]["id"] == corr["version"]["id"]

    conn = get_connection(svc.db_path)
    old_chunks = list_chunks(conn, ver_id, active_only=False)
    conn.close()
    assert old_chunks
    assert all(c["stale"] == 1 and c["is_active"] == 0 for c in old_chunks)


def test_auth_deny_by_default(tmp_path):
    db = tmp_path / "deny.db"
    init_db(db)
    svc = MaterialService(db_path=db, auth_check=deny_all_auth)
    with pytest.raises(MaterialError) as ei:
        svc.list_materials("no-case", user_id="u1")
    assert ei.value.code == ERROR_CODES["AUTH_DENIED"]
