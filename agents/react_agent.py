from agents.base_agent import *
from app.config import *
import inspect
import json
from tools.tools import *
from pathlib import Path
from app.tasks import get_task_service

# ReAct（Reason + Act）模式的核心思想——让 AI 交替进行"推理（Reason）"和"行动（Act）"，并通过观察（Observation）来驱动下一步。
class ReactAgent(BaseAgent):
    def __init__(self, task_id=0):
        super().__init__()
        self.task_id = task_id
        schema_path = Path(__file__).resolve().parent.parent / "tools" / "tools_schema.json"
        with open(schema_path, "r", encoding="utf-8") as f:
            self.tools = json.load(f)

    def execute_tool(self, data) -> str:
        try:
            fn = tool_functions[data["function_name"]]
            args = json.loads(data["arguments"])
            sig = inspect.signature(fn)
            if "task_id" in sig.parameters:
                result = fn(**args, task_id=self.task_id)
            else:
                result = fn(**args)
            if data["function_name"] in WORKSPACE_ONLY_TOOLS:
                result = (
                    "[会话附件-非卷宗] 此内容未经脱敏门控，不得用于抽取或碰撞。"
                    f"\n{result}"
                )
        except Exception as e:
            result = f"工具调用出错: {e}"

        return result

    def switch_id(self, task_id):
        if self.task_id == task_id:
            return

        # 直接从数据库加载新 task_id 的历史消息
        from tools.files import db_session, _rows
        with db_session() as conn:
            rows = _rows(
                conn,
                "SELECT role, content, tool_call_id, metadata_json FROM chat_messages "
                "WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,)
            )
        self.messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"]}
            if row.get("tool_call_id"):
                msg["tool_call_id"] = row["tool_call_id"]
            if row.get("metadata_json"):
                try:
                    meta = json.loads(row.get("metadata_json"))
                    if meta:
                        msg["tool_calls"] = meta
                except:
                    pass
            self.messages.append(msg)

        self.task_id = task_id

    def chat(self, user_input):
        """
        流式对话：添加用户消息 → 获取流式响应 → 逐块 yield 回复内容
        自动将最终完整回复追加到历史中
        """
        self.messages.append({"role": "user", "content": user_input})


        plan_steps = []
        if PLANS:
            for i in range(len(PLANS[0])):
                plan_steps.append(
                    {
                        "title": PLANS[0][i],
                        "description": PLANS[1][i],
                        "status": "pending",
                    }
                )
        yield (
            "plan",
            {
                "title": "执行计划",
                "steps": plan_steps,
            },
        )
        for step in range(len(plan_steps)):
            self.messages.append(
                {
                    "role": "system",
                    "content": f"当前进度：第 {step + 1} 步 / 共 {len(plan_steps)} 步。请根据计划继续执行:{PLANS[1][step]}",
                }
            )
            while 1:
                stream = self.llm_call()

                collected_content = ""
                tool_calls_buffer = {}

                for chunk in stream:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                        yield ("reasoning_content", delta.reasoning_content)

                    if delta.content:
                        collected_content += delta.content
                        yield ("content", delta.content)

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_buffer:
                                tool_calls_buffer[idx] = {
                                    "id": tc.id or "",
                                    "function_name": tc.function.name or "",
                                    "arguments": "",
                                }
                            if tc.function.arguments:
                                tool_calls_buffer[idx]["arguments"] += tc.function.arguments

                if not tool_calls_buffer:
                    self.messages.append({"role": "assistant", "content": collected_content})
                    if step != len(PLANS[0]) - 1:
                        break
                    if collected_content != "":
                        break
                else:
                    tool_calls_msg = []
                    messages_tool_return = []

                    for idx, data in sorted(tool_calls_buffer.items()):
                        tool_calls_msg.append(
                            {
                                "id": data["id"],
                                "type": "function",
                                "function": {
                                    "name": data["function_name"],
                                    "arguments": data["arguments"],
                                },
                            }
                        )
                        yield ("tool_calls", tool_calls_msg[-1]["function"])
                        result = self.execute_tool(data)
                        messages_tool_return.append(
                            {
                                "role": "tool",
                                "tool_call_id": data["id"],
                                "content": str(result),
                            }
                        )
                        yield ("tool_result", str(result))

                    self.messages.append(
                        {
                            "role": "assistant",
                            "content": collected_content,
                            "tool_calls": tool_calls_msg,
                        }
                    )
                    self.messages += messages_tool_return

            # 在循环结束后，保存消息到数据库
            self.save_messages_to_db()
            yield ("done", {})

    def save_messages_to_db(self):
        """将 self.messages 中未持久化的消息保存到数据库"""
        task_service = get_task_service()
        existing = task_service.get_messages(self.task_id)

        # 使用 (role, content, tool_call_id) 三元组去重
        existing_set = {
            (m["role"], m["content"], m.get("tool_call_id", ""))
            for m in existing
        }

        for msg in self.messages:
            # 跳过系统消息（可选）
            if msg["role"] == "system":
                continue

            key = (msg["role"], msg["content"], msg.get("tool_call_id", ""))
            if key not in existing_set:
                # 保存完整消息，包括 tool_call_id 和 tool_calls
                task_service.save_message(
                    self.task_id,
                    msg["role"],
                    msg["content"],
                    tool_call_id=msg.get("tool_call_id"),
                    metadata={"tool_calls": msg.get("tool_calls")} if msg.get("tool_calls") else None,

                )
                existing_set.add(key)