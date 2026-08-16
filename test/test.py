"""手动调用 NVIDIA 接口的探针脚本，直接 python 运行，不参与 pytest。"""

import os

from dotenv import load_dotenv
from openai import OpenAI


def main():
    load_dotenv()
    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=os.getenv("NVIDIA_API_KEY"),
    )
    completion = client.chat.completions.create(
        model="meta/muse-glimmer-30b",
        messages=[{"role": "user", "content": "Which number is larger, 9.11 or 9.8?"}],
        temperature=1,
        top_p=0.7,
        max_tokens=4096,
        stream=False,
    )
    print(completion)


if __name__ == "__main__":
    main()
