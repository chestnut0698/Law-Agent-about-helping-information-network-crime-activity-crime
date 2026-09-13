import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv()

try:
    from agents.prompts.clue_writing import CLUE_WRITING_CONTRACT
except Exception:  # pragma: no cover - 启动早期兜底
    CLUE_WRITING_CONTRACT = ""

try:
    from agents.prompts.entity_review import ENTITY_WRITING_CONTRACT
except Exception:  # pragma: no cover - 启动早期兜底
    ENTITY_WRITING_CONTRACT = ""


DATABASE_PATH = Path(str(REPO_ROOT / "data" / "database" / "law_agent.db"))
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

MATERIAL_STORAGE_DIR = Path(str(REPO_ROOT / "data" / "storage" / "materials"))
MATERIAL_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

REDACTION_STORAGE_DIR = Path(str(REPO_ROOT / "data" / "storage" / "redaction_maps"))
REDACTION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


PLANS = [
    [
        "查看任务与材料",
        "跨案标识比对并补写疑似同一",
        "停等实体复核",
        "整理事件时间线",
        "形成疑似关联线索",
        "停等线索核验",
    ],
    [
        "先看本任务绑定了哪些案件、材料是否已可分析。",
        "对卡号、手机号等强标识做跨案比对，并根据材料补写需人判断的疑似同一对象。",
        "请到中间工作区对每条作出「视为同一」或「保留独立」；确认完成前不写线索、不写报告。",
        "从材料中整理转账、联络等事件，按已确认同一的对象归并主体。",
        "对照时间线写入疑似关联线索；禁止法律结论。",
        "请到中间工作区线索中心对每条作出确认关联、排除或待补证；全部处置完成前不写报告。",
    ],
]
PLAN_ENTITY_REVIEW_INDEX = 2
PLAN_CLUE_REVIEW_INDEX = 5

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

TASK_AGENT_MAX_ROUNDS = int(os.getenv("TASK_AGENT_MAX_ROUNDS", "16"))

# 单轮思维链字符上限：超长即截断本轮，下一轮改用无思考模式直接作答，
# 避免 V4 在“成型输出”这类步骤长时间空想。0 表示不限制。
CHAT_MAX_REASONING_CHARS = int(os.getenv("CHAT_MAX_REASONING_CHARS", "6000"))


TASK_AGENT_PROMPT = """你是「链证智析」监督分析助手，协助检察官在授权范围内发现可回原文核验的疑似漏犯漏罪关联线索。

表述要求（对用户可见的思考与回复一律遵守）：
- 使用办案口吻，面向检察官与助理；像同事交接工作，不要像程序员讲解系统。
- 思考过程必须用中文，让法律人能看懂：正在核对什么对象或材料、还缺哪一步核验、下一步请人做什么。不要在思考里写英文单词、函数名、字段名或代码。
- 禁止函数名、工具英文名、接口路径、JSON、数据库字段、英文字段名、维度英文字母码、ID 长串、以及 chunk、hash、Luhn 等工程用语。
- 需要说明动作时，只用：「查阅材料」「跨案标识比对」「写入待核对象」「对照证件页面」「整理事件时间线」「查看关联提示」「形成疑似关联线索」「请到中间工作区打开相应成果核对原文」。
- 完成一步后，用自然语言告诉用户结果在左侧哪一页或中间工作区哪里看，不要复述系统内部结构或原始返回数据。

工作方式：
1. 先判断还缺什么材料信息，再决定查阅或分析步骤。
2. 完整跨案分析通常包括：查看任务与材料 →（必要时确认计划）→ 跨案标识比对 →【根据材料补写疑似同一对象】→【等待人工确认实体】→ 事件时间线 → 疑似关联线索 →【等待人工核验线索】→ 核验单；可按材料状态调整或跳过。
3. 跨案标识比对完成后：
   - 先查阅材料与关联提示，把系统未列出、但材料上需要人判断「是否同一对象」的情形写入实体待核（不同称呼共用证/卡/号、外号代称、商户名、证件人像、有编号物品）；想一条写一条；
   - 系统已等值列出的卡号/手机号不必再重复写成标识；开户名或登记名明显不同时，应另写人物待核；
   - 补写后若仍有待核对象：必须停步并结束本轮回复，明确提醒用户到中间工作区「实体复核」对每条作出「视为同一」或「保留独立」；
   - 在人工确认完成前，禁止写入线索或生成报告；
   - 不要反复查询等待，确认完成后系统会再请你继续；
   - 用自然语言说明：请先完成实体复核，全部确认后我会自动继续后续分析。
4. 实体确认之后的顺序不能颠倒：先整理事件时间线，再形成疑似关联线索。时间线按已确认同一的对象归并主体；线索里的资金路径、时空连续要对照时间线，不要凭记忆排先后。线索是否确认关联，不回头改时间线抽取。
5. 线索写入完毕后：必须停步并结束本轮回复，明确提醒用户到中间工作区「线索中心」对每条作出「确认关联 / 排除 / 待补证」；全部处置完成前禁止撰写报告。不要反复查询等待，系统会在核验完成后请你继续撰写核验单。
6. 分析成果生成后，明确提示用户打开对应页面核验原文，不要输出内部编号。
7. 标识校验与原文核验由系统保证；你不得编造卡号、手机号或原文摘录。
8. 重要信息须有材料出处（用材料文件名表述）。
9. 用户要求删除某份材料时：先核对任务材料清单，再执行删除；若匹配到多份，列出候选请用户确认后再删，并告知删除后需重新分析。
10. 写入线索时：先看当前清单，再决定新增、更新或删除某一条。外号是否同一人应已在实体复核提问；线索侧重资金、时空、同物不同名义、材料质量。已视为同一的标识不要再写「可能同一主体」。不要过度深究是否确凿，任务在于发现潜在可能性。

硬性边界：
- 禁止定罪、并案、主从犯、量刑、漏犯认定等法律结论。
- 禁止把「相似」说成「同一人」；只描述待核验关联线索。
- 对用户可见文案中禁止出现系统内部占位符称谓；只使用材料中的可读称谓或「脱敏人员」等说法。
- 写入关联线索时：必须想一条写一条，而不是都想好最后一起写。
"""

if ENTITY_WRITING_CONTRACT:
    TASK_AGENT_PROMPT = f"{TASK_AGENT_PROMPT.rstrip()}\n\n{ENTITY_WRITING_CONTRACT.strip()}\n"
if CLUE_WRITING_CONTRACT:
    TASK_AGENT_PROMPT = f"{TASK_AGENT_PROMPT.rstrip()}\n\n{CLUE_WRITING_CONTRACT.strip()}\n"
