"""监督分析任务与产物模型。

一个"监督分析任务"是长期业务容器：绑定案件范围、监督目的、有效期、材料与全部过程产物。
产物是任务里可以单独打开、单独引用、单独留版本的工作结果；任务目录、智能体消息中的链接、
中间工作区标签三者共用同一个 artifact_id，禁止出现两套状态。

本文件按注释分段：状态与错误码、建表、产物依赖与过期传播、TaskService 业务编排。
数据库连接、案件与材料读取直接复用 tools/files.py，不另起一套持久化。
"""

from __future__ import annotations

import json, os, re
from typing import Any, Optional
import hashlib
from pathlib import Path
from app.config import MATERIAL_STORAGE_DIR, REDACTION_STORAGE_DIR

from app.files import (
    PARSER_VERSION,
    MaterialError,
    MaterialService,
    _insert,
    _row,
    _rows,
    _update,
    db_session,
    ensure_demo_case,
    get_material_service,
    infer_material_type,
    init_db,
    list_chunks,
    new_id,
    utc_now,
)
from tools.entities import init_entity_db

# 报告正文是面向检察官的可读文档：禁止混入内部工程代号 / chunk / 引用 hash / 长 ID。
_ENGINEERING_TOKEN_RE = re.compile(
    r"[0-9a-f]{16,}|document_version_id|quote_hash|chunk_[A-Za-z0-9_\-]{6,}"
)


def _report_engineering_tokens(text: str) -> bool:
    return bool(_ENGINEERING_TOKEN_RE.search(text or ""))


# 线索确认状态的唯一判据：人工在线索中心处置写入 disposition；实体复核确认同一会把
# 关联线索升格（promotion=confirmed）。历史数据常只有后者、没有 disposition，两者
# 必须统一解析，否则模型会一直把已确认线索当作待核。
CLUE_DISPOSITION_LABELS = {
    "PENDING": "待确认",
    "CONTINUE": "已确认关联",
    "CONFIRMED": "已确认关联",
    "NEED_MATERIAL": "待补材料",
    "EXCLUDE": "已排除",
    "DEFER": "暂缓",
}


def clue_disposition_code(payload: dict[str, Any]) -> str:
    disp = str(payload.get("disposition") or "").upper()
    if disp:
        return disp
    return "CONFIRMED" if payload.get("promotion") == "confirmed" else "PENDING"


def clue_is_confirmed(payload: dict[str, Any]) -> bool:
    """线索是否已被人工确认关联（含实体复核升格）。"""
    return clue_disposition_code(payload) in {"CONTINUE", "CONFIRMED"}


def clue_is_human_set(payload: dict[str, Any]) -> bool:
    """线索是否已带人工判断（任何处置，或实体升格）——用于锁定模型改写。"""
    return bool(payload.get("disposition")) or payload.get("promotion") == "confirmed"


def clue_state_label(payload: dict[str, Any]) -> str:
    code = clue_disposition_code(payload)
    return CLUE_DISPOSITION_LABELS.get(code, code)


# ---------- 状态与错误码 ----------

TASK_STATUSES = {
    "SCOPE_DRAFT",      # 范围已填，计划未确认
    "PLAN_CONFIRMED",   # 计划已确认，进入工作台
    "RUNNING",          # 有步骤在执行
    "WAITING_USER",     # 等待人工复核
    "CLOSED",
}

# 产物类型：与《智能体工作台信息架构方案》锁定的清单一致
ARTIFACT_TYPES = {
    "TASK_SCOPE",
    "MATERIAL_BATCH",
    "MATERIAL_DOC",
    "ENTITY_CANDIDATE_SET",
    "CLUE_SET",
    "CLUE_ITEM",
    "ROLE_TIMELINE",
    "LINK_GRAPH",
    "SOURCE_VERIFY",
    "REPORT_DRAFT",
    "REPORT_EXPORT",
}

ARTIFACT_STATUSES = {"DRAFT", "PENDING_REVIEW", "VALID", "STALE", "INVALID"}

# 已发布报告是历史快照，永不因上游变更被改写，只提示"可生成新报告"
FROZEN_ARTIFACT_TYPES = {"REPORT_EXPORT"}

# 任务目录分组：左侧业务清单按此归组，未生成的节点显示步骤状态而不是伪文件
DIRECTORY_GROUPS = [
    {"key": "scope", "label": "任务说明", "types": ["TASK_SCOPE"]},
    {"key": "materials", "label": "卷宗材料", "types": ["MATERIAL_BATCH", "MATERIAL_DOC"]},
    {"key": "entities", "label": "跨案对象待核", "types": ["ENTITY_CANDIDATE_SET"]},
    {"key": "clues", "label": "疑似关联线索", "types": ["CLUE_SET", "CLUE_ITEM"]},
    {"key": "views", "label": "事件时间线", "types": ["ROLE_TIMELINE", "LINK_GRAPH"]},
    {"key": "verify", "label": "核验留痕", "types": ["SOURCE_VERIFY"]},
    {"key": "reports", "label": "报告", "types": ["REPORT_DRAFT", "REPORT_EXPORT"]},
]

TASK_ERROR_CODES = {
    "NOT_FOUND": "TASK_NOT_FOUND",
    "INVALID_SCOPE": "TASK_INVALID_SCOPE",
    "ARTIFACT_NOT_FOUND": "TASK_ARTIFACT_NOT_FOUND",
    "STATE_CONFLICT": "TASK_STATE_CONFLICT",
}


class TaskError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "message": self.message, "details": self.details}


# ---------- 建表 ----------

TASK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS supervision_tasks (
    id VARCHAR(36) PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    purpose TEXT NOT NULL,
    authorized_until VARCHAR(32) NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS task_cases (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    case_id VARCHAR(36) NOT NULL,
    display_name VARCHAR(160) NOT NULL,
    auth_status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE(task_id, case_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    type VARCHAR(40) NOT NULL,
    title VARCHAR(200) NOT NULL,
    status VARCHAR(32) NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    parent_ids_json TEXT NOT NULL DEFAULT '[]',
    ref_key VARCHAR(120),
    stale_reason TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE(task_id, type, ref_key)
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id VARCHAR(36) PRIMARY KEY,
    artifact_id VARCHAR(36) NOT NULL,
    version INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    input_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_by_run_id VARCHAR(64),
    created_at DATETIME NOT NULL,
    UNIQUE(artifact_id, version)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    tool_call_id VARCHAR(64), 
    created_at DATETIME NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chat_task_time ON chat_messages(task_id, created_at);
"""


def init_task_db(db_path=None) -> None:
    """建任务与产物相关表，可重复执行。

    仓库里若已有旧版空表 artifacts（无 task_id），则重建为任务产物表。
    """
    with db_session(db_path) as conn:
        conn.executescript(TASK_SCHEMA_SQL)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()}
        if cols and "task_id" not in cols:
            # 旧空表结构不同：先删再建，避免与监督任务产物模型冲突
            count = conn.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
            if count:
                raise RuntimeError("artifacts 表已有数据且缺少 task_id，请先手工迁移")
            conn.execute("DROP TABLE artifacts")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id VARCHAR(36) PRIMARY KEY,
                    task_id VARCHAR(36) NOT NULL,
                    type VARCHAR(40) NOT NULL,
                    title VARCHAR(200) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    current_version INTEGER NOT NULL DEFAULT 1,
                    parent_ids_json TEXT NOT NULL DEFAULT '[]',
                    ref_key VARCHAR(120),
                    stale_reason TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE(task_id, type, ref_key)
                );
                """
            )


# ---------- 产物依赖与过期传播 ----------


def _artifact_row(conn, artifact_id: str) -> Optional[dict[str, Any]]:
    return _row(conn, "SELECT * FROM artifacts WHERE id = ?", (artifact_id,))


def _downstream_ids(conn, task_id: str, root_ids: list[str]) -> list[str]:
    """沿 parent_ids 反向遍历，只收集真正依赖了变化节点的下游产物。"""
    all_rows = _rows(conn, "SELECT * FROM artifacts WHERE task_id = ?", (task_id,))
    parents = {row["id"]: json.loads(row["parent_ids_json"] or "[]") for row in all_rows}
    types = {row["id"]: row["type"] for row in all_rows}

    affected: list[str] = []
    frontier = set(root_ids)
    while frontier:
        nxt = set()
        for artifact_id, parent_ids in parents.items():
            if artifact_id in affected or artifact_id in frontier:
                continue
            if frontier & set(parent_ids):
                nxt.add(artifact_id)
        affected.extend(sorted(nxt))
        frontier = nxt

    # 已发布报告是冻结快照，不进入过期清单
    return [a for a in affected if types.get(a) not in FROZEN_ARTIFACT_TYPES]


# ---------- TaskService 业务编排 ----------


class TaskService:
    """任务范围、计划确认、材料接入与产物版本的统一入口。"""

    def __init__(self, db_path=None):
        self.db_path = db_path
        init_db(db_path)
        init_task_db(db_path)
        init_entity_db(db_path)

    # ----- 任务 -----

    def create_task(
        self,
        *,
        title: str,
        purpose: str,
        authorized_until: str,
        cases: list[dict[str, Any]],
        note: str = "",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        purpose = (purpose or "").strip()
        if not purpose:
            raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], "监督目的必填")
        if not (authorized_until or "").strip():
            raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], "授权有效期必填")
        named = [c for c in cases if (c.get("name") or c.get("case_id"))]
        if len(named) < 2:
            raise TaskError(
                TASK_ERROR_CODES["INVALID_SCOPE"],
                "至少两起案件才能开展跨案分析",
                {"case_count": len(named)},
            )

        now = utc_now()
        task_id = new_id()
        resolved_title = (title or "").strip() or f"跨案监督分析 {now[:10]}"

        with db_session(self.db_path) as conn:
            _insert(
                conn,
                "supervision_tasks",
                {
                    "id": task_id,
                    "title": resolved_title,
                    "purpose": purpose,
                    "authorized_until": authorized_until,
                    "note": note or "",
                    "status": "SCOPE_DRAFT",
                    "created_by": user_id or "local-user",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            for item in named:
                # 不传 case_id 时必须新建案件，避免多起案件被并到同一条已有记录上
                case_id = ensure_demo_case(conn, item.get("case_id") or new_id())
                _insert(
                    conn,
                    "task_cases",
                    {
                        "id": new_id(),
                        "task_id": task_id,
                        "case_id": case_id,
                        "display_name": (item.get("name") or "").strip() or f"案件 {case_id[:8]}",
                        # 授权矩阵尚未实现，这里记录逐案校验结果的占位状态
                        "auth_status": "AUTHORIZED",
                        "created_at": now,
                    },
                )

        scope = self.write_artifact(
            task_id=task_id,
            type="TASK_SCOPE",
            title="案件范围与监督目的",
            ref_key="scope",
            status="VALID",
            payload=self._scope_payload(task_id),
        )
        return {"task": self.get_task(task_id), "scope_artifact_id": scope["id"]}

    def add_case_to_task(
        self,
        task_id: str,
        *,
        name: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """向已有任务追加案件：与 create_task 相同的 cases/材料存储路径。"""
        display = (name or "").strip()
        if not display:
            raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], "案件名称必填")
        task = self.get_task(task_id)
        if task.get("status") == "CLOSED":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "任务已关闭，不能再添加案件")
        for item in task.get("cases") or []:
            existing = (item.get("display_name") or item.get("name") or "").strip()
            if existing == display:
                raise TaskError(
                    TASK_ERROR_CODES["INVALID_SCOPE"],
                    "该案件已在本任务范围内",
                    {"case_id": item.get("case_id")},
                )

        now = utc_now()
        with db_session(self.db_path) as conn:
            case_id = ensure_demo_case(conn, new_id())
            _insert(
                conn,
                "task_cases",
                {
                    "id": new_id(),
                    "task_id": task_id,
                    "case_id": case_id,
                    "display_name": display,
                    "auth_status": "AUTHORIZED",
                    "created_at": now,
                },
            )
            _update(
                conn,
                "supervision_tasks",
                task_id,
                {"updated_at": now},
            )

        self.write_artifact(
            task_id=task_id,
            type="TASK_SCOPE",
            title="案件范围与监督目的",
            ref_key="scope",
            status="VALID",
            payload=self._scope_payload(task_id),
        )
        task = self.get_task(task_id)
        case = next((c for c in task.get("cases") or [] if c.get("case_id") == case_id), None)
        return {
            "task": task,
            "case": case
            or {
                "case_id": case_id,
                "display_name": display,
                "auth_status": "AUTHORIZED",
            },
        }

    def delete_task(self, task_id: str) -> dict:
        with db_session() as conn:
            docs = _rows(conn, """
                        SELECT DISTINCT dv.storage_path, d.id AS document_id
                        FROM document_versions dv
                        JOIN documents d ON d.id = dv.document_id
                        JOIN task_cases tc ON tc.case_id = d.case_id
                        WHERE tc.task_id = ?
                          AND d.deleted_at IS NULL
                    """, (task_id,))

            dirs_to_cleanup = set()

            for doc in docs:
                # 删除原始文件
                storage_path = doc['storage_path']
                if storage_path and os.path.exists(storage_path):
                    try:
                        os.remove(storage_path)
                        dirs_to_cleanup.add(os.path.dirname(storage_path))

                    except OSError as e:
                        print(f"Warning: failed to delete file {storage_path}: {e}")
            root_dirs = {
                str(Path(MATERIAL_STORAGE_DIR).resolve()),
                str(Path(REDACTION_STORAGE_DIR).resolve()),
            }
            for dir_path in sorted(dirs_to_cleanup, key=len, reverse=True):
                # 从最深层开始清理
                current = Path(dir_path).resolve()
                while True:
                    if not current.exists():
                        break
                    # 检查是否到达根目录（停止条件）
                    if str(current) in root_dirs:
                        break
                    # 检查目录是否为空（仅包含 . 和 ..）
                    if any(current.iterdir()):
                        break  # 非空目录，停止向上
                    try:
                        current.rmdir()
                        print(f"Removed empty directory: {current}")
                        # 继续向上检查父目录
                        current = current.parent
                    except OSError as e:
                        print(f"Warning: failed to remove directory {current}: {e}")
                        break
            # 1. 删除关联的聊天消息
            conn.execute("DELETE FROM chat_messages WHERE task_id = ?", (task_id,))

            # 2. 删除关联的案件记录
            conn.execute("DELETE FROM task_cases WHERE task_id = ?", (task_id,))

            # 3. 删除关联的产物版本
            artifacts = _rows(conn, "SELECT id FROM artifacts WHERE task_id = ?", (task_id,))
            for art in artifacts:
                conn.execute("DELETE FROM artifact_versions WHERE artifact_id = ?", (art["id"],))

            # 4. 删除产物本身
            conn.execute("DELETE FROM artifacts WHERE task_id = ?", (task_id,))

            # 5. 删除任务本身（表名是 supervision_tasks，不是 tasks）
            conn.execute("DELETE FROM supervision_tasks WHERE id = ?", (task_id,))

            conn.commit()

        from app.files import GlobalEntityMapper  # 确保导入
        mapper = GlobalEntityMapper(db_path=self.db_path)
        mapper.delete_by_task_id(task_id)
        return {"success": True, "task_id": task_id}

    def list_tasks(self, limit: int = 8) -> list[dict[str, Any]]:
        """最左栏只显示最近若干个任务，更多任务走搜索抽屉。"""
        with db_session(self.db_path) as conn:
            tasks = _rows(
                conn,
                "SELECT * FROM supervision_tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, limit),),
            )
            for task in tasks:
                task["case_count"] = len(
                    _rows(conn, "SELECT id FROM task_cases WHERE task_id = ?", (task["id"],))
                )
                task["stale_count"] = len(
                    _rows(
                        conn,
                        "SELECT id FROM artifacts WHERE task_id = ? AND status = 'STALE'",
                        (task["id"],),
                    )
                )
            return tasks

    def get_task(self, task_id: str) -> dict[str, Any]:
        self._sync_material_doc_artifacts(task_id)
        with db_session(self.db_path) as conn:
            task = _row(conn, "SELECT * FROM supervision_tasks WHERE id = ?", (task_id,))
            if not task:
                raise TaskError(TASK_ERROR_CODES["NOT_FOUND"], "task not found")
            task["cases"] = _rows(
                conn,
                "SELECT * FROM task_cases WHERE task_id = ? ORDER BY created_at, display_name",
                (task_id,),
            )
            artifacts = _rows(
                conn,
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            )

        for artifact in artifacts:
            artifact["parent_ids"] = json.loads(artifact["parent_ids_json"] or "[]")
        live_doc_ids = self._live_document_ids(task_id)
        task["artifacts"] = artifacts
        task["directory"] = self._build_directory(artifacts, live_doc_ids=live_doc_ids)
        return task

    def confirm_plan(self, task_id: str, user_id: str | None = None) -> dict[str, Any]:
        """计划确认后才进入工作台，并建立第一份过程产物（材料接入与质量）。"""
        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            with db_session(self.db_path) as conn:
                _update(
                    conn,
                    "supervision_tasks",
                    task_id,
                    {"status": "PLAN_CONFIRMED", "updated_at": utc_now()},
                )
        scope = self.find_artifact(task_id, "TASK_SCOPE", "scope")
        batch = self.write_artifact(
            task_id=task_id,
            type="MATERIAL_BATCH",
            title="材料接入与质量",
            ref_key="batch",
            status="VALID",
            parent_ids=[scope["id"]] if scope else [],
            payload=self.material_overview(task_id, user_id=user_id),
        )
        return {"task": self.get_task(task_id), "batch_artifact_id": batch["id"]}

    def update_scope(
        self,
        task_id: str,
        *,
        title: str,
        purpose: str,
        authorized_until: str,
        cases: list[dict[str, Any]],
        note: str = "",
    ) -> dict[str, Any]:
        """计划确认前修改任务范围，并为 TASK_SCOPE 追加版本。"""
        task = self.get_task(task_id)
        if task["status"] != "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划确认后需通过 REPLAN 修改范围")
        purpose = (purpose or "").strip()
        named = [c for c in cases if (c.get("name") or c.get("case_id"))]
        if not purpose or not (authorized_until or "").strip() or len(named) < 2:
            raise TaskError(
                TASK_ERROR_CODES["INVALID_SCOPE"],
                "监督目的、授权有效期必填，且至少选择两起案件",
            )

        now = utc_now()
        with db_session(self.db_path) as conn:
            _update(
                conn,
                "supervision_tasks",
                task_id,
                {
                    "title": (title or "").strip() or task["title"],
                    "purpose": purpose,
                    "authorized_until": authorized_until,
                    "note": note or "",
                    "updated_at": now,
                },
            )
            conn.execute("DELETE FROM task_cases WHERE task_id = ?", (task_id,))
            for item in named:
                case_id = ensure_demo_case(conn, item.get("case_id") or new_id())
                _insert(
                    conn,
                    "task_cases",
                    {
                        "id": new_id(),
                        "task_id": task_id,
                        "case_id": case_id,
                        "display_name": (item.get("name") or "").strip()
                        or f"案件 {case_id[:8]}",
                        "auth_status": "AUTHORIZED",
                        "created_at": now,
                    },
                )

        self.write_artifact(
            task_id=task_id,
            type="TASK_SCOPE",
            title="案件范围与监督目的",
            ref_key="scope",
            status="VALID",
            payload=self._scope_payload(task_id),
        )
        return {"task": self.get_task(task_id), "plan": self.plan_preview(task_id)}

    def plan_preview(self, task_id: str) -> dict[str, Any]:
        """计划确认卡：展示范围与受控步骤，标明自动执行与人工确认点。"""
        task = self.get_task(task_id)
        return {
            "task_id": task_id,
            "title": task["title"],
            "purpose": task["purpose"],
            "authorized_until": task["authorized_until"],
            "cases": task["cases"],
            "steps": [
                {"key": "AUTH_CHECK", "label": "逐案授权校验", "mode": "auto"},
                {"key": "PARSE", "label": "材料解析与 OCR", "mode": "auto"},
                {"key": "QUALITY", "label": "识别质量检查", "mode": "review"},
                {"key": "EXTRACT", "label": "对象与行为抽取", "mode": "auto"},
                    {"key": "ENTITY_REVIEW", "label": "跨案对象判断", "mode": "review"},
                {"key": "CLUE", "label": "关联线索生成", "mode": "auto"},
                {"key": "SOURCE_VERIFY", "label": "回原文核验", "mode": "review"},
                {"key": "REPORT", "label": "线索报告", "mode": "review"},
            ],
        }

    # ----- 实体候选复核 -----

    @staticmethod
    def clue_fingerprint_set(payload: dict[str, Any] | None) -> set[str]:
        """兼容标量 fingerprint 与 fingerprints[]。"""
        payload = payload or {}
        fps: set[str] = set()
        for item in payload.get("fingerprints") or []:
            text = str(item or "").strip()
            if text:
                fps.add(text)
        single = str(payload.get("fingerprint") or "").strip()
        if single:
            fps.add(single)
        return fps

    @staticmethod
    def normalize_clue_fingerprints(payload: dict[str, Any]) -> dict[str, Any]:
        """写入侧统一：同时保留 fingerprint 与 fingerprints。"""
        out = dict(payload or {})
        fps = sorted(TaskService.clue_fingerprint_set(out))
        if fps:
            out["fingerprints"] = fps
            out["fingerprint"] = out.get("fingerprint") or fps[0]
        return out

    def clue_matches_candidate(
        self,
        clue_payload: dict[str, Any] | None,
        candidate: dict[str, Any] | None,
    ) -> bool:
        clue_payload = clue_payload or {}
        candidate = candidate or {}
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        linked = {
            str(x).strip()
            for x in (clue_payload.get("linked_candidate_ids") or [])
            if str(x).strip()
        }
        if candidate_id and candidate_id in linked:
            return True
        fp = str(candidate.get("fingerprint") or "").strip()
        if fp and fp in self.clue_fingerprint_set(clue_payload):
            return True

        # 历史线索常把候选写进正文却未填 linked/fingerprint；用正文弱匹配兜底
        blob = " ".join(
            [
                str(clue_payload.get("title") or ""),
                str(clue_payload.get("analysis") or ""),
                str(clue_payload.get("summary") or ""),
                " ".join(str(x) for x in (clue_payload.get("objects") or [])),
            ]
        )
        if not blob.strip():
            return False
        if candidate_id and (candidate_id in blob or candidate_id[:8] in blob):
            return True
        for surface in self._candidate_link_surfaces(candidate):
            if surface and surface in blob:
                return True
        return False

    @staticmethod
    def _candidate_link_surfaces(candidate: dict[str, Any]) -> list[str]:
        """用于线索正文弱匹配的实体表面（不含过短噪声）。"""
        seen: set[str] = set()
        out: list[str] = []

        def add(raw: Any) -> None:
            text = str(raw or "").strip()
            if not text:
                return
            # 去掉展示名两侧引号包裹，如 “某贸易有限公司”组织
            for match in re.findall(r"[“\"「]([^”\"」]{2,})[”\"」]", text):
                add(match)
            text = re.sub(r"[“”\"'「」]", "", text)
            text = re.sub(r"(组织|人物|商户|账户|手机|设备)$", "", text).strip()
            if len(text) < 4 or text in seen:
                return
            seen.add(text)
            out.append(text)

        add(candidate.get("display_name"))
        for alias in candidate.get("aliases") or []:
            if isinstance(alias, dict):
                add(alias.get("surface") or alias.get("value") or alias.get("name"))
            else:
                add(alias)
        for rec in candidate.get("records") or []:
            if isinstance(rec, dict):
                add(rec.get("value"))
        for row in candidate.get("field_compare") or []:
            if not isinstance(row, dict):
                continue
            for cell in row.get("per_case") or []:
                if isinstance(cell, dict):
                    add(cell.get("value"))
        return out

    def _iter_clue_artifact_payloads(
        self, task_id: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """轻量读取线索产物与 payload，供指标匹配（不做 hydrate/回链修复）。"""
        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with db_session(self.db_path) as conn:
            arts = _rows(
                conn,
                "SELECT * FROM artifacts WHERE task_id = ? AND type = ?",
                (task_id, "CLUE_ITEM"),
            )
            for art in arts:
                ver = _row(
                    conn,
                    "SELECT payload_json FROM artifact_versions "
                    "WHERE artifact_id = ? AND version = ?",
                    (art["id"], int(art["current_version"])),
                )
                payload = json.loads(ver["payload_json"]) if ver and ver.get("payload_json") else {}
                rows.append((dict(art), payload))
        return rows

    def list_clues_for_candidate(
        self,
        task_id: str,
        candidate: dict[str, Any],
        *,
        statuses: set[str] | None = None,
        clue_rows: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        """列出与候选链接的 CLUE_ITEM（供指标、升格、Agent 工具共用）。"""
        skip = {"INVALID", "STALE"}
        refs: list[dict[str, Any]] = []
        if clue_rows is None:
            clue_rows = self._iter_clue_artifact_payloads(task_id)
        for art, clue_payload in clue_rows:
            status = art.get("status") or "DRAFT"
            if status in skip:
                continue
            if statuses is not None and status not in statuses:
                continue
            if not self.clue_matches_candidate(clue_payload, candidate):
                continue
            refs.append(
                {
                    "artifact_id": art["id"],
                    "title": art.get("title") or clue_payload.get("title") or "",
                    "status": status,
                }
            )
        return refs

    def apply_candidate_clue_impact(
        self,
        task_id: str,
        candidate: dict[str, Any],
        *,
        clue_rows: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """就地回填 generated_clues 与 impact 计数。"""
        refs = self.list_clues_for_candidate(task_id, candidate, clue_rows=clue_rows)
        candidate["generated_clues"] = refs
        impact = dict(candidate.get("impact") or {})
        impact["clue_count"] = len(refs)
        impact["relation_count"] = sum(1 for item in refs if item.get("status") == "DRAFT")
        impact["case_count"] = impact.get("case_count") or len(candidate.get("cases") or [])
        impact["mention_count"] = impact.get("mention_count") or len(candidate.get("records") or [])
        candidate["impact"] = impact
        return candidate

    def enrich_entity_candidate_payload(
        self,
        task_id: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        candidates = list(payload.get("candidates") or [])
        clue_rows = self._iter_clue_artifact_payloads(task_id)
        for cand in candidates:
            if isinstance(cand, dict):
                self.apply_candidate_clue_impact(task_id, cand, clue_rows=clue_rows)
        payload["candidates"] = candidates
        return payload

    def persist_candidate_field_table(
        self,
        task_id: str,
        candidate_id: str,
        *,
        field_compare: list[dict[str, Any]],
        evidence: list[dict[str, Any]] | None = None,
        supporting_facts: list[str] | None = None,
        conflicts: list[str] | None = None,
        missing_fields: list[str] | None = None,
        field_table_meta: dict[str, Any] | None = None,
        field_compare_columns: list[Any] | None = None,
        clear_field_compare_columns: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """只更新字段对照与证据，不改写模型复核建议文案。"""
        current = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        if not current:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "跨案对象待核清单不存在")
        detail = self.get_artifact(task_id, current["id"])
        payload = detail["payload"]
        found = None
        for candidate in payload.get("candidates", []):
            if candidate.get("candidate_id") == candidate_id:
                found = candidate
                break
        if not found:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "待核对象不存在")

        found["field_compare"] = field_compare or []
        if evidence is not None:
            found["evidence"] = evidence
        if supporting_facts is not None:
            found["supporting_facts"] = supporting_facts
        if conflicts is not None:
            found["conflicts"] = conflicts
        if missing_fields is not None:
            found["missing_fields"] = missing_fields
        if field_table_meta is not None:
            found["field_table_meta"] = field_table_meta
        if field_compare_columns is not None:
            found["field_compare_columns"] = field_compare_columns
        elif clear_field_compare_columns:
            found.pop("field_compare_columns", None)
        self.apply_candidate_clue_impact(task_id, found)

        artifact = self.write_artifact(
            task_id=task_id,
            type="ENTITY_CANDIDATE_SET",
            title=current.get("title") or "跨案对象待核·待判断",
            ref_key="entity-candidates",
            status=current.get("status") or "PENDING_REVIEW",
            parent_ids=json.loads(current["parent_ids_json"] or "[]"),
            payload=payload,
            input_snapshot={"action": "field_table", "candidate_id": candidate_id, "user_id": user_id},
        )
        return {"artifact_id": artifact["id"], "candidate_id": candidate_id, "task": self.get_task(task_id)}

    def save_entity_candidates(
        self,
        task_id: str,
        *,
        candidates: list[dict[str, Any]],
        summary: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """保存抽取/归一步骤生成的候选集，候选只供人工复核，不自动认定同一。"""
        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        normalized = []
        for index, item in enumerate(candidates):
            records = item.get("records") or []
            cases = item.get("cases") or []
            allow_single = bool(item.get("_allow_single_case") or item.get("match_tier") == "SUSPECTED")
            # 强碰撞仍要求两案；疑似化名允许单案（多写法）
            if len(records) < 2 and len(cases) < 2 and not allow_single:
                continue
            if len(cases) < 2 and records:
                seen = {}
                for rec in records:
                    cid = rec.get("case_id")
                    if cid and cid not in seen:
                        seen[cid] = {"case_id": cid, "case_name": rec.get("case_name") or cid}
                cases = list(seen.values())
            if len(cases) < 2 and not allow_single:
                continue
            if len(cases) < 1 and allow_single:
                continue
            normalized.append(
                {
                    "candidate_id": item.get("candidate_id") or new_id(),
                    "fingerprint": item.get("fingerprint") or "",
                    "entity_type": item.get("entity_type") or "OTHER",
                    "display_name": item.get("display_name") or f"候选 {index + 1}",
                    "recall_method": item.get("recall_method") or "",
                    "recalled_at": item.get("recalled_at") or "",
                    "confidence_label": item.get("confidence_label") or "待核验",
                    "match_basis": item.get("match_basis") or [],
                    "match_tier": item.get("match_tier") or "STRONG",
                    "aliases": item.get("aliases") or [],
                    "differences": item.get("differences") or [],
                    "records": records,
                    "cases": cases or item.get("cases") or [],
                    "field_compare": item.get("field_compare") or [],
                    "evidence": item.get("evidence") or [],
                    "supporting_facts": item.get("supporting_facts") or [],
                    "conflicts": item.get("conflicts") or [],
                    "missing_fields": item.get("missing_fields") or [],
                    "impact": item.get("impact") or {},
                    "agent_summary": item.get("agent_summary") or "",
                    "recommendation": item.get("recommendation") or "DEFER",
                    "generated_clues": item.get("generated_clues") or [],
                    "question": item.get("question") or "",
                    "decision": "PENDING",
                    "reason": "",
                    "correction": None,
                }
            )

        batch = self.find_artifact(task_id, "MATERIAL_BATCH", "batch")
        artifact = self.write_artifact(
            task_id=task_id,
            type="ENTITY_CANDIDATE_SET",
            title="跨案对象待核·待判断",
            ref_key="entity-candidates",
            status="PENDING_REVIEW",
            parent_ids=[batch["id"]] if batch else [],
            payload={
                "summary": {
                    "total": len(normalized),
                    "pending": len(normalized),
                    "reviewed": 0,
                    **(summary or {}),
                },
                "candidates": normalized,
                "subject_resolve": {"subjects": {}, "surface_index": {}, "keep_separate": []},
                "boundary": "标识重合仅为待核验提示，不代表系统已认定同一人、同一账户或共同犯罪。",
            },
            run_id=run_id,
        )
        return {"artifact": artifact, "task": self.get_task(task_id)}

    def review_entity_candidate(
        self,
        task_id: str,
        candidate_id: str,
        *,
        decision: str,
        reason: str,
        correction: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """记录合并/分离/修正/暂缓决定，并为候选集追加版本。"""
        allowed = {"MERGE", "KEEP_SEPARATE", "CORRECT", "DEFER"}
        if decision not in allowed:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "不支持的对象判断决定")
        if not (reason or "").strip():
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "对象判断必须填写理由")

        current = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        if not current:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "跨案对象待核清单不存在")
        if current["status"] in {"STALE", "INVALID"}:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "待核清单已过期或失效，请先更新")
        if expected_version is not None and int(current["current_version"]) != int(expected_version):
            raise TaskError(
                TASK_ERROR_CODES["STATE_CONFLICT"],
                "待核清单版本已变化，请刷新后重试",
                {"expected_version": expected_version, "current_version": current["current_version"]},
            )

        detail = self.get_artifact(task_id, current["id"])
        payload = detail["payload"]
        found = None
        previous = "PENDING"
        for candidate in payload.get("candidates", []):
            if candidate.get("candidate_id") == candidate_id:
                previous = str(candidate.get("decision") or "PENDING")
                candidate["decision"] = decision
                candidate["reason"] = reason.strip()
                candidate["correction"] = correction
                candidate["reviewed_at"] = utc_now()
                found = candidate
                break
        if not found:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "待核对象不存在")

        fingerprint = str(found.get("fingerprint") or "").strip()
        if decision == "KEEP_SEPARATE" and fingerprint:
            from tools.entities import remember_rejection

            remember_rejection(
                task_id,
                fingerprint,
                decision,
                reason.strip(),
                db_path=self.db_path,
            )
        elif previous == "KEEP_SEPARATE" and decision != "KEEP_SEPARATE" and fingerprint:
            from tools.entities import forget_rejection

            forget_rejection(task_id, fingerprint, db_path=self.db_path)

        if decision in {"MERGE", "KEEP_SEPARATE"}:
            payload["subject_resolve"] = self._update_subject_resolve(
                payload.get("subject_resolve"),
                found,
                decision=decision,
            )

        candidates = payload.get("candidates", [])
        reviewed = sum(1 for item in candidates if item.get("decision") != "PENDING")
        pending = len(candidates) - reviewed
        analysis_gate = "ENTITY_REVIEW" if pending else ""
        payload["summary"] = {
            **(payload.get("summary") or {}),
            "total": len(candidates),
            "reviewed": reviewed,
            "pending": pending,
            "analysis_gate": analysis_gate,
        }
        payload["analysis_gate"] = analysis_gate
        status = "VALID" if pending == 0 else "PENDING_REVIEW"
        artifact = self.write_artifact(
            task_id=task_id,
            type="ENTITY_CANDIDATE_SET",
            title="跨案对象待核·已完成" if pending == 0 else "跨案对象待核·待判断",
            ref_key="entity-candidates",
            status=status,
            parent_ids=json.loads(current["parent_ids_json"] or "[]"),
            payload=payload,
        )
        self.append_source_verify(
            task_id,
            {
                "action": "实体决策",
                "type": "entity_decision",
                "target": candidate_id,
                "summary": f"{found.get('title') or found.get('display_name') or candidate_id} · {decision}",
                "result": "ok",
                "at": utc_now(),
            },
        )
        followup = self.continue_after_entity_decision(
            task_id, candidate_id, decision=decision, user_id=None
        )
        actions = list(followup.get("actions") or [])
        if pending == 0:
            actions.append("实体复核已全部确认，可继续整理线索与报告")
        elif analysis_gate == "ENTITY_REVIEW":
            actions.append(f"仍有 {pending} 条待核实体，确认后方可继续后续分析")
        return {
            "artifact": artifact,
            "task": self.get_task(task_id),
            "followup_actions": actions,
            "analysis_gate": analysis_gate,
            "pending": pending,
        }

    def propose_entity_review(
        self,
        task_id: str,
        candidate_id: str,
        *,
        suggestion: dict[str, Any],
        user_id: str | None = None,
        advise_only: bool = False,
    ) -> dict[str, Any]:
        """写入 AI 复核建议；不改变人工 decision。

        advise_only=True：只落库 recommendation / agent_summary 等建议字段，
        不覆盖 field_compare / evidence（模型分析建议，非代操作改表）。
        """
        current = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        if not current:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "跨案对象待核清单不存在")
        detail = self.get_artifact(task_id, current["id"])
        payload = detail["payload"]
        found = None
        for candidate in payload.get("candidates", []):
            if candidate.get("candidate_id") == candidate_id:
                found = candidate
                break
        if not found:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "待核对象不存在")

        if not advise_only:
            evidence = suggestion.get("evidence") or suggestion.get("evidence_refs")
            if evidence is None:
                evidence = found.get("evidence") or []
            # 仅当建议携带新证据时强制回链校验；复用已有证据时跳过（避免重复 I/O）
            existing_hashes = {
                (ev.get("chunk_id"), ev.get("quote_hash"))
                for ev in (found.get("evidence") or [])
                if ev.get("chunk_id") and ev.get("quote_hash")
            }
            for ev in evidence:
                if not ev.get("chunk_id") or not ev.get("quote_hash") or not ev.get("quote"):
                    raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], "建议证据缺少引用字段")
                version_id = ev.get("document_version_id")
                if not version_id:
                    raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], "建议证据缺少 document_version_id")
                key = (ev.get("chunk_id"), ev.get("quote_hash"))
                if key in existing_hashes and suggestion.get("evidence") is None and suggestion.get("evidence_refs") is None:
                    continue
                try:
                    get_material_service().read_redacted_chunk(
                        version_id,
                        chunk_id=ev["chunk_id"],
                        user_id=user_id or "system",
                        quote=ev.get("quote"),
                        quote_hash=ev.get("quote_hash"),
                    )
                except MaterialError as exc:
                    raise TaskError(
                        TASK_ERROR_CODES["INVALID_SCOPE"],
                        f"建议证据校验失败：{exc.message}",
                    ) from exc
            if suggestion.get("field_compare"):
                found["field_compare"] = suggestion["field_compare"]
            if suggestion.get("evidence") is not None or suggestion.get("evidence_refs") is not None:
                found["evidence"] = evidence

        summary = (suggestion.get("agent_summary") or suggestion.get("rationale_text") or "").strip()
        if len(summary) > 150:
            summary = summary[:150]
        recommendation = suggestion.get("recommendation") or suggestion.get("suggested_decision") or "DEFER"
        found["agent_summary"] = summary
        found["recommendation"] = recommendation
        if suggestion.get("supporting_facts") is not None or suggestion.get("match_points") is not None:
            found["supporting_facts"] = (
                suggestion.get("supporting_facts")
                or suggestion.get("match_points")
                or []
            )
        if suggestion.get("conflicts") is not None or suggestion.get("difference_points") is not None:
            found["conflicts"] = (
                suggestion.get("conflicts")
                or suggestion.get("difference_points")
                or []
            )
        if suggestion.get("missing_fields") is not None:
            found["missing_fields"] = suggestion.get("missing_fields") or []
        found["ai_suggestion"] = {
            "recommendation": recommendation,
            "agent_summary": summary,
            "confidence": suggestion.get("confidence") or "MEDIUM",
            "proposed_at": utc_now(),
            "producer": suggestion.get("producer") or "DEEPSEEK_ENTITY_REVIEW",
            "source": suggestion.get("source") or "model",
            "fallback": bool(suggestion.get("fallback")),
            "advise_only": bool(advise_only),
        }

        self.apply_candidate_clue_impact(task_id, found)

        artifact = self.write_artifact(
            task_id=task_id,
            type="ENTITY_CANDIDATE_SET",
            title=current.get("title") or "跨案对象待核·待判断",
            ref_key="entity-candidates",
            status=current.get("status") or "PENDING_REVIEW",
            parent_ids=json.loads(current["parent_ids_json"] or "[]"),
            payload=payload,
        )
        self.append_source_verify(
            task_id,
            {
                "action": "AI复核建议",
                "type": "entity_ai_suggestion",
                "target": candidate_id,
                "summary": f"{found.get('display_name') or candidate_id} · {recommendation}",
                "result": "ok",
                "at": utc_now(),
            },
        )
        return {
            "artifact_id": artifact["id"],
            "candidate_id": candidate_id,
            "recommendation": recommendation,
            "agent_summary": summary,
            "task": self.get_task(task_id),
        }

    def continue_after_entity_decision(
        self,
        task_id: str,
        candidate_id: str,
        *,
        decision: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """人工决定后的后续动作：升格线索草稿 / 记录排除 / 标记待补材料。"""
        actions: list[str] = []
        current = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        if not current:
            return {"actions": [], "message": "无候选集"}
        detail = self.get_artifact(task_id, current["id"])
        found = None
        for candidate in (detail.get("payload") or {}).get("candidates") or []:
            if candidate.get("candidate_id") == candidate_id:
                found = candidate
                break
        if not found:
            return {"actions": [], "message": "候选不存在"}

        if decision != "MERGE":
            self._demote_clues_for_candidate(task_id, found, actions)
        if decision == "MERGE":
            # 将关联草稿线索升格为 VALID
            for art in self.get_task(task_id).get("artifacts") or []:
                if art.get("type") != "CLUE_ITEM" or art.get("status") not in {"DRAFT", "VALID"}:
                    continue
                clue_detail = self.get_artifact(task_id, art["id"])
                payload = dict(clue_detail.get("payload") or {})
                if not self.clue_matches_candidate(payload, found):
                    continue
                payload["promotion"] = "confirmed"
                linked = list(payload.get("linked_candidate_ids") or [])
                payload["linked_candidate_ids"] = list({*linked, candidate_id})
                payload = self.normalize_clue_fingerprints(payload)
                fp = str(found.get("fingerprint") or "").strip()
                if fp:
                    fps = list(payload.get("fingerprints") or [])
                    if fp not in fps:
                        fps.append(fp)
                    payload["fingerprints"] = fps
                    payload["fingerprint"] = payload.get("fingerprint") or fp
                self.write_artifact(
                    task_id=task_id,
                    type="CLUE_ITEM",
                    title=payload.get("title") or art["title"],
                    ref_key=art.get("ref_key"),
                    status="VALID",
                    parent_ids=json.loads(art["parent_ids_json"] or "[]"),
                    payload=payload,
                )
                actions.append(f"升格线索：{art.get('title')}")
            refreshed = self.refresh_timeline_subjects(task_id)
            if refreshed.get("ok"):
                actions.append("角色时间线主体已按确认关联归一")
            actions.append("确认关联已写入留痕")
        elif decision == "KEEP_SEPARATE":
            refreshed = self.refresh_timeline_subjects(task_id)
            if refreshed.get("ok"):
                actions.append("角色时间线已应用保留独立")
            actions.append("已记录排除，后续碰撞将跳过该指纹")
        elif decision == "CORRECT":
            actions.append("已记录更正，建议重新执行标识比对")
        elif decision == "DEFER":
            missing = found.get("missing_fields") or []
            actions.append("暂缓判断：" + ("；".join(missing) if missing else "待补材料"))

        self.append_source_verify(
            task_id,
            {
                "action": "实体决定后续",
                "type": "entity_decision_followup",
                "target": candidate_id,
                "summary": "；".join(actions) or decision,
                "result": "ok",
                "at": utc_now(),
            },
        )
        return {"actions": actions, "task": self.get_task(task_id)}

    def _demote_clues_for_candidate(
        self,
        task_id: str,
        candidate: dict[str, Any],
        actions: list[str],
    ) -> None:
        """改判为保留独立等后，撤回仅因该实体确认同一而自动升格的线索。"""
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        fingerprint = str(candidate.get("fingerprint") or "").strip()
        if not candidate_id and not fingerprint:
            return
        for art in self.get_task(task_id).get("artifacts") or []:
            if art.get("type") != "CLUE_ITEM" or art.get("status") in {"STALE", "INVALID"}:
                continue
            clue_detail = self.get_artifact(task_id, art["id"])
            payload = dict(clue_detail.get("payload") or {})
            linked = [
                str(x).strip()
                for x in (payload.get("linked_candidate_ids") or [])
                if str(x).strip()
            ]
            fps = [str(x).strip() for x in (payload.get("fingerprints") or []) if str(x).strip()]
            linked_hit = bool(candidate_id and candidate_id in linked)
            fp_hit = bool(fingerprint and fingerprint in fps)
            if not linked_hit and not fp_hit:
                continue
            if linked_hit:
                linked = [item for item in linked if item != candidate_id]
                payload["linked_candidate_ids"] = linked
            if payload.get("disposition"):
                self.write_artifact(
                    task_id=task_id,
                    type="CLUE_ITEM",
                    title=payload.get("title") or art["title"],
                    ref_key=art.get("ref_key"),
                    status=art.get("status") or "VALID",
                    parent_ids=json.loads(art["parent_ids_json"] or "[]"),
                    payload=payload,
                )
                continue
            if payload.get("promotion") != "confirmed":
                if linked_hit:
                    self.write_artifact(
                        task_id=task_id,
                        type="CLUE_ITEM",
                        title=payload.get("title") or art["title"],
                        ref_key=art.get("ref_key"),
                        status=art.get("status") or "DRAFT",
                        parent_ids=json.loads(art["parent_ids_json"] or "[]"),
                        payload=payload,
                    )
                continue
            if linked:
                payload["promotion"] = "confirmed"
                status = "VALID"
            else:
                payload.pop("promotion", None)
                status = "DRAFT"
            self.write_artifact(
                task_id=task_id,
                type="CLUE_ITEM",
                title=payload.get("title") or art["title"],
                ref_key=art.get("ref_key"),
                status=status,
                parent_ids=json.loads(art["parent_ids_json"] or "[]"),
                payload=payload,
            )
            if status == "DRAFT":
                actions.append(f"已撤回自动确认：{art.get('title')}")

    def _candidate_surfaces(self, candidate: dict[str, Any]) -> list[str]:
        import re

        surfaces: list[str] = []

        def push(text: str | None) -> None:
            raw = (text or "").strip()
            if not raw:
                return
            # 「“赵瑞”人物」→ 赵瑞
            m = re.match(r"^[“\"](.+?)[”\"](?:人物|组织)?$", raw)
            if m:
                raw = m.group(1).strip()
            if len(raw) > 40:
                return
            surfaces.append(raw)

        push(candidate.get("display_name"))
        for alias in candidate.get("aliases") or []:
            push(alias if isinstance(alias, str) else str(alias or ""))
        for rec in candidate.get("records") or []:
            for key in ("value", "surface", "display_name", "surface_raw"):
                push(rec.get(key))
        for case in candidate.get("cases") or []:
            for key in ("value", "display_name"):
                push(case.get(key))
        for ev in candidate.get("evidence") or []:
            push(ev.get("value") or ev.get("surface"))

        seen = set()
        out = []
        for s in surfaces:
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    def _update_subject_resolve(
        self,
        resolve: dict[str, Any] | None,
        candidate: dict[str, Any],
        *,
        decision: str,
    ) -> dict[str, Any]:
        from tools.entities import empty_subject_resolve

        resolve = dict(resolve or empty_subject_resolve())
        subjects = dict(resolve.get("subjects") or {})
        surface_index = dict(resolve.get("surface_index") or {})
        keep_separate = list(resolve.get("keep_separate") or [])
        surfaces = self._candidate_surfaces(candidate)
        object_type = (
            candidate.get("entity_type")
            or candidate.get("object_type")
            or "NAME"
        )
        if object_type in {"PERSON", "NAME"}:
            object_type = "NAME"
        display_name = (candidate.get("display_name") or (surfaces[0] if surfaces else "")).strip()
        subject_id = candidate.get("candidate_id") or new_id()
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        surface_set = set(surfaces)

        if candidate_id:
            subjects = {
                key: value
                for key, value in subjects.items()
                if str((value or {}).get("candidate_id") or "") != candidate_id
            }
        if surface_set:
            surface_index = {
                surface: sid
                for surface, sid in surface_index.items()
                if surface not in surface_set
            }
            keep_separate = [
                group
                for group in keep_separate
                if not (isinstance(group, list) and any(item in surface_set for item in group))
            ]

        if decision == "MERGE" and surfaces:
            subjects[subject_id] = {
                "subject_id": subject_id,
                "display_name": display_name or surfaces[0],
                "object_type": object_type,
                "surfaces": surfaces,
                "candidate_id": candidate.get("candidate_id"),
                "fingerprint": candidate.get("fingerprint") or "",
                "source": "MERGE",
            }
            for surface in surfaces:
                surface_index[surface] = subject_id
            # 从 keep_separate 中移除已合并表面
            keep_separate = [
                group
                for group in keep_separate
                if not any(s in surfaces for s in (group if isinstance(group, list) else []))
            ]
        elif decision == "KEEP_SEPARATE" and surfaces:
            keep_separate.append(surfaces)
            # 各表面各自独立 subject，避免后续误并展示
            for surface in surfaces:
                sid = f"sep:{candidate.get('candidate_id') or new_id()}:{surface}"
                subjects[sid] = {
                    "subject_id": sid,
                    "display_name": surface,
                    "object_type": object_type,
                    "surfaces": [surface],
                    "candidate_id": candidate.get("candidate_id"),
                    "fingerprint": candidate.get("fingerprint") or "",
                    "source": "KEEP_SEPARATE",
                }
                surface_index[surface] = sid

        resolve["subjects"] = subjects
        resolve["surface_index"] = surface_index
        resolve["keep_separate"] = keep_separate
        return resolve

    def get_subject_resolve(self, task_id: str) -> dict[str, Any]:
        from tools.entities import empty_subject_resolve

        current = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        if not current:
            return empty_subject_resolve()
        detail = self.get_artifact(task_id, current["id"])
        return (detail.get("payload") or {}).get("subject_resolve") or empty_subject_resolve()

    def refresh_timeline_subjects(self, task_id: str) -> dict[str, Any]:
        """用 subject_resolve 重写已有角色时间线的 parties / 主体，不重抽事件。"""
        from tools.entities import apply_subject_resolve

        art = self.find_artifact(task_id, "ROLE_TIMELINE", "role-timeline")
        if not art or art.get("status") in {"STALE", "INVALID"}:
            return {"ok": False, "message": "无有效时间线"}
        detail = self.get_artifact(task_id, art["id"])
        payload = dict(detail.get("payload") or {})
        resolve = self.get_subject_resolve(task_id)
        items = []
        for item in payload.get("items") or []:
            row = dict(item)
            parties = apply_subject_resolve(row.get("parties") or [], resolve)
            row["parties"] = parties
            primary = parties[0] if parties else None
            if primary:
                row["subject_id"] = primary.get("subject_id") or ""
                row["subject"] = primary.get("display_name") or primary.get("surface") or ""
            items.append(row)
        payload["items"] = items
        payload["subject_resolve_applied"] = True
        self.write_artifact(
            task_id=task_id,
            type="ROLE_TIMELINE",
            title=art.get("title") or "角色时间线·转账与联络事件",
            ref_key="role-timeline",
            status="VALID",
            parent_ids=json.loads(art.get("parent_ids_json") or "[]"),
            payload=payload,
        )
        return {"ok": True, "item_count": len(items)}

    def dispose_clue_item(
        self,
        task_id: str,
        artifact_id: str,
        disposition: str,
        reason: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """线索卡片内联处置：继续核查 / 需补材料 / 排除 / 暂缓。"""
        allowed = {"CONTINUE", "NEED_MATERIAL", "EXCLUDE", "DEFER"}
        if disposition not in allowed:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "不支持的线索处置决定")
        if not (reason or "").strip():
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "线索处置必须填写理由")

        detail = self.get_artifact(task_id, artifact_id)
        art = detail["artifact"]
        if art["type"] != "CLUE_ITEM":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "仅支持对单条线索处置")
        if art["status"] in {"STALE", "INVALID"}:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "该线索已过期或失效")
        if expected_version is not None and int(art["current_version"]) != int(expected_version):
            raise TaskError(
                TASK_ERROR_CODES["STATE_CONFLICT"],
                "线索版本已变化，请刷新后重试",
                {"expected_version": expected_version, "current_version": art["current_version"]},
            )

        payload = dict(detail.get("payload") or {})
        payload["disposition"] = disposition
        payload["disposition_reason"] = reason.strip()
        payload["disposed_at"] = utc_now()
        artifact = self.write_artifact(
            task_id=task_id,
            type="CLUE_ITEM",
            title=payload.get("title") or art["title"],
            ref_key=art.get("ref_key"),
            status="VALID",
            parent_ids=json.loads(art["parent_ids_json"] or "[]"),
            payload=payload,
        )
        self.append_source_verify(
            task_id,
            {
                "action": "线索处置",
                "type": "clue_disposition",
                "target": artifact_id,
                "summary": f"{payload.get('title') or art['title']} · {disposition}",
                "result": "ok",
                "at": utc_now(),
            },
        )
        return {"artifact": artifact, "task": self.get_task(task_id)}

    def append_source_verify(self, task_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """追加核验留痕事件到 SOURCE_VERIFY 产物。"""
        existing = self.find_artifact(task_id, "SOURCE_VERIFY", "source-verify-log")
        events: list[dict[str, Any]] = []
        parent_ids: list[str] = []
        if existing:
            detail = self.get_artifact(task_id, existing["id"])
            events = list((detail.get("payload") or {}).get("events") or [])
            parent_ids = json.loads(existing["parent_ids_json"] or "[]")
        events.append(event)
        return self.write_artifact(
            task_id=task_id,
            type="SOURCE_VERIFY",
            title="核验留痕",
            ref_key="source-verify-log",
            status="VALID",
            parent_ids=parent_ids,
            payload={
                "events": events[-200:],
                "boundary": "留痕仅记录人工核验、回链、报告撰写与导出操作，不构成法律结论。",
            },
        )

    # ----- 报告：模型写正文 → 每写一版新产物，不覆盖旧版 -----

    @staticmethod
    def _report_aspect_label(aspect: Any) -> str:
        return {
            "ID": "标识",
            "FUND": "资金",
            "TIME": "时空",
            "ROLE": "角色",
            "QUAL": "材料质量",
            "PLAT": "平台",
        }.get(str(aspect or "").upper(), "")

    @staticmethod
    def _report_entity_pending(entity_payload: dict[str, Any]) -> int:
        """待核实体数：优先产物 summary.pending，缺失时按 decision 兜底。"""
        summary = entity_payload.get("summary") or {}
        pending = summary.get("pending")
        if isinstance(pending, int) and pending >= 0:
            return pending
        return sum(
            1
            for c in (entity_payload.get("candidates") or [])
            if (c.get("decision") or "PENDING") == "PENDING"
        )

    def _report_state_blocked(self, state: dict[str, Any]) -> str | None:
        """严格门禁：实体复核未完成或尚无存活线索时，返回阻止报告写作的原因。"""
        if state["entity_payload"] and self._report_entity_pending(state["entity_payload"]) > 0:
            return (
                "跨案对象仍有待人工确认。请先在中间工作区完成「视为同一 / 保留独立」"
                "确认，确认完成后再撰写报告。"
            )
        if not state["clues"]:
            return "当前尚无待核线索。请先在右侧对话中整理出疑似关联线索、写入线索中心，再撰写报告。"
        return None

    def _report_state(self, task_id: str) -> dict[str, Any]:
        """只读快照：可入报告的范围/实体/存活线索（原始 payload，不触发回链修复写库）。"""
        task = self.get_task(task_id)
        scope = self.find_artifact(task_id, "TASK_SCOPE", "scope")
        scope_payload = (
            self._read_artifact_payload_raw(task_id, scope["id"])[1] or {}
            if scope
            else {}
        )
        entity = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        entity_payload = {}
        if entity and entity.get("status") not in {"STALE", "INVALID"}:
            entity_payload = self._read_artifact_payload_raw(task_id, entity["id"])[1] or {}
        return {
            "task": task,
            "scope_id": scope["id"] if scope else None,
            "scope_payload": scope_payload,
            "entity_id": entity["id"] if entity_payload else None,
            "entity_payload": entity_payload,
            "clues": self._active_clue_items(task_id),  # [(artifact, payload)]
        }

    _REPORT_ENTITY_TYPE_LABELS = {
        "PERSON": "人员",
        "NAME": "人员",
        "BANK_ACCOUNT": "银行账户",
        "ACCOUNT": "银行账户",
        "PHONE": "手机号码",
        "DEVICE": "电子设备",
        "ORGANIZATION": "组织主体",
        "ORG": "组织主体",
        "MERCHANT": "商户",
        "ID_CARD": "身份证件",
        "IP": "网络地址",
    }

    @classmethod
    def _report_entity_type_label(cls, entity_type: Any) -> str:
        key = str(entity_type or "").upper()
        return cls._REPORT_ENTITY_TYPE_LABELS.get(key, "对象")

    @staticmethod
    def _report_clip_quote(quote: Any, max_len: int = 40) -> str:
        text = re.sub(r"\s+", " ", str(quote or "").strip())
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    @staticmethod
    def _report_clip_named(items: list[str], limit: int = 30) -> list[str]:
        if len(items) <= limit:
            return items
        rest = len(items) - limit
        return items[:limit] + [f"其余 {rest} 条见实体复核页，此处不逐条列出。"]

    def _report_entity_name_line(self, candidate: dict[str, Any]) -> str:
        type_label = self._report_entity_type_label(
            candidate.get("entity_type") or candidate.get("object_type")
        )
        name = str(
            candidate.get("display_name") or candidate.get("title") or ""
        ).strip() or "未标注对象"
        case_names: list[str] = []
        for row in list(candidate.get("cases") or []) + list(candidate.get("records") or []):
            if not isinstance(row, dict):
                continue
            label = str(
                row.get("display_name") or row.get("name") or row.get("case_name") or ""
            ).strip()
            if label and label not in case_names:
                case_names.append(label)
        case_part = f"（{'、'.join(case_names)}）" if case_names else ""
        return f"{type_label} {name}{case_part}"

    def _report_entity_stats(self, entity_payload: dict[str, Any]) -> dict[str, Any]:
        candidates = entity_payload.get("candidates") or []
        counts: dict[str, int] = {}
        merge_items: list[str] = []
        keep_items: list[str] = []
        other_items: list[str] = []
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            key = str(cand.get("decision") or "PENDING").upper()
            counts[key] = counts.get(key, 0) + 1
            line = self._report_entity_name_line(cand)
            if key == "MERGE":
                merge_items.append(line)
            elif key == "KEEP_SEPARATE":
                keep_items.append(line)
            elif key not in {"PENDING", "DEFER"}:
                other_items.append(f"{line} · {key}")
        return {
            "total": len(candidates),
            "merge": counts.get("MERGE", 0),
            "keep_separate": counts.get("KEEP_SEPARATE", 0),
            "correct": counts.get("CORRECT", 0),
            "defer": counts.get("DEFER", 0),
            "pending": self._report_entity_pending(entity_payload),
            "merge_items": self._report_clip_named(merge_items),
            "keep_separate_items": self._report_clip_named(keep_items),
            "other_items": self._report_clip_named(other_items),
        }

    def _report_source_cite(self, ev: dict[str, Any], source_no: int) -> dict[str, Any]:
        """办案可读的来源编号：案名 · 文件 · 页 · 短摘录，不含内部 ID。"""
        case_name = str(ev.get("case_name") or "").strip()
        filename = Path(str(ev.get("filename") or ev.get("document_name") or "")).name
        page_start = ev.get("page_start") or ev.get("page_no")
        page_end = ev.get("page_end")
        page_text = "页码待核"
        try:
            start_n = int(page_start) if page_start not in (None, "") else 0
            end_n = int(page_end) if page_end not in (None, "") else start_n
            if start_n:
                page_text = f"第{start_n}–{end_n}页" if end_n and end_n != start_n else f"第{start_n}页"
        except (TypeError, ValueError):
            pass
        quote = self._report_clip_quote(ev.get("quote_display") or ev.get("quote"))
        loc = " · ".join(part for part in (case_name, filename, page_text) if part) or "出处待核"
        quote_part = f" · 「{quote}」" if quote else " · 原文摘录待核"
        return {
            "no": source_no,
            "line": f"来源{source_no}：{loc}{quote_part}",
            "case_name": case_name,
            "filename": filename,
            "page": page_text,
            "quote": quote,
        }

    def _report_pack_clues(
        self, alive: list[tuple[dict[str, Any], dict[str, Any]]]
    ) -> tuple[list[dict[str, Any]], int]:
        """线索清单 + 连续来源编号；invalid_refs 为缺少支持材料的条数。"""
        packs: list[dict[str, Any]] = []
        source_no = 0
        invalid_refs = 0
        for idx, (_art, payload) in enumerate(alive, start=1):
            evidence = [ev for ev in (payload.get("evidence") or []) if isinstance(ev, dict)]
            counter = [
                ev for ev in (payload.get("counter_evidence") or []) if isinstance(ev, dict)
            ]
            if not evidence:
                invalid_refs += 1
            sources: list[dict[str, Any]] = []
            for ev in evidence[:6]:
                source_no += 1
                sources.append(self._report_source_cite(ev, source_no))
            counter_sources: list[dict[str, Any]] = []
            for ev in counter[:4]:
                source_no += 1
                cite = self._report_source_cite(ev, source_no)
                cite["stance"] = "counter"
                counter_sources.append(cite)
            case_names = {
                str(ev.get("case_name") or "").strip()
                for ev in evidence
                if str(ev.get("case_name") or "").strip()
            }
            packs.append(
                {
                    "idx": idx,
                    "title": payload.get("title") or "未命名待核事项",
                    "aspect": self._report_aspect_label(payload.get("aspect")) or "未标注",
                    "disposition": clue_state_label(payload),
                    "evidence_count": len(evidence),
                    "counter_count": len(counter),
                    "case_count": len(case_names),
                    "uncertainty": str(
                        payload.get("uncertainty") or payload.get("boundary") or ""
                    ).strip()[:40],
                    "sources": sources,
                    "counter_sources": counter_sources,
                }
            )
        return packs, invalid_refs

    @staticmethod
    def _report_entity_checklist_lines(entity: dict[str, Any]) -> list[str]:
        lines = [
            "### 实体复核结论",
            (
                f"- 计数：候选 {entity['total']} · 视为同一 {entity['merge']} · "
                f"保留独立 {entity['keep_separate']} · 待核 {entity['pending']}"
            ),
        ]
        if entity.get("merge_items"):
            lines.append("- 视为同一：")
            lines.extend(f"  - {item}" for item in entity["merge_items"])
        else:
            lines.append("- 视为同一：无")
        if entity.get("keep_separate_items"):
            lines.append("- 保留独立：")
            lines.extend(f"  - {item}" for item in entity["keep_separate_items"])
        else:
            lines.append("- 保留独立：无")
        if entity.get("other_items"):
            lines.append("- 其他处置：")
            lines.extend(f"  - {item}" for item in entity["other_items"])
        return lines

    @staticmethod
    def _report_clue_checklist_lines(packs: list[dict[str, Any]]) -> list[str]:
        lines = ["### 待核验线索"]
        if not packs:
            lines.append("- 暂无待核线索。")
            return lines
        for pack in packs:
            lines.append(
                f"- {pack['idx']}) {pack['title']} "
                f"（{pack['aspect']} · 处置 {pack['disposition']} · "
                f"支持材料 {pack['evidence_count']} 处）"
            )
            for src in pack.get("sources") or []:
                lines.append(f"  - {src['line']}")
            for src in pack.get("counter_sources") or []:
                lines.append(f"  - 反向 {src['line']}")
            if pack.get("uncertainty"):
                lines.append(f"  - 不确定性：{pack['uncertainty']}")
            if not (pack.get("sources") or pack.get("counter_sources")):
                lines.append("  - 来源：缺原文依据，导出前需补证或排除")
        return lines

    def report_write_context(self, task_id: str) -> dict[str, Any]:
        """报告读工具：把任务范围、实体复核结论、存活线索与既有草稿压缩给模型写作用。"""
        from agents.prompts.report_writing import REPORT_WRITING_CONTRACT

        state = self._report_state(task_id)
        task = state["task"]
        scope_payload = state["scope_payload"] or {}
        clue_packs, _invalid = self._report_pack_clues(state["clues"])

        draft: dict[str, Any] = {"exists": False}
        report = self._latest_report_draft(task_id)
        if report:
            art, payload = self._read_artifact_payload_raw(task_id, report["id"])
            draft = {
                "exists": True,
                "version": self._report_seq_of(art, payload),
                "valid": bool((payload or {}).get("valid")),
                "note": (payload or {}).get("note") or "",
                "body": (payload or {}).get("body") or "",
                "updated_at": art.get("updated_at"),
                "overwrite": False,
            }

        return {
            "ok": True,
            "title": task.get("title"),
            "purpose": task.get("purpose") or scope_payload.get("purpose") or "",
            "authorized_until": task.get("authorized_until")
            or scope_payload.get("authorized_until")
            or "",
            "cases": [
                str(c.get("display_name") or c.get("name") or c.get("case_id") or "").strip()
                for c in (task.get("cases") or [])
                if c
            ],
            "entity": self._report_entity_stats(state["entity_payload"]),
            "clues": clue_packs,
            "blocked": self._report_state_blocked(state) or None,
            "draft": draft,
            "contract": REPORT_WRITING_CONTRACT,
        }

    def write_report_draft(
        self, task_id: str, body: str, note: str = "", user_id: str | None = None
    ) -> dict[str, Any]:
        """写报告：严格门禁 → 收口校验 → 固定边界 + 模型整篇正文 + 系统自动核对清单；每写一版新产物。"""
        from agents.prompts.report_writing import REPORT_BODY_MIN_CHARS

        state = self._report_state(task_id)
        blocked = self._report_state_blocked(state)
        if blocked:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], blocked)

        text = (body or "").strip()
        if len(text) < REPORT_BODY_MIN_CHARS:
            raise TaskError(
                TASK_ERROR_CODES["INVALID_SCOPE"],
                f"正文过短（约 {len(text)} 字），不足以构成核验单，请补充后重新提交整篇正文",
            )
        if _report_engineering_tokens(text):
            raise TaskError(
                TASK_ERROR_CODES["INVALID_SCOPE"],
                "正文疑似含内部工程代号 / 长 ID，请改用办案可读称谓（案件名、脱敏标签）后重写",
            )

        task = state["task"]
        scope_payload = state["scope_payload"] or {}
        alive = state["clues"]
        clue_packs, invalid_refs = self._report_pack_clues(alive)
        boundary = (
            "本文件仅汇集跨案关联候选、待核验事项及材料原文依据，"
            "不构成对犯罪事实、人员责任、证据能力、证明力或证明标准的认定。"
        )

        entity = self._report_entity_stats(state["entity_payload"])
        path_lines = self._compose_path_synthesis([{"payload": p} for _art, p in alive])
        case_names = "、".join(
            str(c.get("display_name") or c.get("name") or "")
            for c in (scope_payload.get("cases") or task.get("cases") or [])
        ) or "—"
        next_version = self._next_report_seq(task_id)

        markdown = "\n".join(
            [
                "# 跨案关联线索核验单",
                "",
                f"**任务**：{task.get('title') or '—'}",
                f"**版本 v{next_version}** · **生成时间**：{utc_now()}",
                "",
                "## 边界声明",
                boundary,
                "",
                "## 正文",
                "",
                text,
                "",
                "## 系统自动核对清单（依据当前已确认产物，写作时刻快照，系统生成）",
                "本清单为事实源。正文只作可读总述，不得改写下列范围、实体结论、来源编号与原文摘录。",
                f"- 监督目的：{task.get('purpose') or scope_payload.get('purpose') or '—'}",
                f"- 授权有效期：{task.get('authorized_until') or scope_payload.get('authorized_until') or '—'}",
                f"- 案件范围：{case_names}",
                "",
                *self._report_entity_checklist_lines(entity),
                "",
                *self._report_clue_checklist_lines(clue_packs),
                "",
                "### 材料路径合成（由多张处置为「已确认关联」的线索排列，非法律结论）",
                *(path_lines or ["- 尚无两条以上可合成路径的已确认线索。"]),
                "",
                "### 有效性",
                (
                    "- 部分线索缺少原文依据，正式导出前需补证或排除。"
                    if invalid_refs
                    else "- 纳入线索均可回链原文；正文由智能体撰写，以当前版本为准。"
                ),
                "",
            ]
        )

        valid = invalid_refs == 0 and bool(alive)
        parent_ids: list[str] = []
        if state["scope_id"]:
            parent_ids.append(state["scope_id"])
        if state["entity_id"]:
            parent_ids.append(state["entity_id"])
        parent_ids.extend(a["id"] for a, _p in alive)
        generated_at = utc_now()
        actor = "智能体"

        artifact = self.write_artifact(
            task_id=task_id,
            type="REPORT_DRAFT",
            title=f"跨案关联线索核验单 · v{next_version}",
            ref_key=f"report-{next_version}",
            status="VALID" if valid else "PENDING_REVIEW",
            parent_ids=parent_ids,
            payload={
                "title": "跨案关联线索核验单",
                "markdown": markdown,
                "text": markdown,
                "body": text,
                "note": (note or "").strip(),
                "valid": valid,
                "invalid_refs": invalid_refs,
                "clue_count": len(alive),
                "boundary": boundary,
                "generated_at": generated_at,
                "generated_by": actor,
                "report_seq": next_version,
                "download_count": 0,
            },
        )
        self.append_source_verify(
            task_id,
            {
                "action": "报告撰写",
                "type": "report_ai_write",
                "actor": actor,
                "target": artifact["id"],
                "summary": (
                    f"v{next_version} · 线索 {len(alive)} 条 · "
                    f"{'有效' if valid else '含待补原文依据'}"
                ),
                "result": "ok" if valid else "warn",
                "at": generated_at,
            },
        )
        return {
            "artifact": artifact,
            "task": self.get_task(task_id),
            "valid": valid,
            "clue_count": len(alive),
            "invalid_refs": invalid_refs,
            "version": next_version,
        }

    def _list_report_drafts(self, task_id: str) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            return _rows(
                conn,
                "SELECT * FROM artifacts WHERE task_id = ? AND type = 'REPORT_DRAFT' "
                "AND status NOT IN ('STALE', 'INVALID') ORDER BY created_at DESC",
                (task_id,),
            )

    def _latest_report_draft(self, task_id: str) -> dict[str, Any] | None:
        rows = self._list_report_drafts(task_id)
        return rows[0] if rows else None

    @staticmethod
    def _report_seq_of(art: dict[str, Any], payload: dict[str, Any] | None = None) -> int:
        payload = payload or {}
        if payload.get("report_seq"):
            try:
                return int(payload["report_seq"])
            except (TypeError, ValueError):
                pass
        matched = re.match(r"report-(\d+)$", str(art.get("ref_key") or ""))
        if matched:
            return int(matched.group(1))
        try:
            return int(art.get("current_version") or 1)
        except (TypeError, ValueError):
            return 1

    def _next_report_seq(self, task_id: str) -> int:
        seq = 0
        with db_session(self.db_path) as conn:
            rows = _rows(
                conn,
                "SELECT ref_key, current_version FROM artifacts "
                "WHERE task_id = ? AND type = 'REPORT_DRAFT'",
                (task_id,),
            )
        for row in rows or []:
            matched = re.match(r"report-(\d+)$", str(row.get("ref_key") or ""))
            if matched:
                seq = max(seq, int(matched.group(1)))
            elif str(row.get("ref_key") or "") == "report-draft":
                try:
                    seq = max(seq, int(row.get("current_version") or 1))
                except (TypeError, ValueError):
                    seq = max(seq, 1)
        return seq + 1

    def _report_download_counts(self, task_id: str) -> dict[tuple[str, int], int]:
        counts: dict[tuple[str, int], int] = {}
        log = self.find_artifact(task_id, "SOURCE_VERIFY", "source-verify-log")
        if not log:
            return counts
        _art, payload = self._read_artifact_payload_raw(task_id, log["id"])
        for ev in (payload or {}).get("events") or []:
            if not isinstance(ev, dict) or ev.get("type") != "report_export":
                continue
            target = str(ev.get("target") or "").strip()
            if not target:
                continue
            ver = ev.get("report_version")
            if ver is None:
                matched = re.search(r"v(\d+)", str(ev.get("summary") or ""))
                ver = int(matched.group(1)) if matched else 0
            try:
                ver_n = int(ver)
            except (TypeError, ValueError):
                ver_n = 0
            key = (target, ver_n)
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _report_version_is_download_bump(
        previous: dict[str, Any] | None, current: dict[str, Any]
    ) -> bool:
        if not previous:
            return False
        return (
            (previous.get("body") or "") == (current.get("body") or "")
            and (previous.get("markdown") or previous.get("text") or "")
            == (current.get("markdown") or current.get("text") or "")
            and int(current.get("download_count") or 0) >= int(previous.get("download_count") or 0)
            and int(current.get("download_count") or 0) != int(previous.get("download_count") or 0)
        )

    def list_report_editions(self, task_id: str) -> dict[str, Any]:
        """列出每一次撰写的核验单（含旧「单产物多版本」），下载次数另计，不把下载当成新版。"""
        self.get_task(task_id)
        download_counts = self._report_download_counts(task_id)
        editions: list[dict[str, Any]] = []
        with db_session(self.db_path) as conn:
            arts = _rows(
                conn,
                "SELECT * FROM artifacts WHERE task_id = ? AND type = 'REPORT_DRAFT' "
                "AND status != 'INVALID' ORDER BY created_at ASC",
                (task_id,),
            )
            for art in arts:
                versions = _rows(
                    conn,
                    "SELECT version, status, created_at, payload_json FROM artifact_versions "
                    "WHERE artifact_id = ? ORDER BY version ASC",
                    (art["id"],),
                )
                previous: dict[str, Any] | None = None
                for ver in versions:
                    payload = json.loads(ver.get("payload_json") or "{}")
                    if self._report_version_is_download_bump(previous, payload):
                        previous = payload
                        continue
                    previous = payload
                    version_no = int(ver["version"])
                    if payload.get("report_seq"):
                        seq = self._report_seq_of(art, payload)
                    elif str(art.get("ref_key") or "") == "report-draft":
                        seq = version_no
                    else:
                        seq = self._report_seq_of(art, payload)
                    valid = (
                        art.get("status") not in {"STALE", "INVALID"}
                        and ver.get("status") not in {"STALE", "INVALID", "PENDING_REVIEW"}
                        and payload.get("valid") is not False
                    )
                    dl = download_counts.get((art["id"], version_no), 0)
                    if not dl:
                        dl = download_counts.get((art["id"], seq), 0)
                    if not dl:
                        dl = int(payload.get("download_count") or 0)
                    editions.append(
                        {
                            "artifact_id": art["id"],
                            "version": version_no,
                            "report_seq": seq,
                            "title": f"跨案关联线索核验单 · v{seq}",
                            "valid": valid,
                            "status": art.get("status") or "",
                            "clue_count": payload.get("clue_count"),
                            "generated_at": payload.get("generated_at") or ver.get("created_at") or "",
                            "generated_by": payload.get("generated_by") or "智能体",
                            "download_count": dl,
                        }
                    )
        editions.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
        return {"ok": True, "reports": editions}

    def report_export_document(
        self,
        task_id: str,
        report_id: str | None = None,
        export_id: str | None = None,
        version: int | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """下载指定核验单的某一撰写版本；只记下载次数，不覆盖、不另开新版。"""
        target_id = report_id or export_id
        if not target_id:
            latest = self._latest_report_draft(task_id)
            if not latest:
                raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "尚未撰写报告或报告已失效")
            target_id = latest["id"]

        with db_session(self.db_path) as conn:
            row = _row(
                conn,
                "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
                (target_id, task_id),
            )
            if not row:
                raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "尚未撰写报告或报告已失效")
            target_version = int(version or row.get("current_version") or 1)
            ver = _row(
                conn,
                "SELECT payload_json, status FROM artifact_versions "
                "WHERE artifact_id = ? AND version = ?",
                (target_id, target_version),
            )
        if not ver:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "该版报告不存在")
        payload = json.loads(ver.get("payload_json") or "{}")
        markdown = payload.get("markdown") or payload.get("text") or ""
        if not str(markdown).strip():
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "报告内容为空")

        art_type = row.get("type")
        if art_type == "REPORT_EXPORT":
            return {
                "markdown": markdown,
                "title": row.get("title") or payload.get("title") or "跨案关联线索核验单",
                "version": int(payload.get("export_seq") or row.get("current_version") or 1),
                "report_id": row["id"],
                "download_count": int(payload.get("download_count") or 0),
            }
        if art_type != "REPORT_DRAFT":
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "不是可下载的核验单")
        if row.get("status") in {"STALE", "INVALID"}:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "该版报告已失效")
        if payload.get("valid") is False:
            raise TaskError(
                TASK_ERROR_CODES["STATE_CONFLICT"],
                "该版存在待补原文依据，补证或排除后方可下载",
            )

        if payload.get("report_seq"):
            seq = self._report_seq_of(row, payload)
        elif str(row.get("ref_key") or "") == "report-draft":
            seq = target_version
        else:
            seq = self._report_seq_of(row, payload)
        self.append_source_verify(
            task_id,
            {
                "action": "报告导出",
                "type": "report_export",
                "actor": user_id or "检察官",
                "target": row["id"],
                "report_version": target_version,
                "summary": f"v{seq} · 下载",
                "result": "ok",
                "at": utc_now(),
            },
        )
        return {
            "markdown": markdown,
            "title": payload.get("title") or row.get("title") or "跨案关联线索核验单",
            "version": seq,
            "report_id": row["id"],
            "download_count": 0,
        }

    # ----- 产物 -----

    def _read_artifact_payload_raw(
        self, task_id: str, artifact_id: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """读产物当前版本 payload（不做 hydrate/回链修复），供模型摘要用。"""
        with db_session(self.db_path) as conn:
            art = _row(
                conn,
                "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
                (artifact_id, task_id),
            )
            if not art:
                raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "artifact not found")
            ver = _row(
                conn,
                "SELECT payload_json FROM artifact_versions "
                "WHERE artifact_id = ? AND version = ?",
                (artifact_id, int(art["current_version"])),
            )
        payload = json.loads(ver["payload_json"]) if ver and ver.get("payload_json") else {}
        return dict(art), payload

    def _entity_digest_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """实体候选集摘要：给模型看已核验结论与可回链引文，而不是 1.6MB 全量。"""
        candidates: list[dict[str, Any]] = []
        for cand in payload.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            evidence = []
            for ev in (cand.get("evidence") or [])[:6]:
                if not isinstance(ev, dict):
                    continue
                evidence.append(
                    {
                        "quote": str(ev.get("quote") or "")[:120],
                        "chunk_id": ev.get("chunk_id"),
                        "document_version_id": ev.get("document_version_id"),
                        "page_start": ev.get("page_start"),
                        "filename": ev.get("filename") or ev.get("document_name") or "",
                        "case_name": ev.get("case_name") or "",
                    }
                )
            records = [
                {"value": str(r.get("value") or "")[:60], "case_name": r.get("case_name") or ""}
                for r in (cand.get("records") or [])[:8]
                if isinstance(r, dict)
            ]
            candidates.append(
                {
                    "candidate_id": cand.get("candidate_id"),
                    "entity_type": cand.get("entity_type"),
                    "display_name": cand.get("display_name") or cand.get("title"),
                    "aliases": (cand.get("aliases") or [])[:6],
                    "cases": cand.get("cases") or [],
                    "match_tier": cand.get("match_tier"),
                    "decision": cand.get("decision") or "PENDING",
                    "reason": str(cand.get("reason") or "")[:60],
                    "reviewed_at": cand.get("reviewed_at"),
                    "records": records,
                    "evidence": evidence,
                }
            )
        summary = dict(payload.get("summary") or {})
        return {
            "summary": {
                "total": summary.get("total") or len(candidates),
                "reviewed": summary.get("reviewed") or 0,
                "pending": summary.get("pending") or 0,
                "analysis_gate": summary.get("analysis_gate")
                or payload.get("analysis_gate")
                or "",
            },
            "candidates": candidates,
            "boundary": "人工核验 decision 已含在每条中：MERGE=视为同一、KEEP_SEPARATE=保留独立、CORRECT=已更正、DEFER=暂缓；PENDING=待核。",
        }

    def _timeline_digest_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """角色时间线摘要：事件按关键字段裁剪，避免整包灌给模型。"""
        items = []
        for item in (payload.get("items") or [])[:200]:
            if not isinstance(item, dict):
                continue
            quote = ""
            source = item.get("source") or {}
            if isinstance(source, dict):
                quote = str(source.get("quote") or "")[:60]
            items.append(
                {
                    "event_id": item.get("event_id"),
                    "event_type": item.get("event_type"),
                    "time_text": item.get("time_text") or item.get("event_time") or "",
                    "time_uncertain": bool(item.get("time_uncertain")),
                    "case_name": item.get("case_name") or "",
                    "subject": item.get("subject") or "",
                    "subject_kind": item.get("subject_kind") or "",
                    "role_or_action": str(item.get("role_or_action") or "")[:40],
                    "summary_text": str(item.get("summary_text") or "")[:100],
                    "source_mode": item.get("source_mode") or "recorded",
                    "quote": quote,
                }
            )
        return {
            "summary": payload.get("summary") or {},
            "items": items,
            "truncated": len(payload.get("items") or []) > len(items),
        }

    def artifact_digest_for_model(self, task_id: str, artifact_id: str) -> dict[str, Any]:
        """给模型读产物：大产物（实体候选集/时间线）返回摘要，其余保持原 payload。"""
        art, payload = self._read_artifact_payload_raw(task_id, artifact_id)
        atype = art["type"]
        if atype == "ENTITY_CANDIDATE_SET":
            payload = self._entity_digest_payload(payload)
        elif atype == "ROLE_TIMELINE":
            payload = self._timeline_digest_payload(payload)
        return {
            "artifact": {
                "id": art["id"],
                "type": atype,
                "title": art.get("title") or "",
                "status": art.get("status") or "",
                "version": art.get("current_version"),
            },
            "payload": payload,
        }

    def find_artifact(self, task_id: str, type: str, ref_key: str | None) -> Optional[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            return _row(
                conn,
                "SELECT * FROM artifacts WHERE task_id = ? AND type = ? AND ref_key IS ?",
                (task_id, type, ref_key),
            )

    def write_artifact(
        self,
        *,
        task_id: str,
        type: str,
        title: str,
        payload: dict[str, Any],
        ref_key: str | None = None,
        status: str = "VALID",
        parent_ids: list[str] | None = None,
        input_snapshot: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """写产物：同一 artifact_id 内版本只追加，不覆盖历史。"""
        if type not in ARTIFACT_TYPES:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], f"unknown artifact type: {type}")
        if status not in ARTIFACT_STATUSES:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], f"unknown status: {status}")

        now = utc_now()
        with db_session(self.db_path) as conn:
            existing = _row(
                conn,
                "SELECT * FROM artifacts WHERE task_id = ? AND type = ? AND ref_key IS ?",
                (task_id, type, ref_key),
            )
            if existing and existing["type"] in FROZEN_ARTIFACT_TYPES:
                raise TaskError(
                    TASK_ERROR_CODES["STATE_CONFLICT"], "已发布报告不可改写，只能生成新版本"
                )

            if existing:
                artifact_id = existing["id"]
                version = int(existing["current_version"]) + 1
                _update(
                    conn,
                    "artifacts",
                    artifact_id,
                    {
                        "title": title,
                        "status": status,
                        "current_version": version,
                        "stale_reason": None,
                        "updated_at": now,
                        "parent_ids_json": json.dumps(
                            parent_ids if parent_ids is not None
                            else json.loads(existing["parent_ids_json"] or "[]"),
                            ensure_ascii=False,
                        ),
                    },
                )
            else:
                artifact_id = new_id()
                version = 1
                _insert(
                    conn,
                    "artifacts",
                    {
                        "id": artifact_id,
                        "task_id": task_id,
                        "type": type,
                        "title": title,
                        "status": status,
                        "current_version": version,
                        "parent_ids_json": json.dumps(parent_ids or [], ensure_ascii=False),
                        "ref_key": ref_key,
                        "stale_reason": None,
                        "created_at": now,
                        "updated_at": now,
                    },
                )

            _insert(
                conn,
                "artifact_versions",
                {
                    "id": new_id(),
                    "artifact_id": artifact_id,
                    "version": version,
                    "status": status,
                    "payload_json": json.dumps(payload, ensure_ascii=False),
                    "input_snapshot_json": json.dumps(input_snapshot or {}, ensure_ascii=False),
                    "created_by_run_id": run_id,
                    "created_at": now,
                },
            )
            _update(conn, "supervision_tasks", task_id, {"updated_at": now})
            return _artifact_row(conn, artifact_id)

    def get_artifact(
        self, task_id: str, artifact_id: str, version: int | None = None
    ) -> dict[str, Any]:
        """目录点击与智能体链接都走这里，保证解析到同一对象。"""
        with db_session(self.db_path) as conn:
            artifact = _row(
                conn,
                "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
                (artifact_id, task_id),
            )
            if not artifact:
                raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "artifact not found")
            target = version or int(artifact["current_version"])
            row = _row(
                conn,
                "SELECT * FROM artifact_versions WHERE artifact_id = ? AND version = ?",
                (artifact_id, target),
            )
            history = _rows(
                conn,
                "SELECT version, status, created_at FROM artifact_versions "
                "WHERE artifact_id = ? ORDER BY version DESC",
                (artifact_id,),
            )

        artifact["parent_ids"] = json.loads(artifact["parent_ids_json"] or "[]")
        payload = json.loads(row["payload_json"]) if row else {}
        try:
            from tools.entities import (
                canonicalize_evidence_citation,
                hydrate_payload_for_display,
            )

            # 线索旧数据常含模型自造 hash：读取时重锚，保证原文核验可用
            if artifact.get("type") == "CLUE_ITEM":
                evidence = list(payload.get("evidence") or [])
                repaired: list[dict[str, Any]] = []
                changed = False
                for ev in evidence:
                    if not isinstance(ev, dict):
                        continue
                    if not ev.get("chunk_id") or not ev.get("document_version_id"):
                        repaired.append(ev)
                        continue
                    try:
                        canon = canonicalize_evidence_citation(
                            document_version_id=ev["document_version_id"],
                            chunk_id=ev["chunk_id"],
                            quote=ev.get("quote"),
                            anchor_terms=[
                                x
                                for x in [
                                    ev.get("value"),
                                    ev.get("extracted_value"),
                                    payload.get("title"),
                                    ev.get("quote_display"),
                                ]
                                if x
                            ],
                            db_path=self.db_path,
                        )
                        merged = {**ev, **canon}
                        if (
                            merged.get("quote") != ev.get("quote")
                            or merged.get("quote_hash") != ev.get("quote_hash")
                        ):
                            changed = True
                        repaired.append(merged)
                    except Exception:
                        repaired.append(ev)
                if repaired:
                    payload["evidence"] = repaired
                if changed:
                    # 静默落盘修好的引用，避免每次打开都失效
                    try:
                        self.write_artifact(
                            task_id=task_id,
                            type="CLUE_ITEM",
                            title=payload.get("title") or artifact.get("title") or "线索",
                            ref_key=artifact.get("ref_key"),
                            status=artifact.get("status") or "DRAFT",
                            parent_ids=artifact.get("parent_ids") or [],
                            payload=payload,
                            input_snapshot={"repaired": "citation-on-read"},
                        )
                    except Exception:
                        pass
            payload = hydrate_payload_for_display(payload)
        except Exception:
            pass
        if artifact.get("type") == "ENTITY_CANDIDATE_SET":
            try:
                payload = self.enrich_entity_candidate_payload(task_id, payload)
            except Exception:
                pass
        return {
            "artifact": artifact,
            "version": target,
            "status": artifact["status"],
            "payload": payload,
            "input_snapshot": json.loads(row["input_snapshot_json"]) if row else {},
            "history": history,
        }

    def preview_impact(self, task_id: str, artifact_id: str) -> dict[str, Any]:
        """影响预览：先算清单给用户看，确认前不打 STALE、不重算。"""
        with db_session(self.db_path) as conn:
            changed = _artifact_row(conn, artifact_id)
            if not changed or changed["task_id"] != task_id:
                raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "artifact not found")
            affected_ids = _downstream_ids(conn, task_id, [artifact_id])
            affected = [_artifact_row(conn, a) for a in affected_ids]
            preserved = _rows(
                conn,
                "SELECT id, type, title FROM artifacts WHERE task_id = ? AND id NOT IN "
                f"({','.join('?' for _ in ([artifact_id] + affected_ids))})",
                (task_id, artifact_id, *affected_ids),
            )
        return {
            "changed": {"id": changed["id"], "type": changed["type"], "title": changed["title"]},
            "stale_artifacts": [
                {"id": a["id"], "type": a["type"], "title": a["title"]} for a in affected
            ],
            "preserved_artifacts": preserved,
        }

    def apply_impact(self, task_id: str, artifact_id: str, reason: str = "") -> dict[str, Any]:
        """用户确认影响预览后才标记过期；过期产物只读，不能处置或正式导出。"""
        impact = self.preview_impact(task_id, artifact_id)
        now = utc_now()
        with db_session(self.db_path) as conn:
            for item in impact["stale_artifacts"]:
                _update(
                    conn,
                    "artifacts",
                    item["id"],
                    {
                        "status": "STALE",
                        "stale_reason": reason or f"上游 {impact['changed']['title']} 已变化",
                        "updated_at": now,
                    },
                )
            _update(conn, "supervision_tasks", task_id, {"updated_at": now})
        return {"applied": True, **impact}

    # ----- 材料接入 -----

    def record_material(
        self,
        *,
        task_id: str,
        case_id: str,
        upload_result: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """材料上传结果落为 MATERIAL_DOC 产物，并刷新 MATERIAL_BATCH。"""
        document = upload_result.get("document") or {}
        document_id = document.get("id")
        if not document_id:
            return {"artifact": None, "upload": upload_result}

        scope = self.find_artifact(task_id, "TASK_SCOPE", "scope")
        doc_artifact = self.write_artifact(
            task_id=task_id,
            type="MATERIAL_DOC",
            title=document.get("filename") or "材料",
            ref_key=document_id,
            status="VALID",
            parent_ids=[scope["id"]] if scope else [],
            payload={
                "document_id": document_id,
                "case_id": case_id,
                "filename": document.get("filename"),
                "size": document.get("size"),
                "status": upload_result.get("status") or document.get("status"),
                "version": (upload_result.get("version") or {}).get("version_no"),
            },
            input_snapshot={"sha256": document.get("sha256")},
        )
        batch = self.refresh_material_batch(task_id, user_id=user_id)
        return {"artifact": doc_artifact, "batch_artifact_id": batch["id"], "upload": upload_result}

    def remove_material(
        self,
        task_id: str,
        document_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """逻辑删除材料，并同步目录：MATERIAL_DOC 作废、刷新批次、下游过期。"""
        task = self.get_task(task_id)
        case_ids = {c["case_id"] for c in task["cases"]}
        service = MaterialService(db_path=self.db_path)
        with db_session(self.db_path) as conn:
            document = _row(conn, "SELECT * FROM documents WHERE id = ?", (document_id,))
        if not document:
            raise TaskError(TASK_ERROR_CODES["NOT_FOUND"], "材料不存在")
        if document["case_id"] not in case_ids:
            raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], "材料不属于本任务案件范围")

        delete_result = service.logical_delete(document_id, user_id=user_id)
        doc_art = self.find_artifact(task_id, "MATERIAL_DOC", document_id)
        if doc_art:
            self.write_artifact(
                task_id=task_id,
                type="MATERIAL_DOC",
                title=doc_art.get("title") or document.get("filename") or "材料",
                ref_key=document_id,
                status="INVALID",
                payload={
                    "document_id": document_id,
                    "case_id": document["case_id"],
                    "filename": document.get("filename"),
                    "status": "DELETED",
                    "deleted": True,
                },
            )
        batch = self.refresh_material_batch(task_id, user_id=user_id)
        impact = self.apply_impact(
            task_id,
            batch["id"],
            reason=f"材料已删除：{document.get('filename') or document_id}",
        )
        return {
            "status": "DELETED",
            "document_id": document_id,
            "delete": delete_result,
            "batch_artifact_id": batch["id"],
            "impact": impact,
            "task": self.get_task(task_id),
        }

    # ----- 阶段4：最小上下文检索与结构化角色 -----

    def list_gated_chunks(
        self,
        task_id: str,
        user_id: str | None = None,
        *,
        chunk_ids: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """只从任务案件范围读取有效版本，并经外发门控取最小脱敏 chunk。"""
        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        service = get_material_service()
        wanted = set(chunk_ids) if chunk_ids else None
        collected: list[dict[str, Any]] = []
        for case in task["cases"]:
            try:
                materials = service.list_materials(case["case_id"], user_id=user_id)
            except MaterialError:
                continue
            for item in materials:
                current = item.get("current_version")
                if not current:
                    continue
                version_id = current["id"]
                with db_session(self.db_path) as conn:
                    rows = list_chunks(conn, version_id, active_only=True)
                for chunk in rows:
                    if wanted and chunk["id"] not in wanted:
                        continue
                    try:
                        gated = service.read_redacted_chunk(
                            version_id,
                            chunk_id=chunk["id"],
                            user_id=user_id or "system",
                        )
                    except MaterialError:
                        continue
                    collected.append(
                        {
                            **gated,
                            "case_id": case["case_id"],
                            "document_id": item.get("id"),
                            "filename": item.get("filename"),
                        }
                    )
                    if not wanted and len(collected) >= limit:
                        return collected
        return collected


    def retire_task_clues(
        self,
        task_id: str,
        *,
        reason: str = "关联线索已按办案风整体再生，旧条作废",
    ) -> int:
        """将本任务全部活线索（CLUE_ITEM / CLUE_SET）标为 STALE，供再生替换。"""
        now = utc_now()
        retired = 0
        with db_session(self.db_path) as conn:
            arts = _rows(
                conn,
                "SELECT id, status FROM artifacts "
                "WHERE task_id = ? AND type IN ('CLUE_ITEM', 'CLUE_SET') "
                "AND status NOT IN ('STALE', 'INVALID')",
                (task_id,),
            )
            for art in arts:
                _update(
                    conn,
                    "artifacts",
                    art["id"],
                    {
                        "status": "STALE",
                        "stale_reason": reason,
                        "updated_at": now,
                    },
                )
                retired += 1
            if retired:
                _update(conn, "supervision_tasks", task_id, {"updated_at": now})
        return retired

    def list_association_hints(
        self,
        task_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """关联提示：跨案规则命中 + 弱平台共现 + 案內一致性（后两者不落跨案线索）。"""
        from tools.entities import (
            collect_rule_hits,
            collect_weak_platform_hints,
            collect_within_case_consistency,
            collect_nick_relation_hints,
        )

        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        hits = collect_rule_hits(task_id, task["cases"], db_path=self.db_path)
        hints: list[dict[str, Any]] = []
        for hit in hits:
            evidence = hit.get("evidence") or []
            case_ids = {item.get("case_id") for item in evidence if item.get("case_id")}
            chunk_ids = {item.get("chunk_id") for item in evidence if item.get("chunk_id")}
            if len(case_ids) < 2 or len(chunk_ids) < 2:
                continue
            case_names = [
                c.get("case_name") or c.get("case_id") or ""
                for c in (hit.get("cases") or [])
            ]
            hints.append(
                {
                    "kind": "cross_case_rule",
                    "rule_id": hit.get("rule_id"),
                    "label": hit.get("label") or hit.get("rule_id"),
                    "fingerprint": hit.get("fingerprint"),
                    "suggested_aspect": self._hint_aspect_for_rule(hit.get("rule_id")),
                    "cases": [n for n in case_names if n],
                    "evidence_count": len(evidence),
                    "note": "跨案规则提示，须结合材料形成待核线索后再写入线索中心",
                }
            )

        platform_hints = collect_weak_platform_hints(
            task_id, task["cases"], db_path=self.db_path
        )
        hints.extend(platform_hints)

        nick_hints = collect_nick_relation_hints(
            task_id, task["cases"], db_path=self.db_path
        )
        hints.extend(nick_hints)

        consistency = collect_within_case_consistency(task_id, db_path=self.db_path)
        return {
            "ok": True,
            "hint_count": len(hints),
            "hints": hints[:40],
            "within_case_consistency": consistency,
            "message": (
                "以上含跨案标识、外号/代称与弱平台共现提示。"
                "外号上线、外号与代称是否同指，应单独写成待核线索；"
                "仅某一案出现的共同参与人可写一条核验「勿挂到其他案」。"
                "案內多卡同号请在实体复核或材料核对中处理，不要写成跨案关联。"
            ),
        }

    @staticmethod
    def _hint_aspect_for_rule(rule_id: str | None) -> str:
        rid = (rule_id or "").upper()
        if rid == "R004":
            return "FUND"
        if rid == "R005":
            return "TIME"
        return "ID"

    def _compose_path_synthesis(
        self,
        clues: list[dict[str, Any]],
    ) -> list[str]:
        """由多张已确认线索合成「材料路径」叙述，禁止单条定性为犯罪链条。"""
        aspect_order = {"FUND": 0, "PLAT": 1, "ID": 2, "TIME": 3, "ROLE": 4, "QUAL": 5}
        aspect_label = {
            "ID": "标识",
            "FUND": "资金",
            "TIME": "时空",
            "ROLE": "角色",
            "QUAL": "材料质量",
            "PLAT": "平台",
        }
        confirmed: list[dict[str, Any]] = []
        for item in clues:
            payload = item.get("payload") or {}
            if not clue_is_confirmed(payload):
                continue
            confirmed.append(payload)
        if len(confirmed) < 2:
            return []

        # 按共享对象聚类
        groups: dict[str, list[dict[str, Any]]] = {}
        for payload in confirmed:
            objects = [str(x).strip() for x in (payload.get("objects") or []) if str(x).strip()]
            key = "|".join(sorted(objects)[:3]) if objects else (payload.get("title") or "")[:24]
            groups.setdefault(key or "_", []).append(payload)

        lines: list[str] = []
        for key, items in groups.items():
            if len(items) < 2:
                continue
            items = sorted(
                items,
                key=lambda p: aspect_order.get(str(p.get("aspect") or "").upper(), 99),
            )
            steps = []
            for p in items:
                asp = str(p.get("aspect") or "").upper()
                label = aspect_label.get(asp, "待核")
                title = str(p.get("title") or "").strip()
                steps.append(f"{label}：{title}")
            lines.append(
                "- 材料路径合成（供核验，非法律结论）：" + " → ".join(steps)
            )
        return lines

    def write_ai_clues(
            self,
            task_id: str,
            clues: list[dict[str, Any]],  # 注意：clues 是列表，不是字典
            user_id: str | None = None,
            replace_all: bool = True,
    ) -> dict[str, Any]:
        """
        AI 写入/更新线索（“整批替换”与“逐条维护”共用同一落库核心）。

        replace_all=True：先作废本任务既有 CLUE_ITEM / CLUE_SET，再用本条/本批重建
            （新一轮/整批替换语义，即旧 write_ai_clues 行为）。
        replace_all=False：逐条追加，仅顶替“同核验维度 + 同核验问题”的旧模型线索
            （避免重复）；已被人工处置的旧条拒绝覆盖。

        无论哪种，写入后都由当前全部存活 CLUE_ITEM 重建 CLUE_SET 聚合产物，
        保证线索中心列表与 CLUE_ITEM 一致。
        """
        from agents.prompts.clue_writing import CLUE_ASPECTS

        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        if not clues:
            return {"artifact": None, "clue_count": 0, "message": "没有线索可写入"}

        def _normalize_title(raw: str) -> str:
            text = (raw or "").strip()
            if not text:
                return ""
            if not text.startswith("请核验"):
                text = f"请核验：{text}"
            return text[:60]

        def _normalize_analysis(clue: dict[str, Any]) -> str:
            text = str(clue.get("analysis") or clue.get("summary") or "").strip()
            text = re.sub(r"\s+", " ", text)
            return text[:80]

        def _normalize_aspect(raw: Any) -> str:
            text = str(raw or "").strip().upper()
            aliases = {
                "IDENTITY": "ID",
                "IDENT": "ID",
                "ACCOUNT": "ID",
                "MONEY": "FUND",
                "FUNDING": "FUND",
                "TEMPORAL": "TIME",
                "TIMELINE": "TIME",
                "QUALITY": "QUAL",
                "PLATFORM": "PLAT",
                "PLAT": "PLAT",
                "PAY": "PLAT",
            }
            text = aliases.get(text, text)
            return text

        def _normalize_objects(raw: Any) -> list[str]:
            items: list[str] = []
            for obj in raw or []:
                text = str(obj or "").strip()
                if not text:
                    continue
                if re.search(r"(?:PERSON|PHONE|ACCOUNT|ORG|DEVICE|ID)_[a-f0-9]{4,}", text, re.I):
                    continue
                if text not in items:
                    items.append(text[:24])
                if len(items) >= 5:
                    break
            return items

        def _object_key(clue: dict[str, Any]) -> str:
            linked = sorted(
                str(x).strip()
                for x in (clue.get("linked_candidate_ids") or [])
                if str(x).strip()
            )
            fps = sorted(
                {
                    str(x).strip()
                    for x in (
                        list(clue.get("fingerprints") or [])
                        + ([clue.get("fingerprint")] if clue.get("fingerprint") else [])
                    )
                    if str(x).strip()
                }
            )
            if linked:
                return "cand:" + "|".join(linked)
            if fps:
                return "fp:" + "|".join(fps)
            objects = _normalize_objects(clue.get("objects"))
            if objects:
                return "obj:" + "|".join(sorted(objects))
            title = _normalize_title(clue.get("title", ""))
            return "title:" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]

        def _canon_evidence_list(
            rows: list[dict[str, Any]] | None,
            *,
            title: str,
            default_stance: str,
            clue_index: int,
            label: str,
        ) -> list[dict[str, Any]]:
            from tools.entities import canonicalize_evidence_citation

            out: list[dict[str, Any]] = []
            for ev in rows or []:
                if not ev.get("chunk_id"):
                    raise TaskError(
                        TASK_ERROR_CODES["INVALID_SCOPE"],
                        f"第 {clue_index + 1} 条线索的 {label} 缺少 chunk_id",
                    )
                version_id = ev.get("document_version_id")
                if not version_id:
                    raise TaskError(
                        TASK_ERROR_CODES["INVALID_SCOPE"],
                        f"第 {clue_index + 1} 条线索的 {label} 缺少 document_version_id",
                    )
                anchors = [
                    a
                    for a in [
                        ev.get("value"),
                        ev.get("extracted_value"),
                        title,
                    ]
                    if a
                ]
                try:
                    canon = canonicalize_evidence_citation(
                        document_version_id=version_id,
                        chunk_id=ev["chunk_id"],
                        quote=ev.get("quote"),
                        anchor_terms=anchors,
                        db_path=self.db_path,
                    )
                except ValueError as exc:
                    raise TaskError(
                        TASK_ERROR_CODES["INVALID_SCOPE"],
                        f"第 {clue_index + 1} 条线索{label}无法回链：{exc}",
                    ) from exc
                out.append(
                    {
                        **ev,
                        **canon,
                        "case_name": ev.get("case_name") or "",
                        "stance": ev.get("stance") or default_stance,
                    }
                )
            return out

        # 先整批校验并准备 payload，全部通过后再作废旧条并落库（失败不伤旧数据）
        aspect_by_object: dict[str, set[str]] = {}
        aspects_seen: set[str] = set()
        prepared: list[dict[str, Any]] = []

        entity_set = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        clue_status = (
            "VALID"
            if entity_set and entity_set.get("status") == "VALID"
            else "DRAFT"
        )

        for idx, clue in enumerate(clues):
            aspect = _normalize_aspect(clue.get("aspect"))
            if aspect not in CLUE_ASPECTS:
                raise TaskError(
                    TASK_ERROR_CODES["INVALID_SCOPE"],
                    f"第 {idx + 1} 条线索核验方面无效（须为：标识同一性/资金路径/时空连续/角色冲突/材料质量/平台共现）",
                )
            key = _object_key(clue)
            used = aspect_by_object.setdefault(key, set())
            if aspect in used:
                raise TaskError(
                    TASK_ERROR_CODES["INVALID_SCOPE"],
                    f"第 {idx + 1} 条线索与同批其他线索在同一关联对象上重复了同一核验方面，请按标识、资金、时空、角色、材料质量等拆开",
                )
            used.add(aspect)
            aspects_seen.add(aspect)

            title = _normalize_title(clue.get("title", ""))
            analysis = _normalize_analysis(clue)
            evidence_raw = clue.get("evidence") or []
            counter_raw = clue.get("counter_evidence") or []
            if "counter_evidence" in clue and not isinstance(counter_raw, list):
                raise TaskError(
                    TASK_ERROR_CODES["INVALID_SCOPE"],
                    f"第 {idx + 1} 条线索 counter_evidence 必须是数组",
                )
            if not title:
                raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], f"第 {idx + 1} 条线索缺少 title")
            if not analysis:
                raise TaskError(
                    TASK_ERROR_CODES["INVALID_SCOPE"],
                    f"第 {idx + 1} 条线索缺少 analysis（办案观察）",
                )
            if len(evidence_raw) < 2:
                raise TaskError(
                    TASK_ERROR_CODES["INVALID_SCOPE"],
                    f"第 {idx + 1} 条线索 evidence 至少需要 2 条可回链摘录",
                )
            # match_basis / objects / counter_evidence 均可缺省，下面自动兜底
            if not counter_raw and "未见反向" not in analysis and "无反向" not in analysis:
                analysis = (analysis.rstrip("。；; ") + "；未见反向材料。")[:80]

            evidence = _canon_evidence_list(
                evidence_raw, title=title, default_stance="support", clue_index=idx, label="evidence"
            )
            counter_evidence = _canon_evidence_list(
                counter_raw,
                title=title,
                default_stance="counter",
                clue_index=idx,
                label="counter_evidence",
            )
            match_basis = str(clue.get("match_basis") or clue.get("rule_id") or "跨案标识关联").strip()[:20]
            objects = _normalize_objects(clue.get("objects"))
            if not objects:
                for ev in evidence:
                    name = str(ev.get("case_name") or "").strip()
                    if name and name not in objects:
                        objects.append(name[:24])
                    if len(objects) >= 3:
                        break
            uncertainty = str(clue.get("uncertainty") or "").strip()[:40]
            if not uncertainty:
                uncertainty = "标识重合仅为待核验线索，不代表同一主体或共同犯罪。"

            hash_input = f"{aspect}|{title}|{analysis}".encode("utf-8")
            short_hash = hashlib.sha256(hash_input).hexdigest()[:16]
            clue_payload = self.normalize_clue_fingerprints(
                {
                    "title": title,
                    "aspect": aspect,
                    "analysis": analysis,
                    "summary": analysis,
                    "match_basis": match_basis,
                    "objects": objects,
                    "evidence": evidence,
                    "counter_evidence": counter_evidence,
                    "uncertainty": uncertainty,
                    "boundary": uncertainty,
                    "producer": "AI_AGENT",
                    "rule_id": match_basis,
                    "promotion": "confirmed" if clue_status == "VALID" else "draft_pending_entity_review",
                    "linked_candidate_ids": list(clue.get("linked_candidate_ids") or []),
                    "fingerprint": clue.get("fingerprint") or "",
                    "fingerprints": list(clue.get("fingerprints") or []),
                }
            )
            prepared.append(
                {
                    "ref_key": f"ai-clue:{short_hash}",
                    "title": title,
                    "aspect": aspect,
                    "status": clue_status,
                    "payload": clue_payload,
                    "input_snapshot": {"clue_index": idx, "aspect": aspect},
                }
            )

        if len(prepared) >= 2 and len(aspects_seen) < 2:
            raise TaskError(
                TASK_ERROR_CODES["INVALID_SCOPE"],
                "一次写入不少于 2 条时，须至少覆盖 2 种核验方面（标识同一性/资金路径/时空连续/角色冲突/材料质量/平台共现），避免同义重复",
            )

        # 整轮替换才作废旧条；逐条追加只顶替“同对象同核验维度”的旧模型线索
        retired = self.retire_task_clues(task_id) if replace_all else 0
        if not replace_all:
            for row in prepared:
                retired += self._retire_active_duplicate(task_id, row)

        item_ids: list[str] = []
        for row in prepared:
            clue_item = self.write_artifact(
                task_id=task_id,
                type="CLUE_ITEM",
                title=row["title"],
                ref_key=row["ref_key"],
                status=row["status"],
                parent_ids=[],
                payload=row["payload"],
                input_snapshot=row["input_snapshot"],
            )
            item_ids.append(clue_item["id"])

        # 回写相关实体候选的线索指标（读取时会重算，这里刷新已落盘快照）
        if entity_set and item_ids:
            try:
                detail = self.get_artifact(task_id, entity_set["id"])
                payload = detail.get("payload") or {}
                touched = False
                for cand in payload.get("candidates") or []:
                    before = len(cand.get("generated_clues") or [])
                    self.apply_candidate_clue_impact(task_id, cand)
                    if len(cand.get("generated_clues") or []) != before:
                        touched = True
                if touched:
                    self.write_artifact(
                        task_id=task_id,
                        type="ENTITY_CANDIDATE_SET",
                        title=entity_set.get("title") or "跨案对象待核·待判断",
                        ref_key="entity-candidates",
                        status=entity_set.get("status") or "PENDING_REVIEW",
                        parent_ids=json.loads(entity_set["parent_ids_json"] or "[]"),
                        payload=payload,
                        input_snapshot={"action": "refresh_clue_impact"},
                    )
            except Exception:
                pass

        artifact = self._rebuild_clue_set(task_id)

        return {
            "artifact": artifact,
            "clue_count": len(item_ids),
            "retired_count": retired,
            "task": self.get_task(task_id),
        }

    # ----- 线索：模型逐条维护（list_task_clues / put_task_clue / delete_task_clue）-----

    def _active_clue_items(
        self, task_id: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """当前存活线索（CLUE_ITEM，非 STALE/INVALID），按创建先后返回 (artifact, payload)。"""
        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with db_session(self.db_path) as conn:
            arts = _rows(
                conn,
                "SELECT * FROM artifacts WHERE task_id = ? AND type = 'CLUE_ITEM' "
                "AND status NOT IN ('STALE', 'INVALID') ORDER BY created_at ASC",
                (task_id,),
            )
            for art in arts:
                ver = _row(
                    conn,
                    "SELECT payload_json FROM artifact_versions "
                    "WHERE artifact_id = ? AND version = ?",
                    (art["id"], int(art["current_version"])),
                )
                payload = json.loads(ver["payload_json"]) if ver and ver.get("payload_json") else {}
                rows.append((dict(art), payload))
        return rows

    @staticmethod
    def _clue_dedup_key(payload: dict[str, Any]) -> str:
        """同一条“核验问题”的稳定键：优先实体关联/指纹，其次对象，最后标题哈希。"""
        linked = sorted(
            str(x).strip()
            for x in (payload.get("linked_candidate_ids") or [])
            if str(x).strip()
        )
        fps = sorted(TaskService.clue_fingerprint_set(payload))
        if linked:
            return "cand:" + "|".join(linked)
        if fps:
            return "fp:" + "|".join(fps)
        objects = sorted(str(x).strip() for x in (payload.get("objects") or []) if str(x).strip())
        if objects:
            return "obj:" + "|".join(objects)
        title = str(payload.get("title") or "").strip()
        return "title:" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]

    def _retire_active_duplicate(self, task_id: str, row: dict[str, Any]) -> int:
        """追加写入时，把存活线索里同核验维度、同核验问题的旧模型线索作废为本次新条。

        旧条若已被人工处置则拒绝覆盖，避免模型悄悄改掉人工判断。
        """
        payload = row.get("payload") or {}
        aspect = str(payload.get("aspect") or "").upper()
        key = self._clue_dedup_key(payload)
        retired = 0
        for art, existing in self._active_clue_items(task_id):
            if str(existing.get("aspect") or "").upper() != aspect:
                continue
            if self._clue_dedup_key(existing) != key:
                continue
            if clue_is_human_set(existing):
                raise TaskError(
                    TASK_ERROR_CODES["STATE_CONFLICT"],
                    "该对象该核验维度的线索已由人工处置，智能体不能覆盖；如需重新形成请向用户说明",
                )
            self._mark_artifact_stale(task_id, art["id"], reason="由智能体更新，旧条作废")
            retired += 1
        return retired

    def _mark_artifact_stale(self, task_id: str, artifact_id: str, *, reason: str) -> None:
        with db_session(self.db_path) as conn:
            _update(
                conn,
                "artifacts",
                artifact_id,
                {"status": "STALE", "stale_reason": reason, "updated_at": utc_now()},
            )
            _update(conn, "supervision_tasks", task_id, {"updated_at": utc_now()})

    def _rebuild_clue_set(self, task_id: str) -> dict[str, Any]:
        """由当前全部存活 CLUE_ITEM 重建 CLUE_SET 聚合产物（线索中心读它）。"""
        from agents.prompts.clue_writing import CLUE_ASPECTS

        active = self._active_clue_items(task_id)
        items: list[dict[str, Any]] = []
        aspects: set[str] = set()
        batch = self.find_artifact(task_id, "MATERIAL_BATCH", "batch")
        entity_set = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        status = "VALID" if entity_set and entity_set.get("status") == "VALID" else "DRAFT"
        for art, payload in active:
            aspect = str(payload.get("aspect") or "").upper()
            aspects.add(aspect)
            evidence = payload.get("evidence") or []
            items.append(
                {
                    "artifact_id": art["id"],
                    "title": payload.get("title") or art.get("title") or "",
                    "aspect": aspect or "QUAL",
                    "rule_id": payload.get("match_basis") or "AI_CLUE",
                    "case_count": len(
                        {
                            str(ev.get("case_name") or "").strip()
                            for ev in evidence
                            if str(ev.get("case_name") or "").strip()
                        }
                    ),
                    "chunk_count": len(evidence),
                }
            )
        return self.write_artifact(
            task_id=task_id,
            type="CLUE_SET",
            title="跨案关联线索",
            ref_key="ai-clues",
            status=status,
            parent_ids=[batch["id"]] if batch else [],
            payload={
                "summary": {
                    "total": len(items),
                    "producer": "AI_AGENT",
                    "aspects": sorted(a for a in aspects if a in CLUE_ASPECTS),
                },
                "items": items,
                "boundary": "AI 生成的待核验关联线索，不代表系统已认定为同一实体或共同犯罪。",
            },
            input_snapshot={"clue_count": len(items), "action": "rebuild_set"},
        )

    def put_clue_item(
        self,
        task_id: str,
        clue: dict[str, Any],
        *,
        replace_all: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """模型逐条写入：单条追加，或作为“整轮重来”的首条（replace_all=True 作废旧条）。"""
        return self.write_ai_clues(task_id, [clue], user_id=user_id, replace_all=replace_all)

    def delete_clue_item(
        self,
        task_id: str,
        index: int,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """模型删除第 index 条存活线索（序号来自 list_clue_items）。已人工处置的拒绝。"""
        active = self._active_clue_items(task_id)
        if not active:
            raise TaskError(TASK_ERROR_CODES["NOT_FOUND"], "当前没有可删除的线索")
        if index < 0 or index >= len(active):
            raise TaskError(
                TASK_ERROR_CODES["NOT_FOUND"],
                f"序号 {index} 越界：当前存活线索 {len(active)} 条，请先重新 list_task_clues",
            )
        art, payload = active[index]
        if clue_is_human_set(payload):
            raise TaskError(
                TASK_ERROR_CODES["STATE_CONFLICT"],
                "该线索已由人工处置，智能体不能删除；请向用户说明",
            )
        self._mark_artifact_stale(task_id, art["id"], reason="由智能体删除")
        self._rebuild_clue_set(task_id)
        return {
            "ok": True,
            "removed_artifact_id": art["id"],
            "removed_title": payload.get("title") or art.get("title") or "",
            "remaining": len(active) - 1,
            "message": f"已删除线索：{payload.get('title') or art.get('title') or ''}",
        }

    def list_clue_items_for_model(self, task_id: str) -> dict[str, Any]:
        """给模型看的存活线索快照（精简，序号供 delete/覆盖引用）。

        state/confirmed 已合并「人工处置」与「实体复核升格」，模型据此判断线索
        是否已确认；confirmed=true 的线索不得覆盖或删除。
        """
        active = self._active_clue_items(task_id)
        clues: list[dict[str, Any]] = []
        for index, (art, payload) in enumerate(active):
            clues.append(
                {
                    "index": index,
                    "artifact_id": art["id"],
                    "title": (payload.get("title") or art.get("title") or "")[:60],
                    "aspect": str(payload.get("aspect") or "").upper() or "QUAL",
                    "analysis": str(payload.get("analysis") or "")[:80],
                    "objects": (payload.get("objects") or [])[:5],
                    "state": clue_state_label(payload),
                    "confirmed": clue_is_confirmed(payload),
                }
            )
        return {"ok": True, "clue_count": len(clues), "clues": clues}

    def generate_clues(
        self,
        task_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """已下线：规则不再直接落 CLUE_ITEM。请用 list_task_clues / put_task_clue / delete_task_clue 由助手逐条维护。"""
        raise TaskError(
            TASK_ERROR_CODES["STATE_CONFLICT"],
            "旧的规则自动落库方式已停用。请在对话中说明需要重新形成疑似关联线索，由助手按不同核验方面写入线索中心（会替换旧条）。",
        )

    def run_collision(
        self,
        task_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """规则抽取原文标识 → 强碰撞 → 写入实体候选产物。系统不自动合并。"""
        from tools.entities import EXTRACTOR_VERSION, extract_and_collide

        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        # 先按同一人名右扩规则修复已落库残缺脱敏跨度，再抽取
        try:
            from app.files import get_global_mapper

            get_global_mapper(self.db_path).repair_truncated_person_spans(task_id=task_id)
        except Exception:
            pass

        result = extract_and_collide(task_id, task["cases"], db_path=self.db_path)
        existing = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        previous = {}
        if existing:
            previous = self.get_artifact(task_id, existing["id"]).get("payload") or {}

        previous_by_fp = {
            item.get("fingerprint"): item
            for item in (previous.get("candidates") or [])
            if item.get("fingerprint")
        }
        candidates = []
        seen = set()
        for item in previous.get("candidates") or []:
            decision = item.get("decision")
            fingerprint = item.get("fingerprint")
            if decision not in {None, "PENDING", "DEFER"}:
                candidates.append(item)
                if fingerprint:
                    seen.add(fingerprint)
        for item in result["candidates"]:
            fingerprint = item.get("fingerprint")
            if not fingerprint or fingerprint in seen:
                continue
            prior = previous_by_fp.get(fingerprint)
            if prior and prior.get("decision") in {None, "PENDING", "DEFER"}:
                merged = dict(item)
                merged["candidate_id"] = prior.get("candidate_id") or merged.get("candidate_id")
                merged["decision"] = prior.get("decision") or "PENDING"
                merged["reason"] = prior.get("reason") or ""
                merged["correction"] = prior.get("correction")
                candidates.append(merged)
            else:
                candidates.append(item)
            seen.add(fingerprint)
        pending = sum(1 for item in candidates if item.get("decision") == "PENDING")
        batch = self.find_artifact(task_id, "MATERIAL_BATCH", "batch")
        analysis_gate = "ENTITY_REVIEW" if pending else ""
        artifact = self.write_artifact(
            task_id=task_id,
            type="ENTITY_CANDIDATE_SET",
            title="跨案对象待核·待判断" if pending else "跨案对象待核·已完成",
            ref_key="entity-candidates",
            status="PENDING_REVIEW" if pending else "VALID",
            parent_ids=[batch["id"]] if batch else [],
            payload={
                "summary": {
                    "total": len(candidates),
                    "pending": pending,
                    "reviewed": len(candidates) - pending,
                    "mention_count": result["mention_count"],
                    "scanned_chunks": result["scanned_chunks"],
                    "mode": "deterministic",
                    "extractor_version": EXTRACTOR_VERSION,
                    "analysis_gate": analysis_gate,
                },
                "mentions": result["mentions"],
                "candidates": candidates,
                "subject_resolve": previous.get("subject_resolve")
                or {"subjects": {}, "surface_index": {}, "keep_separate": []},
                "boundary": "强标识等值与疑似化名均为待核验候选。系统不自动合并，是否同一对象由人工决定。",
                "analysis_gate": analysis_gate,
            },
            input_snapshot={
                "extractor_version": EXTRACTOR_VERSION,
                "exclusion_version": result.get("exclusion_version"),
                "alias_seed_count": result.get("alias_seed_count"),
                "suspect_count": result.get("suspect_count"),
                "case_ids": [item["case_id"] for item in task["cases"]],
            },
        )
        return {
            "artifact": artifact,
            "candidate_count": len(candidates),
            "mention_count": result["mention_count"],
            "analysis_gate": analysis_gate,
            "task": self.get_task(task_id),
        }

    def run_role_timeline(
        self,
        task_id: str,
        user_id: str | None = None,
        *,
        enrich: bool = True,
    ) -> dict[str, Any]:
        """先把转账/联络事件落成可核验产物，再可选做角色表述增强。"""
        from tools.entities import EVENT_EXTRACTOR_VERSION, apply_subject_resolve, extract_task_events
        from tools.timeline_subjects import pick_timeline_subject_refs, timeline_event_sort_key

        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        try:
            from app.files import get_global_mapper

            get_global_mapper(self.db_path).repair_truncated_person_spans(task_id=task_id)
        except Exception:
            pass

        result = extract_task_events(
            task_id,
            [item["case_id"] for item in task["cases"]],
            db_path=self.db_path,
        )
        batch = self.find_artifact(task_id, "MATERIAL_BATCH", "batch")
        resolve = self.get_subject_resolve(task_id)
        counts: dict[str, int] = {}
        dated = 0
        undated = 0
        case_names = {item["case_id"]: item.get("display_name") or item["case_id"] for item in task["cases"]}
        items = []
        for event in result["events"]:
            parties = apply_subject_resolve(event.get("parties") or [], resolve)
            refs = pick_timeline_subject_refs(parties)
            person = refs.get("person")
            account = refs.get("account")
            # 人物主、账户辅；无人物时账户可作主体。绝不回退案件名。
            if person:
                subject_kind = "PERSON"
                subject_id = person.get("subject_id") or ""
                subject = person.get("display_name") or person.get("surface") or ""
            elif account:
                subject_kind = "ACCOUNT"
                subject_id = account.get("subject_id") or ""
                subject = account.get("display_name") or account.get("surface") or ""
            else:
                continue
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
            time_uncertain = event.get("time_precision") == "UNKNOWN" or not event.get("time_text")
            if time_uncertain:
                undated += 1
            else:
                dated += 1
            summary = (event.get("summary_text") or "").strip()
            if summary:
                role_or_action = summary[:80]
            elif event["event_type"] == "TRANSFER":
                amt = (event.get("amount_text") or "").strip()
                role_or_action = f"转账记载{('：' + amt) if amt else ''}"
            else:
                role_or_action = "联络记载"
            case_name = case_names.get(event["case_id"]) or event["case_id"]
            time_text = event.get("time_text") or ""
            items.append(
                {
                    "event_id": event["event_id"],
                    "title": "转账事件" if event["event_type"] == "TRANSFER" else "联络事件",
                    "event_type": event["event_type"],
                    "time_text": time_text,
                    "event_time": time_text,
                    "time_precision": event.get("time_precision") or "UNKNOWN",
                    "time_uncertain": time_uncertain,
                    "amount_text": event.get("amount_text") or "",
                    "channel": event.get("channel") or "",
                    "summary_text": event.get("summary_text") or "",
                    "role_or_action": role_or_action,
                    "parties": parties,
                    "subject_kind": subject_kind,
                    "subject_id": subject_id,
                    "subject": subject,
                    "person_subject_id": (person or {}).get("subject_id") or "",
                    "person_subject": (person or {}).get("display_name")
                    or (person or {}).get("surface")
                    or "",
                    "account_subject_id": (account or {}).get("subject_id") or "",
                    "account_subject": (account or {}).get("display_name")
                    or (account or {}).get("surface")
                    or "",
                    "objects": refs.get("objects") or [],
                    "case_id": event["case_id"],
                    "case_name": case_name,
                    "cases": [case_name],
                    "source_mode": "recorded",
                    "conflict_with": [],
                    "source": {
                        "document_id": event.get("document_id"),
                        "document_version_id": event.get("document_version_id"),
                        "chunk_id": event.get("chunk_id"),
                        "filename": event.get("filename"),
                        "page_start": event.get("page_start"),
                        "page_end": event.get("page_end"),
                        "quote": event.get("quote") or "",
                        "quote_hash": event.get("quote_hash") or "",
                    },
                }
            )

        enrich_meta: dict[str, Any] = {
            "applied": False,
            "enriched_count": 0,
            "inferred_count": 0,
            "rejected_inferred": 0,
            "skipped": not enrich,
        }
        if enrich:
            try:
                from agents.timeline_enrich_agent import enrich_role_timeline_items

                enrich_result = enrich_role_timeline_items(items, task_id=task_id)
                if enrich_result.get("ok") and not enrich_result.get("fallback"):
                    items = enrich_result.get("items") or items
                    enrich_meta = enrich_result.get("enrich_meta") or enrich_meta
                    enrich_meta["skipped"] = False
            except Exception:
                pass

        items.sort(key=timeline_event_sort_key)
        artifact = self.write_artifact(
            task_id=task_id,
            type="ROLE_TIMELINE",
            title="角色时间线·转账与联络事件",
            ref_key="role-timeline",
            status="VALID",
            parent_ids=[batch["id"]] if batch else [],
            payload={
                "summary": {
                    "total": len(items),
                    "dated": dated,
                    "undated": undated,
                    "types": counts,
                    "scanned_chunks": result["scanned_chunks"],
                    "extractor_version": EVENT_EXTRACTOR_VERSION,
                    "enrich": enrich_meta,
                },
                "items": items,
                "subject_resolve_applied": True,
                "subject_policy": {
                    "primary": "PERSON",
                    "secondary": "ACCOUNT",
                    "exclude": ["CASE", "ORGANIZATION", "MERCHANT"],
                },
                "boundary": "这里只记录材料中出现的转账/联络事件及标注的系统推测，供核验角色与行为记载；当前不直接生成共同犯罪或控制关系结论。",
            },
            input_snapshot={
                "case_ids": [item["case_id"] for item in task["cases"]],
                "extractor_version": EVENT_EXTRACTOR_VERSION,
            },
        )
        return {
            "artifact": artifact,
            "event_count": len(items),
            "task": self.get_task(task_id),
        }

    def query_role_timeline(
        self,
        task_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        include_uncertain: bool = True,
        event_type: str | None = None,
        source_mode: str | None = None,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        case_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """读取已有 ROLE_TIMELINE 产物并按条件投影；不重抽。"""
        from tools.timeline_subjects import (
            build_timeline_facets,
            filter_timeline_items,
            parse_csv_set,
            timeline_event_sort_key,
        )

        self.get_task(task_id)
        art = self.find_artifact(task_id, "ROLE_TIMELINE", "role-timeline")
        if not art:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "尚未生成角色时间线")
        detail = self.get_artifact(task_id, art["id"])
        payload = detail.get("payload") or {}
        all_items = list(payload.get("items") or [])
        facets = build_timeline_facets(all_items)
        filtered = filter_timeline_items(
            all_items,
            date_from=date_from,
            date_to=date_to,
            include_uncertain=include_uncertain,
            event_types=parse_csv_set(event_type),
            source_modes=parse_csv_set(source_mode),
            subject_kind=subject_kind,
            subject_id=subject_id,
            case_id=case_id,
        )
        filtered.sort(key=timeline_event_sort_key)
        return {
            "artifact_id": art["id"],
            "summary": payload.get("summary") or {},
            "facets": facets,
            "filters": {
                "date_from": date_from or "",
                "date_to": date_to or "",
                "include_uncertain": include_uncertain,
                "event_type": event_type or "",
                "source_mode": source_mode or "",
                "subject_kind": subject_kind or "",
                "subject_id": subject_id or "",
                "case_id": case_id or "",
            },
            "total": len(all_items),
            "matched": len(filtered),
            "items": filtered,
            "boundary": payload.get("boundary"),
        }

    def material_overview(self, task_id: str, user_id: str | None = None) -> dict[str, Any]:
        """按案件分组的材料处理进度：阶段用页数或状态表达，不造伪百分比。"""
        task = self.get_task(task_id)
        service = get_material_service()
        groups = []
        totals = {"documents": 0, "ready": 0, "attention": 0}
        for case in task["cases"]:
            try:
                materials = service.list_materials(case["case_id"], user_id=user_id)
            except Exception as exc:  # 授权未开或案件无材料时不阻塞工作台
                groups.append(
                    {
                        "case_id": case["case_id"],
                        "case_name": case["display_name"],
                        "materials": [],
                        "note": str(exc),
                    }
                )
                continue

            rows = []
            for item in materials:
                quality = item.get("quality_summary") or {}
                low_pages = quality.get("low_confidence_pages") or []
                status = item.get("status") or "UPLOADED"
                totals["documents"] += 1
                if status == "PARSED" and not low_pages:
                    totals["ready"] += 1
                if status in {"NEEDS_OCR_REVIEW", "OCR_FAILED", "FAILED"} or low_pages:
                    totals["attention"] += 1
                current = item.get("current_version") or {}
                material_type = (
                    quality.get("material_type")
                    or infer_material_type(item.get("filename") or "")
                )
                version_no = current.get("version_no") or item.get("version_count") or 1
                # 有脱敏条目或已解析完成，视为已脱敏（后续可由 Agent/人工覆写）
                redacted = bool(quality.get("redacted"))
                if not redacted and status == "PARSED":
                    redacted = True
                uploaded_at = item.get("created_at") or ""
                if uploaded_at:
                    uploaded_at = str(uploaded_at).replace("T", " ")[:16]
                rows.append(
                    {
                        "document_id": item.get("id"),
                        "filename": item.get("filename"),
                        "size": item.get("size"),
                        "content_type": item.get("content_type"),
                        "status": status,
                        "stage_label": MATERIAL_STAGE_LABELS.get(status, status),
                        "page_count": quality.get("page_count"),
                        "low_confidence_pages": low_pages,
                        "version_count": item.get("version_count"),
                        "version": version_no,
                        "parser_version": current.get("parser_version") or "",
                        "material_type": material_type,
                        "doc_type": material_type,
                        "uploaded_at": uploaded_at,
                        "created_at": item.get("created_at"),
                        "redacted": redacted,
                    }
                )
            groups.append(
                {
                    "case_id": case["case_id"],
                    "case_name": case["display_name"],
                    "materials": rows,
                }
            )
        return {"groups": groups, "totals": totals, "parser_target": PARSER_VERSION}

    def refresh_material_batch(self, task_id: str, user_id: str | None = None) -> dict[str, Any]:
        self._sync_material_doc_artifacts(task_id)
        scope = self.find_artifact(task_id, "TASK_SCOPE", "scope")
        return self.write_artifact(
            task_id=task_id,
            type="MATERIAL_BATCH",
            title="材料接入与质量",
            ref_key="batch",
            status="VALID",
            parent_ids=[scope["id"]] if scope else [],
            payload=self.material_overview(task_id, user_id=user_id),
        )

    def save_message(self, task_id: str, role: str, content: str, tool_call_id: str | None = None, metadata: dict | list | None = None) -> dict:
        """保存单条聊天消息到数据库"""
        with db_session(self.db_path) as conn:
            msg = {
                "id": new_id(),
                "task_id": task_id,
                "role": role,
                "content": content or "",
                "tool_call_id": tool_call_id or None,
                "created_at": utc_now(),
                "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),  # 注意：列表也会被正确序列化
            }
            _insert(conn, "chat_messages", msg)
        return msg

    def get_messages(self, task_id: str) -> list[dict]:
        """获取某个任务的所有聊天消息（按时间升序）"""
        with db_session(self.db_path) as conn:
            return _rows(
                conn,
                "SELECT id, role, content, tool_call_id, created_at, metadata_json FROM chat_messages "
                "WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            )

    def ensure_task_system_prompt(self, task_id: str) -> None:
        """保证任务会话使用当前 TASK_AGENT_PROMPT（旧任务首句写入后不会自动升级）。"""
        from app.config import TASK_AGENT_PROMPT

        rows = self.get_messages(task_id)
        system_rows = [row for row in rows if row.get("role") == "system"]
        if not system_rows:
            # 插到时间序最前，避免补写后落到对话末尾被模型当成“最后一句”
            with db_session(self.db_path) as conn:
                msg = {
                    "id": new_id(),
                    "task_id": task_id,
                    "role": "system",
                    "content": TASK_AGENT_PROMPT,
                    "tool_call_id": None,
                    "created_at": "1970-01-01T00:00:00+00:00" if rows else utc_now(),
                    "metadata_json": json.dumps({}, ensure_ascii=False),
                }
                _insert(conn, "chat_messages", msg)
            return
        # 优先更新已是主提示词的那条；否则更新时间序第一条
        primary = next(
            (
                row
                for row in system_rows
                if (row.get("content") or "").strip().startswith("你是「链证智析」")
                or "办案风" in (row.get("content") or "")
                or len(row.get("content") or "") > 200
            ),
            system_rows[0],
        )
        content = primary.get("content") or ""
        if content.strip() == TASK_AGENT_PROMPT.strip():
            return
        with db_session(self.db_path) as conn:
            _update(conn, "chat_messages", primary["id"], {"content": TASK_AGENT_PROMPT})

    def repair_chat_messages(self, task_id: str) -> list[dict]:
        """删除不成对的 tool_calls 副本，返回可发给模型的历史。"""
        from app.chat_history import row_to_llm_message, sanitize_tool_history, strip_internal_fields

        self.ensure_task_system_prompt(task_id)
        rows = self.get_messages(task_id)
        messages = [row_to_llm_message(row) for row in rows]
        kept = sanitize_tool_history(messages)
        keep_ids = {msg["_row_id"] for msg in kept if msg.get("_row_id")}
        drop_ids = [row["id"] for row in rows if row["id"] not in keep_ids]
        if drop_ids:
            with db_session(self.db_path) as conn:
                conn.executemany(
                    "DELETE FROM chat_messages WHERE id = ?",
                    [(message_id,) for message_id in drop_ids],
                )
        # system 始终置顶，避免历史补写或乱序影响模型；多条时取最长主提示
        systems = [m for m in kept if m.get("role") == "system"]
        others = [m for m in kept if m.get("role") != "system"]
        primary = (
            max(systems, key=lambda m: len(m.get("content") or "")) if systems else None
        )
        ordered = ([primary] if primary else []) + others
        return strip_internal_fields(ordered)
    # ----- 内部 -----

    def _scope_payload(self, task_id: str) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            task = _row(conn, "SELECT * FROM supervision_tasks WHERE id = ?", (task_id,))
            cases = _rows(conn, "SELECT * FROM task_cases WHERE task_id = ?", (task_id,))
        return {
            "title": task["title"],
            "purpose": task["purpose"],
            "authorized_until": task["authorized_until"],
            "note": task["note"],
            "cases": cases,
        }

    def _live_document_ids(self, task_id: str) -> set[str]:
        with db_session(self.db_path) as conn:
            rows = _rows(
                conn,
                """
                SELECT d.id FROM documents d
                JOIN task_cases tc ON tc.case_id = d.case_id
                WHERE tc.task_id = ? AND d.deleted_at IS NULL
                """,
                (task_id,),
            )
        return {row["id"] for row in rows}

    def _sync_material_doc_artifacts(self, task_id: str) -> None:
        """已删材料对应的 MATERIAL_DOC 作废，保证任务目录与批次清单一致。"""
        live = self._live_document_ids(task_id)
        with db_session(self.db_path) as conn:
            stale = _rows(
                conn,
                """
                SELECT * FROM artifacts
                WHERE task_id = ? AND type = 'MATERIAL_DOC' AND status != 'INVALID'
                """,
                (task_id,),
            )
        for art in stale:
            doc_id = art.get("ref_key")
            if doc_id and doc_id not in live:
                self.write_artifact(
                    task_id=task_id,
                    type="MATERIAL_DOC",
                    title=art.get("title") or "材料",
                    ref_key=doc_id,
                    status="INVALID",
                    payload={
                        "document_id": doc_id,
                        "status": "DELETED",
                        "deleted": True,
                    },
                )

    def _build_directory(
        self,
        artifacts: list[dict[str, Any]],
        live_doc_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        directory = []
        for group in DIRECTORY_GROUPS:
            items = []
            for a in artifacts:
                if a["type"] not in group["types"] or a.get("status") == "INVALID":
                    continue
                if a["type"] == "MATERIAL_DOC" and live_doc_ids is not None:
                    if a.get("ref_key") not in live_doc_ids:
                        continue
                items.append(a)
            directory.append(
                {
                    "key": group["key"],
                    "label": group["label"],
                    "items": [
                        {
                            "artifact_id": a["id"],
                            "type": a["type"],
                            "title": a["title"],
                            "status": a["status"],
                            "version": a["current_version"],
                        }
                        for a in items
                    ],
                    "pending": len(items) == 0,
                }
            )
        return directory


MATERIAL_STAGE_LABELS = {
    "UPLOADED": "排队中",
    "PARSING": "解析中",
    "PARSED": "可用于分析",
    "NEEDS_OCR_REVIEW": "扫描件需人工看清",
    "OCR_FAILED": "文字识别失败",
    "DUPLICATE_PENDING": "重复待处理",
    "FAILED": "解析失败",
    "DELETED": "已删除",
}


_task_service: TaskService | None = None

def get_task_service() -> TaskService:
    global _task_service
    if _task_service is None:
        _task_service = TaskService()
    return _task_service
