import pytest

from conftest import (
    make_blank_pdf,
    make_corrupt_pdf,
    make_docx,
    make_image_with_ocr_sidecar,
    make_mixed_pdf,
    make_text_pdf,
    make_txt,
)
from tools.files import (
    ERROR_CODES,
    FallbackOCREngine,
    MaterialError,
    MaterialService,
    _insert,
    allow_all_auth,
    ensure_demo_case,
    get_connection,
    init_db,
    new_id,
    set_ocr_engine,
    utc_now,
)


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    set_ocr_engine(FallbackOCREngine())
    db = tmp_path / "t.db"
    init_db(db)
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tools.files.MATERIAL_STORAGE_DIR", storage)

    service = MaterialService(
        db_path=db,
        auth_check=allow_all_auth,
        redaction_dir=tmp_path / "maps",
    )
    conn = get_connection(db)
    case_a = ensure_demo_case(conn)
    case_b = new_id()
    pool_id = conn.execute("SELECT id FROM case_pools LIMIT 1").fetchone()["id"]
    _insert(
        conn,
        "cases",
        {
            "id": case_b,
            "case_pool_id": pool_id,
            "name": "case-2",
            "case_number": "DEMO-002",
            "created_by": "system",
            "created_at": utc_now(),
        },
    )
    conn.commit()
    conn.close()
    service._case_a = case_a
    service._case_b = case_b
    return service


def test_digital_pdf_and_txt_docx(svc, tmp_path):
    pdf = make_text_pdf(tmp_path / "digital.pdf", ["帮信罪电子卷宗第1页", "第2页继续"])
    txt = make_txt(tmp_path / "a.txt", "TXT卷宗正文")
    docx = make_docx(tmp_path / "a.docx", "DOCX卷宗正文")
    results = svc.upload_many(
        [
            {"case_id": svc._case_a, "filename": "digital.pdf", "path": pdf},
            {"case_id": svc._case_a, "filename": "a.txt", "path": txt},
            {"case_id": svc._case_b, "filename": "a.docx", "path": docx},
        ],
        user_id="u1",
        parse=True,
    )
    assert len(results) == 3
    assert results[0]["status"] in {"PARSED", "NEEDS_OCR_REVIEW"}
    assert results[1]["status"] == "PARSED"
    assert results[2]["document"]["case_id"] == svc._case_b
    # no cross-case leak
    mats_a = svc.list_materials(svc._case_a, user_id="u1")
    mats_b = svc.list_materials(svc._case_b, user_id="u1")
    assert all(m["case_id"] == svc._case_a for m in mats_a)
    assert all(m["case_id"] == svc._case_b for m in mats_b)


def test_image_and_blank_pdf_ocr_path(svc, tmp_path):
    img = make_image_with_ocr_sidecar(tmp_path / "scan.png", "扫描件识别文本ABC")
    blank = make_blank_pdf(tmp_path / "scan.pdf", 1)
    r_img = svc.upload_one(case_id=svc._case_a, filename="scan.png", path=img, user_id="u1")
    r_pdf = svc.upload_one(case_id=svc._case_a, filename="scan.pdf", path=blank, user_id="u1")
    assert r_img["pages"][0]["source"] == "ocr"
    assert "扫描件识别文本ABC" in r_img["pages"][0]["text"]
    assert r_pdf["pages"][0]["source"] in {"ocr", "pdf_text"}


def test_mixed_layout_pdf(svc, tmp_path):
    mixed = make_mixed_pdf(tmp_path / "mixed.pdf")
    r = svc.upload_one(case_id=svc._case_a, filename="mixed.pdf", path=mixed, user_id="u1")
    assert len(r["pages"]) == 2
    assert r["pages"][0]["status"] in {"PARSED", "NEEDS_OCR_REVIEW"}


def test_corrupt_and_too_large_stable_errors(svc, tmp_path, monkeypatch):
    bad = make_corrupt_pdf(tmp_path / "bad.pdf")
    with pytest.raises(MaterialError) as ei:
        svc.upload_one(case_id=svc._case_a, filename="bad.pdf", path=bad, user_id="u1")
    assert ei.value.code in {
        ERROR_CODES["CORRUPT_FILE"],
        ERROR_CODES["PARSE_FAILED"],
        ERROR_CODES["ENCRYPTED_FILE"],
    }

    monkeypatch.setattr("tools.files.MAX_UPLOAD_BYTES", 100)
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 200)
    with pytest.raises(MaterialError) as ej:
        svc.upload_one(case_id=svc._case_a, filename="big.txt", path=big, user_id="u1")
    assert ej.value.code == ERROR_CODES["FILE_TOO_LARGE"]


def test_unsupported_type(svc, tmp_path):
    p = tmp_path / "x.exe"
    p.write_bytes(b"MZ")
    with pytest.raises(MaterialError) as ei:
        svc.upload_one(case_id=svc._case_a, filename="x.exe", path=p, user_id="u1")
    assert ei.value.code == ERROR_CODES["UNSUPPORTED_TYPE"]


def test_agent_tools_redacted_only(svc, tmp_path, monkeypatch):
    from tools import files as material_files

    monkeypatch.setattr(material_files, "_default_service", svc)
    p = make_txt(tmp_path / "t.txt", "嫌疑人手机13812345678")
    r = svc.upload_one(case_id=svc._case_a, filename="t.txt", path=p, user_id="u1")
    out = material_files.read_material_chunk(r["version"]["id"], user_id="u1")
    assert "13812345678" not in out
    assert "PHONE" in out or "redacted" in out
