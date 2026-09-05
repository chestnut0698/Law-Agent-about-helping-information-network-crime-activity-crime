"""实体抽取与候选准入精准度测试。"""

from tools.entities import (
    collide_mentions,
    extract_rule_mentions,
    is_excluded,
    load_exclusions,
    normalize_identifier,
    quote_hash,
)


def _mention(
    object_type: str,
    surface: str,
    *,
    case_id: str,
    chunk_id: str = "c1",
    quote: str | None = None,
    mask_info: dict | None = None,
    document_version_id: str = "ver-1",
):
    normalized = normalize_identifier(object_type, surface)
    q = quote or surface
    return {
        "case_id": case_id,
        "chunk_id": chunk_id,
        "document_version_id": document_version_id,
        "document_id": f"doc-{case_id}",
        "filename": f"{case_id}.txt",
        "page_start": 1,
        "page_end": 1,
        "object_type": object_type,
        "surface_raw": surface,
        "normalized_value": "" if (mask_info or {}).get("masked") else normalized,
        "mask_info": mask_info or {},
        "quote_redacted": q,
        "quote_hash": quote_hash(q),
    }


def test_extract_strong_account_and_phone():
    # 6222021234567890 经 Luhn 可能失败，使用已知合法卡号
    text = "开户卡号 6222021234567890123，联系电话 13812345678。"
    hits = extract_rule_mentions(text)
    types = {h["object_type"] for h in hits if h.get("normalized_value")}
    assert "PHONE" in types


def test_tail_only_not_strong_collision_key():
    text = "卡号尾号：1234"
    hits = extract_rule_mentions(text)
    assert hits
    assert all(not h.get("normalized_value") for h in hits if "尾号" in h.get("surface_raw", ""))


def test_same_name_without_corroboration_no_candidate():
    mentions = [
        _mention("NAME", "赵敏", case_id="A", chunk_id="a1"),
        _mention("NAME", "赵敏", case_id="B", chunk_id="b1"),
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert cands == []


def test_same_name_with_phone_corroboration_forms_candidate():
    mentions = [
        _mention("NAME", "赵敏", case_id="A", chunk_id="a1"),
        _mention("PHONE", "13812345678", case_id="A", chunk_id="a2"),
        _mention("NAME", "赵敏", case_id="B", chunk_id="b1"),
        _mention("PHONE", "13900001111", case_id="B", chunk_id="b2"),
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert len(cands) == 1
    assert cands[0]["entity_type"] in {"PERSON", "NAME"}
    assert len(cands[0]["cases"]) == 2


def test_same_org_name_without_second_field_no_candidate():
    mentions = [
        _mention("ORGANIZATION", "星河科技有限公司", case_id="A"),
        _mention("ORGANIZATION", "星河科技有限公司", case_id="B"),
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert cands == []


def test_same_org_with_account_corroboration_forms_candidate():
    card = "6222021234567890128"
    mentions = [
        _mention("ORGANIZATION", "星河科技有限公司", case_id="A", chunk_id="a1"),
        _mention("ACCOUNT", card, case_id="A", chunk_id="a2"),
        _mention("ORGANIZATION", "星河科技有限公司", case_id="B", chunk_id="b1"),
        _mention("ACCOUNT", "6228480000000000018", case_id="B", chunk_id="b2"),
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    org = [c for c in cands if c.get("_internal_type") == "ORGANIZATION" or c.get("entity_type") == "ORGANIZATION"]
    assert len(org) == 1


def test_credit_code_can_form_candidate_alone():
    code = "91110000MA01234567"
    mentions = [
        {**_mention("ORGANIZATION", code, case_id="A"), "mask_info": {"kind": "credit_code"}},
        {**_mention("ORGANIZATION", code, case_id="B"), "mask_info": {"kind": "credit_code"}},
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert len(cands) == 1


def test_same_phone_across_cases_forms_candidate():
    mentions = [
        _mention("PHONE", "13812345678", case_id="A"),
        _mention("PHONE", "13812345678", case_id="B"),
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert len(cands) == 1
    assert cands[0]["entity_type"] == "PHONE"
    assert cands[0]["display_name"] == "尾号 5678 手机号码"
    assert len(cands[0]["evidence"]) >= 2
    assert [row["field_key"] for row in cands[0]["field_compare"]] == [
        "phone_no",
        "registrant",
        "linked_account",
        "linked_device",
        "contact_context",
    ]


def test_account_candidate_has_clear_name_and_cross_case_field_matrix():
    account = "6222021234567890128"
    mentions = [
        _mention("ACCOUNT", account, case_id="A", chunk_id="a1"),
        _mention("NAME", "王某甲", case_id="A", chunk_id="a1"),
        _mention("ACCOUNT", account, case_id="B", chunk_id="b1"),
        _mention("NAME", "王某乙", case_id="B", chunk_id="b1"),
    ]
    candidates = collide_mentions(
        mentions, case_names={"A": "案A", "B": "案B"}, rejected=set()
    )
    account_candidate = next(
        candidate for candidate in candidates if candidate["entity_type"] == "BANK_ACCOUNT"
    )
    assert account_candidate["display_name"] == "尾号 0128 银行账户"
    assert [row["field_key"] for row in account_candidate["field_compare"]] == [
        "account_no",
        "holder_name",
        "bank_name",
        "reserved_phone",
        "merchant",
    ]
    holder = next(
        row
        for row in account_candidate["field_compare"]
        if row["field_key"] == "holder_name"
    )
    assert [item["value"] for item in holder["per_case"]] == ["王某甲", "王某乙"]
    assert {item["status"] for item in holder["per_case"]} == {"diff"}


def test_masked_phone_excluded_from_candidates():
    mentions = [
        _mention("PHONE", "138****5678", case_id="A", mask_info={"masked": True, "kind": "mask"}),
        _mention("PHONE", "138****5678", case_id="B", mask_info={"masked": True, "kind": "mask"}),
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert cands == []


def test_generic_org_excluded():
    ex = load_exclusions()
    assert is_excluded("ORGANIZATION", "有限公司", ex)
    assert not is_excluded("ORGANIZATION", "星河科技有限公司", ex)


def test_single_case_never_candidate():
    mentions = [
        _mention("PHONE", "13812345678", case_id="A", chunk_id="a1"),
        _mention("PHONE", "13812345678", case_id="A", chunk_id="a2"),
    ]
    cands = collide_mentions(mentions, case_names={"A": "案A"}, rejected=set())
    assert cands == []


def test_duplicate_mentions_same_chunk_deduped():
    m = _mention("PHONE", "13812345678", case_id="A")
    mentions = [m, dict(m), _mention("PHONE", "13812345678", case_id="B")]
    cands = collide_mentions(mentions, case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert len(cands) == 1
    assert cands[0]["impact"]["mention_count"] == 2


def test_missing_quote_hash_blocks_candidate():
    a = _mention("PHONE", "13812345678", case_id="A")
    b = _mention("PHONE", "13812345678", case_id="B")
    b["quote_hash"] = ""
    cands = collide_mentions([a, b], case_names={"A": "案A", "B": "案B"}, rejected=set())
    assert cands == []


def test_public_surface_masks_phone():
    from tools.entities import public_surface

    assert "****" in public_surface("13812345678", "PHONE")
