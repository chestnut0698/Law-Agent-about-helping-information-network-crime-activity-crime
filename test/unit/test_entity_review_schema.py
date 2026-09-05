import pytest
from pydantic import ValidationError

from app.entity_review_schema import (
    EntityType,
    bank_account_example,
    example_candidates_by_type,
    normalize_entity_type,
    validate_candidate,
)


def test_bank_account_example_validates():
    cand = validate_candidate(bank_account_example())
    assert cand.entity_type == EntityType.BANK_ACCOUNT.value
    assert len(cand.cases) == 2
    assert len(cand.evidence) == 2
    assert cand.impact.case_count == 2


def test_five_type_examples_validate():
    for key, raw in example_candidates_by_type().items():
        cand = validate_candidate(raw)
        assert cand.entity_type == key


def test_rejects_illegal_entity_type():
    raw = bank_account_example()
    raw["entity_type"] = "FOOBAR"
    with pytest.raises((ValidationError, ValueError)):
        validate_candidate(raw)


def test_rejects_missing_quote_hash():
    raw = bank_account_example()
    raw["evidence"][0]["quote_hash"] = ""
    with pytest.raises(ValidationError):
        validate_candidate(raw)


def test_rejects_single_case():
    raw = bank_account_example()
    raw["cases"] = [{"case_id": "case-a", "case_name": "案件 A"}]
    raw["evidence"] = [raw["evidence"][0]]
    with pytest.raises(ValidationError):
        validate_candidate(raw)


def test_rejects_no_evidence():
    raw = bank_account_example()
    raw["evidence"] = []
    with pytest.raises(ValidationError):
        validate_candidate(raw)


def test_rejects_extra_fields():
    raw = bank_account_example()
    raw["junk_field"] = "nope"
    with pytest.raises(ValidationError):
        validate_candidate(raw)


def test_rejects_field_outside_whitelist():
    raw = bank_account_example()
    raw["field_compare"].append(
        {
            "field_key": "credit_code",
            "label": "信用代码",
            "per_case": [
                {"case_id": "case-a", "case_name": "案件 A", "value": "x", "status": "same"},
                {"case_id": "case-b", "case_name": "案件 B", "value": "x", "status": "same"},
            ],
        }
    )
    with pytest.raises(ValidationError):
        validate_candidate(raw)


def test_summary_length_limit():
    raw = bank_account_example()
    raw["agent_summary"] = "字" * 151
    with pytest.raises(ValidationError):
        validate_candidate(raw)


def test_normalize_internal_account_type():
    assert normalize_entity_type("ACCOUNT") == EntityType.BANK_ACCOUNT
    assert normalize_entity_type("NAME") == EntityType.PERSON
