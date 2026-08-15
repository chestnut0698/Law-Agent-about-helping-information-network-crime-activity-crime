from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio
from agents.react_agent import *


app = FastAPI()
# 允许跨域（前端在 localhost 上打开时需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def load_meta() -> list:
    if META_FILE.exists():
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_meta(meta: list):
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

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


# 初始化智能体
agent = ReactAgent()


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
                tool_type = 'search' if (chunk_data['name'][:3] == "web" or chunk_data['name'][:3] == "sea") else 'code'
                # 工具调用 → 发送 tool_call 事件
                yield f"data: {json.dumps({'type': 'tool_call', 'tool': {
                    'type': tool_type, 
                    'name': chunk_data['name'],
                    'params': chunk_data.get('arguments', {}),
                    'status': 'running'
                }})}\n\n"

            elif chunk_type == "tool_result":
                # 发送 tool_result 事件
                yield f"data: {json.dumps({'type': 'tool_result', 'tool': {
                    'result': chunk_data,
                    'status': 'success' if not "工具调用出错" in chunk_data else 'error'
                }})}\n\n"

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


app.mount("/", StaticFiles(directory="../ui", html=True), name="ui")

"""
while 1:
    is_reasoning = True
    is_call_tool = True
    is_content = True

    print(agent.messages)
    user_input = input("用户:")


    responses = agent.chat(user_input)
    for chunk in responses:
        if chunk[0] == "reasoning_content":
            if is_reasoning:
                print("思考:", end="")
                is_reasoning = False

            print(chunk[1], end="")
        if chunkp[0] == "tool_calls":
            if

        if chunk[0] == "content":
            if is_content:
                print("\n回答:", end="")
                is_content = False
            print(chunk[1], end="")


    print('\n', end="")"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)