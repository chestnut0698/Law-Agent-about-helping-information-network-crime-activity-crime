"""角色时间线主体选择、排序与查询过滤（纯函数，无重依赖）。"""

from __future__ import annotations

import re
from typing import Any

_PERSON_SUBJECT_TYPES = frozenset({"NAME", "PERSON"})
_ACCOUNT_SUBJECT_TYPES = frozenset({"ACCOUNT"})
# 角色时间线默认主体：人物；辅视角：账户。案件/商户不当主体。
_TIMELINE_SUBJECT_KINDS = frozenset({"PERSON", "ACCOUNT"})

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def classify_timeline_subject_kind(object_type: str | None) -> str | None:
    """将 object_type 映射为时间线主体种类；案件等返回 None。"""
    ot = (object_type or "").strip().upper()
    if ot in _PERSON_SUBJECT_TYPES:
        return "PERSON"
    if ot in _ACCOUNT_SUBJECT_TYPES:
        return "ACCOUNT"
    return None


def pick_timeline_subject_refs(parties: list[Any]) -> dict[str, Any]:
    """从 parties 选出人物主主体与账户辅主体；绝不回退到案件名。"""
    resolved = [p for p in (parties or []) if isinstance(p, dict)]
    person = next(
        (p for p in resolved if classify_timeline_subject_kind(p.get("object_type")) == "PERSON"),
        None,
    )
    account = next(
        (p for p in resolved if classify_timeline_subject_kind(p.get("object_type")) == "ACCOUNT"),
        None,
    )
    objects = []
    for p in resolved:
        kind = classify_timeline_subject_kind(p.get("object_type"))
        if kind in _TIMELINE_SUBJECT_KINDS:
            continue
        objects.append(
            {
                "object_type": p.get("object_type"),
                "display_name": p.get("display_name") or p.get("surface") or "",
                "subject_id": p.get("subject_id") or "",
            }
        )
    return {
        "person": person,
        "account": account,
        "objects": objects[:6],
    }


def timeline_event_sort_key(item: dict[str, Any]) -> tuple:
    """真实时间优先；时间不明置底，不因推测插队。"""
    ts = (item.get("event_time") or item.get("time") or "").strip()
    uncertain = bool(item.get("time_uncertain")) or not ts or ts in {"时间不明", "未知"}
    return (1 if uncertain else 0, ts or "\uffff", str(item.get("event_id") or ""))


def timeline_item_date_prefix(item: dict[str, Any]) -> str | None:
    """从事件取出可比较的 YYYY-MM-DD；无法解析则 None。"""
    raw = (item.get("event_time") or item.get("time_text") or item.get("time") or "").strip()
    if not raw or raw in {"时间不明", "未知"}:
        return None
    m = _DATE_PREFIX_RE.match(raw)
    return m.group(1) if m else None


def timeline_item_is_uncertain(item: dict[str, Any]) -> bool:
    if item.get("time_uncertain"):
        return True
    if (item.get("time_precision") or "").upper() == "UNKNOWN":
        return True
    return timeline_item_date_prefix(item) is None


def build_timeline_facets(items: list[dict[str, Any]]) -> dict[str, Any]:
    """基于全量 items 统计筛选面。"""
    event_types: dict[str, int] = {}
    source_modes: dict[str, int] = {}
    cases: dict[str, dict[str, Any]] = {}
    subject_kinds: dict[str, int] = {}
    persons: dict[str, dict[str, Any]] = {}
    accounts: dict[str, dict[str, Any]] = {}
    uncertain = 0
    for item in items:
        et = (item.get("event_type") or "").upper() or "UNKNOWN"
        event_types[et] = event_types.get(et, 0) + 1
        sm = (item.get("source_mode") or "recorded").lower()
        source_modes[sm] = source_modes.get(sm, 0) + 1
        sk = (item.get("subject_kind") or "").upper()
        if sk:
            subject_kinds[sk] = subject_kinds.get(sk, 0) + 1
        if timeline_item_is_uncertain(item):
            uncertain += 1
        cid = item.get("case_id") or ""
        cname = item.get("case_name") or cid
        if cid:
            bucket = cases.setdefault(cid, {"case_id": cid, "case_name": cname, "count": 0})
            bucket["count"] += 1
        pid = item.get("person_subject_id") or (
            item.get("subject_id") if (item.get("subject_kind") or "").upper() == "PERSON" else ""
        )
        pname = item.get("person_subject") or (
            item.get("subject") if (item.get("subject_kind") or "").upper() == "PERSON" else ""
        )
        if pid:
            pb = persons.setdefault(pid, {"id": pid, "name": pname or pid, "kind": "PERSON", "count": 0})
            pb["count"] += 1
            if pname:
                pb["name"] = pname
        aid = item.get("account_subject_id") or (
            item.get("subject_id") if (item.get("subject_kind") or "").upper() == "ACCOUNT" else ""
        )
        aname = item.get("account_subject") or (
            item.get("subject") if (item.get("subject_kind") or "").upper() == "ACCOUNT" else ""
        )
        if aid:
            ab = accounts.setdefault(aid, {"id": aid, "name": aname or aid, "kind": "ACCOUNT", "count": 0})
            ab["count"] += 1
            if aname:
                ab["name"] = aname
    return {
        "event_types": event_types,
        "source_modes": source_modes,
        "subject_kinds": subject_kinds,
        "cases": list(cases.values()),
        "persons": list(persons.values()),
        "accounts": list(accounts.values()),
        "uncertain_count": uncertain,
        "total": len(items),
    }


def filter_timeline_items(
    items: list[dict[str, Any]],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    include_uncertain: bool = True,
    event_types: set[str] | None = None,
    source_modes: set[str] | None = None,
    subject_kind: str | None = None,
    subject_id: str | None = None,
    case_id: str | None = None,
) -> list[dict[str, Any]]:
    """对 ROLE_TIMELINE items 做投影过滤，不改变原列表。"""
    df = (date_from or "").strip()[:10] or None
    dt = (date_to or "").strip()[:10] or None
    et_set = {x.strip().upper() for x in (event_types or set()) if x and str(x).strip()}
    sm_set = {x.strip().lower() for x in (source_modes or set()) if x and str(x).strip()}
    sk = (subject_kind or "").strip().upper() or None
    sid = (subject_id or "").strip() or None
    cid = (case_id or "").strip() or None

    out: list[dict[str, Any]] = []
    for item in items:
        uncertain = timeline_item_is_uncertain(item)
        prefix = timeline_item_date_prefix(item)
        if df or dt:
            if uncertain or not prefix:
                if not include_uncertain:
                    continue
            else:
                if df and prefix < df:
                    continue
                if dt and prefix > dt:
                    continue
        elif uncertain and not include_uncertain:
            continue

        if et_set and (item.get("event_type") or "").upper() not in et_set:
            continue
        if sm_set and (item.get("source_mode") or "recorded").lower() not in sm_set:
            continue
        if sk:
            item_sk = (item.get("subject_kind") or "").upper()
            if sk == "PERSON":
                if item_sk != "PERSON" and not item.get("person_subject_id"):
                    continue
            elif sk == "ACCOUNT":
                if item_sk != "ACCOUNT" and not item.get("account_subject_id"):
                    continue
            elif item_sk != sk:
                continue
        if sid:
            matched = sid in {
                item.get("subject_id") or "",
                item.get("person_subject_id") or "",
                item.get("account_subject_id") or "",
            }
            if not matched:
                continue
        if cid and (item.get("case_id") or "") != cid:
            continue
        out.append(item)
    return out


def parse_csv_set(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {p.strip() for p in str(raw).split(",") if p.strip()}
