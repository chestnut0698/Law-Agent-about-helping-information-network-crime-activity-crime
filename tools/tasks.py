"""监督分析任务与产物模型。

一个"监督分析任务"是长期业务容器：绑定案件范围、监督目的、有效期、材料与全部过程产物。
产物是任务里可以单独打开、单独引用、单独留版本的工作结果；任务目录、智能体消息中的链接、
中间工作区标签三者共用同一个 artifact_id，禁止出现两套状态。

本文件按注释分段：状态与错误码、建表、产物依赖与过期传播、TaskService 业务编排。
数据库连接、案件与材料读取直接复用 tools/files.py，不另起一套持久化。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from tools.files import (
    _insert,
    _row,
    _rows,
    _update,
    db_session,
    ensure_demo_case,
    get_material_service,
    init_db,
    new_id,
    utc_now,
)

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
    {"key": "scope", "label": "概览", "types": ["TASK_SCOPE"]},
    {"key": "materials", "label": "材料", "types": ["MATERIAL_BATCH", "MATERIAL_DOC"]},
    {"key": "entities", "label": "实体候选", "types": ["ENTITY_CANDIDATE_SET"]},
    {"key": "clues", "label": "关联线索", "types": ["CLUE_SET", "CLUE_ITEM"]},
    {"key": "views", "label": "时间线 / 图谱", "types": ["ROLE_TIMELINE", "LINK_GRAPH"]},
    {"key": "verify", "label": "核验记录", "types": ["SOURCE_VERIFY"]},
    {"key": "reports", "label": "报告版本", "types": ["REPORT_DRAFT", "REPORT_EXPORT"]},
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
        task["artifacts"] = artifacts
        task["directory"] = self._build_directory(artifacts)
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
                {"key": "ENTITY_REVIEW", "label": "实体候选复核", "mode": "review"},
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
            title="实体候选·待复核",
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
                "boundary": "候选相似仅用于辅助复核，不代表系统已认定为同一实体。",
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
    ) -> dict[str, Any]:
        """记录合并/分离/修正/暂缓决定，并为候选集追加版本。"""
        allowed = {"MERGE", "KEEP_SEPARATE", "CORRECT", "DEFER"}
        if decision not in allowed:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "不支持的实体复核决定")
        if not (reason or "").strip():
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "实体复核必须填写理由")

        current = self.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        if not current:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "实体候选集不存在")
        if current["status"] in {"STALE", "INVALID"}:
            raise TaskError(TASK_ERROR_CODES["STATE_CONFLICT"], "候选集已过期或失效，请先更新")

        detail = self.get_artifact(task_id, current["id"])
        payload = detail["payload"]
        found = False
        for candidate in payload.get("candidates", []):
            if candidate.get("candidate_id") == candidate_id:
                candidate["decision"] = decision
                candidate["reason"] = reason.strip()
                candidate["correction"] = correction
                candidate["reviewed_at"] = utc_now()
                found = True
                break
        if not found:
            raise TaskError(TASK_ERROR_CODES["ARTIFACT_NOT_FOUND"], "实体候选不存在")

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
            title="实体候选·已完成" if pending == 0 else "实体候选·待复核",
            ref_key="entity-candidates",
            status=status,
            parent_ids=json.loads(current["parent_ids_json"] or "[]"),
            payload=payload,
        )
        return {"artifact": artifact, "task": self.get_task(task_id)}

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

    def _build_directory(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        directory = []
        for group in DIRECTORY_GROUPS:
            items = [a for a in artifacts if a["type"] in group["types"]]
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
    "NEEDS_OCR_REVIEW": "OCR 待复核",
    "OCR_FAILED": "OCR 失败",
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
