from agents.base_agent import *
from app.config import *
import inspect

# ReAct（Reason + Act）模式的核心思想——让 AI 交替进行"推理（Reason）"和"行动（Act）"，并通过观察（Observation）来驱动下一步。
class ReactAgent(BaseAgent):
    def __init__(self, conv_id=0):
        super().__init__()
        self.conv_id = conv_id
        with open("../tools/tools_schema.json", "r", encoding="utf-8") as f:
            self.tools = json.load(f)  # 直接覆盖空列表
        # TODO 后续可追加更多工具
        self.save_conversation(self.conv_id)


    def execute_tool(self, data) -> str:
        try:
            fn = tool_functions[data["function_name"]]
            args = json.loads(data["arguments"])
            sig = inspect.signature(fn)
            if "conv_id" in sig.parameters:
                result = fn(**args, conv_id=self.conv_id)
            else:
                result = fn(**args)
        except Exception as e:
            result = f"工具调用出错: {e}"

        return result

    def save_conversation(self, conv_id):
        if META_FILE.exists():
            with open(META_FILE, "r", encoding="utf-8") as f:
                meta = json.load(f)
        for c in meta:
            if c["id"] == conv_id:
                path = DATA_DIR / f"{conv_id}.json"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.messages, f, ensure_ascii=False, indent=2)
        return


    def switch_id(self, conv_id):
        if self.conv_id == conv_id:return

        path = WORKSPACE_DIR / str(conv_id)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)

        path = DATA_DIR / f"{conv_id}.json"
        self.save_conversation(self.conv_id)

        if path.exists():
            with open(path, "r", encoding='utf-8') as f:
                self.messages = json.load(f)
        else:
            self.__init__(conv_id)

        self.conv_id = conv_id

    def chat(self, user_input):
        """
        流式对话：添加用户消息 → 获取流式响应 → 逐块 yield 回复内容
        自动将最终完整回复追加到历史中
        """
        self.messages.append({"role": "user", "content": user_input})

        workspace = WORKSPACE_DIR / str(self.conv_id)
        if workspace.exists():
            files =  [f.name for f in workspace.iterdir() if f.is_file()]
            file_list = "\n".join([f"  - {f}" for f in files])
            self.messages.append({"role": "system",
                                  "content": f"\n\n[工作区文件]\n当前对话工作区包含以下文件：\n{file_list}\n你可以读取、分析这些文件来回答用户问题。"})

        if PLANS:
            plan_steps = []
            for i in range(len(PLANS[0])):
                plan_steps.append({
                    "title": PLANS[0][i],
                    "description": PLANS[1][i],
                    "status": "pending"
                })
        yield ("plan", {
            "title": "执行计划",
            "steps": plan_steps
        })
        for step in range(len(plan_steps)):

            self.messages.append({"role": "system","content": f"当前进度：第 {step + 1} 步 / 共 {len(plan_steps)} 步。请根据计划继续执行:{PLANS[1][step]}"})
            while 1:
                stream = self.llm_call()

                collected_content = ""
                tool_calls_buffer = {}

                for chunk in stream:
                    delta = chunk.choices[0].delta
                    # 处理推理内容（可选）
                    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
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
                                    "arguments": ""
                                }
                            # 累加参数片段
                            if tc.function.arguments:
                                tool_calls_buffer[idx]["arguments"] += tc.function.arguments

                # 如果没有工具调用，直接返回（collected_text 已经 yield 过了）
                if not tool_calls_buffer:
                    # 流结束后，将完整助手回复追加到历史
                    self.messages.append({"role": "assistant", "content": collected_content})
                    self.save_conversation(self.conv_id)
                    if  step != len(PLANS[0])-1:
                        break
                    if collected_content != '':
                        break
                else:
                    tool_calls_msg = []
                    messages_tool_return = []

                    for idx, data in sorted(tool_calls_buffer.items()):
                        tool_calls_msg.append({
                            "id": data["id"],
                            "type": "function",
                            "function": {
                                "name": data["function_name"],
                                "arguments": data["arguments"]
                            }
                        })
                        yield ("tool_calls", tool_calls_msg[-1]['function'])
                        # 执行每个工具，并将结果作为 tool 消息加入历史
                        result = self.execute_tool(data)
                        messages_tool_return.append({"role": "tool","tool_call_id": data["id"],"content": str(result)})
                        yield ("tool_result", str(result))

                    # OpenAI 兼容的 Function Calling 流程规定，当模型返回 tool_calls 时，必须将整条 assistant 消息（包含 role、content 和 tool_calls 字段）追加到对话历史中。
                    # 后续请求需要这条消息来识别模型已发出的工具调用，以及对应的 tool_call_id
                    # 部分思考轨迹隐藏，reasoning_content 通常不入库
                    self.messages.append({"role": "assistant", "content": collected_content, "tool_calls": tool_calls_msg})
                    # 加入工具调用结果
                    self.messages += messages_tool_return
                    self.save_conversation(self.conv_id)



                #print(tool_calls_msg)
                #print(messages_tool_return)

            yield ("done", {})
