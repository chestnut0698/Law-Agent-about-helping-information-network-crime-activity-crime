from app.config import PROMPT_VERSIONS, SYSTEM_PROMPT


class BaseAgent:
    def __init__(self):
        self.model = None
        # 为了缓存命中，请务必注意不要将动态变量放入，也尽量不要删改历史消息
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

    def llm_call(self, tool_choice="auto", temperature=0.1, max_tokens=4096):
        """所有模型调用经统一网关，禁止在此直连 OpenAI。"""
        from agents.gateway import get_gateway

        return get_gateway().stream(
            purpose="react_chat",
            prompt_version=PROMPT_VERSIONS["react_chat"],
            messages=self.messages,
            tools=getattr(self, "tools", None),
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
        )
