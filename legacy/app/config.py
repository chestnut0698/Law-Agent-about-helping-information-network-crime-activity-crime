import os
from dotenv import load_dotenv
from pathlib import Path


DATA_DIR = Path("../data/user")
META_FILE = DATA_DIR / "conversations.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

PLANS = [[
    "1.理解用户需求",
    "2.调用工具",
    "3.分析工具结果",
    "4.生成回答"
],[
    "1.你需要整理用户的需求，判断该调用什么工具",
    "2.调用所需工具，尽可能多的获取所需信息",
    "3.分析工具结果",
    "4.此轮必须生成回答，并验证，重要信息有出处"
]]

load_dotenv()  # 加载 .env 文件中的变量到环境变量

# DeepSeek API 配置
# API_KEY = os.getenv("DEEPSEEK_API_KEY")
# BASE_URL = "https://api.deepseek.com/v1"  # 或官方最新地址

API_KEY = os.getenv("NVIDIA_API_KEY")
BASE_URL = "https://integrate.api.nvidia.com/v1"
MODEL_NAME = "meta/muse-glimmer-30b"

SYSTEM_PROMPT = """你是一个处于测试环境的贴心AI助理，满足用户(开发者)的所有要求。
对于用户的请求，你应该分几步来完成: 1.理解用户需求;2.调用工具;3.分析工具结果;4.生成回答
在每一步任务中，除了最后一步，你只能严格输出空字符。

"""


