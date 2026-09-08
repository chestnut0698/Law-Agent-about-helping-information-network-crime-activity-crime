from agents.base_agent import *
from app.chat_history import messages_for_llm
from app.config import *
import inspect
import json
import logging
import traceback
from tools.tools import *
from pathlib import Path
from app.tasks import get_task_service

logger = logging.getLogger(__name__)

# ReAct（Reason + Act）模式的核心思想——让 AI 交替进行"推理（Reason）"和"行动（Act）"，并通过观察（Observation）来驱动下一步。
class ReactAgent(BaseAgent):
    def __init__(self, task_id=0):
        super().__init__()
        self.task_id = task_id
        self._persisted_count = 0
        self._no_think = False
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
        # 每次请求都从库中重载，避免失败后内存里残留半截对话。
        self.messages = get_task_service().repair_chat_messages(task_id)
        self._persisted_count = len(self.messages)
        self.task_id = task_id

    def llm_call(self, tool_choice="auto", temperature=0.1, max_tokens=4096):
        payload = messages_for_llm(self.messages)
        print(f"[llm_call] messages={len(payload)} last_role={(payload[-1]['role'] if payload else None)}")
        kwargs = dict(
            model=self.model,
            messages=payload,
            tools=self.tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        # 思维链超长被截断后，后续轮次转无思考模式直接作答，避免长时间空想
        if self._no_think:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return self.client.chat.completions.create(**kwargs)

    def chat(self, user_input):
        """
        流式对话：添加用户消息 → 获取流式响应 → 逐块 yield 回复内容
        自动将最终完整回复追加到历史中
        """
        self.messages.append({"role": "user", "content": user_input})

        # 每条新消息回到可思考状态；本消息内一旦思维链超长会切到无思考
        self._no_think = False

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
        try:
            max_rounds = 16
            # 纯思考轮不要落成空 assistant，否则下一轮 DeepSeek 会因消息序列/缺
            # reasoning_content 回传而 400；先攒着，并入下一轮有正文或工具的消息。
            pending_reasoning = ""
            for _round in range(max_rounds):
                stream = self.llm_call()

                collected_content = ""
                collected_reasoning = ""
                tool_calls_buffer = {}
                overflow = False

                for chunk in stream:
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = choices[0].delta
                    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                        collected_reasoning += delta.reasoning_content
                        yield ("reasoning_content", delta.reasoning_content)
                        # 思维链过长：截断本轮，下一轮转无思考模式直接作答
                        if CHAT_MAX_REASONING_CHARS and len(collected_reasoning) > CHAT_MAX_REASONING_CHARS:
                            overflow = True
                            break

                    # 内容先缓冲：有工具调用时不当成最终回答提前推给前端
                    if delta.content:
                        collected_content += delta.content

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

                if overflow:
                    # 丢弃本轮未成型的正文/工具调用（思考片段已流给前端），转无思考重发
                    self._no_think = True
                    pending_reasoning = ""
                    continue

                merged_reasoning = f"{pending_reasoning}{collected_reasoning}"

                if not tool_calls_buffer:
                    # 本轮无工具 → 有可见回复才落库并结束；纯思考不落空 assistant
                    if collected_content:
                        msg = {"role": "assistant", "content": collected_content}
                        if merged_reasoning:
                            msg["reasoning_content"] = merged_reasoning
                        self.messages.append(msg)
                        pending_reasoning = ""
                        yield ("content", collected_content)
                        break
                    if collected_reasoning:
                        pending_reasoning = merged_reasoning
                    continue

                # 工具轮：把模型夹带的 content 当作本步思路（无 reasoning 通道时）
                if collected_content and not collected_reasoning and not pending_reasoning:
                    yield ("reasoning_content", collected_content)

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

                assistant_msg = {
                    "role": "assistant",
                    "content": collected_content,
                    "tool_calls": tool_calls_msg,
                }
                if merged_reasoning:
                    assistant_msg["reasoning_content"] = merged_reasoning
                pending_reasoning = ""
                self.messages.append(assistant_msg)
                self.messages += messages_tool_return

            self.save_messages_to_db()
            yield ("done", {})
        except Exception as exc:
            logger.exception("chat interrupted: %s", exc)
            traceback.print_exc()
            yield ("error", _public_chat_error(exc))

    @staticmethod
    def _message_metadata(msg: dict) -> dict | list | None:
        tool_calls = msg.get("tool_calls")
        reasoning = msg.get("reasoning_content")
        if tool_calls and not reasoning:
            return tool_calls
        if not tool_calls and not reasoning:
            return None
        meta: dict = {}
        if tool_calls:
            meta["tool_calls"] = tool_calls
        if reasoning:
            meta["reasoning_content"] = reasoning
        return meta

    def save_messages_to_db(self):
        """将 self.messages 中尚未落库的新消息追加保存。"""
        task_service = get_task_service()
        start = min(self._persisted_count, len(self.messages))
        for msg in self.messages[start:]:
            if msg["role"] == "system":
                continue
            task_service.save_message(
                self.task_id,
                msg["role"],
                msg.get("content") or "",
                tool_call_id=msg.get("tool_call_id") or None,
                metadata=self._message_metadata(msg),
            )
        self._persisted_count = len(self.messages)


def _public_chat_error(exc: Exception) -> str:
    text = str(exc)
    if "tool_calls" in text or "tool_call_id" in text:
        return "对话记录已自动修复，请再发送一次即可继续。"
    if "reasoning_content" in text and "thinking" in text.lower():
        return "思考过程未正确回传，请再发送一次即可继续。"
    if "context length" in text.lower() or "maximum context" in text.lower():
        return "对话上下文过长，请新开一轮对话或精简后再试。"
    if "timeout" in text.lower() or "timed out" in text.lower():
        return "模型响应超时，请稍后重试。"
    return "分析过程中断，请稍后重试。"
