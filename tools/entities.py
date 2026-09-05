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
import hashlib

from app.files import (
    _insert,
    _row,
    _rows,
    db_session,
    utc_now,
    new_id,
    get_global_mapper,
    get_global_analyzer,
    merge_person_spans,
    GlobalEntityMapper,
    _COMPOUND_SURNAMES,
)

EXTRACTOR_VERSION = "stage7-fuzzy-v2"
EVENT_EXTRACTOR_VERSION = "stage5-event-v1"
STRONG_TYPES = ("PHONE", "ACCOUNT", "DEVICE", "ID_CARD", "NAME", "ORGANIZATION", "MERCHANT", "IP")
RULE_TYPE_MAP = {
    "ACCOUNT": "R001",
    "PHONE": "R002",
    "DEVICE": "R003",
    "NAME": "R006",
    "ORGANIZATION": "R008",
    "MERCHANT": "R009",
}
# 姓名/组织不得单靠同名成候选，需第二佐证字段
CORROBORATION_REQUIRED = frozenset({"NAME", "ORGANIZATION"})
CORROBORATION_TYPES = {
    "NAME": frozenset({"PHONE", "ID_CARD", "ACCOUNT", "ORGANIZATION", "DEVICE"}),
    "ORGANIZATION": frozenset({"ACCOUNT", "PHONE", "ID_CARD", "MERCHANT", "IP"}),
}
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

def canonical_hash(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def quote_hash(quote: str) -> str:
    return hashlib.sha256((quote or "").encode("utf-8")).hexdigest()


def init_entity_db(db_path=None) -> None:
    with db_session(db_path) as conn:
        conn.executescript(ENTITY_SCHEMA_SQL)


# ---------- 配置 ----------

def load_exclusions() -> dict[str, Any]:
    return {
        "version": "exclusions-v3",
        "phones": ["10086", "10010", "10000", "110", "119", "120", "122", "13800138000"],
        "bank_accounts": [],
        "devices": [],
        "id_cards": [],
        "ips": ["127.0.0.1", "0.0.0.0", "192.168.0.1", "192.168.1.1"],
        "names": [
            "犯罪嫌疑人", "被告人", "被害人", "当事人", "证人", "原告", "被告",
            "某人", "某某", "张三", "李四", "王五", "法定代表人", "负责人", "联系人",
            "申诉人", "被申诉人", "辩护人", "公安机关", "人民检察院", "人民法院",
            "经查明", "经审理", "本院认为", "上述事实", "相关人员", "办案人员",
        ],
        "organizations": [
            "有限公司", "股份有限公司", "有限责任公司", "股份公司",
            "工作室", "委员会", "派出所", "公安局", "检察院", "法院",
        ],
    }


def load_rules() -> dict[str, Any]:
    return  {
            "version": "rules-v2",
            "rules": [
                {"id": "R001", "version": "v1", "object_type": "ACCOUNT", "label": "同一银行账户/卡跨案出现", "evidence_mode": "DIRECT_MATERIAL"},
                {"id": "R002", "version": "v1", "object_type": "PHONE", "label": "同一手机号跨案出现", "evidence_mode": "DIRECT_MATERIAL"},
                {"id": "R003", "version": "v1", "object_type": "DEVICE", "label": "同一设备标识跨案出现", "evidence_mode": "DIRECT_MATERIAL"},
                {"id": "R004", "version": "v1", "object_type": "TRANSFER_ACCOUNT", "label": "资金路径交叉（同账户转账活动跨案）", "evidence_mode": "RULE_INFERRED", "event_type": "TRANSFER", "party_type": "ACCOUNT"},
                {"id": "R005", "version": "v1", "object_type": "CONTACT_PHONE", "label": "共同联系人（同手机号联络事件跨案）", "evidence_mode": "RULE_INFERRED", "event_type": "CONTACT", "party_type": "PHONE"},
                {"id": "R006", "version": "v1", "object_type": "NAME", "label": "同一姓名跨案出现","evidence_mode": "DIRECT_MATERIAL"},
            ],
        }


def load_ocr_pairs() -> list[tuple[str, str]]:
    data = {"version": "norm-v1", "pairs": [["0", "O"], ["1", "l"], ["8", "B"]]}
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
    if object_type == "MERCHANT":
        # 保留 MCC-6888-001 中的连字符
        return re.sub(r"\s+", "", to_halfwidth(surface or "").strip())
    compact = re.sub(r"[\s\-_.／/]", "", to_halfwidth(surface))
    if object_type == "PHONE":
        # 脱敏号保留掩码作为碰撞键：177****1234
        if re.search(r"[*＊xX×ｘ]", compact):
            masked = re.sub(r"[＊xX×ｘ]", "*", compact)
            masked = re.sub(r"[^\d*]", "", masked)
            if masked.startswith("86") and len(re.sub(r"\D", "", masked)) >= 11:
                # 86 前缀仅当后面像手机时剥离
                rest = masked[2:] if masked.startswith("86") else masked
                if rest.startswith("1"):
                    masked = rest
            return masked
        digits = re.sub(r"\D", "", compact)
        if digits.startswith("86") and len(digits) == 13:
            digits = digits[2:]
        return digits
    if object_type == "ACCOUNT":
        if re.search(r"[*＊xX×ｘ]", compact):
            masked = re.sub(r"[＊xX×ｘ]", "*", compact)
            return re.sub(r"[^\d*]", "", masked)
        return re.sub(r"\D", "", compact).upper()
    if object_type in {"DEVICE", "ID_CARD"}:
        return re.sub(r"\D", "", compact).upper()
    if object_type == "IP":
        return compact
    if object_type == "NAME":
        return compact.strip()
    if object_type in {"ORGANIZATION", "MERCHANT"}:
        return re.sub(r"[\s\(（\)）]", "", compact)
    return compact


def _mask_collide_ok(object_type: str, normalized: str) -> bool:
    """脱敏号是否具备足够可见位，可参与等值碰撞。"""
    if not normalized or "*" not in normalized:
        return False
    digits = re.sub(r"\D", "", normalized)
    if object_type == "PHONE":
        # 前三后四：至少 7 位可见数字，且以 1 开头
        return len(digits) >= 7 and normalized.startswith("1") and normalized.endswith(digits[-4:])
    if object_type == "ACCOUNT":
        # 前四后四或总可见位≥6
        return len(digits) >= 6
    return False


def public_surface(surface_raw: str, object_type: str | None = None) -> str:
    """展示用轻量脱敏：完整强标识中间打码，不回传明文。"""
    if not surface_raw:
        return ""
    text = surface_raw
    digits = re.sub(r"\D", "", text)
    if object_type == "PHONE" or (len(digits) == 11 and digits.startswith("1")):
        if len(digits) >= 7:
            return digits[:3] + "****" + digits[-4:]
    if object_type == "ACCOUNT" or (16 <= len(digits) <= 19):
        if len(digits) >= 8:
            return digits[:4] + "*" * (len(digits) - 8) + digits[-4:]
    if object_type == "ID_CARD" and len(digits) == 18:
        return digits[:4] + "*" * 10 + digits[-4:]
    if object_type == "DEVICE" and len(digits) >= 8:
        return "*" * (len(digits) - 4) + digits[-4:]
    if object_type == "IP":
        parts = text.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.*.*"
    return text

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
        "NAME": "names",
        "ORGANIZATION": "organizations",
        "MERCHANT": "organizations",
    }
    values = {str(item) for item in (exclusions.get(buckets.get(object_type, ""), []) or [])}
    if normalized in values:
        return True
    # 组织/商户：只精确排除裸后缀，禁止 endswith 误杀「某贸易有限公司」
    return False


def candidate_fingerprint(object_type: str, normalized: str, case_ids: list[str]) -> str:
    return canonical_hash(
        {"type": object_type, "value": normalized, "cases": sorted(set(case_ids))}
    )


# ---------- 模型抽取（spaCy zh_core_web_trf via Presidio） ----------

_NAME_PLACEHOLDER_RE = re.compile(r"(?:NAME|PERSON)_[a-f0-9]{8}")
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

# Presidio / spaCy 实体类型 → 本系统 object_type
_ANALYZER_TYPE_MAP = {
    "PERSON": "NAME",
    "ORGANIZATION": "ORGANIZATION",
    "CN_COMPANY": "ORGANIZATION",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "ACCOUNT",
    "CN_ID_CARD": "ID_CARD",
    "CN_IMEI": "DEVICE",
    "IP_ADDRESS": "IP",
    "CN_MERCHANT": "MERCHANT",
    "CN_CREDIT_CODE": "ORGANIZATION",
}
_ANALYZER_ENTITIES = list(_ANALYZER_TYPE_MAP.keys())
_COMMERCIAL_ORG_SUFFIX = (
    "有限责任公司",
    "股份有限公司",
    "集团有限公司",
    "有限公司",
    "股份公司",
)
_ORG_AGENCY_SUFFIX = (
    "公安局",
    "派出所",
    "检察院",
    "人民法院",
    "法院",
    "讯问室",
    "看守所",
    "律师事务所",
    "人民政府",
    "委员会",
)
# spaCy 常见误切：把「赵瑞案/赵瑞犯/博元」等切成人名
_BAD_PERSON_SUFFIX = frozenset("案犯所部庭级记诉判书")
# 人名左侧停在这些角色/虚词边界，避免「嫌疑人侯」整段吞入
_PERSON_LEFT_BOUNDARY = frozenset(
    "人犯员告诉审嫌疑罪案被告与和及的于在向对把将从被让给由同跟到"
)
# 常见单字姓（补 spaCy 只切出姓、漏写名）
_COMMON_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)
_COMPOUND_SURNAME_SET = frozenset(_COMPOUND_SURNAMES)


# 人名右侧停字（虚词/动词），避免「侯博元于/曾」被并入
_PERSON_RIGHT_STOP = frozenset(
    "于在向对把将从与和及的了着过来说道讲问答称供辩述到案犯所部庭级记诉判书曾又再则即并或而由用拿借凭"
)


def _extend_person_span(source: str, start: int, end: int) -> tuple[int, int]:
    """补全 spaCy 残缺人名：裸姓向右并入；两字名若左侧是姓且贴角色边界则向左并入一字。"""
    if start < 0 or end <= start or end > len(source):
        return start, end
    span = source[start:end]

    def _is_cjk(ch: str) -> bool:
        return "\u4e00" <= ch <= "\u9fff"

    bare_single = len(span) == 1 and span in _COMMON_SURNAMES
    bare_compound = len(span) == 2 and span in _COMPOUND_SURNAME_SET
    if bare_single or bare_compound:
        # 单姓最多 3 字、复姓最多 4 字；遇右侧停字立即停
        max_len = 4 if bare_compound else 3
        while (
            end < len(source)
            and (end - start) < max_len
            and _is_cjk(source[end])
            and source[end] not in _BAD_PERSON_SUFFIX
            and source[end] not in _PERSON_RIGHT_STOP
        ):
            end += 1
        return start, end

    # 「博元」←「侯」：仅当左侧一字是单姓，且再左侧不是姓名续写
    if len(span) == 2 and start > 0 and _is_cjk(source[start - 1]):
        prev = source[start - 1]
        if prev in _COMMON_SURNAMES:
            boundary_ok = start == 1 or (not _is_cjk(source[start - 2])) or (
                source[start - 2] in _PERSON_LEFT_BOUNDARY
            )
            if boundary_ok and (end - (start - 1)) <= 4:
                start -= 1
    return start, end


def _person_surface_ok(raw: str, exclusions: dict[str, Any]) -> bool:
    if not re.fullmatch(r"[\u4e00-\u9fff·]{2,4}", raw or ""):
        return False
    if is_excluded("NAME", raw, exclusions):
        return False
    if raw[-1] in _BAD_PERSON_SUFFIX:
        return False
    # 「某某案」「审讯」类非人名
    if raw.endswith(("公司", "银行", "公安", "法院", "检察院")):
        return False
    return True


def _is_commercial_org(value: str) -> bool:
    """商号形态（含「某…有限公司」），可用于跨案同名免佐证。"""
    v = (value or "").strip()
    if not v or any(v.endswith(a) for a in _ORG_AGENCY_SUFFIX):
        return False
    return any(v.endswith(s) for s in _COMMERCIAL_ORG_SUFFIX)


_COMPANY_TRIM_RE = re.compile(
    r"(某[\u4e00-\u9fff·A-Za-z0-9]{0,20}(?:有限责任公司|股份有限公司|集团有限公司|有限公司|股份公司))"
    r"|([\u4e00-\u9fff·A-Za-z0-9]{2,20}(?:有限责任公司|股份有限公司|集团有限公司|有限公司|股份公司))"
)


def _trim_company_surface(raw: str) -> str:
    """从过长跨度中裁出真实商号（避免『A案出现某贸易有限公司』整段入库）。"""
    text = (raw or "").strip()
    if not text:
        return text
    matches = list(_COMPANY_TRIM_RE.finditer(text))
    if not matches:
        return text
    m = matches[-1]
    return (m.group(1) or m.group(2) or text).strip()


def _org_surface_ok(raw: str, exclusions: dict[str, Any]) -> bool:
    if not raw or len(raw.strip()) < 2:
        return False
    norm = normalize_identifier("ORGANIZATION", raw)
    if is_excluded("ORGANIZATION", norm, exclusions):
        return False
    if any(raw.endswith(a) for a in _ORG_AGENCY_SUFFIX):
        return False
    if _is_commercial_org(raw):
        return len(raw) >= 4
    # spaCy 切出的非商号组织（如「易宝」）仍可保留，供同案佐证；跨案仍受佐证门槛约束
    return True


def _drop_nested_person_mentions(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一段文本中，若短名是长名的真子集（如 博元 ⊂ 侯博元），丢掉短名。"""
    persons = [h for h in hits if h.get("object_type") == "NAME" and not (h.get("mask_info") or {}).get("kind") == "placeholder"]
    drop: set[int] = set()
    for i, short in enumerate(persons):
        s = short.get("surface_raw") or ""
        for long in persons:
            if short is long:
                continue
            l = long.get("surface_raw") or ""
            if len(s) < len(l) and s in l:
                drop.add(i)
                break
    if not drop:
        return hits
    drop_ids = {id(persons[i]) for i in drop}
    return [h for h in hits if id(h) not in drop_ids]


def extract_rule_mentions(text: str, mapper: GlobalEntityMapper | None = None) -> list[dict[str, Any]]:
    """用 spaCy zh_core_web_trf（经 Presidio）切片识别实体；号证类走同一分析器上的识别器。"""
    found: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    exclusions = load_exclusions()
    source = text or ""

    def take(start: int, end: int) -> bool:
        if start < 0 or end <= start or end > len(source):
            return False
        for lo, hi in occupied:
            if start < hi and end > lo:
                return False
        occupied.append((start, end))
        return True

    if source.strip():
        try:
            analyzer = get_global_analyzer()
            results = analyzer.analyze(
                text=source,
                language="zh",
                entities=_ANALYZER_ENTITIES,
                score_threshold=0.5,
            )
        except Exception:
            results = []

        # 人名：先补裸姓残缺，再邻接合并（merge 会丢掉单字，必须先 extend）
        from presidio_analyzer import RecognizerResult

        person_only = [r for r in results if r.entity_type == "PERSON"]
        other_results = [r for r in results if r.entity_type != "PERSON"]
        if person_only:
            extended_persons = []
            for r in person_only:
                s, e = _extend_person_span(source, r.start, r.end)
                extended_persons.append(
                    RecognizerResult(
                        entity_type="PERSON",
                        start=s,
                        end=e,
                        score=float(r.score),
                    )
                )
            person_only = merge_person_spans(
                source, sorted(extended_persons, key=lambda r: (r.start, r.end))
            )
        results = list(person_only) + list(other_results)

        # 高分优先，避免重叠切片互相抢占
        for result in sorted(results, key=lambda r: (-float(r.score), r.start, -(r.end - r.start))):
            object_type = _ANALYZER_TYPE_MAP.get(result.entity_type)
            if not object_type:
                continue
            start, end = result.start, result.end
            raw = source[start:end].strip()
            if not raw or not take(start, end):
                continue

            # 校验层：模型给跨度，确定性规则只做准入，不再自己扫全文
            if object_type == "NAME":
                if not _person_surface_ok(raw, exclusions):
                    occupied.pop()
                    continue
            elif object_type == "ORGANIZATION":
                if result.entity_type == "CN_CREDIT_CODE":
                    if id_card_ok(raw):
                        occupied.pop()
                        continue
                    hit = _mention_hit("ORGANIZATION", raw, start, end, producer="PRESIDIO")
                    hit["mask_info"] = {"kind": "credit_code"}
                    found.append(hit)
                    continue
                # 裁切过长商号跨度，并回写起止
                trimmed = _trim_company_surface(raw)
                if trimmed != raw and trimmed:
                    idx = source.find(trimmed, start, end)
                    if idx >= 0:
                        # 释放原跨度占用，改占裁切后区间
                        occupied.pop()
                        start, end = idx, idx + len(trimmed)
                        raw = trimmed
                        if not take(start, end):
                            continue
                    else:
                        raw = trimmed
                if not _org_surface_ok(raw, exclusions):
                    occupied.pop()
                    continue
            elif object_type == "ACCOUNT":
                norm = normalize_identifier("ACCOUNT", raw)
                is_tail = (
                    len(re.sub(r"\D", "", raw)) == 4
                    and (
                        "尾号" in raw
                        or "尾号" in source[max(0, start - 6) : start + 2]
                    )
                )
                if is_tail:
                    digits_only = re.sub(r"\D", "", raw)
                    surface = digits_only if digits_only.isdigit() else raw
                    hit = _mention_hit("ACCOUNT", surface, start, end, producer="PRESIDIO")
                    hit["normalized_value"] = ""
                    hit["mask_info"] = {
                        "masked": True,
                        "positions": list(range(len(surface))),
                        "kind": "tail_only",
                    }
                    hit["possible_forms"] = []
                    found.append(hit)
                    continue
                if "*" in norm or re.search(r"[*＊xX×ｘ]", raw):
                    if not _mask_collide_ok("ACCOUNT", norm):
                        occupied.pop()
                        continue
                    hit = _mention_hit("ACCOUNT", raw, start, end, producer="PRESIDIO")
                    hit["normalized_value"] = norm
                    hit["mask_info"] = {
                        "masked": True,
                        "positions": [i for i, ch in enumerate(raw) if ch in "*＊xX×ｘ"],
                        "kind": "account_mask",
                    }
                    hit["possible_forms"] = []
                    found.append(hit)
                    continue
                digits = re.sub(r"\D", "", norm)
                if len(digits) < 16 or len(digits) > 19:
                    occupied.pop()
                    continue
                # 18 位且像身份证号段：不进银行卡，留给/挡住身份证
                if len(digits) == 18 and re.match(
                    r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]$",
                    digits,
                ):
                    occupied.pop()
                    continue
                if not luhn_ok(digits):
                    hit = _mention_hit("ACCOUNT", raw, start, end, producer="PRESIDIO")
                    hit["normalized_value"] = ""
                    hit["mask_info"] = {
                        "masked": True,
                        "positions": list(range(len(raw))),
                        "kind": "luhn_failed",
                    }
                    hit["possible_forms"] = []
                    found.append(hit)
                    continue
            elif object_type == "ID_CARD":
                if not id_card_ok(raw):
                    # 占住跨度，防止同一串数字再被当成银行卡
                    continue
            elif object_type == "DEVICE":
                if not (raw.isdigit() and len(raw) == 15 and luhn_ok(raw)):
                    occupied.pop()
                    continue
            elif object_type == "IP":
                parts = raw.split(".")
                if not (len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)):
                    occupied.pop()
                    continue
            elif object_type == "PHONE":
                phone_norm = normalize_identifier("PHONE", raw)
                if is_excluded("PHONE", re.sub(r"\D", "", phone_norm) or phone_norm, exclusions):
                    occupied.pop()
                    continue
                if "*" in phone_norm:
                    if not _mask_collide_ok("PHONE", phone_norm):
                        occupied.pop()
                        continue
                    hit = _mention_hit("PHONE", raw, start, end, producer="PRESIDIO")
                    hit["normalized_value"] = phone_norm
                    hit["mask_info"] = {
                        "masked": True,
                        "positions": [i for i, ch in enumerate(raw) if ch in "*＊xX×ｘ"],
                        "kind": "phone_mask",
                    }
                    hit["possible_forms"] = []
                    found.append(hit)
                    continue
            elif object_type == "MERCHANT":
                m = re.search(r"([A-Za-z0-9_-]{6,32})\s*$", raw)
                if not m:
                    occupied.pop()
                    continue
                code = m.group(1)
                found.append(
                    _mention_hit("MERCHANT", code, start, end, producer="PRESIDIO")
                )
                continue

            if is_excluded(object_type, normalize_identifier(object_type, raw), exclusions):
                occupied.pop()
                continue

            producer = "SPACY" if result.entity_type in {"PERSON", "ORGANIZATION"} else "PRESIDIO"
            found.append(
                _mention_hit(object_type, raw, start, end, producer=producer)
            )

    # 脱敏占位符：反查 fingerprint，供跨案碰撞
    for match in _NAME_PLACEHOLDER_RE.finditer(source):
        raw = match.group(0)
        if not take(match.start(0), match.end(0)):
            continue
        hit = _mention_hit("NAME", raw, match.start(0), match.end(0), producer="PLACEHOLDER")
        hit["mask_info"] = {"masked": True, "positions": list(range(len(raw))), "kind": "placeholder"}
        hit["possible_forms"] = []
        if mapper:
            fp = mapper.get_fingerprint_by_anonymous_id(raw)
            hit["normalized_value"] = fp or ""
        else:
            hit["normalized_value"] = ""
        found.append(hit)

    return _drop_nested_person_mentions(found)


def _mention_hit(
    object_type: str,
    surface: str,
    start: int,
    end: int,
    *,
    producer: str = "SPACY",
) -> dict[str, Any]:
    normalized = normalize_identifier(object_type, surface)
    positions = [i for i, ch in enumerate(surface or "") if ch in "*＊xX×ｘ"]
    info: dict[str, Any] = {"masked": bool(positions), "positions": positions}
    # 默认可对齐的脱敏号保留 normalized；由调用方覆盖 kind
    keep_masked_norm = (
        info["masked"]
        and object_type in {"PHONE", "ACCOUNT"}
        and _mask_collide_ok(object_type, normalized)
    )
    if keep_masked_norm:
        info["kind"] = "phone_mask" if object_type == "PHONE" else "account_mask"
    return {
        "object_type": object_type,
        "surface_raw": surface,
        "normalized_value": normalized if (not info["masked"] or keep_masked_norm) else "",
        "mask_info": info,
        "possible_forms": possible_forms(normalized) if not info["masked"] else [],
        "char_start": start,
        "char_end": end,
        "producer": producer,
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
        if hit["object_type"] in {"ACCOUNT", "PHONE", "NAME"}:
            parties.append(hit["surface_raw"]) # 直接使用原文
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
        if hit.get("quote_redacted") and hit.get("quote_hash"):
            quote = hit["quote_redacted"]
            qhash = hit["quote_hash"]
        else:
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
    """规则抽取强标识。解析阶段已写好 text_redacted，这里禁止再跑 ML 脱敏。"""
    init_entity_db(db_path)

    with db_session(db_path) as conn:
        chunks = list_task_raw_chunks(conn, case_ids)
    scanned = len(chunks)
    mapper = get_global_mapper(db_path)

    inserted = 0
    with db_session(db_path) as conn:
        # 抽取器升级后，旧版本提及不再可信，先清掉避免与新结果混读
        conn.execute(
            "DELETE FROM entity_mentions WHERE task_id = ? AND extractor_version != ?",
            (task_id, EXTRACTOR_VERSION),
        )
        for chunk in chunks:
            raw = chunk.get("text_raw") or ""
            redacted = chunk.get("text_redacted") or ""
            hits = extract_rule_mentions(raw, mapper=mapper)
            if redacted and redacted != raw:
                for hit in extract_rule_mentions(redacted, mapper=mapper):
                    kind = (hit.get("mask_info") or {}).get("kind")
                    if hit.get("object_type") == "NAME" and kind == "placeholder":
                        hits.append(hit)
            inserted += persist_mentions(conn, task_id=task_id, chunk=chunk, hits=hits)

        mentions = _rows(
            conn,
            "SELECT * FROM entity_mentions "
            "WHERE task_id = ? AND extractor_version = ? ORDER BY created_at",
            (task_id, EXTRACTOR_VERSION),
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


def _case_corroboration_ok(
    object_type: str,
    case_ids: list[str],
    all_mentions: list[dict[str, Any]],
) -> bool:
    """姓名/组织跨案同名须在各案另有强标识佐证，否则不成候选。"""
    needed = CORROBORATION_TYPES.get(object_type)
    if not needed:
        return True
    for case_id in case_ids:
        case_types = {
            m.get("object_type")
            for m in all_mentions
            if m.get("case_id") == case_id
            and m.get("object_type") in needed
            and (m.get("normalized_value") or "")
            and not (json.loads(m.get("mask_info_json") or "{}") if isinstance(m.get("mask_info_json"), str) else (m.get("mask_info") or {})).get("masked")
        }
        if not case_types:
            return False
    return True


def _primary_field_key(object_type: str) -> tuple[str, str]:
    return {
        "ACCOUNT": ("account_no", "账号"),
        "PHONE": ("phone_no", "号码"),
        "NAME": ("name", "姓名"),
        "DEVICE": ("device_id", "设备号"),
        "ID_CARD": ("id_no", "证件号"),
        "ORGANIZATION": ("org_name", "名称"),
        "MERCHANT": ("merchant_id", "商户号"),
        "IP": ("ip_address", "IP 地址"),
    }.get(object_type, ("value", "标识值"))


_REVIEW_FIELD_ORDER: dict[str, list[tuple[str, str]]] = {
    "ACCOUNT": [
        ("account_no", "账号"),
        ("holder_name", "开户姓名"),
        ("bank_name", "开户行"),
        ("reserved_phone", "预留电话"),
        ("merchant", "关联商户"),
    ],
    "PHONE": [
        ("phone_no", "号码"),
        ("registrant", "登记人"),
        ("linked_account", "关联账户"),
        ("linked_device", "关联设备"),
        ("contact_context", "联络语境"),
    ],
    "NAME": [
        ("name", "姓名"),
        ("id_card", "证件"),
        ("phone", "手机号"),
        ("account", "账户"),
        ("organization", "组织"),
        ("role_in_material", "材料记载角色"),
    ],
    "DEVICE": [
        ("device_id", "设备号"),
        ("linked_phone", "关联手机号"),
        ("linked_account", "关联账户"),
        ("linked_person", "关联人员"),
        ("login_time", "登录时间"),
    ],
    "ORGANIZATION": [
        ("org_name", "名称"),
        ("credit_code", "统一社会信用代码"),
        ("legal_person", "法人"),
        ("address", "地址"),
        ("phone", "电话"),
        ("account", "账户"),
    ],
    "MERCHANT": [
        ("merchant_id", "商户号"),
        ("merchant_name", "商户名称"),
        ("settle_account", "结算账户"),
        ("pay_channel", "支付通道"),
        ("linked_org", "关联组织"),
    ],
    "ID_CARD": [("id_no", "证件号"), ("name", "姓名"), ("address", "地址")],
    "IP": [
        ("ip_address", "IP 地址"),
        ("linked_account", "关联账户"),
        ("linked_device", "关联设备"),
    ],
}


def _mention_mask_info(item: dict[str, Any]) -> dict[str, Any]:
    info = item.get("mask_info")
    if isinstance(info, dict):
        return info
    raw = item.get("mask_info_json")
    if isinstance(raw, str):
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def _candidate_display_name(object_type: str, value: str, items: list[dict[str, Any]]) -> str:
    """生成业务可读且稳定的候选名称，不暴露完整强标识。"""
    surfaces = [str(item.get("surface_raw") or "").strip() for item in items]
    surface = next((item for item in surfaces if item), "")
    normalized = str(value or normalize_identifier(object_type, surface))
    digits = re.sub(r"\D", "", normalized)

    if object_type == "ACCOUNT":
        return f"尾号 {digits[-4:]} 银行账户" if len(digits) >= 4 else "银行账户（同一脱敏标识）"
    if object_type == "PHONE":
        return f"尾号 {digits[-4:]} 手机号码" if len(digits) >= 4 else "手机号码（同一脱敏标识）"
    if object_type == "DEVICE":
        return f"IMEI 尾号 {digits[-4:]} 设备" if len(digits) >= 4 else "电子设备（同一设备标识）"
    if object_type == "ID_CARD":
        return f"尾号 {digits[-4:]} 身份证件" if len(digits) >= 4 else "身份证件（同一脱敏标识）"
    if object_type == "MERCHANT":
        tail = normalized[-6:] if normalized else ""
        return f"商户号尾号 {tail} 商户" if tail else "商户（同一商户标识）"
    if object_type == "IP":
        return f"{public_surface(surface or normalized, 'IP')} 网络地址"
    if object_type == "NAME":
        display = public_surface(surface, "NAME")
        return f"“{display}”人物" if display else "人物（同一脱敏姓名）"
    if object_type == "ORGANIZATION":
        if any(_mention_mask_info(item).get("kind") == "credit_code" for item in items):
            return f"信用代码尾号 {normalized[-6:]} 组织" if normalized else "组织（同一信用代码）"
        display = public_surface(surface, "ORGANIZATION")
        return f"“{display}”组织" if display else "组织主体（同一脱敏名称）"
    return "跨案相似实体"


def _review_field_for_related(primary_type: str, item: dict[str, Any]) -> str | None:
    related_type = item.get("object_type")
    kind = _mention_mask_info(item).get("kind")
    mapping = {
        "ACCOUNT": {
            "NAME": "holder_name",
            "PHONE": "reserved_phone",
            "MERCHANT": "merchant",
        },
        "PHONE": {
            "NAME": "registrant",
            "ACCOUNT": "linked_account",
            "DEVICE": "linked_device",
        },
        "NAME": {
            "ID_CARD": "id_card",
            "PHONE": "phone",
            "ACCOUNT": "account",
            "ORGANIZATION": "organization",
        },
        "DEVICE": {
            "PHONE": "linked_phone",
            "ACCOUNT": "linked_account",
            "NAME": "linked_person",
        },
        "ORGANIZATION": {
            "NAME": "legal_person",
            "PHONE": "phone",
            "ACCOUNT": "account",
        },
        "MERCHANT": {
            "ACCOUNT": "settle_account",
            "ORGANIZATION": "linked_org",
        },
        "ID_CARD": {"NAME": "name"},
        "IP": {"ACCOUNT": "linked_account", "DEVICE": "linked_device"},
    }
    if primary_type == "ORGANIZATION" and related_type == "ORGANIZATION" and kind == "credit_code":
        return "credit_code"
    if primary_type == "ACCOUNT" and related_type == "ORGANIZATION":
        surface = str(item.get("surface_raw") or "")
        return "bank_name" if "银行" in surface or "信用社" in surface else "merchant"
    if primary_type == "MERCHANT" and related_type == "MERCHANT":
        return "merchant_name" if kind != "merchant_id" else None
    return mapping.get(primary_type, {}).get(related_type)


def _context_label_values(object_type: str, text: str) -> dict[str, str]:
    """仅抽取带明确字段标签的短值，避免把邻近文本臆断为实体属性。"""
    compact = str(text or "").replace("\n", " ")
    patterns: dict[str, list[tuple[str, str]]] = {
        "ACCOUNT": [
            ("holder_name", r"(?:开户名|开户姓名|户名)[:：\s]*([\u4e00-\u9fff·]{2,12})"),
            ("bank_name", r"(?:开户行|开户银行)[:：\s]*([\u4e00-\u9fffA-Za-z0-9（）()]{2,30})"),
            ("reserved_phone", r"(?:预留电话|预留手机号)[:：\s]*(1[3-9]\d{9}|1[3-9]\d{1,2}[*＊xX]{2,6}\d{2,4})"),
        ],
        "PHONE": [
            ("registrant", r"(?:登记人|机主|号码所有人)[:：\s]*([\u4e00-\u9fff·]{2,12})"),
        ],
        "ORGANIZATION": [
            ("legal_person", r"(?:法定代表人|法人)[:：\s]*([\u4e00-\u9fff·]{2,12})"),
            ("address", r"(?:注册地址|住所地|地址)[:：\s]*([^，。；;]{4,50})"),
        ],
        "MERCHANT": [
            ("merchant_name", r"(?:商户名称|商户名)[:：\s]*([^，。；;]{2,40})"),
            ("pay_channel", r"(?:支付通道|支付渠道)[:：\s]*([^，。；;]{2,24})"),
        ],
        "ID_CARD": [("address", r"(?:户籍地址|住址|地址)[:：\s]*([^，。；;]{4,50})")],
    }
    values: dict[str, str] = {}
    for field_key, pattern in patterns.get(object_type, []):
        match = re.search(pattern, compact)
        if not match:
            continue
        raw = match.group(1).strip()
        related_type = "PHONE" if "phone" in field_key else None
        values[field_key] = public_surface(raw, related_type)

    if object_type == "PHONE":
        context = next((word for word in ("微信", "短信", "通话", "电话联系", "QQ", "聊天") if word in compact), "")
        if context:
            values["contact_context"] = context
    if object_type == "NAME":
        role = next(
            (
                word
                for word in (
                    "被告人",
                    "犯罪嫌疑人",
                    "被害人",
                    "证人",
                    "法定代表人",
                    "负责人",
                    "联系人",
                )
                if word in compact
            ),
            "",
        )
        if role:
            values["role_in_material"] = role
    return values


def _build_review_field_compare(
    object_type: str,
    items: list[dict[str, Any]],
    all_mentions: list[dict[str, Any]],
    case_names: dict[str, str],
) -> list[dict[str, Any]]:
    case_ids = sorted({item["case_id"] for item in items})
    primary_key, _ = _primary_field_key(object_type)
    if object_type == "ORGANIZATION" and any(
        _mention_mask_info(item).get("kind") == "credit_code" for item in items
    ):
        primary_key = "credit_code"

    values: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    def add(field_key: str, case_id: str, value: str | None) -> None:
        clean = str(value or "").strip()
        if clean and clean not in values[field_key][case_id]:
            values[field_key][case_id].append(clean)

    for item in items:
        add(
            primary_key,
            item["case_id"],
            public_surface(item.get("surface_raw") or "", object_type),
        )
        for key, context_value in _context_label_values(
            object_type, item.get("quote_redacted") or ""
        ).items():
            add(key, item["case_id"], context_value)

    # 只使用同一 chunk 中已抽取出的其他实体作为关联字段，避免跨段误关联。
    item_chunks = {(item["case_id"], item.get("chunk_id")) for item in items}
    for related in all_mentions:
        if (related.get("case_id"), related.get("chunk_id")) not in item_chunks:
            continue
        field_key = _review_field_for_related(object_type, related)
        if field_key:
            add(
                field_key,
                related["case_id"],
                public_surface(related.get("surface_raw") or "", related.get("object_type")),
            )

    rows = []
    for field_key, label in _REVIEW_FIELD_ORDER.get(
        object_type, [(_primary_field_key(object_type))]
    ):
        rendered = {
            case_id: "、".join(values[field_key].get(case_id) or [])
            for case_id in case_ids
        }
        present = [item for item in rendered.values() if item]
        differs = len(set(present)) > 1
        per_case = []
        for case_id in case_ids:
            field_value = rendered[case_id] or None
            per_case.append(
                {
                    "case_id": case_id,
                    "case_name": case_names.get(case_id) or case_id,
                    "value": field_value,
                    "status": "missing" if field_value is None else ("diff" if differs else "same"),
                }
            )
        rows.append({"field_key": field_key, "label": label, "per_case": per_case})
    return rows


def _enrich_candidate_for_review(
    *,
    object_type: str,
    value: str,
    items: list[dict[str, Any]],
    all_mentions: list[dict[str, Any]],
    case_names: dict[str, str],
    fingerprint: str,
) -> dict[str, Any]:
    """将碰撞结果 enrich 为实体复核 Schema 形态。"""
    from app.entity_review_schema import INTERNAL_TO_PUBLIC_TYPE, ENTITY_TYPE_LABELS, EntityType

    public_type = INTERNAL_TO_PUBLIC_TYPE.get(object_type, EntityType.BANK_ACCOUNT)
    case_ids = sorted({item["case_id"] for item in items})
    cases = [{"case_id": cid, "case_name": case_names.get(cid) or cid} for cid in case_ids]

    # 每案保留一条最佳证据
    evidence = []
    per_case_values = []
    seen_cases = set()
    for item in items:
        cid = item["case_id"]
        display = public_surface(item.get("surface_raw") or "", object_type)
        if cid not in seen_cases:
            seen_cases.add(cid)
            evidence.append(
                {
                    "case_id": cid,
                    "case_name": case_names.get(cid) or cid,
                    "chunk_id": item["chunk_id"],
                    "document_version_id": item.get("document_version_id") or "",
                    "document_id": item.get("document_id"),
                    "filename": item.get("filename"),
                    "page_start": item.get("page_start"),
                    "page_end": item.get("page_end"),
                    "quote": item.get("quote_redacted") or display,
                    "quote_hash": item.get("quote_hash") or "",
                }
            )
            per_case_values.append(
                {
                    "case_id": cid,
                    "case_name": case_names.get(cid) or cid,
                    "value": display,
                    "status": "same",
                }
            )

    type_label = ENTITY_TYPE_LABELS.get(public_type, object_type)
    display_name = _candidate_display_name(object_type, value, items)
    field_compare = _build_review_field_compare(
        object_type, items, all_mentions, case_names
    )
    primary_row = next(
        (
            row
            for row in field_compare
            if any(item.get("value") for item in row.get("per_case", []))
        ),
        field_compare[0] if field_compare else None,
    )
    primary_statuses = {
        row["status"] for row in (primary_row or {}).get("per_case", []) if row.get("value")
    }
    status = "diff" if "diff" in primary_statuses else "same"
    field_label = (primary_row or {}).get("label") or _primary_field_key(object_type)[1]

    records = []
    for item in items:
        records.append(
            {
                "case_id": item["case_id"],
                "case_name": case_names.get(item["case_id"]) or item["case_id"],
                "value": public_surface(item.get("surface_raw") or "", object_type),
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

    supporting = [f"{field_label}一致"] if status == "same" else []
    conflicts = [f"{field_label}记载存在差异"] if status == "diff" else []

    return {
        "candidate_id": new_id(),
        "fingerprint": fingerprint,
        "entity_type": public_type.value,
        "display_name": display_name,
        "cases": cases,
        "field_compare": field_compare,
        "evidence": evidence,
        "supporting_facts": supporting,
        "conflicts": conflicts,
        "missing_fields": [],
        "impact": {
            "case_count": len(case_ids),
            "relation_count": 0,
            "event_count": 0,
            "clue_count": 0,
            "mention_count": len(items),
        },
        "agent_summary": "",
        "recommendation": "DEFER",
        "generated_clues": [],
        "decision": "PENDING",
        "reason": "",
        "confidence_label": "待核验",
        "match_tier": "STRONG",
        "aliases": [],
        "match_basis": [
            f"规范化值在 {len(case_ids)} 起案件中等值出现",
            "每案至少一条可定位原文证据",
        ],
        "differences": conflicts or ["系统不自动合并，是否同一对象由人工决定"],
        "records": records,
        "question": f"请核验：相关案件中该{type_label}是否指向同一主体？",
        "correction": None,
        # 内部兼容字段
        "_internal_type": object_type,
        "_normalized_value": value,
    }


# ---------- 疑似同一人（别名表 + 同案称呼簇） ----------

_NICK_SUFFIX = ("哥", "姐", "叔", "总", "嫂", "爷")
_ALIAS_TABLE_NAME_HINT = ("别名", "化名", "实体别名")


def _person_surname(name: str) -> str:
    text = (name or "").strip().replace("·", "")
    if not text:
        return ""
    for compound in _COMPOUND_SURNAME_SET:
        if text.startswith(compound):
            return compound
    return text[0]


def _person_variant_kind(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    if any(text.endswith(s) for s in _NICK_SUFFIX) and len(text) <= 4:
        return "nick"
    if re.fullmatch(r"[\u4e00-\u9fff]某{1,2}", text):
        return "anon"
    if re.fullmatch(r"[\u4e00-\u9fff·]{2,4}", text):
        return "full"
    return "other"


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return out


def _parse_alias_table_text(text: str) -> list[dict[str, Any]]:
    """从别名表纯文本解析种子：统一称呼 + 别名列表。"""
    seeds: list[dict[str, Any]] = []
    if not text:
        return seeds
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("|")
        if not line:
            continue
        if "统一称呼" in line and "别名" in line:
            continue
        parts = [p.strip() for p in re.split(r"[|\t]+", line) if p.strip()]
        if len(parts) < 2:
            parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
        if len(parts) < 2:
            continue
        canonical = parts[0]
        aliases_raw = parts[1]
        same_flag = parts[3] if len(parts) >= 4 else (parts[2] if len(parts) >= 3 else "是")
        if same_flag and ("否" in same_flag or same_flag in {"N", "n", "NO", "no"}):
            continue
        aliases = [canonical]
        for token in re.split(r"[、,，;/；]", aliases_raw):
            token = token.strip()
            if token and token not in aliases:
                aliases.append(token)
        aliases = [a for a in aliases if re.search(r"[\u4e00-\u9fffA-Za-z0-9]", a)]
        if len(aliases) < 2:
            continue
        seeds.append({"canonical": canonical, "aliases": aliases, "basis": "任务材料·实体别名表"})
    return seeds


def _load_alias_seeds_from_materials(
    task_id: str,
    case_ids: list[str],
    db_path=None,
) -> list[dict[str, Any]]:
    """从案件材料中识别别名表类文件并解析种子。"""
    seeds: list[dict[str, Any]] = []
    if not case_ids:
        return seeds
    placeholders = ",".join("?" for _ in case_ids)
    with db_session(db_path) as conn:
        docs = _rows(
            conn,
            f"""
            SELECT d.id, d.filename, d.case_id, dv.storage_path, dv.id AS version_id
            FROM documents d
            JOIN document_versions dv ON dv.id = d.current_version_id
            WHERE d.case_id IN ({placeholders})
              AND (d.deleted_at IS NULL OR d.deleted_at = '')
            """,
            tuple(case_ids),
        )
        for doc in docs:
            name = doc.get("filename") or ""
            if not any(h in name for h in _ALIAS_TABLE_NAME_HINT):
                continue
            path = Path(doc.get("storage_path") or "")
            text = ""
            if path.suffix.lower() in {".xlsx", ".xls"} and path.is_file():
                try:
                    import openpyxl

                    wb = openpyxl.load_workbook(str(path), data_only=True)
                    lines = []
                    for ws in wb.worksheets:
                        for row in ws.iter_rows(values_only=True):
                            cells = ["" if c is None else str(c).strip() for c in row]
                            if any(cells):
                                lines.append(" | ".join(cells))
                    text = "\n".join(lines)
                except Exception:
                    text = ""
            if not text:
                chunks = _rows(
                    conn,
                    """
                    SELECT text_raw FROM document_chunks
                    WHERE document_version_id = ? AND COALESCE(is_active, 1) = 1
                    ORDER BY ordinal
                    """,
                    (doc["version_id"],),
                )
                text = "\n".join((c.get("text_raw") or "") for c in chunks)
            for seed in _parse_alias_table_text(text):
                seed["source_file"] = name
                seed["case_id"] = doc.get("case_id")
                seeds.append(seed)
    return seeds


def _cluster_same_case_name_variants(
    mentions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """同案同姓：外号 / X某 / 全名 → 疑似簇。"""
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in mentions:
        if m.get("object_type") != "NAME":
            continue
        info = m.get("mask_info")
        if isinstance(info, str):
            info = json.loads(info or "{}")
        elif m.get("mask_info_json"):
            info = json.loads(m.get("mask_info_json") or "{}")
        if (info or {}).get("kind") == "placeholder":
            continue
        surface = (m.get("surface_raw") or m.get("normalized_value") or "").strip()
        if not surface or _person_variant_kind(surface) == "other":
            continue
        by_case[m["case_id"]].append(m)

    clusters: list[dict[str, Any]] = []
    for case_id, items in by_case.items():
        by_surname: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            surface = (item.get("surface_raw") or item.get("normalized_value") or "").strip()
            surname = _person_surname(surface)
            if surname:
                by_surname[surname].append(item)
        for surname, group in by_surname.items():
            surfaces = sorted(
                {
                    (item.get("normalized_value") or item.get("surface_raw") or "").strip()
                    for item in group
                    if (item.get("normalized_value") or item.get("surface_raw") or "").strip()
                }
            )
            if len(surfaces) < 2:
                continue
            kinds = {_person_variant_kind(s) for s in surfaces}
            # 必须有外号（X哥/姐等）才同姓成簇；禁止仅凭「全名+X某」误并（赵瑞≠赵某）
            if "nick" not in kinds:
                continue
            clusters.append(
                {
                    "case_id": case_id,
                    "aliases": surfaces,
                    "items": group,
                    "basis": f"同案同姓称呼簇（{surname}）",
                }
            )
    return clusters


def build_alias_suspect_candidates(
    mentions: list[dict[str, Any]],
    *,
    case_names: dict[str, str],
    alias_seeds: list[dict[str, Any]],
    rejected: set[str],
) -> list[dict[str, Any]]:
    """生成疑似同一人候选；不自动合并，供实体复核列出。"""
    uf = _UnionFind()
    basis_by_key: dict[str, list[str]] = defaultdict(list)

    name_mentions = [
        m
        for m in mentions
        if m.get("object_type") == "NAME"
        and (m.get("normalized_value") or m.get("surface_raw") or "").strip()
    ]
    surface_to_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in name_mentions:
        key = (m.get("normalized_value") or m.get("surface_raw") or "").strip()
        surface_to_items[key].append(m)
        uf.add(key)

    for seed in alias_seeds:
        aliases = [a.strip() for a in (seed.get("aliases") or []) if a and str(a).strip()]
        if len(aliases) < 2:
            continue
        present = [a for a in aliases if a in surface_to_items]
        if len(present) < 1:
            continue
        for a in aliases:
            uf.add(a)
        for i in range(1, len(aliases)):
            uf.union(aliases[0], aliases[i])
        root = uf.find(aliases[0])
        basis_by_key[root].append(seed.get("basis") or "实体别名表")

    for cluster in _cluster_same_case_name_variants(mentions):
        aliases = cluster["aliases"]
        for a in aliases:
            uf.add(a)
        for i in range(1, len(aliases)):
            uf.union(aliases[0], aliases[i])
        root = uf.find(aliases[0])
        basis_by_key[root].append(cluster["basis"])

    candidates: list[dict[str, Any]] = []
    for root, members in uf.groups().items():
        members = sorted(set(members))
        if len(members) < 2:
            continue
        items: list[dict[str, Any]] = []
        for name in members:
            items.extend(surface_to_items.get(name) or [])
        present_members = [m for m in members if m in surface_to_items]
        if len(present_members) < 2 and len(items) < 2:
            continue
        if not items:
            continue
        deduped = []
        seen = set()
        for item in items:
            key = (
                item.get("case_id"),
                item.get("chunk_id"),
                item.get("surface_raw") or item.get("normalized_value"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        items = deduped
        if len(items) < 2:
            continue

        case_ids = sorted({item["case_id"] for item in items})
        value_key = "|".join(sorted(present_members or members))
        fingerprint = candidate_fingerprint("NAME_ALIAS", value_key, case_ids or ["task"])
        if fingerprint in rejected:
            continue

        bases = list(dict.fromkeys(basis_by_key.get(root) or ["疑似化名关联"]))
        confidence = "疑似·高" if any("别名表" in b for b in bases) else "疑似·中"
        display_aliases = present_members if len(present_members) >= 2 else members
        display_name = f"疑似同一人：{' / '.join(display_aliases[:4])}"
        if len(display_aliases) > 4:
            display_name += f" 等{len(display_aliases)}种写法"

        cand = _enrich_candidate_for_review(
            object_type="NAME",
            value=display_aliases[0],
            items=items,
            all_mentions=mentions,
            case_names=case_names,
            fingerprint=fingerprint,
        )
        for row in cand.get("field_compare") or []:
            if row.get("field_key") == "name":
                for per in row.get("per_case") or []:
                    cid = per.get("case_id")
                    vals = sorted(
                        {
                            public_surface(it.get("surface_raw") or "", "NAME")
                            for it in items
                            if it.get("case_id") == cid
                        }
                    )
                    if vals:
                        per["value"] = "、".join(vals)
                        per["status"] = "diff" if len(vals) > 1 else "same"

        cand["display_name"] = display_name
        cand["confidence_label"] = confidence
        cand["match_tier"] = "SUSPECTED"
        cand["aliases"] = display_aliases
        cand["match_basis"] = bases + [
            f"材料中出现 {len(display_aliases)} 种写法",
            "系统不自动认定同一人，请检察官复核",
        ]
        cand["question"] = "请核验：下列化名/称呼是否指向同一人？"
        cand["differences"] = [
            f"化名列表：{'、'.join(display_aliases)}",
            "疑似关联，是否合并由人工决定",
        ]
        cand["recommendation"] = "DEFER"
        if len(cand.get("cases") or []) < 2 and len(items) >= 2:
            cand["_allow_single_case"] = True
        candidates.append(cand)
    return candidates


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
        elif mention.get("mask_info_json"):
            info = json.loads(mention.get("mask_info_json") or "{}")
        info = info or {}
        value = mention.get("normalized_value") or ""
        kind = info.get("kind")
        if info.get("masked"):
            # 仅可对齐的脱敏手机/卡号参与碰撞；尾号/校验失败等仍跳过
            if kind not in {"phone_mask", "account_mask"} or not _mask_collide_ok(
                object_type, value
            ):
                continue
        if not value or is_excluded(object_type, value, exclusions):
            continue
        groups[(object_type, value)].append(mention)

    candidates = []
    for (object_type, value), items in groups.items():
        # 同材料重复出现合并：按 (case_id, chunk_id, surface) 去重
        deduped = []
        seen_keys = set()
        for item in items:
            key = (item["case_id"], item.get("chunk_id"), item.get("surface_raw") or item.get("normalized_value"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(item)
        items = deduped

        case_ids = sorted({item["case_id"] for item in items})
        if len(case_ids) < 2:
            continue
        if any(not any(item["chunk_id"] for item in items if item["case_id"] == case_id) for case_id in case_ids):
            continue
        if object_type in CORROBORATION_REQUIRED:
            # 统一社会信用代码 / 商号形态：同名跨案可直接进待核
            strong_org = object_type == "ORGANIZATION" and (
                _is_commercial_org(value)
                or any(
                    (
                        (
                            json.loads(item.get("mask_info_json") or "{}")
                            if isinstance(item.get("mask_info_json"), str)
                            else (item.get("mask_info") or {})
                        )
                        or {}
                    ).get("kind")
                    == "credit_code"
                    for item in items
                )
            )
            if not strong_org and not _case_corroboration_ok(object_type, case_ids, mentions):
                continue
        fingerprint = candidate_fingerprint(object_type, value, case_ids)
        if fingerprint in rejected:
            continue
        # 每案至少一条有效 quote_hash
        if any(
            not any((item.get("quote_hash") or "") for item in items if item["case_id"] == case_id)
            for case_id in case_ids
        ):
            continue
        candidates.append(
            _enrich_candidate_for_review(
                object_type=object_type,
                value=value,
                items=items,
                all_mentions=mentions,
                case_names=case_names,
                fingerprint=fingerprint,
            )
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
              AND m.extractor_version = ?
            """,
            (task_id, EXTRACTOR_VERSION),
        )
        rejected = list_rejected_fingerprints(conn, task_id)
    candidates = collide_mentions(
        mentions,
        case_names=case_names,
        rejected=rejected,
        exclusions=exclusions,
    )
    alias_seeds = _load_alias_seeds_from_materials(task_id, case_ids, db_path=db_path)
    suspects = build_alias_suspect_candidates(
        mentions,
        case_names=case_names,
        alias_seeds=alias_seeds,
        rejected=rejected,
    )
    # 强碰撞优先；疑似卡按 fingerprint 去重追加
    seen_fp = {c.get("fingerprint") for c in candidates if c.get("fingerprint")}
    for item in suspects:
        fp = item.get("fingerprint")
        if fp and fp not in seen_fp:
            candidates.append(item)
            seen_fp.add(fp)
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
        "alias_seed_count": len(alias_seeds),
        "suspect_count": len(suspects),
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
    all_rules = load_rules().get("rules") or []
    # 按 object_type 索引，只取 DIRECT_MATERIAL 类型的规则
    rules_by_type = {
        item["object_type"]: item
        for item in all_rules
        if item.get("evidence_mode") == "DIRECT_MATERIAL"
    }
    hits = []
    now = utc_now()
    with db_session(db_path) as conn:
        for candidate in collision["candidates"]:
            object_type = candidate["entity_type"]
            spec = rules_by_type.get(object_type)
            if not spec:
                continue   # 不是 DIRECT_MATERIAL 的候选（如事件层规则）跳过
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