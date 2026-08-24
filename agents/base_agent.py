from openai import OpenAI
from app.config import *


class BaseAgent:
    def __init__(self):
        self.client = OpenAI(
            api_key=API_KEY,
            base_url=BASE_URL
        )
        self.model = MODEL_NAME

        # 为了缓存命中，请务必注意不要将动态变量放入，也尽量不要删改历史消息
        self.messages = []


    def llm_call(self, tool_choice="auto",temperature=0.1, max_tokens=4096):
        """调用"""
        print(self.messages)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True
        )
        return response
