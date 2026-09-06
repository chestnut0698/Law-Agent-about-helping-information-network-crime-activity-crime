import json
from app.tasks import TaskError,get_task_service
from typing import Any


def _tool_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)

# ---------- Agent 工具：任务级能力，供 DeepSeek ReAct 调用 ----------

def _artifact_brief(artifact: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    if not artifact:
        return {"ok": False, "message": "未生成分析成果", **extra}
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
        service = get_task_service()
        task = service.get_task(task_id)
        overview = service.material_overview(task_id, user_id=user_id or "system")
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
        analysis_gate = ""
        pending_entities = 0
        entity_art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
        if entity_art and entity_art.get("status") not in {"STALE", "INVALID"}:
            payload = (service.get_artifact(task_id, entity_art["id"]).get("payload") or {})
            analysis_gate = (
                payload.get("analysis_gate")
                or (payload.get("summary") or {}).get("analysis_gate")
                or ""
            )
            pending_entities = int((payload.get("summary") or {}).get("pending") or 0)
            if not analysis_gate and pending_entities > 0:
                analysis_gate = "ENTITY_REVIEW"
        gate_hint = ""
        if analysis_gate == "ENTITY_REVIEW":
            gate_hint = (
                f"当前分析门闩：实体复核（仍有 {pending_entities} 条待核）。"
                "请提示用户到中间工作区完成「视为同一 / 保留独立」确认；"
                "在此之前不要生成线索或报告。"
            )
        return _tool_json(
            {
                "ok": True,
                "task_id": task_id,
                "title": task.get("title"),
                "purpose": task.get("purpose"),
                "status": task.get("status"),
                "analysis_gate": analysis_gate or None,
                "pending_entity_reviews": pending_entities,
                "gate_hint": gate_hint or None,
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
        return _tool_json(_artifact_brief(artifact, message="材料接入情况已刷新"))
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def delete_task_material(
    task_id: str,
    document_id: str | None = None,
    filename: str | None = None,
    user_id: str | None = None,
) -> str:
    """删除任务范围内的材料。可按 document_id，或按文件名关键词匹配后删除。

    用户在对话中说「删掉某某材料」时应先 get_task_overview / 核对文件名，再调用本工具。
    匹配到多份时不直接删，返回候选清单请用户确认。
    """
    try:
        service = get_task_service()
        uid = user_id or "system"
        resolved_id = (document_id or "").strip() or None
        keyword = (filename or "").strip() or None

        if not resolved_id and not keyword:
            return _tool_json(
                {
                    "ok": False,
                    "message": "请提供 document_id，或提供 filename（文件名关键词）",
                }
            )

        if not resolved_id and keyword:
            overview = service.material_overview(task_id, user_id=uid)
            matches = []
            for group in overview.get("groups") or []:
                for item in group.get("materials") or []:
                    name = item.get("filename") or ""
                    if item.get("status") == "DELETED":
                        continue
                    if keyword in name or name == keyword:
                        matches.append(
                            {
                                "document_id": item.get("document_id"),
                                "filename": name,
                                "case_id": group.get("case_id"),
                                "case_name": group.get("case_name"),
                                "status": item.get("status"),
                            }
                        )
            if not matches:
                return _tool_json(
                    {
                        "ok": False,
                        "message": f"未找到文件名包含「{keyword}」的材料",
                        "hint": "可先调用 get_task_overview 查看 materials",
                    }
                )
            if len(matches) > 1:
                return _tool_json(
                    {
                        "ok": False,
                        "need_confirm": True,
                        "message": f"匹配到 {len(matches)} 份材料，请指定 document_id 后再删",
                        "candidates": matches,
                    }
                )
            resolved_id = matches[0]["document_id"]

        result = service.remove_material(task_id, resolved_id, user_id=uid)
        return _tool_json(
            {
                "ok": True,
                "message": "材料已删除",
                "document_id": result.get("document_id") or resolved_id,
                "batch_artifact_id": result.get("batch_artifact_id"),
                "status": result.get("status"),
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def _entity_review_gate(task_id: str) -> dict[str, Any] | None:
    """若仍有待核实体，返回阻止后续线索/报告的提示。"""
    service = get_task_service()
    entity_art = service.find_artifact(task_id, "ENTITY_CANDIDATE_SET", "entity-candidates")
    if not entity_art or entity_art.get("status") in {"STALE", "INVALID"}:
        return None
    payload = (service.get_artifact(task_id, entity_art["id"]).get("payload") or {})
    pending = int((payload.get("summary") or {}).get("pending") or 0)
    gate = (
        payload.get("analysis_gate")
        or (payload.get("summary") or {}).get("analysis_gate")
        or ("" if pending == 0 else "ENTITY_REVIEW")
    )
    if gate != "ENTITY_REVIEW" or pending <= 0:
        return None
    return {
        "ok": False,
        "blocked_by_gate": "ENTITY_REVIEW",
        "pending_entity_reviews": pending,
        "message": (
            f"跨案对象仍有 {pending} 条待人工确认。"
            "请先提示用户在中间工作区完成「视为同一 / 保留独立」，"
            "确认完成后再整理线索或报告。"
        ),
    }


def run_task_collision(task_id: str, user_id: str | None = None) -> str:
    """对任务范围内材料执行强标识确定性碰撞，写入实体候选产物。"""
    try:
        result = get_task_service().run_collision(task_id, user_id=user_id or "system")
        art = result.get("artifact") or {}
        gate = result.get("analysis_gate") or ""
        message = "跨案标识比对完成，已生成对象待核清单"
        if gate == "ENTITY_REVIEW":
            message = (
                "跨案标识比对完成。请提示用户到中间工作区打开「实体复核」，"
                "对每条候选作出「视为同一」或「保留独立」；确认完成前不要继续写线索或报告。"
            )
        brief = _artifact_brief(
            art,
            message=message,
            candidate_count=result.get("candidate_count"),
            mention_count=result.get("mention_count"),
        )
        brief["analysis_gate"] = gate or None
        return _tool_json(brief)
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def run_task_timeline(task_id: str, user_id: str | None = None) -> str:
    """抽取转账/联络事件并写入角色时间线产物。"""
    try:
        result = get_task_service().run_role_timeline(task_id, user_id=user_id or "system")
        return _tool_json(
            _artifact_brief(
                result.get("artifact"),
                message="事件时间线已整理",
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
    blocked = _entity_review_gate(task_id)
    if blocked:
        return _tool_json(blocked)
    try:
        result = get_task_service().write_ai_clues(task_id, clues, user_id=user_id or "system")
        return _tool_json({
            "ok": True,
            "artifact_id": result["artifact"]["id"] if result.get("artifact") else None,
            "clue_count": result["clue_count"],
            "message": f"已写入 {result['clue_count']} 条疑似关联线索，请到中间工作区核验",
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