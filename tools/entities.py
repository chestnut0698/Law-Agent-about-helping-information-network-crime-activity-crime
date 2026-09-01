"""确定性实体提及、标准化、强标识碰撞与 R001–R005 规则命中。

规则抽取在本地扫描 document_chunks.text_raw，不出网，不经过模型网关。
对外产物与线索卡只携带脱敏展示值与 quote_hash；规范值仅用于库内等值碰撞。
R004/R005 基于已落库的转账/联络事件，不引入 Neo4j。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from agents.gateway import canonical_hash, quote_hash
from app.config import REPO_ROOT
from app.files import (
    _insert,
    _row,
    _rows,
    db_session,
    redact_text,
    utc_now,
    new_id,
    get_global_mapper
)

EXTRACTOR_VERSION = "stage5-rule-v2"
EVENT_EXTRACTOR_VERSION = "stage5-event-v1"
STRONG_TYPES = ("PHONE", "ACCOUNT", "DEVICE", "ID_CARD")
RULE_TYPE_MAP = {"ACCOUNT": "R001", "PHONE": "R002", "DEVICE": "R003"}
LEGAL_BOUNDARY = (
    "疑似漏犯漏罪关联线索（待核验）。禁止作为定罪、并案、主从犯或量刑依据。"
)

# ---------- 建表 ----------

ENTITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entity_mentions (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    case_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    document_version_id VARCHAR(36) NOT NULL,
    chunk_id VARCHAR(64) NOT NULL,
    object_type VARCHAR(32) NOT NULL,
    surface_raw TEXT NOT NULL,
    normalized_value VARCHAR(128) NOT NULL DEFAULT '',
    mask_info_json TEXT NOT NULL DEFAULT '{}',
    possible_forms_json TEXT NOT NULL DEFAULT '[]',
    producer VARCHAR(16) NOT NULL,
    extractor_version VARCHAR(64) NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    page_start INTEGER,
    page_end INTEGER,
    quote_redacted TEXT NOT NULL DEFAULT '',
    quote_hash VARCHAR(64) NOT NULL DEFAULT '',
    run_id VARCHAR(64),
    created_at DATETIME NOT NULL,
    UNIQUE(chunk_id, extractor_version, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_mentions_task_type
ON entity_mentions(task_id, object_type, normalized_value);

CREATE TABLE IF NOT EXISTS rejected_candidates (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    fingerprint VARCHAR(64) NOT NULL,
    decision VARCHAR(32) NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    UNIQUE(task_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS rule_hits (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    rule_id VARCHAR(16) NOT NULL,
    rule_version VARCHAR(32) NOT NULL,
    fingerprint VARCHAR(64) NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME NOT NULL,
    UNIQUE(task_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS event_mentions (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    case_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    document_version_id VARCHAR(36) NOT NULL,
    chunk_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(24) NOT NULL,
    time_text VARCHAR(64) NOT NULL DEFAULT '',
    time_precision VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN',
    amount_text VARCHAR(64) NOT NULL DEFAULT '',
    channel VARCHAR(32) NOT NULL DEFAULT '',
    summary_text TEXT NOT NULL DEFAULT '',
    parties_json TEXT NOT NULL DEFAULT '[]',
    payload_hash VARCHAR(64) NOT NULL,
    extractor_version VARCHAR(64) NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    page_start INTEGER,
    page_end INTEGER,
    quote_redacted TEXT NOT NULL DEFAULT '',
    quote_hash VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    UNIQUE(chunk_id, extractor_version, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_event_mentions_task
ON event_mentions(task_id, event_type, time_text);
"""


def init_entity_db(db_path=None) -> None:
    with db_session(db_path) as conn:
        conn.executescript(ENTITY_SCHEMA_SQL)


# ---------- 配置 ----------

def _load_yaml(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        import yaml
    except ImportError:
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else default


def load_exclusions() -> dict[str, Any]:
    return _load_yaml(
        REPO_ROOT / "config" / "rules" / "exclusions.yaml",
        {
            "version": "exclusions-v1",
            "phones": ["10086", "10010", "10000", "110", "13800138000"],
            "bank_accounts": [],
            "devices": [],
            "id_cards": [],
            "ips": ["127.0.0.1", "0.0.0.0"],
        },
    )


def load_rules() -> dict[str, Any]:
    return _load_yaml(
        REPO_ROOT / "config" / "rules" / "rules.yaml",
        {
            "version": "rules-v2",
            "rules": [
                {"id": "R001", "version": "v1", "object_type": "ACCOUNT", "label": "同一银行账户/卡跨案出现", "evidence_mode": "DIRECT_MATERIAL"},
                {"id": "R002", "version": "v1", "object_type": "PHONE", "label": "同一手机号跨案出现", "evidence_mode": "DIRECT_MATERIAL"},
                {"id": "R003", "version": "v1", "object_type": "DEVICE", "label": "同一设备标识跨案出现", "evidence_mode": "DIRECT_MATERIAL"},
                {"id": "R004", "version": "v1", "object_type": "TRANSFER_ACCOUNT", "label": "资金路径交叉（同账户转账活动跨案）", "evidence_mode": "RULE_INFERRED", "event_type": "TRANSFER", "party_type": "ACCOUNT"},
                {"id": "R005", "version": "v1", "object_type": "CONTACT_PHONE", "label": "共同联系人（同手机号联络事件跨案）", "evidence_mode": "RULE_INFERRED", "event_type": "CONTACT", "party_type": "PHONE"},
            ],
        },
    )


def load_ocr_pairs() -> list[tuple[str, str]]:
    data = _load_yaml(
        REPO_ROOT / "config" / "normalization" / "ocr_confusables.yaml",
        {"version": "norm-v1", "pairs": [["0", "O"], ["1", "l"], ["8", "B"]]},
    )
    pairs = []
    for item in data.get("pairs") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
    return pairs


# ---------- 校验与标准化 ----------

def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def id_card_ok(value: str) -> bool:
    if len(value) != 18:
        return False
    body, check = value[:17], value[-1].upper()
    if not body.isdigit():
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    mapping = "10X98765432"
    total = sum(int(body[i]) * weights[i] for i in range(17))
    return mapping[total % 11] == check


def to_halfwidth(text: str) -> str:
    chars = []
    for ch in text or "":
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    return "".join(chars)


def strip_separators(text: str) -> str:
    return re.sub(r"[\s\-_.／/]", "", to_halfwidth(text or ""))


def mask_info(surface: str) -> dict[str, Any]:
    positions = [i for i, ch in enumerate(surface or "") if ch in "*＊xX"]
    return {"masked": bool(positions), "positions": positions}


def possible_forms(normalized: str) -> list[str]:
    """OCR 混淆只生成标注候选，不替换原文。"""
    if not normalized:
        return []
    forms = {normalized}
    for src, dst in load_ocr_pairs():
        if src in normalized:
            forms.add(normalized.replace(src, dst))
        if dst in normalized:
            forms.add(normalized.replace(dst, src))
    if normalized.startswith("86") and len(normalized) == 13 and normalized[2:].startswith("1"):
        forms.add(normalized[2:])
    forms.discard(normalized)
    return sorted(forms)


def normalize_identifier(object_type: str, surface: str) -> str:
    compact = strip_separators(surface)
    if object_type == "PHONE":
        digits = re.sub(r"\D", "", compact)
        if digits.startswith("86") and len(digits) == 13:
            digits = digits[2:]
        return digits
    if object_type in {"ACCOUNT", "DEVICE", "ID_CARD"}:
        return re.sub(r"\D", "", compact).upper()
    if object_type == "IP":
        return compact
    return compact


def public_surface(surface_raw: str) -> str:
    redacted, _ = redact_text(surface_raw or "")
    return redacted


def redacted_quote(chunk: dict[str, Any], start: int, end: int) -> tuple[str, str]:
    """从整段脱敏文本中切窗口，避免窗口重脱敏导致占位符序号错位。

    前提：chunk 中必须已包含 'text_redacted' 和 '_hits' 字段，
          这些由上层预处理（extract_task_mentions）在事务外填充。
    """
    target = chunk.get("text_redacted")
    if not target:
        # 容错：如果没有预脱敏，则直接返回原始文本片段
        raw = chunk.get("text_raw") or ""
        snippet = raw[start:end]
        return snippet, quote_hash(snippet)

    # 优先尝试精确匹配占位符（根据 start/end）
    hits = chunk.get("_hits") or []
    for hit in hits:
        if hit.start == start and hit.end == end:
            placeholder = hit.placeholder
            idx = target.find(placeholder)
            if idx != -1:
                lo = max(0, idx - 16)
                hi = min(len(target), idx + len(placeholder) + 16)
                snippet = target[lo:hi]
                return snippet, quote_hash(snippet)

    # 没有精确匹配，直接截取脱敏文本中的对应区间
    snippet = target[start:end]
    return snippet, quote_hash(snippet)


def is_excluded(object_type: str, normalized: str, exclusions: dict[str, Any]) -> bool:
    buckets = {
        "PHONE": "phones",
        "ACCOUNT": "bank_accounts",
        "DEVICE": "devices",
        "ID_CARD": "id_cards",
        "IP": "ips",
    }
    values = {str(item) for item in (exclusions.get(buckets.get(object_type, ""), []) or [])}
    return normalized in values


def candidate_fingerprint(object_type: str, normalized: str, case_ids: list[str]) -> str:
    return canonical_hash(
        {"type": object_type, "value": normalized, "cases": sorted(set(case_ids))}
    )


# ---------- 规则抽取 ----------

_ID_CARD_RE = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)
_PHONE_FULL_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_PHONE_MASK_RE = re.compile(r"(?<!\d)(1[3-9]\d{0,2}[*＊xX]{2,6}\d{2,4})(?!\d)")
# 连续卡号，或带空格/横杠等分隔符的分组写法（先匹配再规范化做 Luhn）
_CARD_RE = re.compile(
    r"(?<!\d)("
    r"[1-9]\d{14,18}"
    r"|"
    r"[1-9]\d{3}(?:[\s\-_.／/]+\d{4}){2,3}(?:[\s\-_.／/]+\d{1,4})?"
    r")(?!\d)"
)
_IMEI_RE = re.compile(r"(?<!\d)(\d{15})(?!\d)")
_IP_RE = re.compile(r"(?<!\d)((?:\d{1,3}\.){3}\d{1,3})(?!\d)")
_TAIL_ONLY_RE = re.compile(r"(?:尾号|卡号尾号|账号尾号)[:：\s]*(\d{4})(?!\d)")
_EVENT_SPLIT_RE = re.compile(r"[。；;\n]+")
_DATE_RE = re.compile(
    r"((?:20\d{2}|19\d{2})[年\-/\.](?:0?[1-9]|1[0-2])[月\-/\.](?:0?[1-9]|[12]\d|3[01])日?"
    r"|(?:0?[1-9]|1[0-2])月(?:0?[1-9]|[12]\d|3[01])日)"
)
_AMOUNT_RE = re.compile(
    r"((?:人民币)?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:万|万元|元|万元人民币|元人民币))"
)
_TRANSFER_VERBS = ("转账", "转入", "转出", "汇款", "汇入", "汇出", "打款", "付款", "支付", "收款")
_CONTACT_KEYWORDS = (
    ("通话", "PHONE_CALL"),
    ("电话联系", "PHONE_CALL"),
    ("拨打", "PHONE_CALL"),
    ("致电", "PHONE_CALL"),
    ("联系", "CONTACT"),
    ("联络", "CONTACT"),
    ("微信", "WECHAT"),
    ("QQ", "QQ"),
    ("短信", "SMS"),
    ("聊天", "CHAT"),
)


def extract_rule_mentions(text: str) -> list[dict[str, Any]]:
    """从原文抽取强标识。部分掩码会入表但不参与碰撞。"""
    found: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []

    def take(start: int, end: int) -> bool:
        for lo, hi in occupied:
            if start < hi and end > lo:
                return False
        occupied.append((start, end))
        return True

    for match in _ID_CARD_RE.finditer(text or ""):
        raw = match.group(1)
        if id_card_ok(raw) and take(match.start(1), match.end(1)):
            found.append(_mention_hit("ID_CARD", raw, match.start(1), match.end(1)))

    for match in _CARD_RE.finditer(text or ""):
        raw = match.group(1)
        digits = normalize_identifier("ACCOUNT", raw)
        # 仅尾号四位、未满 16 位或身份证号不进银行卡强标识
        if len(digits) < 16 or len(digits) > 19:
            continue
        if len(digits) == 18 and id_card_ok(digits):
            continue
        if not take(match.start(1), match.end(1)):
            continue
        if luhn_ok(digits):
            found.append(_mention_hit("ACCOUNT", raw, match.start(1), match.end(1)))
        else:
            # 格式像卡号但校验失败：入表可见，不参与强碰撞（避免静默丢掉导致“看不见为何无线索”）
            hit = _mention_hit("ACCOUNT", raw, match.start(1), match.end(1))
            hit["normalized_value"] = ""
            hit["mask_info"] = {
                "masked": True,
                "positions": list(range(len(raw))),
                "kind": "luhn_failed",
            }
            hit["possible_forms"] = []
            found.append(hit)

    # 尾号单独出现只记为掩码式提及，不参与强碰撞（normalized 为空）
    for match in _TAIL_ONLY_RE.finditer(text or ""):
        raw = match.group(0)
        if take(match.start(0), match.end(0)):
            hit = _mention_hit("ACCOUNT", raw, match.start(0), match.end(0))
            hit["normalized_value"] = ""
            hit["mask_info"] = {"masked": True, "positions": list(range(len(raw))), "kind": "tail_only"}
            hit["possible_forms"] = []
            found.append(hit)

    for match in _IMEI_RE.finditer(text or ""):
        raw = match.group(1)
        if luhn_ok(raw) and take(match.start(1), match.end(1)):
            found.append(_mention_hit("DEVICE", raw, match.start(1), match.end(1)))

    for match in _PHONE_FULL_RE.finditer(text or ""):
        raw = match.group(1)
        if take(match.start(1), match.end(1)):
            found.append(_mention_hit("PHONE", raw, match.start(1), match.end(1)))

    for match in _PHONE_MASK_RE.finditer(text or ""):
        raw = match.group(1)
        if "*" in raw or "＊" in raw or "x" in raw.lower():
            if take(match.start(1), match.end(1)):
                found.append(_mention_hit("PHONE", raw, match.start(1), match.end(1)))

    for match in _IP_RE.finditer(text or ""):
        raw = match.group(1)
        parts = raw.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            if take(match.start(1), match.end(1)):
                found.append(_mention_hit("IP", raw, match.start(1), match.end(1)))
    return found


def _mention_hit(object_type: str, surface: str, start: int, end: int) -> dict[str, Any]:
    normalized = normalize_identifier(object_type, surface)
    info = mask_info(surface)
    return {
        "object_type": object_type,
        "surface_raw": surface,
        "normalized_value": "" if info["masked"] else normalized,
        "mask_info": info,
        "possible_forms": possible_forms(normalized) if not info["masked"] else [],
        "char_start": start,
        "char_end": end,
        "producer": "RULE",
    }


# ---------- 事件抽取 ----------

def _event_time(sentence: str) -> tuple[str, str]:
    match = _DATE_RE.search(sentence or "")
    if not match:
        return "", "UNKNOWN"
    value = match.group(1)
    return value, "DAY" if "日" in value or value.count("-") >= 2 or value.count("/") >= 2 else "MONTH"


def _event_channel(sentence: str) -> str:
    for keyword, channel in _CONTACT_KEYWORDS:
        if keyword in (sentence or ""):
            return channel
    return ""


def _event_parties(sentence: str) -> list[str]:
    parties: list[str] = []
    for hit in extract_rule_mentions(sentence or ""):
        if hit["object_type"] in {"ACCOUNT", "PHONE"}:
            parties.append(public_surface(hit["surface_raw"]))
    seen = set()
    deduped = []
    for item in parties:
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped[:4]


def extract_event_mentions(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not text:
        return found
    cursor = 0
    for part in _EVENT_SPLIT_RE.split(text):
        sentence = (part or "").strip()
        if not sentence:
            cursor += len(part or "") + 1
            continue
        start = text.find(part, cursor)
        if start < 0:
            start = text.find(sentence, cursor)
        if start < 0:
            start = cursor
        end = start + len(part)
        cursor = end + 1
        time_text, time_precision = _event_time(sentence)
        amount = ""
        amount_match = _AMOUNT_RE.search(sentence)
        if amount_match:
            amount = amount_match.group(1)

        if any(keyword in sentence for keyword in _TRANSFER_VERBS):
            found.append(
                {
                    "event_type": "TRANSFER",
                    "time_text": time_text,
                    "time_precision": time_precision,
                    "amount_text": amount,
                    "channel": "BANKING",
                    "summary_text": sentence[:240],
                    "parties": _event_parties(sentence),
                    "char_start": start,
                    "char_end": end,
                }
            )

        channel = _event_channel(sentence)
        if channel:
            found.append(
                {
                    "event_type": "CONTACT",
                    "time_text": time_text,
                    "time_precision": time_precision,
                    "amount_text": "",
                    "channel": channel,
                    "summary_text": sentence[:240],
                    "parties": _event_parties(sentence),
                    "char_start": start,
                    "char_end": end,
                }
            )
    return found


# ---------- 任务范围原文 chunk ----------

def list_task_raw_chunks(conn, case_ids: list[str]) -> list[dict[str, Any]]:
    if not case_ids:
        return []
    placeholders = ",".join("?" for _ in case_ids)
    return _rows(
        conn,
        f"""
        SELECT c.id AS chunk_id, c.document_version_id, c.ordinal,
               c.page_start, c.page_end, c.text_raw, c.text_redacted,
               d.id AS document_id, d.filename, d.case_id
        FROM document_chunks c
        JOIN document_versions v ON v.id = c.document_version_id
        JOIN documents d ON d.id = v.document_id
        WHERE d.case_id IN ({placeholders})
          AND d.deleted_at IS NULL
          AND v.is_current = 1
          AND v.is_active = 1
          AND c.is_active = 1
          AND IFNULL(c.stale, 0) = 0
        ORDER BY d.case_id, d.filename, c.ordinal
        """,
        tuple(case_ids),
    )


def persist_mentions(
    conn,
    *,
    task_id: str,
    chunk: dict[str, Any],
    hits: list[dict[str, Any]],
    run_id: str | None = None,
) -> int:
    inserted = 0
    now = utc_now()
    for hit in hits:
        quote, qhash = redacted_quote(
            chunk,
            int(hit["char_start"]),
            int(hit["char_end"]),
        )
        payload_hash = canonical_hash(
            {
                "type": hit["object_type"],
                "surface": hit["surface_raw"],
                "start": hit["char_start"],
                "producer": hit["producer"],
            }
        )
        existing = _row(
            conn,
            "SELECT id FROM entity_mentions WHERE chunk_id = ? AND extractor_version = ? AND payload_hash = ?",
            (chunk["chunk_id"], EXTRACTOR_VERSION, payload_hash),
        )
        if existing:
            continue
        _insert(
            conn,
            "entity_mentions",
            {
                "id": new_id(),
                "task_id": task_id,
                "case_id": chunk["case_id"],
                "document_id": chunk["document_id"],
                "document_version_id": chunk["document_version_id"],
                "chunk_id": chunk["chunk_id"],
                "object_type": hit["object_type"],
                "surface_raw": hit["surface_raw"],
                "normalized_value": hit.get("normalized_value") or "",
                "mask_info_json": json.dumps(hit.get("mask_info") or {}, ensure_ascii=False),
                "possible_forms_json": json.dumps(hit.get("possible_forms") or [], ensure_ascii=False),
                "producer": hit.get("producer") or "RULE",
                "extractor_version": EXTRACTOR_VERSION,
                "payload_hash": payload_hash,
                "char_start": hit.get("char_start"),
                "char_end": hit.get("char_end"),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "quote_redacted": quote,
                "quote_hash": qhash,
                "run_id": run_id,
                "created_at": now,
            },
        )
        inserted += 1
    return inserted


def extract_task_mentions(task_id: str, case_ids: list[str], db_path=None) -> dict[str, Any]:
    init_entity_db(db_path)

    # ---- 第一步：获取所有 chunks（事务外） ----
    with db_session(db_path) as conn:
        chunks = list_task_raw_chunks(conn, case_ids)
    scanned = len(chunks)

    # ---- 第二步：预处理所有 chunks（事务外） ----
    mapper = get_global_mapper()
    for chunk in chunks:
        raw = chunk.get("text_raw") or ""
        redacted, hits = redact_text(raw)  # 纯脱敏，不写数据库
        # 将临时占位符替换为持久化占位符（写入 entity_global_map）
        for hit in hits:
            persistent_placeholder = mapper.get_or_create(
                original=hit.original,
                sens_type=hit.sens_type,
                task_id=task_id
            )
            hit.placeholder = persistent_placeholder
        # 缓存结果到 chunk 中
        chunk["text_redacted"] = redacted
        chunk["_hits"] = hits

    # ---- 第三步：事务内持久化 mentions ----
    inserted = 0
    with db_session(db_path) as conn:
        for chunk in chunks:
            # extract_rule_mentions 不涉及脱敏，可以安全在事务内调用
            rule_hits = extract_rule_mentions(chunk.get("text_raw") or "")
            # persist_mentions 内部调用 redacted_quote，现在会使用 chunk 中缓存的脱敏数据
            inserted += persist_mentions(conn, task_id=task_id, chunk=chunk, hits=rule_hits)

        mentions = _rows(
            conn,
            "SELECT * FROM entity_mentions WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )

    return {
        "inserted": inserted,
        "scanned_chunks": scanned,
        "mention_count": len(mentions),
        "mentions": mentions,
    }

def persist_events(conn, *, task_id: str, chunk: dict[str, Any], hits: list[dict[str, Any]]) -> int:
    inserted = 0
    now = utc_now()
    for hit in hits:
        quote, qhash = redacted_quote(chunk, int(hit["char_start"]), int(hit["char_end"]))
        payload_hash = canonical_hash(
            {
                "event_type": hit["event_type"],
                "summary": hit.get("summary_text") or "",
                "start": hit.get("char_start"),
                "amount": hit.get("amount_text") or "",
                "channel": hit.get("channel") or "",
            }
        )
        existing = _row(
            conn,
            "SELECT id FROM event_mentions WHERE chunk_id = ? AND extractor_version = ? AND payload_hash = ?",
            (chunk["chunk_id"], EVENT_EXTRACTOR_VERSION, payload_hash),
        )
        if existing:
            continue
        _insert(
            conn,
            "event_mentions",
            {
                "id": new_id(),
                "task_id": task_id,
                "case_id": chunk["case_id"],
                "document_id": chunk["document_id"],
                "document_version_id": chunk["document_version_id"],
                "chunk_id": chunk["chunk_id"],
                "event_type": hit["event_type"],
                "time_text": hit.get("time_text") or "",
                "time_precision": hit.get("time_precision") or "UNKNOWN",
                "amount_text": hit.get("amount_text") or "",
                "channel": hit.get("channel") or "",
                "summary_text": hit.get("summary_text") or "",
                "parties_json": json.dumps(hit.get("parties") or [], ensure_ascii=False),
                "payload_hash": payload_hash,
                "extractor_version": EVENT_EXTRACTOR_VERSION,
                "char_start": hit.get("char_start"),
                "char_end": hit.get("char_end"),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "quote_redacted": quote,
                "quote_hash": qhash,
                "created_at": now,
            },
        )
        inserted += 1
    return inserted


def extract_task_events(task_id: str, case_ids: list[str], db_path=None) -> dict[str, Any]:
    init_entity_db(db_path)
    inserted = 0
    scanned = 0
    with db_session(db_path) as conn:
        chunks = list_task_raw_chunks(conn, case_ids)
        scanned = len(chunks)
        for chunk in chunks:
            hits = extract_event_mentions(chunk.get("text_raw") or "")
            inserted += persist_events(conn, task_id=task_id, chunk=chunk, hits=hits)
        events = _rows(
            conn,
            """
            SELECT e.*, d.filename
            FROM event_mentions e
            JOIN documents d ON d.id = e.document_id
            WHERE e.task_id = ?
              AND d.deleted_at IS NULL
            ORDER BY
                CASE WHEN e.time_precision = 'UNKNOWN' THEN 1 ELSE 0 END,
                e.time_text,
                e.created_at
            """,
            (task_id,),
        )
    public_events = []
    for item in events:
        public_events.append(
            {
                "event_id": item["id"],
                "event_type": item["event_type"],
                "time_text": item.get("time_text") or "",
                "time_precision": item.get("time_precision") or "UNKNOWN",
                "amount_text": item.get("amount_text") or "",
                "channel": item.get("channel") or "",
                "summary_text": item.get("summary_text") or "",
                "parties": json.loads(item.get("parties_json") or "[]"),
                "case_id": item["case_id"],
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
                "chunk_id": item["chunk_id"],
                "filename": item.get("filename"),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "quote": item.get("quote_redacted") or "",
                "quote_hash": item.get("quote_hash") or "",
            }
        )
    return {
        "inserted": inserted,
        "scanned_chunks": scanned,
        "event_count": len(public_events),
        "events": public_events,
        "extractor_version": EVENT_EXTRACTOR_VERSION,
    }


# ---------- 碰撞 ----------

def list_rejected_fingerprints(conn, task_id: str) -> set[str]:
    rows = _rows(
        conn,
        "SELECT fingerprint FROM rejected_candidates WHERE task_id = ?",
        (task_id,),
    )
    return {row["fingerprint"] for row in rows}


def remember_rejection(task_id: str, fingerprint: str, decision: str, reason: str, db_path=None) -> None:
    init_entity_db(db_path)
    with db_session(db_path) as conn:
        existing = _row(
            conn,
            "SELECT id FROM rejected_candidates WHERE task_id = ? AND fingerprint = ?",
            (task_id, fingerprint),
        )
        if existing:
            return
        _insert(
            conn,
            "rejected_candidates",
            {
                "id": new_id(),
                "task_id": task_id,
                "fingerprint": fingerprint,
                "decision": decision,
                "reason": reason or "",
                "created_at": utc_now(),
            },
        )


def collide_mentions(
    mentions: list[dict[str, Any]],
    *,
    case_names: dict[str, str],
    rejected: set[str],
    exclusions: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    exclusions = exclusions or load_exclusions()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for mention in mentions:
        object_type = mention.get("object_type")
        if object_type not in STRONG_TYPES:
            continue
        info = mention.get("mask_info")
        if isinstance(info, str):
            info = json.loads(info or "{}")
        if (info or {}).get("masked"):
            continue
        value = mention.get("normalized_value") or ""
        if not value or is_excluded(object_type, value, exclusions):
            continue
        groups[(object_type, value)].append(mention)

    candidates = []
    for (object_type, value), items in groups.items():
        case_ids = sorted({item["case_id"] for item in items})
        if len(case_ids) < 2:
            continue
        if any(not any(item["chunk_id"] for item in items if item["case_id"] == case_id) for case_id in case_ids):
            continue
        fingerprint = candidate_fingerprint(object_type, value, case_ids)
        if fingerprint in rejected:
            continue
        records = []
        for item in items:
            records.append(
                {
                    "case_id": item["case_id"],
                    "case_name": case_names.get(item["case_id"]) or item["case_id"],
                    "value": public_surface(item.get("surface_raw") or ""),
                    "source": {
                        "document_name": item.get("filename") or item.get("document_id"),
                        "page_no": item.get("page_start"),
                        "chunk_id": item["chunk_id"],
                        "document_version_id": item.get("document_version_id"),
                        "document_id": item.get("document_id"),
                        "quote": item.get("quote_redacted") or "",
                        "quote_hash": item.get("quote_hash") or "",
                    },
                }
            )
        display = {
            "PHONE": "同一手机号跨案出现",
            "ACCOUNT": "同一银行账户/卡跨案出现",
            "DEVICE": "同一设备标识跨案出现",
            "ID_CARD": "同一身份证件号跨案出现",
        }.get(object_type, object_type)
        candidates.append(
            {
                "candidate_id": new_id(),
                "fingerprint": fingerprint,
                "entity_type": object_type,
                "display_name": display,
                "confidence_label": "待核验",
                "match_basis": [
                    f"规范化值在 {len(case_ids)} 起案件中等值出现",
                    "每案至少一条可定位 chunk 证据",
                ],
                "differences": ["系统不自动合并，是否同一对象由人工决定"],
                "records": records,
                "impact": {"case_count": len(case_ids), "mention_count": len(items)},
                "decision": "PENDING",
                "reason": "",
                "correction": None,
            }
        )
    return candidates


def extract_and_collide(
    task_id: str,
    cases: list[dict[str, Any]],
    db_path=None,
) -> dict[str, Any]:
    case_ids = [item["case_id"] for item in cases]
    case_names = {item["case_id"]: item.get("display_name") or item["case_id"] for item in cases}
    extracted = extract_task_mentions(task_id, case_ids, db_path=db_path)
    exclusions = load_exclusions()
    with db_session(db_path) as conn:
        mentions = _rows(
            conn,
            """
            SELECT m.*, d.filename FROM entity_mentions m
            JOIN documents d ON d.id = m.document_id
            WHERE m.task_id = ?
            """,
            (task_id,),
        )
        rejected = list_rejected_fingerprints(conn, task_id)
    candidates = collide_mentions(
        mentions,
        case_names=case_names,
        rejected=rejected,
        exclusions=exclusions,
    )
    public_mentions = [
        {
            "mention_id": item["id"],
            "object_type": item["object_type"],
            "display_name": public_surface(item.get("surface_raw") or ""),
            "producer": item["producer"],
            "masked": json.loads(item.get("mask_info_json") or "{}").get("masked", False),
            "mask_kind": json.loads(item.get("mask_info_json") or "{}").get("kind") or "",
            "records": [
                {
                    "case_id": item["case_id"],
                    "case_name": case_names.get(item["case_id"]) or item["case_id"],
                    "chunk_id": item["chunk_id"],
                    "document_version_id": item["document_version_id"],
                    "filename": item.get("filename"),
                    "page_start": item.get("page_start"),
                    "quote": item.get("quote_redacted"),
                    "quote_hash": item.get("quote_hash"),
                }
            ],
        }
        for item in mentions
    ]
    return {
        "scanned_chunks": extracted["scanned_chunks"],
        "inserted": extracted["inserted"],
        "mention_count": extracted["mention_count"],
        "mentions": public_mentions,
        "candidates": candidates,
        "exclusion_version": exclusions.get("version"),
        "extractor_version": EXTRACTOR_VERSION,
    }


# ---------- 规则命中 ----------

def _strong_parties_from_text(text: str) -> list[dict[str, Any]]:
    """从事件句子再抽强标识；忽略掩码/尾号/Luhn 失败，避免假命中。"""
    parties: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for hit in extract_rule_mentions(text or ""):
        object_type = hit.get("object_type")
        normalized = hit.get("normalized_value") or ""
        if object_type not in {"ACCOUNT", "PHONE"} or not normalized:
            continue
        key = (object_type, normalized)
        if key in seen:
            continue
        seen.add(key)
        parties.append(
            {
                "object_type": object_type,
                "normalized_value": normalized,
                "surface": public_surface(hit["surface_raw"]),
            }
        )
    return parties


def _event_evidence(event: dict[str, Any], case_names: dict[str, str]) -> dict[str, Any]:
    return {
        "chunk_id": event.get("chunk_id"),
        "document_version_id": event.get("document_version_id"),
        "quote": event.get("quote") or "",
        "quote_hash": event.get("quote_hash") or "",
        "case_id": event.get("case_id"),
        "case_name": case_names.get(event.get("case_id") or "", event.get("case_id")),
        "filename": event.get("filename"),
        "page_start": event.get("page_start"),
        "event_type": event.get("event_type"),
        "time_text": event.get("time_text") or "",
        "amount_text": event.get("amount_text") or "",
    }


def collect_event_rule_hits(
    task_id: str,
    cases: list[dict[str, Any]],
    db_path=None,
) -> list[dict[str, Any]]:
    """R004/R005：同一强标识出现在 ≥2 案的转账/联络事件中。

    不做多跳图搜索（留给 Neo4j）；本版只认「事件层同键跨案」作为路径交叉 / 共同联系人的可核验入口。
    """
    init_entity_db(db_path)
    case_ids = [item["case_id"] for item in cases]
    case_names = {
        item["case_id"]: item.get("display_name") or item["case_id"] for item in cases
    }
    events = extract_task_events(task_id, case_ids, db_path=db_path).get("events") or []
    rules = {item["id"]: item for item in (load_rules().get("rules") or [])}
    r004 = rules.get("R004")
    r005 = rules.get("R005")
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_phone: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for event in events:
        parties = _strong_parties_from_text(event.get("summary_text") or "")
        evidence = _event_evidence(event, case_names)
        for party in parties:
            row = {
                "event": event,
                "party": party,
                "evidence": evidence,
            }
            if event.get("event_type") == "TRANSFER" and party["object_type"] == "ACCOUNT":
                by_account[party["normalized_value"]].append(row)
            if event.get("event_type") == "CONTACT" and party["object_type"] == "PHONE":
                by_phone[party["normalized_value"]].append(row)

    hits: list[dict[str, Any]] = []
    now = utc_now()

    def _flush(spec: dict[str, Any] | None, buckets: dict[str, list[dict[str, Any]]], entity_type: str):
        if not spec:
            return
        with db_session(db_path) as conn:
            for normalized, rows in buckets.items():
                case_ids_hit = sorted(
                    {
                        item["event"]["case_id"]
                        for item in rows
                        if item["event"].get("case_id")
                    }
                )
                evidence = []
                chunk_ids = []
                seen_chunks = set()
                for item in rows:
                    ev = item["evidence"]
                    chunk_id = ev.get("chunk_id")
                    if not chunk_id or chunk_id in seen_chunks:
                        continue
                    seen_chunks.add(chunk_id)
                    chunk_ids.append(chunk_id)
                    evidence.append(ev)
                if len(case_ids_hit) < 2 or len(seen_chunks) < 2:
                    continue
                fingerprint = canonical_hash(
                    {
                        "rule_id": spec["id"],
                        "rule_version": spec.get("version"),
                        "normalized_value": normalized,
                        "chunks": sorted(set(chunk_ids)),
                    }
                )
                surface = next(
                    (item["party"]["surface"] for item in rows if item["party"].get("surface")),
                    "",
                )
                payload = {
                    "rule_id": spec["id"],
                    "rule_version": spec.get("version"),
                    "label": spec.get("label"),
                    "evidence_mode": spec.get("evidence_mode") or "RULE_INFERRED",
                    "entity_type": entity_type,
                    "normalized_value_public": surface,
                    "cases": [
                        {"case_id": cid, "case_name": case_names.get(cid, cid)}
                        for cid in case_ids_hit
                    ],
                    "evidence": evidence,
                    "fingerprint": fingerprint,
                    "trigger": {
                        "event_type": spec.get("event_type"),
                        "party_type": spec.get("party_type"),
                        "note": "事件层同键跨案；不作共同犯罪或控制关系认定。",
                    },
                }
                existing = _row(
                    conn,
                    "SELECT id FROM rule_hits WHERE task_id = ? AND fingerprint = ?",
                    (task_id, fingerprint),
                )
                if not existing:
                    _insert(
                        conn,
                        "rule_hits",
                        {
                            "id": new_id(),
                            "task_id": task_id,
                            "rule_id": spec["id"],
                            "rule_version": spec.get("version") or "v1",
                            "fingerprint": fingerprint,
                            "payload_json": json.dumps(payload, ensure_ascii=False),
                            "created_at": now,
                        },
                    )
                hits.append(payload)

    _flush(r004, by_account, "TRANSFER_ACCOUNT")
    _flush(r005, by_phone, "CONTACT_PHONE")
    return hits


def collect_rule_hits(
    task_id: str,
    cases: list[dict[str, Any]],
    db_path=None,
) -> list[dict[str, Any]]:
    """R001–R003（强标识提及）+ R004/R005（事件层），输出脱敏证据。"""
    init_entity_db(db_path)
    collision = extract_and_collide(task_id, cases, db_path=db_path)
    rules = {item["object_type"]: item for item in (load_rules().get("rules") or [])}
    hits = []
    now = utc_now()
    with db_session(db_path) as conn:
        for candidate in collision["candidates"]:
            object_type = candidate["entity_type"]
            spec = rules.get(object_type)
            if not spec or spec.get("id") not in {"R001", "R002", "R003"}:
                continue
            evidence = []
            chunk_ids = []
            for record in candidate.get("records") or []:
                source = record.get("source") or {}
                if not source.get("chunk_id"):
                    continue
                chunk_ids.append(source["chunk_id"])
                evidence.append(
                    {
                        "chunk_id": source["chunk_id"],
                        "document_version_id": source.get("document_version_id"),
                        "quote": source.get("quote") or "",
                        "quote_hash": source.get("quote_hash") or "",
                        "case_id": record.get("case_id"),
                        "case_name": record.get("case_name"),
                        "filename": source.get("document_name"),
                        "page_start": source.get("page_no"),
                    }
                )
            case_ids = sorted({record["case_id"] for record in candidate.get("records") or [] if record.get("case_id")})
            if len(case_ids) < 2 or len({item["chunk_id"] for item in evidence}) < 2:
                continue
            fingerprint = canonical_hash(
                {
                    "rule_id": spec["id"],
                    "rule_version": spec.get("version"),
                    "candidate": candidate.get("fingerprint"),
                    "chunks": sorted(set(chunk_ids)),
                }
            )
            payload = {
                "rule_id": spec["id"],
                "rule_version": spec.get("version"),
                "label": spec.get("label"),
                "evidence_mode": spec.get("evidence_mode") or "DIRECT_MATERIAL",
                "entity_type": object_type,
                "cases": [
                    {"case_id": cid, "case_name": next(
                        (r.get("case_name") for r in candidate["records"] if r.get("case_id") == cid),
                        cid,
                    )}
                    for cid in case_ids
                ],
                "evidence": evidence,
                "fingerprint": fingerprint,
                "candidate_fingerprint": candidate.get("fingerprint"),
            }
            existing = _row(
                conn,
                "SELECT id, payload_json FROM rule_hits WHERE task_id = ? AND fingerprint = ?",
                (task_id, fingerprint),
            )
            if not existing:
                _insert(
                    conn,
                    "rule_hits",
                    {
                        "id": new_id(),
                        "task_id": task_id,
                        "rule_id": spec["id"],
                        "rule_version": spec.get("version") or "v1",
                        "fingerprint": fingerprint,
                        "payload_json": json.dumps(payload, ensure_ascii=False),
                        "created_at": now,
                    },
                )
            hits.append(payload)
    hits.extend(collect_event_rule_hits(task_id, cases, db_path=db_path))
    return hits


def verify_quote_hash(chunk_text: str, quote: str, expected_hash: str) -> bool:
    if not quote or quote not in (chunk_text or ""):
        return False
    return quote_hash(quote) == expected_hash
