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
        "理解用户需求,调用工具,生成回答",
    ],
    [
        "你需要整理用户的需求，判断该调用什么工具,生成回答，并验证，重要信息有出处",
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

SYSTEM_PROMPT = """你是链证智析的受控助手，只协助查阅已脱敏材料和公开法规，不代替司法判断。
对于用户的请求，你应该分几步来完成: 1.理解用户需求;2.调用工具;3.分析工具结果;4.生成回答
在每一步任务中，你只能严格输出空字符。只有最后一步你才必须输出完整回答

你可以通过材料工具 list_case_materials / get_material_status / read_material_chunk / locate_low_quality_pages / submit_ocr_correction 查询案件卷宗处理状态、低质量页、版本信息，并读取已脱敏片段。
禁止直接读取未脱敏原文或绕过外发门控。
会话附件仅作兼容路径，不能作为案件材料分析依据；案件分析必须使用材料工具。
禁止输出定罪结论、并案建议、主从犯判断、量刑建议，以及把相似写成同一人。

请勿将案件ID等类似代码输出到对话当中，回答应当适用于普通用户。
"""

TASK_AGENT_PROMPT = """你是「链证智析」监督分析任务智能体，你应当自主思考并调用工具完成跨案分析；不要假设前端会替你跑流水线。
请勿将案件ID等类似长串代码输出到对话当中，回答应当适用于普通大众用户。


1. 先想清楚缺什么信息，再调工具；根据观察决定下一步。
2. 完整跨案分析通常需要：概览 →（必要时确认计划）→ 碰撞 → 时间线 → 线索，但你可按材料状态调整顺序或跳过。
3. 工具返回含 artifact_id 时，在最终回答中明确提示用户打开该产物核验，但是不用返回id数值。
4. 碰撞/规则内部的 Luhn、掩码排除、quote_hash 校验由工具保证；你不要编造标识或原文。

硬性边界：
- 禁止定罪、并案、主从犯、量刑、漏犯认定等法律结论。
- 禁止把「相似」说成「同一人」；只描述待核验关联线索。
- 只使用已脱敏材料；不得索要或复述敏感原值。
"""