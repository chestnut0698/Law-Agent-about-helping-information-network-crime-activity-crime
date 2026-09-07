from app.chat_history import messages_for_llm, sanitize_tool_history
from app.tasks import TaskService
from agents.react_agent import ReactAgent


def _assistant_tools(content, call_ids, name="get_task_overview"):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _tool(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_sanitize_keeps_complete_tool_groups():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "开始分析"},
        _assistant_tools("先看范围", ["c1"]),
        _tool("c1", "overview"),
        {"role": "assistant", "content": "已完成"},
    ]
    kept = sanitize_tool_history(messages)
    assert [m["role"] for m in kept] == ["system", "user", "assistant", "tool", "assistant"]
    assert kept[2]["tool_calls"][0]["id"] == "c1"


def test_sanitize_drops_orphan_assistant_tool_calls():
    messages = [
        {"role": "user", "content": "开始分析"},
        _assistant_tools("先看范围", ["c1"]),
        _tool("c1", "overview"),
        _assistant_tools("先看范围", ["c1"]),  # 落库去重失败复制出来的残缺副本
        _assistant_tools("再碰撞", ["c2", "c3"]),
        {"role": "user", "content": "请再分析一次"},
    ]
    kept = sanitize_tool_history(messages)
    roles = [m["role"] for m in kept]
    assert roles == ["user", "assistant", "tool", "user"]
    payload = messages_for_llm(kept)
    assert payload[-1]["role"] == "user"
    assert all("tool_calls" not in m or m["role"] == "assistant" for m in payload)


def test_sanitize_drops_orphan_tool_results():
    messages = [
        {"role": "user", "content": "问"},
        _tool("dangling", "no parent"),
        {"role": "assistant", "content": "答"},
    ]
    kept = sanitize_tool_history(messages)
    assert [m["role"] for m in kept] == ["user", "assistant"]


def test_repair_chat_messages_removes_duplicates(tmp_path):
    db = tmp_path / "chat.db"
    svc = TaskService(db_path=db)
    task_id = "task-g003"
    svc.save_message(task_id, "user", "开始分析")
    svc.save_message(
        task_id,
        "assistant",
        "先看范围",
        metadata=[{"id": "c1", "type": "function", "function": {"name": "get_task_overview", "arguments": "{}"}}],
    )
    svc.save_message(task_id, "tool", "overview", tool_call_id="c1")
    svc.save_message(
        task_id,
        "assistant",
        "先看范围",
        metadata=[{"id": "c1", "type": "function", "function": {"name": "get_task_overview", "arguments": "{}"}}],
    )
    svc.save_message(task_id, "assistant", "最终说明")

    repaired = svc.repair_chat_messages(task_id)
    # ensure_task_system_prompt 会补当前 TASK_AGENT_PROMPT，并置顶
    assert [m["role"] for m in repaired] == ["system", "user", "assistant", "tool", "assistant"]
    remaining = svc.get_messages(task_id)
    assert len(remaining) == 5
    assert any(r["role"] == "system" for r in remaining)
    assert remaining[-1]["content"] == "最终说明" or any(
        r["content"] == "最终说明" for r in remaining
    )


def test_messages_for_llm_keeps_reasoning_content():
    messages = [
        {"role": "user", "content": "继续"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_task_overview", "arguments": "{}"},
                }
            ],
            "reasoning_content": "先看任务范围",
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    payload = messages_for_llm(messages)
    assert payload[1]["reasoning_content"] == "先看任务范围"
    assert payload[2]["role"] == "tool"


def test_public_chat_error_reasoning_hint():
    from agents.react_agent import _public_chat_error

    text = _public_chat_error(
        Exception(
            "Error code: 400 - The `reasoning_content` in the thinking mode must be passed back to the API."
        )
    )
    assert "思考过程" in text
