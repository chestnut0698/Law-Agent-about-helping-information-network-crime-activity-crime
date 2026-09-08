"""时间线查询过滤：日期/类型/来源/主体（纯函数）。"""

from tools.timeline_subjects import (
    build_timeline_facets,
    filter_timeline_items,
    parse_csv_set,
)


def _sample_items():
    return [
        {
            "event_id": "1",
            "event_type": "TRANSFER",
            "time_text": "2024-01-10",
            "event_time": "2024-01-10",
            "time_uncertain": False,
            "source_mode": "recorded",
            "subject_kind": "PERSON",
            "subject_id": "p1",
            "person_subject_id": "p1",
            "person_subject": "甲某",
            "case_id": "c1",
            "case_name": "案件一",
        },
        {
            "event_id": "2",
            "event_type": "CONTACT",
            "time_text": "2024-02-01",
            "event_time": "2024-02-01",
            "time_uncertain": False,
            "source_mode": "recorded",
            "subject_kind": "PERSON",
            "subject_id": "p1",
            "person_subject_id": "p1",
            "case_id": "c2",
            "case_name": "案件二",
        },
        {
            "event_id": "3",
            "event_type": "TRANSFER",
            "time_text": "",
            "event_time": "",
            "time_uncertain": True,
            "source_mode": "recorded",
            "subject_kind": "ACCOUNT",
            "subject_id": "a1",
            "account_subject_id": "a1",
            "case_id": "c1",
            "case_name": "案件一",
        },
        {
            "event_id": "4",
            "event_type": "INFERRED",
            "time_text": "2024-01-15",
            "event_time": "2024-01-15",
            "time_uncertain": False,
            "source_mode": "inferred",
            "subject_kind": "PERSON",
            "subject_id": "p1",
            "person_subject_id": "p1",
            "case_id": "c1",
            "case_name": "案件一",
        },
    ]


def test_filter_by_date_range_keeps_uncertain_when_flagged():
    items = _sample_items()
    out = filter_timeline_items(
        items, date_from="2024-01-01", date_to="2024-01-31", include_uncertain=True
    )
    ids = {x["event_id"] for x in out}
    assert "1" in ids
    assert "4" in ids
    assert "3" in ids  # uncertain kept
    assert "2" not in ids


def test_filter_exclude_uncertain():
    items = _sample_items()
    out = filter_timeline_items(items, include_uncertain=False)
    assert all(x["event_id"] != "3" for x in out)


def test_filter_event_type_and_source_mode():
    items = _sample_items()
    out = filter_timeline_items(
        items,
        event_types={"TRANSFER"},
        source_modes={"recorded"},
    )
    assert {x["event_id"] for x in out} == {"1", "3"}


def test_filter_subject_and_case():
    items = _sample_items()
    out = filter_timeline_items(items, subject_kind="ACCOUNT", subject_id="a1")
    assert [x["event_id"] for x in out] == ["3"]
    out2 = filter_timeline_items(items, case_id="c2")
    assert [x["event_id"] for x in out2] == ["2"]


def test_facets_cover_dimensions():
    facets = build_timeline_facets(_sample_items())
    assert facets["total"] == 4
    assert facets["event_types"]["TRANSFER"] == 2
    assert facets["uncertain_count"] == 1
    assert any(p["id"] == "p1" for p in facets["persons"])
    assert any(a["id"] == "a1" for a in facets["accounts"])


def test_parse_csv_set():
    assert parse_csv_set("a, b ,c") == {"a", "b", "c"}
    assert parse_csv_set("") == set()
