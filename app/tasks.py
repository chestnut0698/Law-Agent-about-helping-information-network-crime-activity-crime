"""监督分析任务与产物模型。

一个"监督分析任务"是长期业务容器：绑定案件范围、监督目的、有效期、材料与全部过程产物。
产物是任务里可以单独打开、单独引用、单独留版本的工作结果；任务目录、智能体消息中的链接、
中间工作区标签三者共用同一个 artifact_id，禁止出现两套状态。

本文件按注释分段：状态与错误码、建表、产物依赖与过期传播、TaskService 业务编排。
数据库连接、案件与材料读取直接复用 tools/files.py，不另起一套持久化。
"""

from __future__ import annotations

import json, os
from typing import Any, Optional
import hashlib
from pathlib import Path
from app.config import MATERIAL_STORAGE_DIR, REDACTION_STORAGE_DIR

from app.files import (
    MaterialError,
    MaterialService,
    _insert,
    _row,
    _rows,
    _update,
    db_session,
    ensure_demo_case,
    get_material_service,
    init_db,
    list_chunks,
    new_id,
    utc_now,
)
from tools.entities import init_entity_db

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
            if len(records) < 2:
                continue
            normalized.append(
                {
                    "candidate_id": item.get("candidate_id") or new_id(),
                    "entity_type": item.get("entity_type") or "OTHER",
                    "display_name": item.get("display_name") or f"候选 {index + 1}",
                    "confidence_label": item.get("confidence_label") or "待核验",
                    "match_basis": item.get("match_basis") or [],
                    "differences": item.get("differences") or [],
                    "records": records,
                    "impact": item.get("impact") or {},
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
        for candidate in payload.get("candidates", []):
            if candidate.get("candidate_id") == candidate_id:
                candidate["decision"] = decision
                candidate["reason"] = reason.strip()
                candidate["correction"] = correction
                candidate["reviewed_at"] = utc_now()
                found = candidate
                break
        if not found:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "待核对象不存在")

        if decision == "KEEP_SEPARATE" and found.get("fingerprint"):
            from tools.entities import remember_rejection

            remember_rejection(
                task_id,
                found["fingerprint"],
                decision,
                reason.strip(),
                db_path=self.db_path,
            )

        candidates = payload.get("candidates", [])
        reviewed = sum(1 for item in candidates if item.get("decision") != "PENDING")
        pending = len(candidates) - reviewed
        payload["summary"] = {
            **(payload.get("summary") or {}),
            "total": len(candidates),
            "reviewed": reviewed,
            "pending": pending,
        }
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
        return {"artifact": artifact, "task": self.get_task(task_id)}

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
                "boundary": "留痕仅记录人工核验与回链操作，不构成法律结论。",
            },
        )

    def build_report_draft(self, task_id: str) -> dict[str, Any]:
        """汇总范围、实体、线索为跨案关联线索核验单草稿。"""
        task = self.get_task(task_id)
        scope = None
        entity = None
        clues: list[dict[str, Any]] = []
        for art in task.get("artifacts") or []:
            if art["type"] == "TASK_SCOPE":
                scope = self.get_artifact(task_id, art["id"])
            elif art["type"] == "ENTITY_CANDIDATE_SET" and art["status"] not in {"STALE", "INVALID"}:
                entity = self.get_artifact(task_id, art["id"])
            elif art["type"] == "CLUE_ITEM" and art["status"] not in {"STALE", "INVALID"}:
                clues.append(self.get_artifact(task_id, art["id"]))

        boundary = (
            "本文件仅汇集跨案关联候选、待核验事项及材料原文依据，"
            "不构成对犯罪事实、人员责任、证据能力、证明力或证明标准的认定。"
        )
        invalid_refs = 0
        clue_lines: list[str] = []
        for item in clues:
            payload = item.get("payload") or {}
            evidence = payload.get("evidence") or []
            if not evidence:
                invalid_refs += 1
            clue_lines.append(
                f"- {payload.get('title') or item['artifact']['title']} "
                f"（状态：{payload.get('disposition') or 'PENDING'}；"
                f"支持材料 {len(evidence)} 处）"
            )

        entity_payload = (entity or {}).get("payload") or {}
        candidates = entity_payload.get("candidates") or []
        confirmed = sum(1 for c in candidates if c.get("decision") == "MERGE")
        separated = sum(1 for c in candidates if c.get("decision") == "KEEP_SEPARATE")
        pending = sum(1 for c in candidates if c.get("decision", "PENDING") == "PENDING")

        scope_payload = (scope or {}).get("payload") or {}
        case_names = "、".join(
            c.get("display_name") or c.get("name") or ""
            for c in (scope_payload.get("cases") or task.get("cases") or [])
        ) or "—"

        markdown = "\n".join(
            [
                "# 跨案关联线索核验单",
                "",
                f"**任务**：{task.get('title') or '—'}",
                f"**生成时间**：{utc_now()}",
                "",
                "## 边界声明",
                boundary,
                "",
                "## 1. 分析范围概览",
                f"- 监督目的：{task.get('purpose') or scope_payload.get('purpose') or '—'}",
                f"- 授权有效期：{task.get('authorized_until') or scope_payload.get('authorized_until') or '—'}",
                f"- 案件范围：{case_names}",
                "",
                "## 2. 实体处理摘要",
                f"- 候选总数：{len(candidates)}",
                f"- 视为同一：{confirmed} · 不是同一：{separated} · 待核：{pending}",
                "",
                "## 3. 待核验线索清单",
                *(clue_lines or ["- （暂无线索）"]),
                "",
                "## 4. 有效性",
                (
                    "- 存在缺少原文依据的线索，正式导出前需补证或排除。"
                    if invalid_refs
                    else "- 当前纳入线索均可回链，可导出。"
                ),
                "",
            ]
        )
        valid = invalid_refs == 0 and len(clues) > 0
        parent_ids = []
        if scope:
            parent_ids.append(scope["artifact"]["id"])
        if entity:
            parent_ids.append(entity["artifact"]["id"])
        parent_ids.extend(c["artifact"]["id"] for c in clues)

        artifact = self.write_artifact(
            task_id=task_id,
            type="REPORT_DRAFT",
            title="跨案关联线索核验单（草稿）",
            ref_key="report-draft",
            status="VALID" if valid else "PENDING_REVIEW",
            parent_ids=parent_ids,
            payload={
                "title": "跨案关联线索核验单",
                "markdown": markdown,
                "text": markdown,
                "valid": valid,
                "invalid_refs": invalid_refs,
                "clue_count": len(clues),
                "boundary": boundary,
                "generated_at": utc_now(),
            },
        )
        self.append_source_verify(
            task_id,
            {
                "action": "生成报告草稿",
                "type": "report_draft",
                "target": artifact["id"],
                "summary": f"线索 {len(clues)} 条 · {'有效' if valid else '含待补证'}",
                "result": "ok" if valid else "warn",
                "at": utc_now(),
            },
        )
        return {"artifact": artifact, "task": self.get_task(task_id), "valid": valid}

    # ----- 产物 -----

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
        return {
            "artifact": artifact,
            "version": target,
            "status": artifact["status"],
            "payload": json.loads(row["payload_json"]) if row else {},
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


    def write_ai_clues(
            self,
            task_id: str,
            clues: list[dict[str, Any]],  # 注意：clues 是列表，不是字典
            user_id: str | None = None,
    ) -> dict[str, Any]:
        """
        AI 通过工具调用写入线索。clues 格式：
        [
            {
                "title": "线索标题",
                "summary": "线索描述（200字内）",
                "evidence": [
                    {
                        "chunk_id": "xxx",
                        "quote": "脱敏原文片段",
                        "quote_hash": "sha256",
                        "page_start": 1,
                        "page_end": 1,
                        "document_version_id": "xxx"
                    }
                ]
            }
        ]
        """
        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        if not clues:
            return {"artifact": None, "clue_count": 0, "message": "没有线索可写入"}

        # 校验每条线索
        item_ids = []
        for idx, clue in enumerate(clues):
            title = clue.get("title", "").strip()
            summary = clue.get("summary", "").strip()
            evidence = clue.get("evidence", [])

            if not title:
                raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], f"第 {idx + 1} 条线索缺少 title")
            if not summary:
                raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], f"第 {idx + 1} 条线索缺少 summary")
            if not evidence:
                raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], f"第 {idx + 1} 条线索至少需要一条 evidence")

            # 校验每条 evidence
            for ev in evidence:
                if not ev.get("chunk_id"):
                    raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"], f"第 {idx + 1} 条线索的 evidence 缺少 chunk_id")
                if not ev.get("quote_hash"):
                    raise TaskError(TASK_ERROR_CODES["INVALID_SCOPE"],
                                    f"第 {idx + 1} 条线索的 evidence 缺少 quote_hash")

            # 生成唯一的 ref_key（不使用 canonical_hash）
            hash_input = f"{title}{summary}".encode("utf-8")
            short_hash = hashlib.sha256(hash_input).hexdigest()[:16]
            ref_key = f"ai-clue:{short_hash}"

            # 创建独立的 CLUE_ITEM 产物
            clue_item = self.write_artifact(
                task_id=task_id,
                type="CLUE_ITEM",
                title=title,
                ref_key=ref_key,
                status="VALID",
                parent_ids=[],  # 稍后由 CLUE_SET 统一关联
                payload={
                    "title": title,
                    "summary": summary,
                    "evidence": evidence,
                    "producer": "AI_AGENT",
                    "rule_id": "AI_CLUE",
                },
                input_snapshot={"clue_index": idx},
            )
            item_ids.append(clue_item["id"])

        # 获取批次作为父产物
        batch = self.find_artifact(task_id, "MATERIAL_BATCH", "batch")

        # 创建 CLUE_SET 产物，items 中携带 artifact_id
        clue_set_payload = {
            "summary": {
                "total": len(item_ids),
                "producer": "AI_AGENT",
            },
            "items": [
                {
                    "artifact_id": aid,
                    "title": clue["title"],  # 可直接取第一条，但最好遍历
                    "rule_id": "AI_CLUE",
                    "case_count": len(clue.get("evidence", [])),
                    "chunk_count": len(clue.get("evidence", [])),
                }
                for aid, clue in zip(item_ids, clues)
            ],
            "boundary": "AI 生成的待核验关联线索，不代表系统已认定为同一实体或共同犯罪。",
        }

        artifact = self.write_artifact(
            task_id=task_id,
            type="CLUE_SET",
            title="AI 生成的跨案线索",
            ref_key="ai-clues",  # 固定 ref_key，确保覆盖旧版本
            status="VALID" if item_ids else "DRAFT",
            parent_ids=[batch["id"]] if batch else [],
            payload=clue_set_payload,
            input_snapshot={"clue_count": len(item_ids)},
        )

        return {
            "artifact": artifact,
            "clue_count": len(item_ids),
            "task": self.get_task(task_id),
        }

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
                },
                "mentions": result["mentions"],
                "candidates": candidates,
                "boundary": "强标识等值仅为待核验候选。系统不自动合并，是否同一对象由人工决定。",
            },
            input_snapshot={
                "extractor_version": EXTRACTOR_VERSION,
                "exclusion_version": result.get("exclusion_version"),
                "case_ids": [item["case_id"] for item in task["cases"]],
            },
        )
        return {
            "artifact": artifact,
            "candidate_count": len(candidates),
            "mention_count": result["mention_count"],
            "task": self.get_task(task_id),
        }

    def generate_clues(
        self,
        task_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """R001–R005 规则命中 → 模板化线索表述 → 落 CLUE 产物（不经 LLM 改写）。"""
        from tools.entities import LEGAL_BOUNDARY, collect_rule_hits

        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        hits = collect_rule_hits(task_id, task["cases"], db_path=self.db_path)
        existing_set = self.find_artifact(task_id, "CLUE_SET", "clues")
        existing_fps = set()
        if existing_set:
            prev = self.get_artifact(task_id, existing_set["id"]).get("payload") or {}
            existing_fps = {
                item.get("fingerprint")
                for item in (prev.get("items") or [])
                if item.get("fingerprint")
            }

        created = []
        skipped = []
        parent = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        timeline = self.find_artifact(task_id, "ROLE_TIMELINE", "role-timeline")
        parent_ids = []
        if parent:
            parent_ids.append(parent["id"])
        if timeline:
            parent_ids.append(timeline["id"])

        for hit in hits:
            fingerprint = hit.get("fingerprint")
            if fingerprint in existing_fps:
                skipped.append({"fingerprint": fingerprint, "reason": "duplicate"})
                continue
            evidence = hit.get("evidence") or []
            case_ids = {item.get("case_id") for item in evidence if item.get("case_id")}
            chunk_ids = {item.get("chunk_id") for item in evidence if item.get("chunk_id")}
            if len(case_ids) < 2 or len(chunk_ids) < 2:
                skipped.append({"fingerprint": fingerprint, "reason": "below_cross_case_threshold"})
                continue

            case_names = [
                c.get("case_name") or c.get("case_id") or ""
                for c in (hit.get("cases") or [])
            ]
            title = hit.get("label") or f"{hit.get('rule_id') or '规则'}跨案命中"
            summary = (
                f"规则 {hit.get('rule_id') or ''} 在 { '、'.join([n for n in case_names if n]) or '多案' } "
                f"命中可回链证据 {len(evidence)} 处。标识重合仅为待核验线索，须打开原文核对。"
            )
            item_payload = {
                "title": title,
                "summary": summary,
                "rule_id": hit.get("rule_id"),
                "rule_version": hit.get("rule_version"),
                "evidence_mode": hit.get("evidence_mode") or "DIRECT_MATERIAL",
                "cases": hit.get("cases") or [],
                "evidence": evidence,
                "generation": "rule+template",
                "uncertainty": "标识重合仅为待核验线索，不代表同一人、同一账户控制关系或共同犯罪。",
                "boundary": LEGAL_BOUNDARY,
                "fingerprint": fingerprint,
                "degraded": False,
            }
            artifact = self.write_artifact(
                task_id=task_id,
                type="CLUE_ITEM",
                title=item_payload["title"],
                ref_key=f"clue:{fingerprint[:16]}",
                status="VALID",
                parent_ids=parent_ids,
                payload=item_payload,
                input_snapshot={"rule_id": hit.get("rule_id"), "fingerprint": fingerprint},
            )
            created.append(
                {
                    "artifact_id": artifact["id"],
                    "title": item_payload["title"],
                    "rule_id": hit.get("rule_id"),
                    "fingerprint": fingerprint,
                    "case_count": len(case_ids),
                    "chunk_count": len(chunk_ids),
                }
            )
            existing_fps.add(fingerprint)

        all_items = created[:]
        if existing_set:
            prev = self.get_artifact(task_id, existing_set["id"]).get("payload") or {}
            for item in prev.get("items") or []:
                if item.get("fingerprint") not in {c["fingerprint"] for c in created}:
                    all_items.append(item)

        clue_set = self.write_artifact(
            task_id=task_id,
            type="CLUE_SET",
            title="关联线索",
            ref_key="clues",
            status="VALID" if all_items else "DRAFT",
            parent_ids=parent_ids,
            payload={
                "summary": {
                    "total": len(all_items),
                    "created": len(created),
                    "skipped": len(skipped),
                },
                "items": all_items,
                "skipped": skipped,
                "boundary": LEGAL_BOUNDARY,
            },
        )
        return {
            "artifact": clue_set,
            "created": created,
            "skipped": skipped,
            "hit_count": len(hits),
            "task": self.get_task(task_id),
        }

    def run_role_timeline(
        self,
        task_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """先把转账/联络事件落成可核验产物，为后续 R004/R005 提供事实层。"""
        from tools.entities import EVENT_EXTRACTOR_VERSION, extract_task_events

        task = self.get_task(task_id)
        if task["status"] == "SCOPE_DRAFT":
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "计划尚未确认")

        result = extract_task_events(
            task_id,
            [item["case_id"] for item in task["cases"]],
            db_path=self.db_path,
        )
        batch = self.find_artifact(task_id, "MATERIAL_BATCH", "batch")
        counts: dict[str, int] = {}
        dated = 0
        undated = 0
        case_names = {item["case_id"]: item.get("display_name") or item["case_id"] for item in task["cases"]}
        items = []
        for event in result["events"]:
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
            if event.get("time_precision") == "UNKNOWN" or not event.get("time_text"):
                undated += 1
            else:
                dated += 1
            items.append(
                {
                    "event_id": event["event_id"],
                    "title": "转账事件" if event["event_type"] == "TRANSFER" else "联络事件",
                    "event_type": event["event_type"],
                    "time_text": event.get("time_text") or "",
                    "time_precision": event.get("time_precision") or "UNKNOWN",
                    "amount_text": event.get("amount_text") or "",
                    "channel": event.get("channel") or "",
                    "summary_text": event.get("summary_text") or "",
                    "parties": event.get("parties") or [],
                    "case_id": event["case_id"],
                    "case_name": case_names.get(event["case_id"]) or event["case_id"],
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
        items.sort(
            key=lambda item: (
                1 if item.get("time_precision") == "UNKNOWN" or not item.get("time_text") else 0,
                item.get("time_text") or "9999",
                item.get("case_name") or "",
            )
        )
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
                },
                "items": items,
                "boundary": "这里只记录材料中出现的转账/联络事件，供后续资金路径与共同联系人规则使用；当前不直接生成共同犯罪或控制关系结论。",
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
                    }
                )
            groups.append(
                {
                    "case_id": case["case_id"],
                    "case_name": case["display_name"],
                    "materials": rows,
                }
            )
        return {"groups": groups, "totals": totals}

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

    def repair_chat_messages(self, task_id: str) -> list[dict]:
        """删除不成对的 tool_calls 副本，返回可发给模型的历史。"""
        from app.chat_history import row_to_llm_message, sanitize_tool_history, strip_internal_fields

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
        return strip_internal_fields(kept)
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
