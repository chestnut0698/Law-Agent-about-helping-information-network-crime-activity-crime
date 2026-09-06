from fastapi import FastAPI, Form, Header, Request, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio
from agents.react_agent import *
from typing import Optional
import json
from app.config import REPO_ROOT
from app.files import MaterialError, get_material_service, init_db, get_global_mapper
from app.tasks import TaskError, get_task_service, init_task_db


app = FastAPI()
# 允许跨域（前端在 localhost 上打开时需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 数据库初始化
init_db()
init_task_db()


# ---------- 案件卷宗上传与处理 ----------
def material_error_response(exc: MaterialError) -> JSONResponse:
    return JSONResponse(status_code=400, content=exc.to_dict())

@app.post("/api/mappings/batch")
async def batch_update_mappings(payload: dict):
    """
    批量更新脱敏映射，并自动重脱敏受影响的文档。
    payload: {
        "document_id": "xxx",  # 可选，限定范围
        "updates": {"fingerprint": {"sens_type": "NAME", "anonymous_id": "NEW_ID"}},
        "deletions": ["fingerprint1", "fingerprint2"],
        "additions": [{"original": "张三", "sens_type": "PERSON"}]
    }
    """
    mapper = get_global_mapper()
    result = mapper.batch_apply_and_redact(payload)
    return result

@app.get("/api/mappings")
async def list_mappings(
    task_id: str | None = None,
    sens_type: str | None = None,
    anonymous_id: str | None = None,
    limit: int = 100,
    offset: int = 0
):
    """列出脱敏映射。"""
    mapper = get_global_mapper()
    return mapper.list_mappings(
        task_id=task_id,
        sens_type=sens_type,
        anonymous_id=anonymous_id,
        limit=limit,
        offset=offset
    )

@app.put("/api/mappings/{fingerprint}")
async def update_mapping(
    fingerprint: str,
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id")
):
    """更新脱敏映射（仅限修改 anonymous_id 或 sens_type）。"""
    mapper = get_global_mapper()
    new_anon = payload.get("anonymous_id")
    new_type = payload.get("sens_type")
    if not new_anon and not new_type:
        return JSONResponse(
            status_code=400,
            content={"error": "至少提供 anonymous_id 或 sens_type 之一"}
        )
    try:
        ok = mapper.update_mapping(fingerprint, new_anonymous_id=new_anon, new_sens_type=new_type)
        # 记录操作日志（可复用 add_audit）
        return {"ok": ok, "fingerprint": fingerprint}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

@app.delete("/api/mappings/{fingerprint}")
async def delete_mapping(
    fingerprint: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id")
):
    """删除脱敏映射。如果被引用则拒绝。"""
    mapper = get_global_mapper()
    ok, msg = mapper.delete_mapping(fingerprint)
    if not ok:
        return JSONResponse(status_code=409, content={"error": msg})
    return {"ok": True, "message": msg}

@app.post("/api/materials/upload")
async def upload_materials(
    case_id: str = Form(...),
    files: list[UploadFile] = File(...),
    parse: bool = Form(True),
    keep_duplicate: bool = Form(False),
    replace_document_id: Optional[str] = Form(None),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    service = get_material_service()
    try:
        results = []
        for item in files:
            results.append(
                service.upload_one(
                    case_id=case_id,
                    filename=item.filename or "unnamed.bin",
                    content=await item.read(),
                    user_id=x_user_id,
                    parse=parse,
                    keep_duplicate=keep_duplicate,
                    replace_document_id=replace_document_id,
                )
            )
        return {"results": results}
    except MaterialError as exc:
        return material_error_response(exc)


@app.get("/api/materials/cases/{case_id}")
async def list_case_materials_api(
    case_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return {"materials": get_material_service().list_materials(case_id, user_id=x_user_id)}
    except MaterialError as exc:
        return material_error_response(exc)


@app.get("/api/materials/documents/{document_id}/status")
async def material_status(
    document_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_material_service().get_status(document_id, user_id=x_user_id)
    except MaterialError as exc:
        return material_error_response(exc)


@app.post("/api/materials/documents/{document_id}/reparse")
async def reparse_material(
    document_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    service = get_material_service()
    try:
        current = service.get_status(document_id, user_id=x_user_id).get("current_version")
        if not current:
            raise MaterialError("MATERIAL_NOT_FOUND", "no current version")
        return service.parse_version(current["id"], user_id=x_user_id)
    except MaterialError as exc:
        return material_error_response(exc)


@app.post("/api/materials/documents/{document_id}/correct")
async def correct_material_page(
    document_id: str,
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_material_service().apply_correction(
            document_id=document_id,
            source_version_id=payload["source_version_id"],
            page_no=int(payload["page_no"]),
            corrected_text=payload["corrected_text"],
            user_id=x_user_id,
        )
    except MaterialError as exc:
        return material_error_response(exc)
    except KeyError as exc:
        return JSONResponse(
            status_code=400,
            content={"error_code": "MATERIAL_PARSE_FAILED", "message": f"missing field: {exc}"},
        )


@app.get("/api/materials/documents/{document_id}/delete-impact")
async def material_delete_impact(
    document_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_material_service().preview_delete_impact(document_id, user_id=x_user_id)
    except MaterialError as exc:
        return material_error_response(exc)

@app.get("/api/materials/{document_id}/preview")
async def preview_material(
    document_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    from app.files import db_session, _rows, _row, render_workbench_text, infer_structure_markdown

    with db_session() as conn:
        doc = _row(conn, "SELECT filename FROM documents WHERE id = ?", (document_id,))
        filename = doc["filename"] if doc else ""

        rows = _rows(conn, """
            SELECT c.text_redacted, c.ordinal, c.page_start, c.page_end
            FROM document_chunks c
            JOIN document_versions v ON v.id = c.document_version_id
            JOIN documents d ON d.id = v.document_id
            WHERE d.id = ?
              AND d.deleted_at IS NULL
              AND v.is_current = 1
              AND v.is_active = 1
              AND c.is_active = 1
              AND IFNULL(c.stale, 0) = 0
            ORDER BY c.ordinal
        """, (document_id,))

        raw_full_text = "\n".join(row['text_redacted'] or '' for row in rows)
        # 预览：还原真实原文 + 结构推断；不回写存储
        structured = infer_structure_markdown(render_workbench_text(raw_full_text))

        return JSONResponse(content={
            'ok': True,
            'document_id': document_id,
            'filename': filename,
            'text': structured,
            'format': 'markdown',
            'raw_text': raw_full_text,
            'chunk_count': len(rows)
        })


@app.delete("/api/tasks/{task_id}/materials/{document_id}")
async def delete_material(
        task_id: str,
        document_id: str,
        x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        result = get_task_service().remove_material(
            task_id, document_id, user_id=x_user_id
        )
        return {
            "status": "DELETED",
            "batch_artifact_id": result["batch_artifact_id"],
            "task": result["task"],
            "document_id": result["document_id"],
        }
    except MaterialError as exc:
        return material_error_response(exc)
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/materials/documents/{document_id}/duplicate")
async def resolve_material_duplicate(
    document_id: str,
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_material_service().resolve_duplicate(
            document_id, action=payload.get("action", "keep"), user_id=x_user_id
        )
    except MaterialError as exc:
        return material_error_response(exc)


@app.get("/api/materials/versions/{version_id}/chunks/{chunk_id}")
async def read_material_chunk_api(
    version_id: str,
    chunk_id: str,
    quote: Optional[str] = None,
    quote_hash: Optional[str] = None,
    restore: int = 1,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_material_service().read_redacted_chunk(
            version_id,
            chunk_id=chunk_id,
            user_id=x_user_id,
            quote=quote,
            quote_hash=quote_hash,
            restore_original=bool(restore),
        )
    except MaterialError as exc:
        return material_error_response(exc)


@app.post("/api/materials/versions/{version_id}/chunks/{chunk_id}/verify")
async def verify_material_chunk_api(
    version_id: str,
    chunk_id: str,
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """原文回链校验：quote 走 JSON body，避免 GET 编码损坏换行与长片段。"""
    try:
        anchors = payload.get("anchor_terms") or payload.get("highlight_terms") or []
        if not isinstance(anchors, list):
            anchors = [anchors]
        return get_material_service().read_redacted_chunk(
            version_id,
            chunk_id=chunk_id,
            user_id=x_user_id,
            quote=payload.get("quote"),
            quote_hash=payload.get("quote_hash"),
            restore_original=bool(payload.get("restore", 0)),
            anchor_terms=[str(x) for x in anchors if x],
        )
    except MaterialError as exc:
        return material_error_response(exc)


# ---------- 监督分析任务与产物 ----------
def task_error_response(exc: TaskError) -> JSONResponse:
    return JSONResponse(status_code=400, content=exc.to_dict())


@app.get("/api/tasks")
async def list_tasks(limit: int = 8):
    return {"tasks": get_task_service().list_tasks(limit=limit)}


@app.post("/api/tasks")
async def create_task(
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().create_task(
            title=payload.get("title", ""),
            purpose=payload.get("purpose", ""),
            authorized_until=payload.get("authorized_until", ""),
            cases=payload.get("cases", []),
            note=payload.get("note", ""),
            user_id=x_user_id,
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    try:
        return get_task_service().get_task(task_id)
    except TaskError as exc:
        return task_error_response(exc)

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    try:
        return get_task_service().delete_task(task_id)
    except TaskError as exc:
        return task_error_response(exc)

@app.get("/api/tasks/{task_id}/plan")
async def task_plan_preview(task_id: str):
    try:
        return get_task_service().plan_preview(task_id)
    except TaskError as exc:
        return task_error_response(exc)


@app.patch("/api/tasks/{task_id}/scope")
async def update_task_scope(task_id: str, payload: dict):
    try:
        return get_task_service().update_scope(
            task_id,
            title=payload.get("title", ""),
            purpose=payload.get("purpose", ""),
            authorized_until=payload.get("authorized_until", ""),
            cases=payload.get("cases", []),
            note=payload.get("note", ""),
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/plan/confirm")
async def task_plan_confirm(
    task_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().confirm_plan(task_id, user_id=x_user_id)
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/entity-candidates")
async def save_task_entity_candidates(task_id: str, payload: dict):
    """供抽取/归一步骤写入候选集；候选不会被自动认定为同一实体。"""
    try:
        return get_task_service().save_entity_candidates(
            task_id,
            candidates=payload.get("candidates", []),
            summary=payload.get("summary"),
            run_id=payload.get("run_id"),
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/entity-candidates/{candidate_id}/decision")
async def review_task_entity_candidate(task_id: str, candidate_id: str, payload: dict):
    try:
        return get_task_service().review_entity_candidate(
            task_id,
            candidate_id,
            decision=payload.get("decision", ""),
            reason=payload.get("reason", ""),
            correction=payload.get("correction"),
            expected_version=payload.get("expected_version"),
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/entity-candidates/{candidate_id}/field-table")
async def build_candidate_field_table_api(
    task_id: str,
    candidate_id: str,
    payload: dict | None = None,
):
    """由 DeepSeek 现场设计该候选的字段对照表并写回待核清单。"""
    import json as _json

    from tools.entity_review import build_candidate_field_table

    body = payload or {}
    result = _json.loads(
        build_candidate_field_table(
            task_id, candidate_id, force=bool(body.get("force"))
        )
    )
    if not result.get("ok"):
        return JSONResponse(status_code=200, content=result)
    result["task"] = get_task_service().get_task(task_id)
    return result


@app.post("/api/tasks/{task_id}/entity-candidates/review")
async def run_entity_review_agent_api(
    task_id: str,
    payload: dict | None = None,
):
    """触发 DeepSeek 实体复核 Agent（单候选或批量待核）。"""
    try:
        from agents.entity_review_agent import run_entity_review_for_task

        body = payload or {}
        return run_entity_review_for_task(task_id, candidate_id=body.get("candidate_id"))
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"error_code": "ENTITY_REVIEW_FAILED", "message": str(exc)},
        )


@app.post("/api/tasks/{task_id}/clues/{artifact_id}/disposition")
async def dispose_task_clue(task_id: str, artifact_id: str, payload: dict):
    try:
        return get_task_service().dispose_clue_item(
            task_id,
            artifact_id,
            disposition=payload.get("disposition", ""),
            reason=payload.get("reason", ""),
            expected_version=payload.get("expected_version"),
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/report/draft")
async def task_report_draft(task_id: str):
    try:
        return get_task_service().build_report_draft(task_id)
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/collision/run")
async def task_run_collision(
    task_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().run_collision(task_id, user_id=x_user_id)
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/clues/generate")
async def task_generate_clues(
    task_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().generate_clues(task_id, user_id=x_user_id)
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/timeline/run")
async def task_run_role_timeline(
    task_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().run_role_timeline(task_id, user_id=x_user_id)
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/materials")
async def upload_task_materials(
    task_id: str,
    case_id: str = Form(...),
    files: list[UploadFile] = File(...),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """材料必须归属任务内的已选案件，上传后同步落为 MATERIAL_DOC 产物。"""
    tasks = get_task_service()
    materials = get_material_service()
    # documents.uploaded_by 有外键约束；本地演示未登录时回落到 system
    actor = x_user_id or "system"
    try:
        task = tasks.get_task(task_id)
        if case_id not in {c["case_id"] for c in task["cases"]}:
            raise TaskError("TASK_INVALID_SCOPE", "材料所属案件不在本任务范围内")

        results = []
        for item in files:
            upload = materials.upload_one(
                case_id=case_id,
                filename=item.filename or "unnamed.bin",
                content=await item.read(),
                user_id=actor,
            )
            results.append(
                tasks.record_material(
                    task_id=task_id, case_id=case_id, upload_result=upload, user_id=actor
                )
            )
        return {"results": results, "task": tasks.get_task(task_id)}
    except TaskError as exc:
        return task_error_response(exc)
    except MaterialError as exc:
        return material_error_response(exc)


@app.get("/api/tasks/{task_id}/materials")
async def refresh_task_materials(
    task_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        artifact = get_task_service().refresh_material_batch(task_id, user_id=x_user_id)
        return {"artifact_id": artifact["id"], "version": artifact["current_version"]}
    except TaskError as exc:
        return task_error_response(exc)


@app.get("/api/tasks/{task_id}/artifacts/{artifact_id}")
async def get_task_artifact(task_id: str, artifact_id: str, version: Optional[int] = None):
    """任务目录与智能体消息链接共用此入口，保证解析到同一产物对象。"""
    try:
        return get_task_service().get_artifact(task_id, artifact_id, version=version)
    except TaskError as exc:
        return task_error_response(exc)


@app.get("/api/tasks/{task_id}/artifacts/{artifact_id}/impact")
async def preview_artifact_impact(task_id: str, artifact_id: str):
    try:
        return get_task_service().preview_impact(task_id, artifact_id)
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/artifacts/{artifact_id}/impact")
async def apply_artifact_impact(task_id: str, artifact_id: str, payload: dict | None = None):
    try:
        return get_task_service().apply_impact(
            task_id, artifact_id, reason=(payload or {}).get("reason", "")
        )
    except TaskError as exc:
        return task_error_response(exc)

@app.get("/chat/{task_id}/messages")
async def get_chat_history(task_id: str):
    """获取任务的历史聊天记录（含 tool_calls，供前端还原折叠步骤）"""
    messages = get_task_service().repair_chat_messages(task_id)
    payload = []
    for msg in messages:
        if msg.get("role") == "system":
            continue
        item = {
            "role": msg.get("role"),
            "content": msg.get("content") or "",
            "created_at": msg.get("created_at"),
        }
        if msg.get("tool_call_id"):
            item["tool_call_id"] = msg["tool_call_id"]
        if msg.get("tool_calls"):
            item["tool_calls"] = msg["tool_calls"]
        if msg.get("reasoning_content"):
            item["reasoning_content"] = msg["reasoning_content"]
        payload.append(item)
    return {"messages": payload}

# 前后端通信
@app.post("/chat/{task_id}")
async def chat(request: Request, task_id):
    data = await request.json()
    messages = data.get("messages", [])
    # 首句注入系统提示词
    from app.files import db_session, _rows
    with db_session() as conn:
        rows = _rows(
            conn,
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,)
        )

    # 查询任务详情，构建系统上下文
    task = get_task_service().get_task(task_id)
    cases_info = "\n".join(
        f"- 案件 {c['case_id']}: {c['display_name']}"
        for c in task.get("cases", [])
    )
    system_context = (
        f"当前监督任务：{task['title']}\n"
        f"监督目的：{task['purpose']}\n"
        f"包含案件：\n{cases_info}\n"
        f"你可以调用 list_case_materials(case_id) 等工具来分析材料。"
    )

    # 提取最后一条用户消息
    user_message = messages[-1]["content"] if messages else ""

    # 保存消息,只在第一句添加
    if not rows:
        get_task_service().save_message(task_id, "system", TASK_AGENT_PROMPT)
        get_task_service().save_message(task_id, "system", f"监督目的：{task['purpose']}\n")


    async def event_stream():
        try:
            agent.switch_id(task_id)
            # 调用 agent.chat() 获得生成器
            responses = agent.chat(user_message)

            assistant_content = []
            for chunk in responses:
                chunk_type = chunk[0]
                chunk_data = chunk[1]

                if chunk_type == "reasoning_content":
                    # 思考过程 → 发送 thinking 事件
                    yield f"data: {json.dumps({'type': 'thinking', 'content': chunk_data})}\n\n"

                elif chunk_type == "tool_calls":
                    name = chunk_data["name"]
                    if name[:3] in {"web", "sea"}:
                        tool_type = "search"
                    elif "file" in name:
                        tool_type = "file"
                    else:
                        tool_type = "code"
                    payload = {
                        "type": "tool_call",
                        "tool": {
                            "type": tool_type,
                            "name": name,
                            "params": chunk_data.get("arguments", {}),
                            "status": "running",
                        },
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                elif chunk_type == "tool_result":
                    payload = {
                        "type": "tool_result",
                        "tool": {
                            "result": chunk_data,
                            "status": (
                                "error" if "工具调用出错" in str(chunk_data) else "success"
                            ),
                        },
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                elif chunk_type == "content":
                    assistant_content.append(chunk_data)
                    # 文本内容 → 逐字符发送 text_delta（或整段发送）
                    for char in chunk_data:
                        yield f"data: {json.dumps({'type': 'text_delta', 'text': char})}\n\n"

                elif chunk_type == "plan":
                    yield f"data: {json.dumps({'type': 'plan', 'plan': chunk_data})}\n\n"

                elif chunk_type == "done":
                    yield  f"data: {json.dumps({'type': 'done', 'done': chunk_data})}\n\n"

                elif chunk_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': chunk_data}, ensure_ascii=False)}\n\n"
                # 其他类型可扩展（如 plan, citation 等）

                # 每次 yield 后小睡，让出事件循环 → 触发真实网络 flush
                await asyncio.sleep(0)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as exc:
            from agents.react_agent import _public_chat_error
            yield f"data: {json.dumps({'type': 'error', 'message': _public_chat_error(exc)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"


    return StreamingResponse(event_stream(), media_type="text/event-stream")



# 初始化智能体
agent = ReactAgent()

app.mount("/", StaticFiles(directory=str(REPO_ROOT / "ui"), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)