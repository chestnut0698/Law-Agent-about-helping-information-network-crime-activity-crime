import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv()


DATABASE_PATH = Path(str(REPO_ROOT / "data" / "database" / "law_agent.db"))
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

MATERIAL_STORAGE_DIR = Path(str(REPO_ROOT / "data" / "storage" / "materials"))
MATERIAL_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

REDACTION_STORAGE_DIR = Path(str(REPO_ROOT / "data" / "storage" / "redaction_maps"))
REDACTION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


PLANS = [
    [
        "查看任务范围与材料情况",
        "必要时确认分析计划",
        "开展跨案标识比对",
        "整理转账与联络事件",
        "形成疑似关联线索并提示核验",
    ],
    [
        "先了解本监督任务绑定了哪些案件、材料是否已可分析。",
        "若计划尚未确认，先确认后再继续；已确认则可跳过。",
        "对银行卡号、手机号、设备号等强标识做跨案比对，生成待核对象清单。",
        "从材料中整理转账、联络等事件，形成可回原文的时间线。",
        "汇总疑似漏犯漏罪关联线索，提示用户在中间工作区打开成果核验；禁止法律结论。",
    ],
]

# 默认走 DeepSeek；队友本地若只配置 NVIDIA，则自动切到 NVIDIA，互不影响。
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
NVIDIA_KEY = os.getenv("NVIDIA_API_KEY")

if DEEPSEEK_KEY:
    API_KEY = DEEPSEEK_KEY
    BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
elif NVIDIA_KEY:
    API_KEY = NVIDIA_KEY
    BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    MODEL_NAME = os.getenv("MODEL_NAME", "meta/muse-glimmer-30b")
else:
    API_KEY = None
    BASE_URL = "https://api.deepseek.com"
    MODEL_NAME = "deepseek-v4-flash"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

PROMPT_VERSIONS = {
    "extraction": "extract-v1",
    "normalization":  "normalize-v1",
    "clue_wording": "clue-v1",
    "output_verify": "verify-v1",
    "react_chat": "react-v1",
    "task_react": "task-react-v1",
}

# 外呼关闭或网关不可达时进入「仅确定性规则」降级；Fake 供无外呼验证。
DEEPSEEK_EXTERNAL_CALLS_ENABLED = _env_bool("DEEPSEEK_EXTERNAL_CALLS_ENABLED", True)
GATEWAY_FAKE_MODE = _env_bool("GATEWAY_FAKE_MODE", False)
GATEWAY_TIMEOUT_SECONDS = float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "30"))
GATEWAY_MAX_RETRIES = int(os.getenv("GATEWAY_MAX_RETRIES", "3"))
GATEWAY_RETRY_BASE_SECONDS = float(os.getenv("GATEWAY_RETRY_BASE_SECONDS", "0.5"))

_DEFAULT_MODEL_WHITELIST = {
    "deepseek-v4-flash",
    "meta/muse-glimmer-30b",
    MODEL_NAME,
}
_EXTRA_MODELS = {
    item.strip()
    for item in os.getenv("GATEWAY_MODEL_WHITELIST", "").split(",")
    if item.strip()
}
GATEWAY_MODEL_WHITELIST = frozenset(_DEFAULT_MODEL_WHITELIST | _EXTRA_MODELS)

TASK_AGENT_MAX_ROUNDS = int(os.getenv("TASK_AGENT_MAX_ROUNDS", "12"))


TASK_AGENT_PROMPT = """你是「链证智析」监督分析助手，协助检察官在授权范围内发现可回原文核验的疑似漏犯漏罪关联线索。

表述要求（对用户可见的思考与回复一律遵守）：
- 使用办案口吻，面向检察官与助理；禁止函数名、接口路径、JSON、数据库字段、ID 长串、chunk/quote_hash/Luhn 等工程用语。
- 需要说明技术动作时，改用：「查阅材料」「跨案标识比对」「整理事件时间线」「形成疑似关联线索」「请到中间工作区打开相应分析成果核对原文」。
- 调用工具后，用自然语言告知结果在左侧目录 / 中间工作区，不要复述工具返回的原始数据结构。

工作方式：
1. 先判断还缺什么材料信息，再决定查阅或分析步骤。
2. 完整跨案分析通常包括：查看任务与材料 →（必要时确认计划）→ 跨案标识比对 → 事件时间线 → 疑似关联线索；可按材料状态调整或跳过。
3. 分析成果生成后，明确提示用户打开对应成果核验原文，不要输出成果编号。
4. 标识校验与原文核验由系统工具保证；你不得编造卡号、手机号或原文摘录。
5. 重要信息须有材料出处（用材料文件名表述）。
6. 用户要求删除某份材料时：先核对任务材料清单，再执行删除；若匹配到多份，列出候选请用户确认后再删，并告知删除后需重新分析。

硬性边界：
- 禁止定罪、并案、主从犯、量刑、漏犯认定等法律结论。
- 禁止把「相似」说成「同一人」；只描述待核验关联线索。
"""