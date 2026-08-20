from agents.base_agent import *
from agents.gateway import GatewayError
from app.config import *
from tools.tools import WORKSPACE_ONLY_TOOLS, tool_functions
import inspect
import json
from pathlib import Path

# ReAct（Reason + Act）：推理与工具调用交替；历史按 task_id 落 SQLite。
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
            elif "conv_id" in sig.parameters:
                result = fn(**args, conv_id=self.task_id)
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
        if self.task_id == task_id and len(self.messages) > 1:
            return

        from tools.tasks import get_task_service

        rows = get_task_service().get_messages(task_id)
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for row in rows:
            if row.get("role") in {"user", "assistant", "system", "tool"}:
                self.messages.append({"role": row["role"], "content": row["content"]})
        self.task_id = task_id

    def chat(self, user_input):
        """
        流式对话：添加用户消息 → 获取流式响应 → 逐块 yield 回复内容
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
                try:
                    stream = self.llm_call()
                except GatewayError as exc:
                    notice = (
                        "当前处于仅确定性规则模式，模型对话暂不可用。"
                        if exc.degraded
                        else f"模型网关错误：{exc.message}"
                    )
                    yield ("content", notice)
                    self.messages.append({"role": "assistant", "content": notice})
                    break

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

            yield ("done", {})
