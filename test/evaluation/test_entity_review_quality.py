"""实体复核精准度评测。"""

from __future__ import annotations

import json
from pathlib import Path

from tools.entities import collide_mentions, normalize_identifier, quote_hash, verify_quote_hash

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "entity_review_cases" / "cases.json"
STRONG_TYPES = {"BANK_ACCOUNT", "PHONE", "DEVICE", "ID_CARD", "MERCHANT", "IP", "ACCOUNT"}


def _build_mentions(raw_list: list[dict]) -> list[dict]:
    out = []
    for raw in raw_list:
        object_type = raw["object_type"]
        surface = raw["surface"]
        masked = bool(raw.get("masked"))
        q = surface
        item = {
            "case_id": raw["case_id"],
            "chunk_id": raw.get("chunk_id") or "c1",
            "document_version_id": raw.get("document_version_id") or "ver-1",
            "document_id": f"doc-{raw['case_id']}",
            "filename": f"{raw['case_id']}.txt",
            "page_start": 1,
            "page_end": 1,
            "object_type": object_type,
            "surface_raw": surface,
            "normalized_value": "" if masked else normalize_identifier(object_type, surface),
            "mask_info": {"masked": True, "kind": "mask"} if masked else {},
            "quote_redacted": q,
            "quote_hash": "" if raw.get("drop_hash") else quote_hash(q),
        }
        if raw.get("kind"):
            item["mask_info"] = {**(item.get("mask_info") or {}), "kind": raw["kind"]}
        out.append(item)
    return out


def evaluate_fixture(path: Path = FIXTURE) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    results = []
    strong_tp = strong_fp = strong_total = 0
    soft_tp = soft_fp = soft_total = 0
    invented = 0
    dup = 0
    summary_overflow = 0

    for case in data["cases"]:
        mentions = _build_mentions(case["mentions"])
        case_names = {m["case_id"]: m["case_id"] for m in mentions}
        cands = collide_mentions(mentions, case_names=case_names, rejected=set())
        # 重复指纹
        fps = [c.get("fingerprint") for c in cands]
        if len(fps) != len(set(fps)):
            dup += 1
        expected = int(case.get("expect_candidates") or 0)
        got = len(cands)
        ok = got == expected
        if case.get("expect_types"):
            got_types = {c.get("entity_type") for c in cands}
            ok = ok and set(case["expect_types"]).issubset(got_types)

        is_strong = case["id"] in {
            "same_account_cross_case",
            "tail_only_no_match",
            "merchant_id_same_name_diff",
            "phone_account_cross",
            "duplicate_in_same_material",
            "missing_quote_hash",
        }
        if is_strong:
            strong_total += 1
            if ok:
                strong_tp += 1
            elif got > expected:
                strong_fp += 1
        else:
            soft_total += 1
            if ok:
                soft_tp += 1
            elif got > expected:
                soft_fp += 1

        for c in cands:
            summary = c.get("agent_summary") or ""
            if len(summary) > 150:
                summary_overflow += 1
            # 无证据事实：证据缺 hash 视为发明风险
            for ev in c.get("evidence") or []:
                if not ev.get("quote_hash"):
                    invented += 1
                elif ev.get("quote") and not verify_quote_hash(ev["quote"], ev["quote"], ev["quote_hash"]):
                    # quote 自身哈希应匹配
                    invented += 1

        results.append({"id": case["id"], "expected": expected, "got": got, "ok": ok})

    strong_precision = 1.0 if strong_total == 0 else strong_tp / strong_total
    soft_precision = 1.0 if soft_total == 0 else soft_tp / soft_total
    return {
        "results": results,
        "strong_precision": strong_precision,
        "person_org_precision": soft_precision,
        "duplicate_candidate_rate": dup / max(1, len(results)),
        "invented_fact_rate": invented,
        "summary_overflow": summary_overflow,
        "gates": {
            "strong_precision_ge_0_98": strong_precision >= 0.98,
            "person_org_precision_ge_0_90": soft_precision >= 0.90,
            "no_duplicates": dup == 0,
            "no_invented": invented == 0,
            "summary_ok": summary_overflow == 0,
        },
    }


def test_entity_review_quality_gates():
    report = evaluate_fixture()
    assert report["gates"]["strong_precision_ge_0_98"], report
    assert report["gates"]["person_org_precision_ge_0_90"], report
    assert report["gates"]["no_duplicates"], report
    assert report["gates"]["no_invented"], report
    assert report["gates"]["summary_ok"], report
    assert all(r["ok"] for r in report["results"]), report["results"]
