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

# 废弃：规则不再直接落 CLUE_ITEM。请用 list_task_clues / put_task_clue / delete_task_clue 维护线索工作集。

def list_association_hints(task_id: str, user_id: str | None = None) -> str:
    try:
        result = get_task_service().list_association_hints(
            task_id, user_id=user_id or "system"
        )
        return _tool_json(result)
    except TaskError as exc:
        return _tool_json(exc.to_dict())

def list_task_clues(task_id: str, user_id: str | None = None) -> str:
    """列出当前存活待核线索的精简快照（含序号，供 delete/覆盖引用），不改任何数据。"""
    try:
        result = get_task_service().list_clue_items_for_model(task_id)
        return _tool_json(result)
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def put_task_clue(
    task_id: str,
    clue: dict[str, Any],
    replace_all: bool = False,
    user_id: str | None = None,
) -> str:
    """写入或更新单条待核线索。

    replace_all=True 表示开始新一轮线索：先作废旧条再写本条，只在用户明确要求
    「重新形成/覆盖全部线索」时使用；默认 False 为逐条追加/更新当前工作集。
    同对象同核验维度的旧模型线索会被本条顶替；已由人工处置的线索不允许覆盖。
    """
    blocked = _entity_review_gate(task_id)
    if blocked:
        return _tool_json(blocked)
    try:
        result = get_task_service().put_clue_item(
            task_id, clue, replace_all=bool(replace_all), user_id=user_id or "system"
        )
        replaced = result.get("retired_count") or 0
        return _tool_json(
            {
                "ok": True,
                "clue_count": result.get("clue_count"),
                "replaced_count": replaced,
                "message": (
                    f"已写入待核线索 {result.get('clue_count')} 条"
                    + (f"（顶替旧条 {replaced} 条）" if replaced else "")
                    + "，请到中间工作区「线索中心」核验"
                ),
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def delete_task_clue(task_id: str, index: int, user_id: str | None = None) -> str:
    """删除第 index 条存活待核线索（序号来自 list_task_clues）。

    仅能删除智能体自己写入、且尚未被人工处置的线索；已处置的会返回拒绝并说明。
    """
    blocked = _entity_review_gate(task_id)
    if blocked:
        return _tool_json(blocked)
    try:
        result = get_task_service().delete_clue_item(
            task_id, int(index), user_id=user_id or "system"
        )
        return _tool_json(result)
    except TaskError as exc:
        return _tool_json(exc.to_dict())

def read_artifact(task_id: str, artifact_id: str, user_id: str | None = None) -> str:
    """
    读取指定产物的内容供 AI 分析。大产物（实体候选集 / 角色时间线）返回精简摘要：
    实体候选含每条的人工核验 decision 与理由、可回链引文；时间线按事件关键字段裁剪。
    其它类型返回原 payload。先经 get_task_overview 或左侧产物目录拿到 artifact_id。
    """
    try:
        service = get_task_service()
        result = service.artifact_digest_for_model(task_id, artifact_id)
        meta = result["artifact"]
        return _tool_json({
            "ok": True,
            "artifact_id": artifact_id,
            "type": meta["type"],
            "title": meta["title"],
            "status": meta["status"],
            "version": meta["version"],
            "payload": result["payload"],
        })
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def read_report(task_id: str, user_id: str | None = None) -> str:
    """读取撰写《跨案关联线索核验单》所需的素材：任务范围、实体复核结论、存活线索、既有草稿与写作规范。"""
    try:
        return _tool_json(get_task_service().report_write_context(task_id))
    except TaskError as exc:
        return _tool_json(exc.to_dict())


def write_report(
    task_id: str,
    body: str,
    note: str = "",
    user_id: str | None = None,
) -> str:
    """提交你撰写完成的《跨案关联线索核验单》整篇正文；系统自动套固定边界并重算核对清单。"""
    try:
        result = get_task_service().write_report_draft(
            task_id, body=body, note=note, user_id=user_id or "system"
        )
        return _tool_json(
            {
                "ok": True,
                "artifact_id": result["artifact"]["id"],
                "version": result["version"],
                "valid": result["valid"],
                "clue_count": result["clue_count"],
                "invalid_refs": result["invalid_refs"],
                "message": (
                    f"报告已写入 v{result['version']}。"
                    + ("当前报告有效，可导出。" if result["valid"] else "部分线索缺少原文依据，需补证或排除后再导出。")
                ),
            }
        )
    except TaskError as exc:
        return _tool_json(exc.to_dict())