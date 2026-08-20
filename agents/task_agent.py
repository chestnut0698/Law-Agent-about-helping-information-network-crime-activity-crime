"""任务级 ReAct 智能体：DeepSeek 主导思考与工具编排，产物由工具落库。

与会话 ReactAgent 的区别：绑定 supervision task_id，工具面向跨案分析；
思考 / 工具调用 / 观察在 SSE 中原样流出，产物以 artifact_id 供中间区预览。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Iterator

from agents.base_agent import BaseAgent
from agents.gateway import GatewayError, get_gateway
from app.config import (
    DATA_DIR,
    PROMPT_VERSIONS,
    TASK_AGENT_PROMPT,
    TASK_AGENT_MAX_ROUNDS,
)
from app.config import GATEWAY_FAKE_MODE
from tools.tools import tool_functions

TASK_TOOL_NAMES = {
    "get_task_overview",
    "confirm_task_plan",
    "refresh_task_materials",
    "run_task_collision",
    "run_task_timeline",
    "generate_task_clues",
    "list_case_materials",
    "get_material_status",
    "locate_low_quality_pages",
    "read_material_chunk",
    "search_lawlibrary",
    "search_policy",
    "get_current_time",
}

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "tools" / "tools_schema.json"


def _load_task_tools() -> list[dict[str, Any]]:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        all_tools = json.load(f)
    return [
        item
        for item in all_tools
        if item.get("function", {}).get("name") in TASK_TOOL_NAMES
    ]


def _parse_artifact(payload: str) -> dict[str, Any] | None:
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    artifact_id = data.get("artifact_id")
    if not artifact_id or data.get("ok") is False:
        return None
    return {
        "artifact_id": artifact_id,
        "title": data.get("title") or data.get("artifact_type") or "产物",
        "summary": data.get("message") or "",
        "artifact_type": data.get("type") or data.get("artifact_type") or "",
    }


class TaskAgent(BaseAgent):
    """绑定单个监督分析任务的 DeepSeek ReAct 智能体。"""

    def __init__(self, task_id: str, user_id: str | None = None):
        self.task_id = task_id
        self.user_id = user_id or "system"
        self.tools = _load_task_tools()
        self.messages = [
            {
                "role": "system",
                "content": TASK_AGENT_PROMPT.format(task_id=task_id),
            }
        ]
        self._history_path = DATA_DIR / f"task_{task_id}.json"
        self._load_history()

    def _load_history(self) -> None:
        if not self._history_path.exists():
            return
        try:
            data = json.loads(self._history_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                # 始终用最新系统提示覆盖首条
                self.messages = [
                    self.messages[0],
                    *[m for m in data if m.get("role") != "system"],
                ]
        except Exception:
            pass

    def _save_history(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._history_path.write_text(
            json.dumps(self.messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def execute_tool(self, data: dict[str, Any]) -> str:
        name = data.get("function_name") or ""
        try:
            fn = tool_functions[name]
            args = json.loads(data.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {}
            # 任务工具默认注入 task_id；材料工具不强制
            sig = inspect.signature(fn)
            if "task_id" in sig.parameters and "task_id" not in args:
                args["task_id"] = self.task_id
            if "user_id" in sig.parameters and "user_id" not in args:
                args["user_id"] = self.user_id
            # 任务绑定工具禁止改绑其他 task
            if name.startswith(("get_task_", "confirm_task_", "refresh_task_", "run_task_", "generate_task_")):
                args["task_id"] = self.task_id
            result = fn(**args)
            return str(result)
        except Exception as exc:
            return json.dumps(
                {"ok": False, "error": f"工具调用出错: {exc}"},
                ensure_ascii=False,
            )

    def llm_call(self, tool_choice="auto", temperature=0.1, max_tokens=4096):
        return get_gateway().stream(
            purpose="task_react",
            prompt_version=PROMPT_VERSIONS.get("task_react", "task-react-v1"),
            messages=self.messages,
            tools=self.tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            approval_id=f"task:{self.task_id}",
        )

    def chat(self, user_input: str) -> Iterator[tuple[str, Any]]:
        self.messages.append({"role": "user", "content": user_input})
        self._save_history()

        # Fake / 降级：仍走工具链，保证 IDE 式体验可演示
        gateway = get_gateway()
        degraded, reason = gateway.degraded_state()
        if GATEWAY_FAKE_MODE or degraded:
            yield from self._scripted_analysis(reason if degraded else "GATEWAY_FAKE_MODE")
            return

        yield (
            "plan",
            {
                "title": "智能体自行编排",
                "steps": [
                    {"title": "理解任务与材料", "description": "查看任务范围与材料状态", "status": "pending"},
                    {"title": "调用分析工具", "description": "按需碰撞 / 事件 / 线索", "status": "pending"},
                    {"title": "汇总并给出产物链接", "description": "不输出法律结论", "status": "pending"},
                ],
            },
        )

        for _round in range(TASK_AGENT_MAX_ROUNDS):
            try:
                stream = self.llm_call()
            except GatewayError as exc:
                notice = (
                    "当前处于仅确定性规则模式，改用工具直跑分析。"
                    if exc.degraded
                    else f"模型网关错误：{exc.message}"
                )
                if exc.degraded:
                    yield from self._scripted_analysis(exc.message)
                else:
                    yield ("content", notice)
                    self.messages.append({"role": "assistant", "content": notice})
                    self._save_history()
                return

            collected_content = ""
            tool_calls_buffer: dict[int, dict[str, Any]] = {}

            for chunk in stream:
                delta = chunk.choices[0].delta
                if getattr(delta, "reasoning_content", None):
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
                                "function_name": (tc.function.name if tc.function else None) or "",
                                "arguments": "",
                            }
                        if tc.id:
                            tool_calls_buffer[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls_buffer[idx]["function_name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            if not tool_calls_buffer:
                self.messages.append({"role": "assistant", "content": collected_content})
                self._save_history()
                yield ("done", {})
                return

            tool_calls_msg = []
            tool_returns = []
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
                yield (
                    "tool_calls",
                    {
                        "name": data["function_name"],
                        "arguments": data["arguments"],
                    },
                )
                result = self.execute_tool(data)
                tool_returns.append(
                    {
                        "role": "tool",
                        "tool_call_id": data["id"],
                        "content": result,
                    }
                )
                yield ("tool_result", result)
                artifact = _parse_artifact(result)
                if artifact:
                    yield ("artifact", artifact)

            self.messages.append(
                {
                    "role": "assistant",
                    "content": collected_content,
                    "tool_calls": tool_calls_msg,
                }
            )
            self.messages.extend(tool_returns)
            self._save_history()

        wrap = "已达到本轮最大工具步数。请根据已有观察给出阶段性汇总，或让用户继续发指令。"
        yield ("content", wrap)
        self.messages.append({"role": "assistant", "content": wrap})
        self._save_history()
        yield ("done", {})

    def _scripted_analysis(self, reason: str) -> Iterator[tuple[str, Any]]:
        """无外呼时：按工具顺序演示智能体框内的思考与执行过程。"""
        yield (
            "plan",
            {
                "title": "确定性工具编排（降级/Fake）",
                "steps": [
                    {"title": "查看任务", "description": reason, "status": "pending"},
                    {"title": "确认计划与材料", "description": "", "status": "pending"},
                    {"title": "碰撞 → 时间线 → 线索", "description": "", "status": "pending"},
                ],
            },
        )
        yield (
            "reasoning_content",
            f"（降级模式：{reason}）将依次调用任务工具完成跨案分析，不经模型自由推理。\n",
        )

        steps = [
            ("get_task_overview", "{}"),
            ("confirm_task_plan", "{}"),
            ("refresh_task_materials", "{}"),
            ("run_task_collision", "{}"),
            ("run_task_timeline", "{}"),
            ("generate_task_clues", "{}"),
        ]
        summaries = []
        for name, arguments in steps:
            yield ("tool_calls", {"name": name, "arguments": arguments})
            result = self.execute_tool(
                {"id": name, "function_name": name, "arguments": arguments}
            )
            yield ("tool_result", result)
            artifact = _parse_artifact(result)
            if artifact:
                yield ("artifact", artifact)
                summaries.append(
                    f"- [{artifact['title']}](artifact:{artifact['artifact_id']})"
                )
            self.messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": name,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
            self.messages.append(
                {"role": "tool", "tool_call_id": name, "content": result}
            )

        body = (
            "本轮分析已由工具链完成（模型外呼不可用时的降级路径）。\n"
            "产物请点击下方卡片或目录打开核验：\n"
            + ("\n".join(summaries) if summaries else "- 暂无新产物")
            + "\n\n提醒：结果仅为待核验关联线索，不作定罪、并案、主从犯或量刑判断。"
        )
        yield ("content", body)
        self.messages.append({"role": "assistant", "content": body})
        self._save_history()
        yield ("done", {})
