import json
from app.tasks import TaskError,get_task_service
from typing import Any


def _tool_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)

# ---------- Agent 工具：任务级能力，供 DeepSeek ReAct 调用 ----------

def _artifact_brief(artifact: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    if not artifact:
        return {"ok": False, "message": "未生成产物", **extra}
    return {
        "ok": True,
        "artifact_id": artifact.get("id"),
        "artifact_type": artifact.get("type"),
        "title": artifact.get("title"),
        "status": artifact.get("status"),
        "version": artifact.get("current_version"),
        **extra,
    }


def get_task_overview(task_id: str, user_id: str | None = None) -> str:
    """查看监督分析任务范围、案件、材料与已有产物清单。"""
    try:
        task = get_task_service().get_task(task_id)
        overview = get_task_service().material_overview(task_id, user_id=user_id or "system")
        artifacts = [
            {
                "artifact_id": a["id"],
                "type": a["type"],
                "title": a["title"],
                "status": a["status"],
            }
            for a in (task.get("artifacts") or [])
            if a.get("status") != "INVALID"
        ]
        return _tool_json(
            {
                "ok": True,
                "task_id": task_id,
                "title": task.get("title"),
                "purpose": task.get("purpose"),
                "status": task.get("status"),
                "cases": [
                    {"case_id": c["case_id"], "display_name": c.get("display_name")}
                    for c in (task.get("cases") or [])
                ],
                "materials": overview,
                "artifacts": artifacts,
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def confirm_task_plan(task_id: str, user_id: str | None = None) -> str:
    """确认分析计划并进入工作台（若仍为 SCOPE_DRAFT）。"""
    try:
        result = get_task_service().confirm_plan(task_id, user_id=user_id or "system")
        task = result.get("task") or get_task_service().get_task(task_id)
        batch_id = result.get("batch_artifact_id")
        batch_art = next(
            (a for a in (task.get("artifacts") or []) if a.get("id") == batch_id),
            next(
                (a for a in (task.get("artifacts") or []) if a.get("type") == "MATERIAL_BATCH"),
                None,
            ),
        )
        return _tool_json(
            _artifact_brief(
                batch_art,
                message="计划已确认",
                task_status=task.get("status"),
            )
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def refresh_task_materials(task_id: str, user_id: str | None = None) -> str:
    """刷新任务材料批次产物。"""
    try:
        artifact = get_task_service().refresh_material_batch(task_id, user_id=user_id or "system")
        return _tool_json(_artifact_brief(artifact, message="材料批次已刷新"))
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def run_task_collision(task_id: str, user_id: str | None = None) -> str:
    """对任务范围内材料执行强标识确定性碰撞，写入实体候选产物。"""
    try:
        result = get_task_service().run_collision(task_id, user_id=user_id or "system")
        return _tool_json(
            _artifact_brief(
                result.get("artifact"),
                message="强标识碰撞完成",
                candidate_count=result.get("candidate_count"),
                mention_count=result.get("mention_count"),
            )
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def run_task_timeline(task_id: str, user_id: str | None = None) -> str:
    """抽取转账/联络事件并写入角色时间线产物。"""
    try:
        result = get_task_service().run_role_timeline(task_id, user_id=user_id or "system")
        return _tool_json(
            _artifact_brief(
                result.get("artifact"),
                message="事件时间线已生成",
                event_count=result.get("event_count"),
            )
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())

# 废弃
"""
def generate_task_clues(task_id: str, user_id: str | None = None) -> str:
    # 根据 R001–R006 规则命中生成跨案线索产物（含表述与校核）。
    try:
        result = get_task_service().generate_clues(task_id, user_id=user_id or "system")
        return _tool_json(
            _artifact_brief(
                result.get("artifact"),
                message="线索生成完成",
                created_count=len(result.get("created") or []),
                skipped_count=len(result.get("skipped") or []),
                hit_count=result.get("hit_count"),
            )
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())
"""

def write_ai_clues(task_id: str, clues: list[dict[str, Any]], user_id: str | None = None) -> str:
    try:
        result = get_task_service().write_ai_clues(task_id, clues, user_id=user_id or "system")
        return _tool_json({
            "ok": True,
            "artifact_id": result["artifact"]["id"] if result.get("artifact") else None,
            "clue_count": result["clue_count"],
            "message": f"成功写入 {result['clue_count']} 条线索",
        })
    except TaskError as exc:
        return _tool_json(exc.to_dict())

def read_artifact(task_id: str, artifact_id: str, user_id: str | None = None) -> str:
    """
    读取指定产物的完整内容（含 payload）。
    用于 AI 分析 ENTITY_CANDIDATE_SET 或 ROLE_TIMELINE 的详细数据。
    """
    try:
        service = get_task_service()
        result = service.get_artifact(task_id, artifact_id)
        return _tool_json({
            "ok": True,
            "artifact_id": artifact_id,
            "type": result["artifact"]["type"],
            "title": result["artifact"]["title"],
            "status": result["artifact"]["status"],
            "version": result["version"],
            "payload": result["payload"],  # 核心数据
        })
    except TaskError as exc:
        return _tool_json(exc.to_dict())