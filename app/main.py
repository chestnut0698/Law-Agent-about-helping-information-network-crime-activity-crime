from fastapi import FastAPI, Form, Header, Request, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio
import json
from agents.react_agent import *
import shutil
from pathlib import Path
from typing import Optional

from app.config import REPO_ROOT
from agents.gateway import GatewayError, get_gateway, init_gateway_db
from tools.files import ERROR_CODES, MaterialError, get_material_service, init_db
from tools.entities import init_entity_db
from tools.tasks import TaskError, get_task_service, init_task_db


app = FastAPI()
# 允许跨域（前端在 localhost 上打开时需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()
init_task_db()
init_gateway_db()
init_entity_db()

def load_meta() -> list:
    if META_FILE.exists():
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_meta(meta: list):
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def get_workspace(conv_id: str) -> Path:
    """获取对话的工作目录，不存在则创建"""
    path = WORKSPACE_DIR / str(conv_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------- 对话管理 API ----------
@app.get("/conversations")
async def list_conversations():
    return {"conversations": load_meta()}

@app.post("/conversations")
async def create_conversation(request: Request):
    data = await request.json()
    title = data.get("title", "新对话")
    import time
    conv_id = str(int(time.time() * 1000))

    meta = load_meta()
    meta.append({"id": conv_id, "title": title, "time": "刚刚"})
    save_meta(meta)

    return {"id": conv_id, "title": title}

@app.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    meta = load_meta()
    meta = [c for c in meta if c["id"] != conv_id]
    save_meta(meta)
    # 删除消息文件
    path = DATA_DIR / f"{conv_id}.json"
    if path.exists():
        path.unlink()

    workspace = get_workspace(conv_id)
    if workspace.exists():
        shutil.rmtree(workspace)

    return {"status": "ok"}

@app.patch("/conversations/{conv_id}")
async def rename_conversation(conv_id: str, request: Request):
    data = await request.json()
    meta = load_meta()
    for c in meta:
        if c["id"] == conv_id:
            c["title"] =  data.get("title", "")
            break
    save_meta(meta)
    return {"status": "ok"}

@app.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str):
    path = DATA_DIR / f"{conv_id}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return {"messages": json.load(f)}
    return {"messages": []}


# ---------- 文件上传 ----------
@app.post("/conversations/{conv_id}/upload")
async def upload_file(conv_id: str, file: UploadFile = File(...)):
    workspace = get_workspace(conv_id)
    # 安全处理文件名（防止路径遍历）
    safe_name = Path(file.filename).name
    file_path = workspace / safe_name

    # 写入文件
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # 返回文件信息
    return {
        "filename": safe_name,
        "size": file_path.stat().st_size,
        "path": str(file_path),
        "url": f"/files/{conv_id}/{safe_name}"
    }


# ---------- 文件列表 ----------
@app.get("/conversations/{conv_id}/files")
async def list_files(conv_id: str):
    workspace = get_workspace(conv_id)
    files = []
    for f in sorted(workspace.iterdir()):
        if f.is_file():
            files.append({
                "filename": f.name,
                "size": f.stat().st_size,
                "url": f"/files/{conv_id}/{f.name}"
            })
    return {"files": files}


# ---------- 文件删除 ----------
@app.delete("/conversations/{conv_id}/files/{filename}")
async def delete_file(conv_id: str, filename: str):
    workspace = get_workspace(conv_id)
    safe_name = Path(filename).name
    file_path = workspace / safe_name
    if file_path.exists():
        file_path.unlink()
    return {"status": "ok"}


# ---------- 案件卷宗上传与处理 ----------
def material_error_response(exc: MaterialError) -> JSONResponse:
    return JSONResponse(status_code=400, content=exc.to_dict())


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


@app.delete("/api/materials/documents/{document_id}")
async def delete_material(
    document_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_material_service().logical_delete(document_id, user_id=x_user_id)
    except MaterialError as exc:
        return material_error_response(exc)


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
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        payload = get_material_service().read_redacted_chunk(
            version_id, chunk_id=chunk_id, user_id=x_user_id
        )
        if quote_hash:
            from tools.entities import verify_quote_hash

            if not quote or not verify_quote_hash(payload.get("text") or "", quote, quote_hash):
                raise MaterialError(
                    ERROR_CODES["CITATION_STALE"],
                    "引用失效：原文已变更或哈希不匹配，禁止展示旧内容",
                    {"chunk_id": chunk_id, "quote_hash": quote_hash},
                )
            payload["cite_ok"] = True
        return payload
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


@app.delete("/api/tasks/{task_id}/materials/{document_id}")
async def delete_task_material(
    task_id: str,
    document_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """删除任务内材料：逻辑删除文档，并同步目录中的 MATERIAL_DOC / MATERIAL_BATCH。"""
    try:
        return get_task_service().remove_material(
            task_id, document_id, user_id=x_user_id or "system"
        )
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


def gateway_error_response(exc: GatewayError) -> JSONResponse:
    status = 503 if exc.degraded else 400
    return JSONResponse(status_code=status, content=exc.to_dict())


@app.get("/api/agent/status")
async def agent_status():
    """降级状态：关闭外呼后系统仍可走确定性规则，界面据此明示。"""
    return get_gateway().status()


@app.get("/api/agent/runs")
async def agent_runs(purpose: Optional[str] = None, limit: int = 50):
    return {"runs": get_gateway().list_runs(purpose=purpose, limit=limit)}


@app.get("/api/agent/repair-queue")
async def agent_repair_queue(limit: int = 50):
    return {"items": get_gateway().list_repair_queue(limit=limit)}


@app.post("/api/tasks/{task_id}/roles/extract")
async def task_extract_role(
    task_id: str,
    payload: dict | None = None,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        body = payload or {}
        return get_task_service().run_extraction(
            task_id,
            user_id=x_user_id,
            chunk_ids=body.get("chunk_ids"),
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/roles/normalize")
async def task_normalize_role(
    task_id: str,
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().run_normalization(
            task_id,
            entity_a=payload.get("entity_a") or {},
            entity_b=payload.get("entity_b") or {},
            user_id=x_user_id,
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/roles/clue-wording")
async def task_clue_wording_role(
    task_id: str,
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().run_clue_wording(
            task_id,
            rule_hits=payload.get("rule_hits") or [],
            user_id=x_user_id,
        )
    except TaskError as exc:
        return task_error_response(exc)


@app.post("/api/tasks/{task_id}/roles/verify")
async def task_verify_role(
    task_id: str,
    payload: dict,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_task_service().run_output_verify(
            task_id,
            clue_text=payload.get("clue_text") or "",
            evidence=payload.get("evidence") or [],
            reverse_materials=payload.get("reverse_materials"),
            user_id=x_user_id,
        )
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


@app.post("/api/tasks/{task_id}/agent/chat")
async def task_agent_chat(
    task_id: str,
    request: Request,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """任务级 ReAct：DeepSeek 思考并调工具，SSE 推送思考/工具/产物链接。"""
    from agents.task_agent import TaskAgent

    try:
        get_task_service().get_task(task_id)
    except TaskError as exc:
        return task_error_response(exc)

    data = await request.json()
    messages = data.get("messages") or []
    user_message = ""
    if messages:
        user_message = messages[-1].get("content") or ""
    if not user_message:
        user_message = data.get("message") or ""

    agent_runner = TaskAgent(task_id, user_id=x_user_id or "system")

    async def event_stream():
        for chunk in agent_runner.chat(user_message):
            chunk_type, chunk_data = chunk[0], chunk[1]
            if chunk_type == "reasoning_content":
                yield f"data: {json.dumps({'type': 'thinking', 'content': chunk_data}, ensure_ascii=False)}\n\n"
            elif chunk_type == "tool_calls":
                name = chunk_data.get("name") or ""
                if name.startswith("run_task_") or name.startswith("generate_task_"):
                    tool_type = "code"
                elif "material" in name or "file" in name or name.startswith("list_"):
                    tool_type = "file"
                elif name[:3] in {"web", "sea"}:
                    tool_type = "search"
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
                            "error"
                            if ("工具调用出错" in str(chunk_data) or '"ok": false' in str(chunk_data).lower())
                            else "success"
                        ),
                    },
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif chunk_type == "artifact":
                yield f"data: {json.dumps({'type': 'artifact', **chunk_data}, ensure_ascii=False)}\n\n"
            elif chunk_type == "content":
                for char in chunk_data:
                    yield f"data: {json.dumps({'type': 'text_delta', 'text': char}, ensure_ascii=False)}\n\n"
            elif chunk_type == "plan":
                yield f"data: {json.dumps({'type': 'plan', 'plan': chunk_data}, ensure_ascii=False)}\n\n"
            elif chunk_type == "done":
                yield f"data: {json.dumps({'type': 'done', 'done': chunk_data}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- 静态文件服务（让前端能预览/下载）----------
from fastapi.responses import FileResponse


@app.get("/files/{conv_id}/{filename}")
async def serve_file(conv_id: str, filename: str):
    workspace = get_workspace(conv_id)
    safe_name = Path(filename).name
    file_path = workspace / safe_name
    if not file_path.exists():
        return {"error": "文件不存在"}
    return FileResponse(file_path)



# 前后端通信
@app.post("/chat/{conv_id}")
async def chat(request: Request, conv_id):
    data = await request.json()
    messages = data.get("messages", [])
    # 提取最后一条用户消息
    user_message = messages[-1]["content"] if messages else ""

    async def event_stream():
        agent.switch_id(conv_id)
        # 调用 agent.chat() 获得生成器
        responses = agent.chat(user_message)

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
                # 文本内容 → 逐字符发送 text_delta（或整段发送）
                for char in chunk_data:
                    yield f"data: {json.dumps({'type': 'text_delta', 'text': char})}\n\n"

            elif chunk_type == "plan":
                yield f"data: {json.dumps({'type': 'plan', 'plan': chunk_data})}\n\n"

            elif chunk_type == "done":
                yield  f"data: {json.dumps({'type': 'done', 'done': chunk_data})}\n\n"
            # 其他类型可扩展（如 plan, citation 等）

            # 每次 yield 后小睡，让出事件循环 → 触发真实网络 flush
            await asyncio.sleep(0)

        # 发送完成信号
        yield f"data: {json.dumps({'type': 'done'})}\n\n"


    return StreamingResponse(event_stream(), media_type="text/event-stream")



# 初始化智能体
agent = ReactAgent()

app.mount("/", StaticFiles(directory=str(REPO_ROOT / "ui"), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)