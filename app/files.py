"""案件电子卷宗的文件上传与后续处理。

覆盖上传落盘与哈希、版本链、解析与页级 OCR、质量状态、分块、脱敏、
外发门控、人工修正，以及提供给 Agent 调用的材料工具。
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol
from prikit import PDFAnonymizer
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer import RecognizerResult

from app.config import DATABASE_PATH,MATERIAL_STORAGE_DIR,REDACTION_STORAGE_DIR


# PriKit/Presidio 实体类型 → 内部 sens_type 映射
PRIKIT_ENTITY_MAP = {
    "PERSON": "name",
    "PHONE_NUMBER": "phone",
    "ID": "id_card",
    "CREDIT_CARD": "bank_card",
    "LOCATION": "address",
    "EMAIL_ADDRESS": "email",
    "IP_ADDRESS": "ip",
    "URL": "url",
    "DATE_TIME": "datetime",
    "NRP": "nrp",
    "CRYPTO": "crypto",
    "IBAN_CODE": "iban",
}

# 各实体类型的置信度阈值
CONFIDENCE_THRESHOLD = {
    "name": 0.6,       # NLP 识别，阈值低一些
    "address": 0.6,
    "phone": 0.85,     # 正则为主，阈值高
    "id_card": 0.9,
    "bank_card": 0.9,
    "email": 0.9,
    "ip": 0.9,
}
# 允许上传的材料文件扩展名列表
ALLOWED_MATERIAL_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".docx",
    ".txt",
}
# 文档分片时相邻 chunk 之间的重叠字符数，用于保证跨片语义不丢失
CHUNK_OVERLAP = 120
# 每个chunk的目标字符数（或token数），决定文档切分的粒度
CHUNK_SIZE = 1000
# 看门的
MATERIAL_AUTH_MODE = "allow_all"
# 单次上传文件的最大字节数限制，50MB
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# OCR文本密度阈值，用于判断页面是否有足够文字
OCR_TEXT_DENSITY_THRESHOLD = 0.08
# OCR 识别置信度阈值，低于此值的页面会被标记为低置信度，需要人工复核
OCR_LOW_CONFIDENCE_THRESHOLD = 0.75
# OCR处理失败时每页的最大重试次数
OCR_MAX_PAGE_RETRIES = 2
# 当前解析器的版本号，用于版本追踪和缓存失效
PARSER_VERSION = "stage3-v1"

# ---------- 状态与错误码 ----------

MATERIAL_STATUSES = {
    "UPLOADED",
    "PARSING",
    "PARSED",
    "NEEDS_OCR_REVIEW",
    "OCR_FAILED",
    "DUPLICATE_PENDING",
    "FAILED",
    "DELETED",
}

ERROR_CODES = {
    "UNSUPPORTED_TYPE": "MATERIAL_UNSUPPORTED_TYPE",
    "FILE_TOO_LARGE": "MATERIAL_FILE_TOO_LARGE",
    "CORRUPT_FILE": "MATERIAL_CORRUPT_FILE",
    "ENCRYPTED_FILE": "MATERIAL_ENCRYPTED_FILE",
    "OCR_FAILED": "MATERIAL_OCR_FAILED",
    "PARSE_FAILED": "MATERIAL_PARSE_FAILED",
    "AUTH_DENIED": "MATERIAL_AUTH_DENIED",
    "EGRESS_DENIED": "MATERIAL_EGRESS_DENIED",
    "NOT_FOUND": "MATERIAL_NOT_FOUND",
    "DUPLICATE_PENDING": "MATERIAL_DUPLICATE_PENDING",
    "CITATION_STALE": "MATERIAL_CITATION_STALE",
}


class MaterialError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "message": self.message, "details": self.details}


# ---------- 数据库 ----------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(100) NOT NULL,
    role VARCHAR(32) NOT NULL
);

CREATE TABLE IF NOT EXISTS case_pools (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    purpose TEXT NOT NULL,
    owner_user_id VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS case_pool_members (
    id VARCHAR(36) PRIMARY KEY,
    case_pool_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    member_role VARCHAR(32) NOT NULL,
    UNIQUE(case_pool_id, user_id)
);

CREATE TABLE IF NOT EXISTS cases (
    id VARCHAR(36) PRIMARY KEY,
    case_pool_id VARCHAR(36) NOT NULL,
    name VARCHAR(160) NOT NULL,
    case_number VARCHAR(100) NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(case_pool_id, case_number)
);

CREATE TABLE IF NOT EXISTS documents (
    id VARCHAR(36) PRIMARY KEY,
    case_id VARCHAR(36) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    stored_name VARCHAR(255) NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    size BIGINT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    uploaded_by VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'UPLOADED',
    current_version_id VARCHAR(36),
    deleted_at DATETIME,
    quality_summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS document_versions (
    id VARCHAR(36) PRIMARY KEY,
    document_id VARCHAR(36) NOT NULL,
    version_no INTEGER NOT NULL,
    parent_version_id VARCHAR(36),
    source_type VARCHAR(32) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    storage_path TEXT NOT NULL,
    content_type VARCHAR(120) NOT NULL,
    size BIGINT NOT NULL,
    parser_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    quality_summary_json TEXT NOT NULL DEFAULT '{}',
    error_code VARCHAR(64),
    error_message TEXT,
    created_by VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(document_id, version_no)
);

CREATE TABLE IF NOT EXISTS parse_jobs (
    id VARCHAR(36) PRIMARY KEY,
    document_version_id VARCHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    error_code VARCHAR(64),
    error_message TEXT,
    started_at DATETIME,
    finished_at DATETIME,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS document_pages (
    id VARCHAR(36) PRIMARY KEY,
    document_version_id VARCHAR(36) NOT NULL,
    page_no INTEGER NOT NULL,
    source VARCHAR(32) NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    text_density REAL NOT NULL DEFAULT 0,
    avg_confidence REAL,
    min_confidence REAL,
    bbox_json TEXT NOT NULL DEFAULT '[]',
    lines_json TEXT NOT NULL DEFAULT '[]',
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    status VARCHAR(32) NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_code VARCHAR(64),
    error_message TEXT,
    UNIQUE(document_version_id, page_no)
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id VARCHAR(64) PRIMARY KEY,
    document_version_id VARCHAR(36) NOT NULL,
    ordinal INTEGER NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    bbox_json TEXT NOT NULL DEFAULT '[]',
    text_raw TEXT NOT NULL,
    text_redacted TEXT NOT NULL,
    text_sha256 VARCHAR(64) NOT NULL,
    parser_version VARCHAR(64) NOT NULL,
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    is_active INTEGER NOT NULL DEFAULT 1,
    stale INTEGER NOT NULL DEFAULT 0,
    UNIQUE(document_version_id, ordinal)
);

CREATE TABLE IF NOT EXISTS redaction_items (
    id VARCHAR(36) PRIMARY KEY,
    document_version_id VARCHAR(36) NOT NULL,
    chunk_id VARCHAR(64),
    sens_type VARCHAR(32) NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    placeholder VARCHAR(64) NOT NULL,
    map_ref VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS material_audit_events (
    id VARCHAR(36) PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    actor_user_id VARCHAR(64),
    case_id VARCHAR(36),
    document_id VARCHAR(36),
    document_version_id VARCHAR(36),
    decision VARCHAR(32) NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_global_map (
    fingerprint VARCHAR(64) PRIMARY KEY,
    anonymous_id VARCHAR(48) NOT NULL UNIQUE,
    sens_type VARCHAR(24) NOT NULL,
    task_id VARCHAR(36) NOT NULL DEFAULT '',
    first_seen_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_case ON documents(case_id);
CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id);
CREATE INDEX IF NOT EXISTS idx_pages_version ON document_pages(document_version_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON document_chunks(document_version_id);
CREATE INDEX IF NOT EXISTS idx_redaction_version ON redaction_items(document_version_id);
"""

def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_connection(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DATABASE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db(db_path=None) -> None:
    """建表并补齐旧库缺失的列，可重复执行。"""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        for column, ddl in (
            ("status", "status VARCHAR(32) NOT NULL DEFAULT 'UPLOADED'"),
            ("current_version_id", "current_version_id VARCHAR(36)"),
            ("deleted_at", "deleted_at DATETIME"),
            ("quality_summary_json", "quality_summary_json TEXT NOT NULL DEFAULT '{}'"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {ddl}")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_session(db_path=None) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row(conn, sql: str, params=()) -> Optional[dict[str, Any]]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _rows(conn, sql: str, params=()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _insert(conn, table: str, fields: dict[str, Any]) -> None:
    columns = list(fields)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
        [fields[column] for column in columns],
    )


def _update(conn, table: str, row_id: str, fields: dict[str, Any]) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        [*fields.values(), row_id],
    )


def get_case(conn, case_id: str) -> Optional[dict[str, Any]]:
    return _row(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))


def ensure_demo_case(conn, case_id: str | None = None) -> str:
    """为本地导入准备可用案件：指定 id 时建该案件，未指定时复用任一已有案件。"""
    if case_id and get_case(conn, case_id):
        return case_id
    if case_id is None:
        existing = _row(conn, "SELECT id FROM cases LIMIT 1")
        if existing:
            return existing["id"]

    now = utc_now()
    if not _row(conn, "SELECT id FROM users WHERE id = ?", ("system",)):
        _insert(
            conn,
            "users",
            {"id": "system", "display_name": "system", "role": "system"},
        )

    pool = _row(conn, "SELECT id FROM case_pools LIMIT 1")
    if pool:
        pool_id = pool["id"]
    else:
        pool_id = new_id()
        _insert(
            conn,
            "case_pools",
            {
                "id": pool_id,
                "name": "demo-pool",
                "purpose": "stage3",
                "owner_user_id": "system",
                "created_at": now,
            },
        )

    resolved_case_id = case_id or new_id()
    _insert(
        conn,
        "cases",
        {
            "id": resolved_case_id,
            "case_pool_id": pool_id,
            "name": "demo-case",
            "case_number": f"DEMO-{resolved_case_id[:8]}",
            "created_by": "system",
            "created_at": now,
        },
    )
    return resolved_case_id


def get_document(conn, document_id: str) -> Optional[dict[str, Any]]:
    return _row(conn, "SELECT * FROM documents WHERE id = ?", (document_id,))


def get_version(conn, version_id: str) -> Optional[dict[str, Any]]:
    return _row(conn, "SELECT * FROM document_versions WHERE id = ?", (version_id,))


def list_versions(conn, document_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version_no ASC",
        (document_id,),
    )


def list_pages(conn, version_id: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT * FROM document_pages WHERE document_version_id = ? ORDER BY page_no ASC",
        (version_id,),
    )


def list_chunks(conn, version_id: str, active_only: bool = True) -> list[dict[str, Any]]:
    condition = " AND is_active = 1" if active_only else ""
    return _rows(
        conn,
        f"SELECT * FROM document_chunks WHERE document_version_id = ?{condition} ORDER BY ordinal ASC",
        (version_id,),
    )


def replace_chunks(conn, version_id: str, chunks: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM document_chunks WHERE document_version_id = ?", (version_id,))
    for chunk in chunks:
        _insert(conn, "document_chunks", chunk)


def deactivate_version_chunks(conn, version_id: str) -> None:
    conn.execute(
        "UPDATE document_chunks SET is_active = 0, stale = 1 WHERE document_version_id = ?",
        (version_id,),
    )


def upsert_page(conn, fields: dict[str, Any]) -> None:
    existing = _row(
        conn,
        "SELECT id FROM document_pages WHERE document_version_id = ? AND page_no = ?",
        (fields["document_version_id"], fields["page_no"]),
    )
    if existing:
        _update(conn, "document_pages", existing["id"], {k: v for k, v in fields.items() if k != "id"})
    else:
        fields.setdefault("id", new_id())
        _insert(conn, "document_pages", fields)


def set_current_version(conn, document_id: str, version_id: str) -> None:
    conn.execute(
        "UPDATE document_versions SET is_current = 0 WHERE document_id = ?", (document_id,)
    )
    conn.execute("UPDATE document_versions SET is_current = 1 WHERE id = ?", (version_id,))
    _update(conn, "documents", document_id, {"current_version_id": version_id})


def next_version_no(conn, document_id: str) -> int:
    row = _row(
        conn,
        "SELECT COALESCE(MAX(version_no), 0) AS current FROM document_versions WHERE document_id = ?",
        (document_id,),
    )
    return int(row["current"]) + 1


def add_audit(conn, **fields) -> None:
    fields.setdefault("id", new_id())
    fields.setdefault("created_at", utc_now())
    fields.setdefault("detail_json", "{}")
    _insert(conn, "material_audit_events", fields)


# ---------- 授权（仅契约，完整 RBAC 未实现） ----------


def deny_all_auth(user_id: str | None, case_id: str | None, action: str) -> tuple[bool, str]:
    """默认策略：未接入真实授权前一律拒绝材料操作。"""
    return False, "未配置材料授权。本地演示请在 .env 设置 MATERIAL_AUTH_MODE=allow_all 后重启服务"


def allow_all_auth(user_id: str | None, case_id: str | None, action: str) -> tuple[bool, str]:
    """本地与测试用桩，不是真实 RBAC。"""
    return True, "allow_all_stub"


def _default_auth():
    mode = (MATERIAL_AUTH_MODE or "").strip().lower() or "allow_all"
    return allow_all_auth if mode == "allow_all" else deny_all_auth

class GlobalEntityMapper:
    """全局实体映射器：指纹 → 匿名ID（单向，不可逆）。"""

    def __init__(self, db_path=None, salt: str = "default-salt-change-me"):
        self.db_path = db_path
        self.salt = salt

    def _fingerprint(self, original: str) -> str:
        return hashlib.sha256(f"{original}{self.salt}".encode()).hexdigest()

    def _new_anonymous_id(self, sens_type: str) -> str:
        short_uuid = uuid.uuid4().hex[:8]
        return f"{sens_type.upper()}_{short_uuid}"

    def delete_by_task_id(self, task_id: str) -> int:
        """删除指定任务的所有实体映射记录，返回删除的行数。"""
        conn = get_connection(self.db_path)
        try:
            cursor = conn.execute(
                "DELETE FROM entity_global_map WHERE task_id = ?",
                (task_id,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def get_or_create(self, original: str, sens_type: str, task_id: str = "") -> str:
        fp = self._fingerprint(original)
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT anonymous_id FROM entity_global_map WHERE fingerprint = ?",
                (fp,)
            ).fetchone()
            if row:
                # 更新 last_seen_at，同时确保 task_id 被记录（如果原来为空则补充）
                conn.execute(
                    "UPDATE entity_global_map SET last_seen_at = ?, task_id = COALESCE(NULLIF(task_id,''), ?) WHERE fingerprint = ?",
                    (utc_now(), task_id, fp)
                )
                conn.commit()
                return row[0]

            anon_id = self._new_anonymous_id(sens_type)
            now = utc_now()
            conn.execute(
                "INSERT INTO entity_global_map (fingerprint, anonymous_id, sens_type, task_id, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (fp, anon_id, sens_type, task_id, now, now)
            )
            conn.commit()
            return anon_id
        finally:
            conn.close()


# ---------- 敏感信息检测与脱敏 ----------

REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 身份证（不变）
    ("id_card", re.compile(
        r"(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
    )),
    # 银行卡：去掉18位排除，匹配15~19位纯数字或带分隔符的格式
    ("bank_card", re.compile(
        r"(?<!\d)("
        r"[1-9]\d{14,18}"                      # 纯数字15~19位
        r"|"
        r"[1-9]\d{3}(?:[\s\-_.／/]+\d{4}){2,3}(?:[\s\-_.／/]+\d{1,4})?"  # 带分隔符
        r")(?!\d)"
    )),
    # 手机号（不变）
    ("phone", re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")),
    # IMEI（不变）
    ("imei", re.compile(r"(?<!\d)(\d{15})(?!\d)")),
    # IP（不变）
    ("ip", re.compile(r"(?<!\d)((?:\d{1,3}\.){3}\d{1,3})(?!\d)")),
    # 账号（不变）
    ("account", re.compile(r"(?i)(?:账号|帐户|账户|user(?:name)?|login)[:：\s]*([A-Za-z0-9_.-]{4,32})")),
    # 地址（不变）
    ("address", re.compile(
        r"([\u4e00-\u9fff]{2,10}(?:省|市|自治区|特别行政区))?"
        r"[\u4e00-\u9fff]{1,10}(?:市|州|盟)?"
        r"[\u4e00-\u9fff]{1,12}(?:区|县|旗)"
        r"[\u4e00-\u9fff0-9\-号弄幢栋单元室楼]{0,30}"
    )),
    # 姓名：扩充关键词前缀，并增加独立人名匹配（2~4个汉字，前后非汉字或标点）
    ("name", re.compile(
    r"(?:"
    r"(?:姓名|被告人|嫌疑人|当事人|原告|被告|证人|辩护人|被害人|申诉人|被申诉人|法定代表人|负责人|联系人)"
    r"[:：\s]*([\u4e00-\u9fff·]{2,4})"
    r"|"
    r"(?<![^\s,，。；：、\(\)（）])([\u4e00-\u9fff·]{2,4})(?![^\s,，。；：、\(\)（）])"
    r")"
    )),
]

_UNREDACTED_PATTERNS = [
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
    ),
    re.compile(r"(?<!\d)\d{15}(?!\d)"),
]



@dataclass
class RedactionHit:
    sens_type: str
    start: int
    end: int
    original: str
    placeholder: str


def merge_person_spans(text: str, person_results: list) -> list:
    """
    合并相邻/重叠的 PERSON 实体，过滤单字名，去除尾部动词，去除头部动词。
    """
    if not person_results:
        return []

    # ----- 常量定义 -----
    MERGE_SEPARATORS = set("，,、；;。.！!？?：:""''（）()【】[]《》<>／/\\\t\n\r ")
    MERGE_STOP_WORDS = {"的", "和", "与", "及", "或", "以及", "及其", "暨"}
    VERB_PREFIXES = set("受被让给为由将把向对与同跟从在到予以用拿借凭靠沿顺朝往冲离除比")
    VERB_SUFFIXES = ["说", "道", "讲", "问", "答"]
    # ------------------

    merged = []
    current = person_results[0]

    for r in person_results[1:]:
        gap = text[current.end : r.start]
        clean_gap = (gap == "") or (
            not any(ch in MERGE_SEPARATORS for ch in gap) and
            not any(w in gap for w in MERGE_STOP_WORDS)
        )
        if clean_gap:
            # 检查第二个实体的第一个字符是否是动词，若是则禁止合并
            second_first_char = text[r.start:r.start+1]
            if second_first_char in VERB_PREFIXES:
                merged.append(current)
                current = r
                continue
            # 正常合并
            new_start = min(current.start, r.start)
            new_end = max(current.end, r.end)
            new_score = max(current.score, r.score)
            current = RecognizerResult(
                entity_type="PERSON",
                start=new_start,
                end=new_end,
                score=new_score
            )
        else:
            merged.append(current)
            current = r
    merged.append(current)

    # 过滤单字名
    merged = [r for r in merged if (r.end - r.start) >= 2]

    # 去除尾部动词
    filtered = []
    for r in merged:
        name = text[r.start:r.end]
        while len(name) >= 2 and name[-1] in VERB_SUFFIXES:
            name = name[:-1]
        if name != text[r.start:r.end]:
            r = RecognizerResult(
                entity_type="PERSON",
                start=r.start,
                end=r.start + len(name),
                score=r.score
            )
        filtered.append(r)

    # 去除头部动词（兜底）
    trimmed = []
    for r in filtered:
        name = text[r.start:r.end]
        trim_count = 0
        while len(name) >= 2 and name[0] in VERB_PREFIXES:
            name = name[1:]
            trim_count += 1
        if trim_count > 0 and len(name) >= 2:
            r = RecognizerResult(
                entity_type="PERSON",
                start=r.start + trim_count,
                end=r.end,
                score=r.score
            )
        trimmed.append(r)
    return trimmed

def mask_phone_number(phone: str) -> str:
    """
    对手机号进行掩码处理：保留前3位和后4位，中间用 * 填充。
    如果长度不足11位或不是纯数字，则返回原字符串（不处理）。
    """
    # 去除可能的 +86、空格、横线等前缀（简单处理）
    cleaned = phone.replace("+86", "").replace("-", "").replace(" ", "")
    if cleaned.isdigit() and len(cleaned) == 11:
        return cleaned[:3] + "****" + cleaned[-4:]
    # 对于其他格式（如座机、短号），不做掩码，直接返回原字符串（或可改用占位符）
    return phone

def redact_text(
    text: str,
    mapper: GlobalEntityMapper,
    analyzer: AnalyzerEngine
) -> Tuple[str, List[RedactionHit]]:
    """
    对文本进行脱敏处理，返回脱敏后的文本和命中的脱敏记录列表。
    支持：人名、手机号（掩码）、银行卡号、身份证号、地址等（不含时间）。
    """
    # 1. 使用 Presidio 分析器识别实体（排除 DATE_TIME）
    results = analyzer.analyze(
        text=text,
        language="zh",
        entities=[
            "PERSON", "LOCATION",
            "PHONE_NUMBER", "CREDIT_CARD", "ID", "EMAIL_ADDRESS", "URL"
        ],
        score_threshold=0.8  # 提高阈值，过滤低分结果
    )

    # 2. 分离 PERSON 和其他实体
    person_results = [r for r in results if r.entity_type == "PERSON"]
    other_results = [r for r in results if r.entity_type != "PERSON"]

    # 3. 处理 PERSON 实体（合并、过滤、别名等）
    person_results.sort(key=lambda r: (r.start, r.end))
    person_results = merge_person_spans(text, person_results)

    # 4. 提取所有需要替换的实体（包括 PERSON 和其他）
    all_entities = {}  # text -> placeholder

    # 4a. 处理 PERSON
    for r in person_results:
        name = text[r.start:r.end]
        if name not in all_entities:
            placeholder = mapper.get_or_create(name, "name")
            all_entities[name] = placeholder

    # ---- 基于“往后读一位”的合并（仅用于 PERSON） ----
    VERB_PREFIXES = set("受被让给为由将把向对与同跟从在到予以用拿借凭靠沿顺朝往冲离除比")
    sorted_names = sorted(all_entities.keys(), key=len, reverse=True)
    alias_map = {}
    for short_name in sorted_names:
        if len(short_name) < 2:
            continue
        candidates = [
            ln for ln in sorted_names
            if len(ln) > len(short_name) and ln.startswith(short_name)
        ]
        if not candidates:
            continue
        short_positions = []
        pos = 0
        while True:
            pos = text.find(short_name, pos)
            if pos == -1:
                break
            short_positions.append(pos)
            pos += 1
        found = False
        for long_name in candidates:
            remaining = long_name[len(short_name):]
            if remaining and remaining[0] in VERB_PREFIXES:
                continue
            for sp in short_positions:
                next_start = sp + len(short_name)
                if (next_start + len(remaining) <= len(text) and
                        text[next_start:next_start + len(remaining)] == remaining):
                    after_end = next_start + len(remaining)
                    if after_end < len(text):
                        next_char = text[after_end]
                        if '\u4e00' <= next_char <= '\u9fff':
                            continue
                    alias_map[short_name] = long_name
                    print(f"  🔗 '{short_name}' 在位置 {sp} 后紧跟 '{remaining}' → 合并到 '{long_name}'")
                    found = True
                    break
            if found:
                break
        if not found:
            print(f"  ✖ '{short_name}' 在所有出现位置后均未匹配到任何长名字 → 保持独立")

    # 应用别名映射
    for short_name, long_name in alias_map.items():
        all_entities[short_name] = all_entities[long_name]

    # 4b. 处理其他实体（手机号、银行卡号、身份证号、地址等）
    for r in other_results:
        raw_text = text[r.start:r.end]
        if raw_text in all_entities:
            continue
        entity_type = r.entity_type

        if entity_type == "PHONE_NUMBER":
            # 手机号：生成掩码字符串作为占位符
            masked = mask_phone_number(raw_text)
            # 如果掩码结果与原字符串相同（非11位手机号），则使用默认占位符
            if masked == raw_text:
                placeholder = mapper.get_or_create(raw_text, "phone")
            else:
                # 直接使用掩码字符串作为占位符，并确保相同号码映射到相同掩码
                # 注意：如果多个不同号码掩码后相同（极小概率），这里会合并，但一般不会
                placeholder = masked
                # 同时也存入 mapper 以便追踪（可选）
                # mapper.get_or_create(raw_text, "phone")  # 如果不需要追踪可以不调用
        else:
            # 其他实体（银行卡、身份证、地址等）使用 mapper 生成占位符
            placeholder = mapper.get_or_create(raw_text, entity_type.lower())

        all_entities[raw_text] = placeholder

    # 调试打印
    print(f"\n=== 识别出的唯一实体（共 {len(all_entities)} 个）===")
    for txt, ph in all_entities.items():
        print(f"  '{txt}' -> {ph}")
    print("=========================\n")

    # 5. 构建替换映射：按长度降序排序，避免短文本被提前替换
    replace_list = sorted(all_entities.items(), key=lambda x: -len(x[0]))

    # 6. 执行全局替换
    redacted_text = text
    hits = []
    for original, placeholder in replace_list:
        import re
        pattern = re.compile(re.escape(original))
        last_end = 0
        new_text_parts = []
        for match in pattern.finditer(redacted_text):
            start = match.start()
            end = match.end()
            hit = RedactionHit(
                sens_type="PERSON",  # 可根据需要细化，此处保持统一
                start=start,
                end=end,
                original=original,
                placeholder=placeholder
            )
            hits.append(hit)
            new_text_parts.append(redacted_text[last_end:start])
            new_text_parts.append(placeholder)
            last_end = end
        new_text_parts.append(redacted_text[last_end:])
        redacted_text = ''.join(new_text_parts)

    return redacted_text, hits


# ---------- 解析与页级 OCR ----------


@dataclass
class PageResult:
    page_no: int
    source: str
    text: str
    text_density: float = 0.0
    avg_confidence: float | None = None
    min_confidence: float | None = None
    bbox: list[Any] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    status: str = "PARSED"
    error_code: str | None = None
    error_message: str | None = None
    image_bytes: bytes | None = None


@dataclass
class OCRLine:
    text: str
    confidence: float
    bbox: list[float]


class OCREngine(Protocol):
    def recognize(self, image_bytes: bytes) -> list[OCRLine]:
        ...


class FallbackOCREngine:
    """未安装 PaddleOCR 时使用的确定性后备引擎，便于本地与测试运行。"""

    def recognize(self, image_bytes: bytes) -> list[OCRLine]:
        marker = b"__OCR_TEXT__:"
        if marker in image_bytes:
            text = image_bytes.split(marker, 1)[1].decode("utf-8", errors="ignore")
            return [OCRLine(text, 0.95 if text.strip() else 0.2, [0, 0, 100, 20])]

        printable = "".join(chr(value) if 32 <= value < 127 else " " for value in image_bytes)
        tokens = [token for token in printable.split() if len(token) >= 4]
        if not tokens:
            return [OCRLine("", 0.1, [0, 0, 1, 1])]
        return [OCRLine(" ".join(tokens[:50]), 0.55, [0, 0, 100, 20])]


class PaddleOCREngine:
    def __init__(self):
        from paddleocr import PaddleOCR

        self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    def recognize(self, image_bytes: bytes) -> list[OCRLine]:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        lines: list[OCRLine] = []
        for block in self._ocr.ocr(np.array(image), cls=True) or []:
            for box, (text, confidence) in block or []:
                flat = [float(value) for point in box for value in point]
                xs, ys = flat[0::2], flat[1::2]
                lines.append(
                    OCRLine(str(text), float(confidence), [min(xs), min(ys), max(xs), max(ys)])
                )
        return lines


_ocr_engine: OCREngine | None = None


def set_ocr_engine(engine: OCREngine | None) -> None:
    """注入 OCR 引擎，用于替换实现或测试。"""
    global _ocr_engine
    _ocr_engine = engine


def _get_ocr_engine() -> OCREngine:
    global _ocr_engine
    if _ocr_engine is None:
        try:
            _ocr_engine = PaddleOCREngine()
        except Exception:
            _ocr_engine = FallbackOCREngine()
    return _ocr_engine


def _recognize_image(image_bytes: bytes) -> dict[str, Any]:
    lines = _get_ocr_engine().recognize(image_bytes)
    texts = [line.text for line in lines if line.text]
    confidences = [line.confidence for line in lines] or [0.0]
    average = sum(confidences) / len(confidences)
    minimum = min(confidences)
    flags = []
    if minimum < OCR_LOW_CONFIDENCE_THRESHOLD or average < OCR_LOW_CONFIDENCE_THRESHOLD:
        flags.append("LOW_OCR_CONFIDENCE")
    if not "".join(texts).strip():
        flags.append("EMPTY_OCR")
    return {
        "text": "\n".join(texts),
        "lines": [
            {"text": line.text, "confidence": line.confidence, "bbox": line.bbox}
            for line in lines
        ],
        "avg_confidence": average,
        "min_confidence": minimum,
        "quality_flags": flags,
        "bbox": [line.bbox for line in lines],
    }


def _extract_pdf(path: Path) -> list[PageResult]:
    try:
        import fitz
    except ImportError as exc:
        raise MaterialError(ERROR_CODES["PARSE_FAILED"], "PyMuPDF(fitz) not installed") from exc

    try:
        document = fitz.open(path)
    except Exception as exc:
        message = str(exc).lower()
        code = (
            ERROR_CODES["ENCRYPTED_FILE"]
            if "password" in message or "encrypt" in message
            else ERROR_CODES["CORRUPT_FILE"]
        )
        raise MaterialError(code, f"Failed to open PDF: {exc}") from exc

    needs_password = getattr(document, "needs_pass", False) or getattr(
        document, "is_encrypted", False
    )
    if needs_password and not document.authenticate(""):
        document.close()
        raise MaterialError(ERROR_CODES["ENCRYPTED_FILE"], "PDF requires a password")

    pages: list[PageResult] = []
    try:
        for index, page in enumerate(document):
            try:
                text = page.get_text("text") or ""
                bbox = [
                    [float(block[0]), float(block[1]), float(block[2]), float(block[3])]
                    for block in (page.get_text("blocks") or [])
                    if len(block) >= 4
                ]
                area = float(page.rect.width * page.rect.height) if page.rect else 1.0
                density = len(text.strip()) / max(area / 10000.0, 1.0)
                needs_ocr = density < OCR_TEXT_DENSITY_THRESHOLD or not text.strip()
                pages.append(
                    PageResult(
                        page_no=index + 1,
                        source="pdf_text",
                        text=text,
                        text_density=density,
                        bbox=bbox,
                        status="NEEDS_OCR" if needs_ocr else "PARSED",
                        image_bytes=(
                            page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
                            if needs_ocr
                            else None
                        ),
                        quality_flags=["LOW_TEXT_DENSITY"] if needs_ocr else [],
                    )
                )
            except Exception as exc:
                pages.append(
                    PageResult(
                        page_no=index + 1,
                        source="pdf_text",
                        text="",
                        status="FAILED",
                        error_code=ERROR_CODES["PARSE_FAILED"],
                        error_message=str(exc),
                        quality_flags=["PAGE_PARSE_FAILED"],
                    )
                )
    finally:
        document.close()
    return pages


def _extract_docx(path: Path) -> list[PageResult]:
    try:
        from docx import Document

        document = Document(str(path))
    except ImportError as exc:
        raise MaterialError(ERROR_CODES["PARSE_FAILED"], "python-docx not installed") from exc
    except Exception as exc:
        raise MaterialError(ERROR_CODES["CORRUPT_FILE"], f"Cannot open DOCX: {exc}") from exc

    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append("\t".join(cells))
    return [PageResult(1, "docx", "\n".join(parts), text_density=1.0)]


def _extract_txt(path: Path) -> list[PageResult]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="gbk")
        except Exception as exc:
            raise MaterialError(
                ERROR_CODES["CORRUPT_FILE"], f"Cannot decode text file: {exc}"
            ) from exc
    except Exception as exc:
        raise MaterialError(ERROR_CODES["CORRUPT_FILE"], f"Cannot read text file: {exc}") from exc
    return [PageResult(1, "txt", text, text_density=1.0)]


def _extract_image(path: Path) -> list[PageResult]:
    return [
        PageResult(
            1,
            "image",
            "",
            status="NEEDS_OCR",
            image_bytes=path.read_bytes(),
            quality_flags=["IMAGE_REQUIRES_OCR"],
        )
    ]


def _run_page_ocr(page: PageResult, retries: int) -> PageResult:
    if not page.image_bytes:
        page.status = "OCR_FAILED"
        page.error_code = ERROR_CODES["OCR_FAILED"]
        page.error_message = "missing page image for OCR"
        page.quality_flags = sorted(set(page.quality_flags + ["OCR_FAILED"]))
        return page

    last_error: Exception | None = None
    for _ in range(retries + 1):
        try:
            result = _recognize_image(page.image_bytes)
            page.text = result["text"]
            page.lines = result["lines"]
            page.bbox = result["bbox"]
            page.avg_confidence = result["avg_confidence"]
            page.min_confidence = result["min_confidence"]
            page.quality_flags = sorted(set(page.quality_flags + result["quality_flags"]))
            page.source = "ocr"
            if "EMPTY_OCR" in result["quality_flags"]:
                page.status = "OCR_FAILED"
                page.error_code = ERROR_CODES["OCR_FAILED"]
                page.error_message = "OCR returned empty text"
            elif "LOW_OCR_CONFIDENCE" in result["quality_flags"]:
                page.status = "NEEDS_OCR_REVIEW"
            else:
                page.status = "PARSED"
            return page
        except Exception as exc:
            last_error = exc

    page.status = "OCR_FAILED"
    page.error_code = ERROR_CODES["OCR_FAILED"]
    page.error_message = str(last_error) if last_error else "OCR failed"
    page.quality_flags = sorted(set(page.quality_flags + ["OCR_FAILED"]))
    return page


def parse_file_to_pages(path: Path | str, max_retries: int | None = None) -> list[dict[str, Any]]:
    """按文件类型取文，只对需要识别的页面执行 OCR，成功页不重跑。"""
    file_path = Path(path)
    extractors = {
        ".pdf": _extract_pdf,
        ".docx": _extract_docx,
        ".txt": _extract_txt,
        ".png": _extract_image,
        ".jpg": _extract_image,
        ".jpeg": _extract_image,
    }
    extractor = extractors.get(file_path.suffix.lower())
    if extractor is None:
        raise MaterialError(
            ERROR_CODES["UNSUPPORTED_TYPE"], f"Unsupported extension: {file_path.suffix.lower()}"
        )

    retries = OCR_MAX_PAGE_RETRIES if max_retries is None else max_retries
    parsed = []
    for page in extractor(file_path):
        if page.status == "NEEDS_OCR":
            page = _run_page_ocr(page, retries)
        page.image_bytes = None
        parsed.append(
            {
                "page_no": page.page_no,
                "source": page.source,
                "text": page.text,
                "text_density": page.text_density,
                "avg_confidence": page.avg_confidence,
                "min_confidence": page.min_confidence,
                "bbox": page.bbox,
                "lines": page.lines,
                "quality_flags": page.quality_flags,
                "status": page.status,
                "error_code": page.error_code,
                "error_message": page.error_message,
            }
        )
    return parsed


def summarize_page_quality(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总页级识别质量，供状态展示与人工修正定位。"""
    confidences = [p["avg_confidence"] for p in pages if p.get("avg_confidence") is not None]
    minimums = [p["min_confidence"] for p in pages if p.get("min_confidence") is not None]
    low_pages: list[int] = []
    warnings: list[str] = []
    statuses: dict[str, str] = {}
    for page in pages:
        page_no = str(page["page_no"])
        status = page.get("status") or "PARSED"
        flags = page.get("quality_flags") or []
        statuses[page_no] = status
        if status in {"OCR_FAILED", "FAILED"}:
            warnings.append(f"page {page_no}: {status}")
        if "LOW_OCR_CONFIDENCE" in flags:
            low_pages.append(int(page["page_no"]))
            warnings.append(f"page {page_no}: low OCR confidence")
        if "EMPTY_OCR" in flags:
            warnings.append(f"page {page_no}: empty OCR")
    return {
        "avg_confidence": sum(confidences) / len(confidences) if confidences else None,
        "min_confidence": min(minimums) if minimums else None,
        "low_confidence_pages": sorted(set(low_pages)),
        "warnings": warnings,
        "page_statuses": statuses,
    }


def derive_version_status(pages: list[dict[str, Any]]) -> str:
    statuses = [page.get("status") for page in pages]
    if not statuses:
        return "FAILED"
    if any(status == "OCR_FAILED" for status in statuses):
        return "OCR_FAILED"
    if any(status == "NEEDS_OCR_REVIEW" for status in statuses):
        return "NEEDS_OCR_REVIEW"
    if any(status == "FAILED" for status in statuses):
        return "FAILED"
    return "PARSED" if all(status == "PARSED" for status in statuses) else "NEEDS_OCR_REVIEW"


# ---------- 分块与定位 ----------


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_chunks(
    document_version_id: str,
    pages: list[dict[str, Any]],
    *,
    chunk_size: int | None = None,
    overlap: int | None = None,
    parser_version: str | None = None,
) -> list[dict[str, Any]]:
    """生成稳定重叠文本块，ID 由版本、块序与文本哈希派生。"""
    size = CHUNK_SIZE if chunk_size is None else chunk_size
    overlap_size = CHUNK_OVERLAP if overlap is None else overlap
    version = parser_version or PARSER_VERSION
    if size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap_size >= size:
        overlap_size = max(0, size // 4)

    stream: list[tuple[str, int, list]] = []
    for page in pages:
        page_no = int(page["page_no"])
        text = page.get("text") or ""
        bbox = page.get("bbox") or []
        stream.extend((character, page_no, bbox) for character in text)
        if text and not text.endswith("\n"):
            stream.append(("\n", page_no, bbox))

    full_text = "".join(character for character, _, _ in stream)
    if not full_text.strip():
        digest = _text_sha256("")
        return [
            {
                "id": f"{document_version_id}:0:{digest[:16]}",
                "document_version_id": document_version_id,
                "ordinal": 0,
                "page_start": int(pages[0]["page_no"]) if pages else 1,
                "page_end": int(pages[-1]["page_no"]) if pages else 1,
                "char_start": 0,
                "char_end": 0,
                "bbox_json": "[]",
                "text_raw": "",
                "text_redacted": "",
                "text_sha256": digest,
                "parser_version": version,
                "quality_flags_json": "[]",
                "is_active": 1,
                "stale": 0,
            }
        ]

    chunks: list[dict[str, Any]] = []
    start = 0
    ordinal = 0
    while start < len(full_text):
        end = min(start + size, len(full_text))
        if end < len(full_text):
            window = full_text[start:end]
            break_at = max(window.rfind("\n"), window.rfind(" "), window.rfind("\u3000"))
            if break_at > size * 0.5:
                end = start + break_at + 1

        text = full_text[start:end]
        page_start = stream[start][1]
        page_end = stream[end - 1][1]
        flags: list[str] = []
        for page in pages:
            if page_start <= int(page["page_no"]) <= page_end:
                flags.extend(page.get("quality_flags") or [])
        digest = _text_sha256(text)
        chunks.append(
            {
                "id": f"{document_version_id}:{ordinal}:{digest[:16]}",
                "document_version_id": document_version_id,
                "ordinal": ordinal,
                "page_start": page_start,
                "page_end": page_end,
                "char_start": start,
                "char_end": end,
                "bbox_json": json.dumps(stream[start][2], ensure_ascii=False),
                "text_raw": text,
                "text_redacted": text,
                "text_sha256": digest,
                "parser_version": version,
                "quality_flags_json": json.dumps(sorted(set(flags)), ensure_ascii=False),
                "is_active": 1,
                "stale": 0,
            }
        )
        ordinal += 1
        if end >= len(full_text):
            break
        start = max(end - overlap_size, start + 1)
    return chunks


# ---------- 材料处理服务 ----------


class MaterialService:
    """案件卷宗上传后的处理入口：版本、解析、质量、修正、删除与安全读取。"""

    def __init__(self, db_path=None, auth_check=None,
                 mapper_salt=None):
        self.db_path = db_path
        self.auth_check = auth_check or _default_auth()
        self.mapper = GlobalEntityMapper(db_path=db_path, salt=mapper_salt or "change-me")

        # 直接初始化 PriKit/Presidio AnalyzerEngine
        self.analyzer = self._init_analyzer()

        init_db(self.db_path)

    def _init_analyzer(self) -> AnalyzerEngine | None:
        try:
            from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            configuration = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "zh", "model_name": "zh_core_web_trf"}],
            }
            provider = NlpEngineProvider(nlp_configuration=configuration)
            nlp_engine = provider.create_engine()
            analyzer = AnalyzerEngine(nlp_engine=nlp_engine)

            # ---- 新增：中文姓名专用识别器 ----
            # 匹配"姓名/被告人/原告/受害人..."等上下文后的 2-4 个汉字
            chinese_name_pattern = Pattern(
                name="chinese_name_pattern",
                # 2-4 个汉字，支持中间有 ·（少数民族姓名）
                regex=r"(?:姓名|被告人|嫌疑人|当事人|原告|被告|证人|辩护人|被害人|申诉人|被申诉人|法定代表人|负责人|联系人|申请人|被申请人)[:：\s]*([\u4e00-\u9fa5·]{2,4})",
                score=0.9,
            )
            chinese_name_recognizer = PatternRecognizer(
                supported_entity="PERSON",
                name="chinese_name_recognizer",
                patterns=[chinese_name_pattern],
                # context 词能进一步提升 score
                context=["姓名", "被告人", "原告", "被告", "证人", "受害人", "当事人"],
            )
            analyzer.registry.add_recognizer(chinese_name_recognizer)

            # ---- 新增：纯姓名兜底（无上下文时，2-3 字中文串）----
            # 这个 score 低一些，避免误伤普通词语
            bare_name_pattern = Pattern(
                name="bare_chinese_name_pattern",
                regex=r"(?<![\u4e00-\u9fa5])([\u4e00-\u9fa5]{2,3})(?![\u4e00-\u9fa5])",
                score=0.4,
            )
            bare_name_recognizer = PatternRecognizer(
                supported_entity="PERSON",
                name="bare_chinese_name_recognizer",
                patterns=[bare_name_pattern],
            )
            analyzer.registry.add_recognizer(bare_name_recognizer)

            return analyzer
        except Exception as e:
            print(f"⚠️ AnalyzerEngine 初始化失败，将使用正则降级: {e}")
            return None

    def _authorize(self, user_id: str | None, case_id: str | None, action: str) -> None:
        allowed, reason = self.auth_check(user_id, case_id, action)
        if not allowed:
            with db_session(self.db_path) as conn:
                add_audit(
                    conn,
                    event_type="auth_check",
                    actor_user_id=user_id,
                    case_id=case_id,
                    decision="deny",
                    reason=reason or "denied",
                    detail_json=json.dumps({"action": action}, ensure_ascii=False),
                )
            raise MaterialError(ERROR_CODES["AUTH_DENIED"], reason or "authorization denied")

    # ----- 上传与版本 -----

    def upload_many(
        self,
        items: list[dict[str, Any]],
        *,
        user_id: str | None = None,
        parse: bool = True,
        keep_duplicate: bool = False,
    ) -> list[dict[str, Any]]:
        """一次导入多份材料，可分别归属不同案件。"""
        return [
            self.upload_one(
                case_id=item["case_id"],
                filename=item["filename"],
                content=item.get("content"),
                path=item.get("path"),
                user_id=user_id,
                parse=parse,
                keep_duplicate=keep_duplicate,
                replace_document_id=item.get("replace_document_id"),
            )
            for item in items
        ]

    def upload_one(
        self,
        *,
        case_id: str,
        filename: str,
        content: bytes | None = None,
        path: str | Path | None = None,
        user_id: str | None = None,
        parse: bool = True,
        keep_duplicate: bool = False,
        replace_document_id: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(user_id, case_id, "material.upload")
        safe_name = Path(filename).name
        extension = Path(safe_name).suffix.lower()
        if extension not in ALLOWED_MATERIAL_EXTENSIONS:
            raise MaterialError(ERROR_CODES["UNSUPPORTED_TYPE"], f"unsupported type: {extension}")

        if content is None:
            if path is None:
                raise MaterialError(ERROR_CODES["CORRUPT_FILE"], "empty upload")
            content = Path(path).read_bytes()

        size = len(content)
        if size > MAX_UPLOAD_BYTES:
            raise MaterialError(
                ERROR_CODES["FILE_TOO_LARGE"],
                f"file exceeds limit {MAX_UPLOAD_BYTES} bytes",
                {"size": size},
            )
        if size == 0:
            raise MaterialError(ERROR_CODES["CORRUPT_FILE"], "empty file")

        digest = hashlib.sha256(content).hexdigest()
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

        with db_session(self.db_path) as conn:
            if not get_case(conn, case_id):
                if user_id == "system":
                    ensure_demo_case(conn, case_id)
                else:
                    raise MaterialError(ERROR_CODES["NOT_FOUND"], f"case not found: {case_id}")

            duplicate = _row(
                conn,
                """
                SELECT d.* FROM documents d
                JOIN document_versions v ON v.document_id = d.id
                WHERE d.case_id = ? AND v.sha256 = ? AND d.deleted_at IS NULL
                ORDER BY v.created_at DESC LIMIT 1
                """,
                (case_id, digest),
            )
            if duplicate and not replace_document_id and not keep_duplicate:
                _update(conn, "documents", duplicate["id"], {"status": "DUPLICATE_PENDING"})
                add_audit(
                    conn,
                    event_type="duplicate_pending",
                    actor_user_id=user_id,
                    case_id=case_id,
                    document_id=duplicate["id"],
                    decision="pending",
                    reason="same sha256 in case",
                    detail_json=json.dumps({"sha256": digest}, ensure_ascii=False),
                )
                return {
                    "status": "DUPLICATE_PENDING",
                    "error_code": ERROR_CODES["DUPLICATE_PENDING"],
                    "document": get_document(conn, duplicate["id"]),
                    "message": "duplicate hash in case; decide keep or cancel",
                }

            now = utc_now()
            if replace_document_id:
                document = get_document(conn, replace_document_id)
                if not document or document["case_id"] != case_id:
                    raise MaterialError(ERROR_CODES["NOT_FOUND"], "document to replace not found")
                document_id = document["id"]
                parent_version_id = document.get("current_version_id")
                source_type = "REPLACE"
                if parent_version_id:
                    deactivate_version_chunks(conn, parent_version_id)
                    _update(conn, "document_versions", parent_version_id, {"is_current": 0})
            else:
                document_id = new_id()
                parent_version_id = None
                source_type = "UPLOAD"
                _insert(
                    conn,
                    "documents",
                    {
                        "id": document_id,
                        "case_id": case_id,
                        "filename": safe_name,
                        "stored_name": safe_name,
                        "content_type": content_type,
                        "size": size,
                        "sha256": digest,
                        "uploaded_by": user_id or "anonymous",
                        "created_at": now,
                        "status": "UPLOADED",
                        "current_version_id": None,
                        "deleted_at": None,
                        "quality_summary_json": "{}",
                    },
                )

            version_id = new_id()
            version_no = next_version_no(conn, document_id)
            storage_path = (
                Path(MATERIAL_STORAGE_DIR)
                / case_id
                / document_id
                / f"v{version_no}_{digest[:12]}{extension}"
            )
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage_path.write_bytes(content)

            _insert(
                conn,
                "document_versions",
                {
                    "id": version_id,
                    "document_id": document_id,
                    "version_no": version_no,
                    "parent_version_id": parent_version_id,
                    "source_type": source_type,
                    "sha256": digest,
                    "storage_path": str(storage_path),
                    "content_type": content_type,
                    "size": size,
                    "parser_version": PARSER_VERSION,
                    "status": "UPLOADED",
                    "is_current": 1,
                    "is_active": 1,
                    "quality_summary_json": "{}",
                    "error_code": None,
                    "error_message": None,
                    "created_by": user_id or "anonymous",
                    "created_at": now,
                },
            )
            set_current_version(conn, document_id, version_id)
            _update(
                conn,
                "documents",
                document_id,
                {
                    "filename": safe_name,
                    "stored_name": storage_path.name,
                    "content_type": content_type,
                    "size": size,
                    "sha256": digest,
                    "status": "UPLOADED",
                },
            )
            add_audit(
                conn,
                event_type="upload",
                actor_user_id=user_id,
                case_id=case_id,
                document_id=document_id,
                document_version_id=version_id,
                decision="allow",
                reason=source_type.lower(),
            )

        if parse:
            return self.parse_version(version_id, user_id=user_id)
        with db_session(self.db_path) as conn:
            return {
                "document": get_document(conn, document_id),
                "version": get_version(conn, version_id),
                "status": "UPLOADED",
            }

    # ----- 解析 -----

    def parse_version(self, version_id: str, user_id: str | None = None) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            version = get_version(conn, version_id)
            if not version:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "version not found")
            document = get_document(conn, version["document_id"])
            case_id = document["case_id"] if document else None
            storage_path = Path(version["storage_path"])

        self._authorize(user_id, case_id, "material.parse")

        job_id = new_id()
        with db_session(self.db_path) as conn:
            now = utc_now()
            _insert(
                conn,
                "parse_jobs",
                {
                    "id": job_id,
                    "document_version_id": version_id,
                    "status": "RUNNING",
                    "attempt": 1,
                    "max_attempts": OCR_MAX_PAGE_RETRIES,
                    "error_code": None,
                    "error_message": None,
                    "started_at": now,
                    "finished_at": None,
                    "created_at": now,
                },
            )
            _update(conn, "document_versions", version_id, {"status": "PARSING"})
            if document:
                _update(conn, "documents", document["id"], {"status": "PARSING"})

        try:
            pages = parse_file_to_pages(storage_path)
        except Exception as exc:
            code = exc.code if isinstance(exc, MaterialError) else ERROR_CODES["PARSE_FAILED"]
            message = exc.message if isinstance(exc, MaterialError) else str(exc)
            with db_session(self.db_path) as conn:
                _update(
                    conn,
                    "parse_jobs",
                    job_id,
                    {
                        "status": "FAILED",
                        "error_code": code,
                        "error_message": message,
                        "finished_at": utc_now(),
                    },
                )
                _update(
                    conn,
                    "document_versions",
                    version_id,
                    {"status": "FAILED", "error_code": code, "error_message": message},
                )
                if document:
                    _update(conn, "documents", document["id"], {"status": "FAILED"})
            if isinstance(exc, MaterialError):
                raise
            raise MaterialError(code, message) from exc

        return self._persist_pages(
            version_id=version_id,
            document_id=version["document_id"],
            case_id=case_id,
            pages=pages,
            user_id=user_id,
            job_id=job_id,
            event_type="parse",
        )

    def _persist_pages(
        self,
        *,
        version_id: str,
        document_id: str,
        case_id: str | None,
        pages: list[dict[str, Any]],
        user_id: str | None,
        job_id: str | None,
        event_type: str,
        audit_reason: str | None = None,
    ) -> dict[str, Any]:
        """落库页面、分块与脱敏结果，并同步材料状态。"""
        quality = summarize_page_quality(pages)
        status = derive_version_status(pages)
        chunks = build_chunks(version_id, pages)

        redaction_rows: list[dict[str, Any]] = []
        for chunk in chunks:
            redacted, hits = redact_text(chunk["text_raw"], self.mapper, self.analyzer)
            chunk["text_redacted"] = redacted
            for hit in hits:
                redaction_rows.append(
                    {
                        "id": new_id(),
                        "document_version_id": version_id,
                        "chunk_id": chunk["id"],
                        "sens_type": hit.sens_type,
                        "start_offset": hit.start,
                        "end_offset": hit.end,
                        "placeholder": hit.placeholder,
                        "map_ref": "",
                        "created_at": utc_now(),
                    }
                )

        for row in redaction_rows:
            row["map_ref"] = ""

        with db_session(self.db_path) as conn:
            for page in pages:
                upsert_page(
                    conn,
                    {
                        "id": new_id(),
                        "document_version_id": version_id,
                        "page_no": page["page_no"],
                        "source": page["source"],
                        "text": page["text"],
                        "text_density": page.get("text_density") or 0,
                        "avg_confidence": page.get("avg_confidence"),
                        "min_confidence": page.get("min_confidence"),
                        "bbox_json": json.dumps(page.get("bbox") or [], ensure_ascii=False),
                        "lines_json": json.dumps(page.get("lines") or [], ensure_ascii=False),
                        "quality_flags_json": json.dumps(
                            page.get("quality_flags") or [], ensure_ascii=False
                        ),
                        "status": page.get("status") or "PARSED",
                        "retry_count": 0,
                        "error_code": page.get("error_code"),
                        "error_message": page.get("error_message"),
                    },
                )
            replace_chunks(conn, version_id, chunks)
            conn.execute(
                "DELETE FROM redaction_items WHERE document_version_id = ?", (version_id,)
            )
            for row in redaction_rows:
                _insert(conn, "redaction_items", row)

            quality_json = json.dumps(quality, ensure_ascii=False)
            _update(
                conn,
                "document_versions",
                version_id,
                {"status": status, "quality_summary_json": quality_json},
            )
            _update(
                conn,
                "documents",
                document_id,
                {"status": status, "quality_summary_json": quality_json},
            )
            if job_id:
                _update(conn, "parse_jobs", job_id, {"status": "DONE", "finished_at": utc_now()})
            add_audit(
                conn,
                event_type=event_type,
                actor_user_id=user_id,
                case_id=case_id,
                document_id=document_id,
                document_version_id=version_id,
                decision="allow",
                reason=audit_reason or status,
            )
            return {
                "document": get_document(conn, document_id),
                "version": get_version(conn, version_id),
                "pages": list_pages(conn, version_id),
                "chunks": [
                    {key: value for key, value in chunk.items() if key != "text_raw"}
                    for chunk in list_chunks(conn, version_id)
                ],
                "quality": quality,
                "status": status,
            }

    # ----- 查询 -----

    def list_materials(self, case_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        self._authorize(user_id, case_id, "material.list")
        with db_session(self.db_path) as conn:
            documents = _rows(
                conn,
                "SELECT * FROM documents WHERE case_id = ? AND deleted_at IS NULL ORDER BY created_at DESC",
                (case_id,),
            )
            materials = []
            for document in documents:
                versions = list_versions(conn, document["id"])
                materials.append(
                    {
                        **document,
                        "quality_summary": json.loads(document.get("quality_summary_json") or "{}"),
                        "current_version": next(
                            (v for v in versions if v.get("is_current")), None
                        ),
                        "version_count": len(versions),
                    }
                )
            return materials

    def get_status(self, document_id: str, user_id: str | None = None) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            document = get_document(conn, document_id)
            if not document:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "document not found")
            case_id = document["case_id"]

        self._authorize(user_id, case_id, "material.read")

        with db_session(self.db_path) as conn:
            document = get_document(conn, document_id)
            current = (
                get_version(conn, document["current_version_id"])
                if document.get("current_version_id")
                else None
            )
            pages = list_pages(conn, current["id"]) if current else []
            low_quality = []
            for page in pages:
                flags = json.loads(page.get("quality_flags_json") or "[]")
                if page["status"] in {"NEEDS_OCR_REVIEW", "OCR_FAILED", "FAILED"} or (
                    "LOW_OCR_CONFIDENCE" in flags
                ):
                    low_quality.append(
                        {
                            "page_no": page["page_no"],
                            "status": page["status"],
                            "quality_flags": flags,
                            "min_confidence": page.get("min_confidence"),
                            "avg_confidence": page.get("avg_confidence"),
                        }
                    )
            return {
                "document": document,
                "versions": list_versions(conn, document_id),
                "current_version": current,
                "quality_summary": json.loads(document.get("quality_summary_json") or "{}"),
                "low_quality_pages": low_quality,
            }

    # ----- 外发门控 -----

    def read_redacted_chunk(
        self,
        document_version_id: str,
        chunk_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """经门控读取脱敏片段：版本有效、已脱敏、最小必要片段。"""
        with db_session(self.db_path) as conn:
            version = get_version(conn, document_version_id)
            if not version:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "version not found")
            document = get_document(conn, version["document_id"])
            case_id = document["case_id"] if document else None

        self._authorize(user_id, case_id, "material.read_redacted")

        with db_session(self.db_path) as conn:
            version = get_version(conn, document_version_id)
            document_id = version["document_id"]

            def deny(reason: str, message: str, code: str, detail: dict | None = None):
                add_audit(
                    conn,
                    event_type="egress_check",
                    actor_user_id=user_id,
                    case_id=case_id,
                    document_id=document_id,
                    document_version_id=document_version_id,
                    decision="deny",
                    reason=reason,
                    detail_json=json.dumps(detail or {}, ensure_ascii=False),
                )
                return MaterialError(code, message)

            if not version.get("is_active"):
                raise deny(
                    "version_inactive",
                    "inactive version cannot egress",
                    ERROR_CODES["EGRESS_DENIED"],
                )

            if chunk_id:
                chunk = _row(conn, "SELECT * FROM document_chunks WHERE id = ?", (chunk_id,))
            else:
                available = list_chunks(conn, document_version_id, active_only=True)
                chunk = available[0] if available else None
            if not chunk:
                raise deny("chunk_not_found", "chunk not found", ERROR_CODES["NOT_FOUND"])

            if not chunk.get("is_active") or chunk.get("stale"):
                raise deny(
                    "chunk_stale_or_inactive",
                    "stale/inactive chunk cannot egress",
                    ERROR_CODES["EGRESS_DENIED"],
                    {"chunk_id": chunk["id"]},
                )

            redactions = _rows(
                conn,
                "SELECT * FROM redaction_items WHERE document_version_id = ?",
                (document_version_id,),
            )
            unchanged = chunk.get("text_raw") and chunk["text_redacted"] == chunk["text_raw"]
            if unchanged and redactions:
                raise deny(
                    "unredacted_with_items",
                    "unredacted text cannot egress",
                    ERROR_CODES["EGRESS_DENIED"],
                    {"chunk_id": chunk["id"]},
                )
            text = chunk.get("text_redacted") or ""
            if any(pattern.search(text) for pattern in _UNREDACTED_PATTERNS):
                raise deny(
                    "looks_unredacted",
                    "unredacted sensitive text cannot egress",
                    ERROR_CODES["EGRESS_DENIED"],
                    {"chunk_id": chunk["id"]},
                )

            add_audit(
                conn,
                event_type="egress_check",
                actor_user_id=user_id,
                case_id=case_id,
                document_id=document_id,
                document_version_id=document_version_id,
                decision="allow",
                reason="ok",
                detail_json=json.dumps({"chunk_id": chunk["id"]}, ensure_ascii=False),
            )
            return {
                "chunk_id": chunk["id"],
                "document_version_id": document_version_id,
                "ordinal": chunk["ordinal"],
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
                "text": chunk["text_redacted"],
                "quality_flags": json.loads(chunk.get("quality_flags_json") or "[]"),
                "redacted": True,
            }

    # ----- 人工修正 -----

    def apply_correction(
        self,
        *,
        document_id: str,
        source_version_id: str,
        page_no: int,
        corrected_text: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """人工修正生成新版本，旧版本原文与 chunk 保留但标记失效。"""
        with db_session(self.db_path) as conn:
            document = get_document(conn, document_id)
            if not document:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "document not found")
            case_id = document["case_id"]

        self._authorize(user_id, case_id, "material.correct")

        with db_session(self.db_path) as conn:
            source = get_version(conn, source_version_id)
            if not source or source["document_id"] != document_id:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "source version not found")
            pages = list_pages(conn, source_version_id)
            if not pages:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "source pages not found")

            corrected_pages: list[dict[str, Any]] = []
            found = False
            for page in pages:
                item = {
                    "page_no": page["page_no"],
                    "source": page["source"],
                    "text": page["text"],
                    "text_density": page["text_density"],
                    "avg_confidence": page["avg_confidence"],
                    "min_confidence": page["min_confidence"],
                    "bbox": json.loads(page.get("bbox_json") or "[]"),
                    "lines": json.loads(page.get("lines_json") or "[]"),
                    "quality_flags": json.loads(page.get("quality_flags_json") or "[]"),
                    "status": "PARSED",
                    "error_code": None,
                    "error_message": None,
                }
                if int(page["page_no"]) == int(page_no):
                    found = True
                    item.update(
                        {
                            "text": corrected_text,
                            "source": "human_correction",
                            "avg_confidence": 1.0,
                            "min_confidence": 1.0,
                            "quality_flags": ["HUMAN_CORRECTED"],
                        }
                    )
                corrected_pages.append(item)
            if not found:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], f"page {page_no} not found")

            version_id = new_id()
            deactivate_version_chunks(conn, source_version_id)
            _update(conn, "document_versions", source_version_id, {"is_current": 0})
            _insert(
                conn,
                "document_versions",
                {
                    "id": version_id,
                    "document_id": document_id,
                    "version_no": next_version_no(conn, document_id),
                    "parent_version_id": source_version_id,
                    "source_type": "CORRECTION",
                    "sha256": source["sha256"],
                    "storage_path": source["storage_path"],
                    "content_type": source["content_type"],
                    "size": source["size"],
                    "parser_version": PARSER_VERSION,
                    "status": "PARSING",
                    "is_current": 1,
                    "is_active": 1,
                    "quality_summary_json": "{}",
                    "error_code": None,
                    "error_message": None,
                    "created_by": user_id or "anonymous",
                    "created_at": utc_now(),
                },
            )
            set_current_version(conn, document_id, version_id)

        result = self._persist_pages(
            version_id=version_id,
            document_id=document_id,
            case_id=case_id,
            pages=corrected_pages,
            user_id=user_id,
            job_id=None,
            event_type="correction",
            audit_reason=f"correct_page_{page_no}",
        )
        result["parent_version_id"] = source_version_id
        return result

    # ----- 删除与重复裁决 -----

    def preview_delete_impact(self, document_id: str, user_id: str | None = None) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            document = get_document(conn, document_id)
            if not document:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "document not found")
            case_id = document["case_id"]

        self._authorize(user_id, case_id, "material.delete")

        with db_session(self.db_path) as conn:
            impact = []
            for version in list_versions(conn, document_id):
                active = _row(
                    conn,
                    "SELECT COUNT(*) AS total FROM document_chunks WHERE document_version_id = ? AND is_active = 1",
                    (version["id"],),
                )["total"]
                impact.append(
                    {
                        "kind": "version",
                        "id": version["id"],
                        "detail": f"v{version['version_no']} status={version['status']} active_chunks={active}",
                    }
                )
                for chunk in list_chunks(conn, version["id"], active_only=False):
                    impact.append(
                        {
                            "kind": "chunk",
                            "id": chunk["id"],
                            "detail": f"ordinal={chunk['ordinal']} stale={chunk['stale']}",
                        }
                    )
            return {"document_id": document_id, "impact": impact}

    def logical_delete(self, document_id: str, user_id: str | None = None) -> dict[str, Any]:
        impact = self.preview_delete_impact(document_id, user_id=user_id)
        with db_session(self.db_path) as conn:
            document = get_document(conn, document_id)
            _update(
                conn,
                "documents",
                document_id,
                {"deleted_at": utc_now(), "status": "DELETED"},
            )
            for version in list_versions(conn, document_id):
                deactivate_version_chunks(conn, version["id"])
                _update(
                    conn, "document_versions", version["id"], {"is_active": 0, "is_current": 0}
                )
            add_audit(
                conn,
                event_type="logical_delete",
                actor_user_id=user_id,
                case_id=document["case_id"],
                document_id=document_id,
                decision="allow",
                reason="logical_delete",
                detail_json=json.dumps(impact, ensure_ascii=False),
            )
            return {"status": "DELETED", **impact}

    def resolve_duplicate(
        self, document_id: str, *, action: str, user_id: str | None = None
    ) -> dict[str, Any]:
        """裁决重复导入：keep 保留并解析，cancel 逻辑删除。"""
        with db_session(self.db_path) as conn:
            document = get_document(conn, document_id)
            if not document:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "document not found")
            case_id = document["case_id"]
            current_version_id = document.get("current_version_id")

        self._authorize(user_id, case_id, "material.upload")

        if action == "cancel":
            return self.logical_delete(document_id, user_id=user_id)
        if action == "keep":
            if not current_version_id:
                raise MaterialError(ERROR_CODES["NOT_FOUND"], "no current version")
            with db_session(self.db_path) as conn:
                _update(conn, "documents", document_id, {"status": "UPLOADED"})
            return self.parse_version(current_version_id, user_id=user_id)
        raise MaterialError(ERROR_CODES["PARSE_FAILED"], f"unknown action: {action}")


_default_service: MaterialService | None = None


def get_material_service() -> MaterialService:
    global _default_service
    if _default_service is None:
        _default_service = MaterialService()
    return _default_service


def set_material_service(service: MaterialService | None) -> None:
    global _default_service
    _default_service = service


