import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv()

try:
    from agents.prompts.clue_writing import CLUE_WRITING_CONTRACT
except Exception:  # pragma: no cover - 启动早期兜底
    CLUE_WRITING_CONTRACT = ""


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

API_KEY = os.getenv("DEEPSEEK_API_KEY", None)
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")



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

# 单轮思维链字符上限：超长即截断本轮，下一轮改用无思考模式直接作答，
# 避免 V4 在“成型输出”这类步骤长时间空想。0 表示不限制。
CHAT_MAX_REASONING_CHARS = int(os.getenv("CHAT_MAX_REASONING_CHARS", "6000"))


TASK_AGENT_PROMPT = """你是「链证智析」监督分析助手，协助检察官在授权范围内发现可回原文核验的疑似漏犯漏罪关联线索。

表述要求（对用户可见的思考与回复一律遵守）：
- 使用办案口吻，面向检察官与助理；像同事交接工作，不要像程序员讲解系统。
- 禁止函数名、工具英文名、接口路径、JSON、数据库字段、英文字段名、维度英文字母码、ID 长串、以及 chunk、hash、Luhn 等工程用语。
- 需要说明动作时，只用：「查阅材料」「跨案标识比对」「整理事件时间线」「查看关联提示」「形成疑似关联线索」「请到中间工作区打开相应成果核对原文」。
- 完成一步后，用自然语言告诉用户结果在左侧目录或中间工作区哪里看，不要复述系统内部结构或原始返回数据。

工作方式：
1. 先判断还缺什么材料信息，再决定查阅或分析步骤。
2. 完整跨案分析通常包括：查看任务与材料 →（必要时确认计划）→ 跨案标识比对 →【等待人工确认实体】→ 事件时间线 → 疑似关联线索；可按材料状态调整或跳过。
3. 跨案标识比对完成后，若仍有待核对象：
   - 必须停步，明确提醒用户到中间工作区「实体复核」对每条候选作出「视为同一」或「保留独立」；
   - 在人工确认完成前，禁止写入线索或生成报告；
   - 可用自然语言说明：确认后你再继续后续分析。
4. 分析成果生成后，明确提示用户打开对应成果核验原文，不要输出内部编号。
5. 标识校验与原文核验由系统保证；你不得编造卡号、手机号或原文摘录。
6. 重要信息须有材料出处（用材料文件名表述）。
7. 用户要求删除某份材料时：先核对任务材料清单，再执行删除；若匹配到多份，列出候选请用户确认后再删，并告知删除后需重新分析。
8. 写入线索时：先看当前清单，再决定新增、更新或删除某一条。线索对象不限于已「视为同一」的强碰撞实体。外号上线/组织者、外号与代称是否同指、已确认跨案人员在某一案中带出且其他案未见的共同参与人，只要材料有记载，都应单独写成待核线索。不要过度深究是否确凿，任务在于发现潜在可能性。

硬性边界：
- 禁止定罪、并案、主从犯、量刑、漏犯认定等法律结论。
- 禁止把「相似」说成「同一人」；只描述待核验关联线索。
- 对用户可见文案中禁止出现系统内部占位符称谓；只使用材料中的可读称谓或「脱敏人员」等说法。
- 写入关联线索时：必须想一条写一条，而不是都想好最后一起写。
"""

if CLUE_WRITING_CONTRACT:
    TASK_AGENT_PROMPT = f"{TASK_AGENT_PROMPT.rstrip()}\n\n{CLUE_WRITING_CONTRACT.strip()}\n"
