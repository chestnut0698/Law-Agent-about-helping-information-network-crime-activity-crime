from app.files import build_chunks


def test_chunk_ids_stable_across_runs():
    pages = [
        {"page_no": 1, "text": "A" * 500 + "\n" + "B" * 500, "bbox": [[0, 0, 10, 10]], "quality_flags": []},
        {"page_no": 2, "text": "C" * 300, "bbox": [[0, 0, 10, 10]], "quality_flags": ["LOW_OCR_CONFIDENCE"]},
    ]
    v = "version-stable-1"
    a = build_chunks(v, pages, chunk_size=200, overlap=40)
    b = build_chunks(v, pages, chunk_size=200, overlap=40)
    assert [c["id"] for c in a] == [c["id"] for c in b]
    assert [c["ordinal"] for c in a] == list(range(len(a)))
    assert a[0]["document_version_id"] == v
    assert "text_sha256" in a[0]


def test_chunk_order_and_overlap():
    pages = [{"page_no": 1, "text": "0123456789" * 30, "bbox": [], "quality_flags": []}]
    chunks = build_chunks("v2", pages, chunk_size=50, overlap=10)
    assert len(chunks) > 1
    # Overlap means next start is before previous end in char space.
    assert chunks[1]["char_start"] < chunks[0]["char_end"]
