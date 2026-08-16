from fastapi import FastAPI, Form, Header, Request, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio
from agents.react_agent import *
import shutil
from pathlib import Path
from typing import Optional

from app.config import REPO_ROOT
from tools.files import MaterialError, get_material_service, init_db


app = FastAPI()
# 允许跨域（前端在 localhost 上打开时需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

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
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    try:
        return get_material_service().read_redacted_chunk(
            version_id, chunk_id=chunk_id, user_id=x_user_id
        )
    except MaterialError as exc:
        return material_error_response(exc)


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